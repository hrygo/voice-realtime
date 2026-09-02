import Foundation

@_spi(Testing)
public protocol CaptureEngine: AnyObject, Sendable {
    func prepare(
        request: TapCaptureRequest,
        onReady: @escaping @Sendable () -> Void,
        onFrame: @escaping @Sendable (PCMFrame) -> Void,
        onFailure: @escaping @Sendable (TapCaptureError) -> Void
    ) throws

    func stop() throws
}

extension TapCaptureEngine: CaptureEngine {}

@_spi(Testing)
public enum CaptureControllerOutput: Equatable, Sendable {
    case control(ControlMessage)
    case pcm(PCMFrame)
    case closeConnection
}

@_spi(Testing)
public final class CaptureController: @unchecked Sendable {
    private static let helperVersion = "1.0.0"
    private static let capabilities = [
        "device_scoped_output_capture",
        "two_phase_commit",
    ]

    private let captureToken: String
    private let catalog: DeviceCatalog
    private let engine: any CaptureEngine
    private let readinessTimeout: TimeInterval
    private let emit: @Sendable (CaptureControllerOutput) -> Void
    private let lock = NSLock()

    private var authenticated = false
    private var phase: CapturePhase = .stopped
    private var lastTerminalCaptureID: UUID?

    public init(
        captureToken: String,
        catalog: DeviceCatalog,
        engine: any CaptureEngine,
        readinessTimeout: TimeInterval = 4,
        emit: @escaping @Sendable (CaptureControllerOutput) -> Void
    ) {
        self.captureToken = captureToken
        self.catalog = catalog
        self.engine = engine
        self.readinessTimeout = readinessTimeout
        self.emit = emit
    }

    public func receive(_ message: ControlMessage) {
        let type = message.string(for: "type") ?? ""
        if type == "hello" {
            handleHello(message)
            return
        }
        guard lock.withLock({ authenticated }) else {
            respond(
                error: .authenticationFailed,
                requestID: message.string(for: "request_id"),
                close: true
            )
            return
        }

        switch type {
        case "list_devices":
            handleListDevices(message)
        case "prepare_capture":
            handlePrepare(message)
        case "commit_capture":
            handleCommit(message)
        case "abort_capture", "stop_capture":
            handleTerminate(message, command: type)
        default:
            respond(
                error: .invalidMessage,
                requestID: message.string(for: "request_id")
            )
        }
    }

    public func clientDisconnected() {
        let shouldStop = lock.withLock { () -> Bool in
            authenticated = false
            defer {
                phase = .stopped
                lastTerminalCaptureID = nil
            }
            return phase.context != nil
        }
        if shouldStop {
            try? engine.stop()
        }
    }

    private func handleHello(_ message: ControlMessage) {
        let requestID = message.string(for: "request_id")
        guard exactKeys(
            message,
            required: [
                "type", "request_id", "protocol_major", "protocol_minor",
                "capture_token", "client_pid",
            ]
        ),
        let requestID,
        let major = integer(message, key: "protocol_major"),
        let minor = integer(message, key: "protocol_minor"),
        let token = message.string(for: "capture_token"),
        let clientPID = integer(message, key: "client_pid"),
        (0 ... 255).contains(minor),
        (1 ... Int64(Int32.max)).contains(clientPID)
        else {
            respond(error: .invalidMessage, requestID: requestID, close: true)
            return
        }
        guard major == Int64(WireProtocol.major) else {
            respond(error: .unsupportedProtocol, requestID: requestID, close: true)
            return
        }
        guard CaptureTokenSecurity.constantTimeEqual(token, captureToken) else {
            respond(error: .authenticationFailed, requestID: requestID, close: true)
            return
        }

        let accepted = lock.withLock { () -> Bool in
            guard !authenticated else { return false }
            authenticated = true
            return true
        }
        guard accepted else {
            respond(error: .invalidState, requestID: requestID, close: true)
            return
        }
        sendControl([
            "type": .string("hello_ack"),
            "request_id": .string(requestID),
            "helper_version": .string(Self.helperVersion),
            "protocol_major": .integer(Int64(WireProtocol.major)),
            "protocol_minor": .integer(Int64(WireProtocol.minor)),
            "capabilities": .array(Self.capabilities.map(JSONValue.string)),
        ])
    }

    private func handleListDevices(_ message: ControlMessage) {
        let requestID = message.string(for: "request_id")
        guard exactKeys(message, required: ["type", "request_id"]), let requestID else {
            respond(error: .invalidMessage, requestID: requestID)
            return
        }
        do {
            let devices = try catalog.devices()
            guard devices.count <= 128 else {
                respond(error: .internalError, requestID: requestID)
                return
            }
            sendControl([
                "type": .string("devices"),
                "request_id": .string(requestID),
                "devices": .array(devices.map { .object(devicePayload($0)) }),
            ])
        } catch {
            respond(error: .deviceUnavailable, requestID: requestID)
        }
    }

    private func handlePrepare(_ message: ControlMessage) {
        let requestID = message.string(for: "request_id")
        let required: Set<String> = [
            "type", "request_id", "capture_id", "follow_default_output",
            "exclude_pids",
        ]
        let allowed = required.union(["device_ref"])
        guard exactKeys(message, required: required, allowed: allowed),
              let requestID,
              let captureText = message.string(for: "capture_id"),
              let captureID = UUID(uuidString: captureText),
              let followDefault = boolean(message, key: "follow_default_output"),
              let excludePIDs = processIDs(message, key: "exclude_pids"),
              validOptionalDeviceReference(message.payload["device_ref"])
        else {
            respond(error: .invalidMessage, requestID: requestID)
            return
        }

        let resolved: ResolvedOutputDevice
        do {
            if followDefault {
                resolved = try catalog.resolvedDefaultDevice()
            } else {
                guard let referenceText = message.string(for: "device_ref"),
                      let reference = DeviceReference(rawValue: referenceText)
                else {
                    respond(error: .invalidMessage, requestID: requestID)
                    return
                }
                resolved = try catalog.resolvedDevice(reference: reference)
            }
        } catch {
            respond(error: .deviceUnavailable, requestID: requestID)
            return
        }

        let context = CaptureContext(
            captureID: captureID,
            captureIDText: captureText,
            sourceID: UUID(),
            deviceGeneration: 0,
            device: resolved,
            prepareRequestID: requestID
        )
        let rejection = lock.withLock { () -> (CaptureControlError, Bool)? in
            switch phase {
            case .stopped:
                phase = .preparing(context)
                lastTerminalCaptureID = nil
                return nil
            case .failed:
                return (.invalidState, false)
            case let .preparing(current),
                 let .ready(current),
                 let .active(current):
                return current.captureID == captureID
                    ? (.invalidState, false)
                    : (.captureConflict, true)
            }
        }
        if let rejection {
            respond(
                error: rejection.0,
                requestID: requestID,
                close: rejection.1
            )
            return
        }

        do {
            try engine.prepare(
                request: TapCaptureRequest(
                    captureID: context.captureID,
                    sourceID: context.sourceID,
                    deviceUID: context.device.uid,
                    deviceGeneration: context.deviceGeneration,
                    excludedProcessIDs: excludePIDs
                ),
                onReady: { [weak self] in self?.captureBecameReady(context) },
                onFrame: { [weak self] in self?.received(frame: $0, context: context) },
                onFailure: { [weak self] in self?.captureFailed($0, context: context) }
            )
        } catch let error as TapCaptureError {
            preparationFailed(error, context: context)
            return
        } catch {
            preparationFailed(nil, context: context)
            return
        }

        DispatchQueue.global(qos: .utility).asyncAfter(
            deadline: .now() + readinessTimeout
        ) { [weak self] in
            self?.readinessTimedOut(context)
        }
    }

    private func handleCommit(_ message: ControlMessage) {
        let requestID = message.string(for: "request_id")
        guard let command = validatedCaptureCommand(message),
              command.command == "commit_capture"
        else {
            respond(error: .invalidMessage, requestID: requestID)
            return
        }

        let result = lock.withLock { () -> CommandResult in
            switch phase {
            case let .ready(context) where context.captureID == command.captureID:
                phase = .active(context)
                return .ack(context.captureIDText)
            case let .active(context) where context.captureID == command.captureID:
                return .ack(context.captureIDText)
            case .preparing:
                return phase.context?.captureID == command.captureID
                    ? .error(.invalidState, false)
                    : .error(.captureConflict, true)
            case .ready, .active:
                return .error(.captureConflict, true)
            case .stopped, .failed:
                return .error(.invalidState, false)
            }
        }
        finish(command: command, requestID: requestID, result: result)
    }

    private func handleTerminate(_ message: ControlMessage, command: String) {
        let requestID = message.string(for: "request_id")
        guard let validated = validatedCaptureCommand(message),
              validated.command == command
        else {
            respond(error: .invalidMessage, requestID: requestID)
            return
        }

        let transition = lock.withLock { () -> (CommandResult, Bool) in
            if let context = phase.context {
                guard context.captureID == validated.captureID else {
                    return (.error(.captureConflict, true), false)
                }
                phase = .stopped
                lastTerminalCaptureID = context.captureID
                return (.ack(context.captureIDText), true)
            }
            if lastTerminalCaptureID == validated.captureID {
                return (.ack(validated.captureIDText), false)
            }
            return (.error(.invalidState, false), false)
        }

        if transition.1 {
            do {
                try engine.stop()
            } catch {
                respond(error: .ioFailed, requestID: requestID)
                return
            }
        }
        finish(command: validated, requestID: requestID, result: transition.0)
    }

    private func captureBecameReady(_ context: CaptureContext) {
        let shouldAnnounce = lock.withLock { () -> Bool in
            guard case let .preparing(current) = phase,
                  current.sourceID == context.sourceID
            else { return false }
            phase = .ready(current)
            return true
        }
        guard shouldAnnounce else { return }
        sendControl([
            "type": .string("ready"),
            "request_id": .string(context.prepareRequestID),
            "capture_id": .string(context.captureIDText),
            "source_id": .string(context.sourceID.uuidString.lowercased()),
            "device_generation": .integer(Int64(context.deviceGeneration)),
            "device": .object(devicePayload(context.device.descriptor)),
        ])
    }

    private func received(frame: PCMFrame, context: CaptureContext) {
        lock.withLock {
            guard case let .active(current) = phase,
                  current.sourceID == context.sourceID,
                  frame.captureID == current.captureID,
                  frame.sourceID == current.sourceID,
                  frame.deviceGeneration == current.deviceGeneration
            else { return }
            emit(.pcm(frame))
        }
    }

    private func captureFailed(_ error: TapCaptureError, context: CaptureContext) {
        let response = lock.withLock { () -> FailureResponse in
            guard phase.context?.sourceID == context.sourceID else { return .ignore }
            let requestID: String? = switch phase {
            case .preparing, .ready: context.prepareRequestID
            case .active, .failed, .stopped: nil
            }
            phase = .failed
            lastTerminalCaptureID = context.captureID
            return .send(requestID)
        }
        guard case let .send(requestID) = response else { return }
        try? engine.stop()
        respond(error: CaptureControlError(error), requestID: requestID)
    }

    private func preparationFailed(
        _ error: TapCaptureError?,
        context: CaptureContext
    ) {
        let shouldRespond = lock.withLock { () -> Bool in
            guard phase.context?.sourceID == context.sourceID else { return false }
            phase = .stopped
            lastTerminalCaptureID = context.captureID
            return true
        }
        guard shouldRespond else { return }
        respond(
            error: error.map(CaptureControlError.init) ?? .internalError,
            requestID: context.prepareRequestID
        )
    }

    private func readinessTimedOut(_ context: CaptureContext) {
        let timedOut = lock.withLock { () -> Bool in
            guard case let .preparing(current) = phase,
                  current.sourceID == context.sourceID
            else { return false }
            phase = .stopped
            lastTerminalCaptureID = context.captureID
            return true
        }
        guard timedOut else { return }
        try? engine.stop()
        respond(error: .callbackTimeout, requestID: context.prepareRequestID)
    }

    private func validatedCaptureCommand(
        _ message: ControlMessage
    ) -> CaptureCommand? {
        guard exactKeys(message, required: ["type", "request_id", "capture_id"]),
              let command = message.string(for: "type"),
              let captureIDText = message.string(for: "capture_id"),
              let captureID = UUID(uuidString: captureIDText)
        else { return nil }
        return CaptureCommand(
            command: command,
            captureID: captureID,
            captureIDText: captureIDText
        )
    }

    private func finish(
        command: CaptureCommand,
        requestID: String?,
        result: CommandResult
    ) {
        guard let requestID else {
            respond(error: .invalidMessage, requestID: nil)
            return
        }
        switch result {
        case let .ack(captureIDText):
            sendControl([
                "type": .string("ack"),
                "request_id": .string(requestID),
                "command": .string(command.command),
                "capture_id": .string(captureIDText),
            ])
        case let .error(error, close):
            respond(error: error, requestID: requestID, close: close)
        }
    }

    private func respond(
        error: CaptureControlError,
        requestID: String?,
        close: Bool = false
    ) {
        sendControl([
            "type": .string("error"),
            "request_id": requestID.map(JSONValue.string) ?? .null,
            "code": .string(error.code),
            "message": .string(error.message),
            "retryable": .boolean(error.retryable),
        ])
        if close {
            emit(.closeConnection)
        }
    }

    private func sendControl(_ payload: [String: JSONValue]) {
        guard let message = try? ControlMessage(payload: payload) else { return }
        emit(.control(message))
    }
}

private struct CaptureContext: Sendable {
    let captureID: UUID
    let captureIDText: String
    let sourceID: UUID
    let deviceGeneration: UInt32
    let device: ResolvedOutputDevice
    let prepareRequestID: String
}

private enum CapturePhase {
    case stopped
    case preparing(CaptureContext)
    case ready(CaptureContext)
    case active(CaptureContext)
    case failed

    var context: CaptureContext? {
        switch self {
        case let .preparing(context), let .ready(context), let .active(context):
            context
        case .stopped, .failed:
            nil
        }
    }
}

private struct CaptureCommand {
    let command: String
    let captureID: UUID
    let captureIDText: String
}

private enum CommandResult {
    case ack(String)
    case error(CaptureControlError, Bool)
}

private enum FailureResponse {
    case ignore
    case send(String?)
}

private struct CaptureControlError: Error, Sendable {
    let code: String
    let message: String
    let retryable: Bool

    static let invalidMessage = Self(
        code: "invalid_message",
        message: "请求消息无效",
        retryable: false
    )
    static let unsupportedProtocol = Self(
        code: "unsupported_protocol",
        message: "协议版本不兼容",
        retryable: false
    )
    static let authenticationFailed = Self(
        code: "authentication_failed",
        message: "身份校验失败",
        retryable: false
    )
    static let deviceUnavailable = Self(
        code: "device_unavailable",
        message: "输出设备不可用",
        retryable: true
    )
    static let invalidState = Self(
        code: "invalid_state",
        message: "当前采集状态不允许该操作",
        retryable: false
    )
    static let captureConflict = Self(
        code: "capture_conflict",
        message: "采集标识冲突",
        retryable: false
    )
    static let callbackTimeout = Self(
        code: "callback_timeout",
        message: "等待音频回调超时",
        retryable: true
    )
    static let ioFailed = Self(
        code: "io_failed",
        message: "系统输出采集 I/O 失败",
        retryable: true
    )
    static let internalError = Self(
        code: "internal_error",
        message: "Helper 内部错误",
        retryable: false
    )

    init(code: String, message: String, retryable: Bool) {
        self.code = code
        self.message = message
        self.retryable = retryable
    }

    init(_ error: TapCaptureError) {
        code = error.code.rawValue
        message = error.description
        retryable = error.retryable
    }
}

private func exactKeys(
    _ message: ControlMessage,
    required: Set<String>,
    allowed: Set<String>? = nil
) -> Bool {
    let keys = Set(message.payload.keys)
    return required.isSubset(of: keys) && keys.isSubset(of: allowed ?? required)
}

private func integer(_ message: ControlMessage, key: String) -> Int64? {
    guard case let .integer(value) = message.payload[key] else { return nil }
    return value
}

private func boolean(_ message: ControlMessage, key: String) -> Bool? {
    guard case let .boolean(value) = message.payload[key] else { return nil }
    return value
}

private func processIDs(_ message: ControlMessage, key: String) -> [Int32]? {
    guard case let .array(values) = message.payload[key], values.count <= 32 else {
        return nil
    }
    var observed = Set<Int32>()
    var processIDs: [Int32] = []
    for value in values {
        guard case let .integer(rawValue) = value,
              (1 ... Int64(Int32.max)).contains(rawValue)
        else { return nil }
        let processID = Int32(rawValue)
        guard observed.insert(processID).inserted else { return nil }
        processIDs.append(processID)
    }
    return processIDs
}

private func validOptionalDeviceReference(_ value: JSONValue?) -> Bool {
    guard let value else { return true }
    switch value {
    case .null:
        return true
    case let .string(rawValue):
        return DeviceReference(rawValue: rawValue) != nil
    default:
        return false
    }
}

private func devicePayload(
    _ descriptor: OutputDeviceDescriptor
) -> [String: JSONValue] {
    [
        "device_ref": .string(descriptor.deviceReference),
        "label": .string(descriptor.label),
        "transport": .string(descriptor.transport.rawValue),
        "is_default": .boolean(descriptor.isDefault),
    ]
}

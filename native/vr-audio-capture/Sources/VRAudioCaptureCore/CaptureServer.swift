import Darwin
import Foundation

@_spi(Testing)
public enum OutboundWireFrame: Equatable, Sendable {
    case control(Data)
    case pcm(sequence: UInt64, data: Data)

    public var isControl: Bool {
        if case .control = self { return true }
        return false
    }

    public var pcmSequence: UInt64? {
        if case let .pcm(sequence, _) = self { return sequence }
        return nil
    }

    fileprivate var data: Data {
        switch self {
        case let .control(data), let .pcm(_, data):
            data
        }
    }
}

@_spi(Testing)
public final class BoundedWriteQueue: @unchecked Sendable {
    private let capacity: Int
    private let condition = NSCondition()
    private var frames: [OutboundWireFrame] = []
    private var finished = false
    private var droppedFrames = 0

    public init(capacity: Int) {
        precondition(capacity > 0)
        self.capacity = capacity
    }

    public var droppedPCMFrames: Int {
        condition.withLock { droppedFrames }
    }

    @discardableResult
    public func enqueue(_ frame: OutboundWireFrame) -> Bool {
        condition.withLock {
            guard !finished else { return false }
            if frames.count >= capacity {
                if let oldestPCM = frames.firstIndex(where: { !$0.isControl }) {
                    frames.remove(at: oldestPCM)
                    droppedFrames += 1
                } else if frame.isControl {
                    return false
                } else {
                    droppedFrames += 1
                    return true
                }
            }
            frames.append(frame)
            condition.signal()
            return true
        }
    }

    public func take() -> OutboundWireFrame? {
        condition.lock()
        defer { condition.unlock() }
        while frames.isEmpty, !finished {
            condition.wait()
        }
        if !frames.isEmpty {
            return frames.removeFirst()
        }
        return nil
    }

    public func finish(drain: Bool) {
        condition.withLock {
            guard !finished else {
                if !drain {
                    frames.removeAll(keepingCapacity: true)
                }
                condition.broadcast()
                return
            }
            finished = true
            if !drain {
                frames.removeAll(keepingCapacity: true)
            }
            condition.broadcast()
        }
    }
}

public final class CaptureServer: @unchecked Sendable {
    private let socketPath: URL
    private let captureToken: String
    private let catalog: DeviceCatalog
    private let engine: any CaptureEngine
    private let writeQueueCapacity: Int
    private let readinessTimeout: TimeInterval
    private let condition = NSCondition()

    private var running = false
    private var listenerDescriptor: Int32 = -1
    private var activeConnection: ClientConnection?

    public convenience init(socketPath: URL, captureToken: String) throws {
        try self.init(
            socketPath: socketPath,
            captureToken: captureToken,
            catalog: DeviceCatalog.live(),
            engine: TapCaptureEngine(),
            writeQueueCapacity: 8
        )
    }

    @_spi(Testing)
    public init(
        socketPath: URL,
        captureToken: String,
        catalog: DeviceCatalog,
        engine: any CaptureEngine,
        writeQueueCapacity: Int,
        readinessTimeout: TimeInterval = 4
    ) throws {
        guard CaptureTokenSecurity.isValid(captureToken) else {
            throw UnixPeerError(.invalidCaptureToken, "采集令牌无效")
        }
        guard (1 ... 128).contains(writeQueueCapacity),
              readinessTimeout.isFinite,
              (0 ... 30).contains(readinessTimeout),
              readinessTimeout > 0
        else {
            throw UnixPeerError(.socketOperationFailed, "服务参数无效")
        }
        self.socketPath = socketPath
        self.captureToken = captureToken
        self.catalog = catalog
        self.engine = engine
        self.writeQueueCapacity = writeQueueCapacity
        self.readinessTimeout = readinessTimeout
    }

    deinit {
        stop()
    }

    public func start() throws {
        let descriptor = try UnixPeerSecurity.makeListeningSocket(at: socketPath)
        let accepted = condition.withLock { () -> Bool in
            guard !running else { return false }
            running = true
            listenerDescriptor = descriptor
            return true
        }
        guard accepted else {
            Darwin.close(descriptor)
            UnixPeerSecurity.removeOwnedSocket(at: socketPath)
            return
        }
        Thread.detachNewThread { [weak self] in
            self?.acceptLoop(descriptor: descriptor)
        }
    }

    public func waitUntilStopped() {
        condition.lock()
        defer { condition.unlock() }
        while running {
            condition.wait()
        }
    }

    public func stop() {
        let snapshot = condition.withLock { () -> (Int32, ClientConnection?)? in
            guard running || listenerDescriptor >= 0 || activeConnection != nil else {
                return nil
            }
            running = false
            let listener = listenerDescriptor
            listenerDescriptor = -1
            let connection = activeConnection
            activeConnection = nil
            condition.broadcast()
            return (listener, connection)
        }
        guard let snapshot else { return }
        if snapshot.0 >= 0 {
            _ = shutdown(snapshot.0, SHUT_RDWR)
            Darwin.close(snapshot.0)
        }
        snapshot.1?.closeImmediately()
        UnixPeerSecurity.removeOwnedSocket(at: socketPath)
    }

    private func acceptLoop(descriptor: Int32) {
        while condition.withLock({ running }) {
            let clientDescriptor = Darwin.accept(descriptor, nil, nil)
            if clientDescriptor < 0 {
                if errno == EINTR { continue }
                if condition.withLock({ running }) { stop() }
                return
            }
            accept(clientDescriptor: clientDescriptor)
        }
    }

    private func accept(clientDescriptor: Int32) {
        do {
            try UnixPeerSecurity.configureConnectedSocket(clientDescriptor)
            let peerUID = try UnixPeerSecurity.effectivePeerUID(
                descriptor: clientDescriptor
            )
            guard UnixPeerSecurity.peerIsAuthorized(
                peerUID: peerUID,
                expectedUID: geteuid()
            ) else {
                Darwin.close(clientDescriptor)
                return
            }
        } catch {
            Darwin.close(clientDescriptor)
            return
        }

        let connectionID = UUID()
        let connection = ClientConnection(
            identifier: connectionID,
            descriptor: clientDescriptor,
            captureToken: captureToken,
            catalog: catalog,
            engine: engine,
            writeQueueCapacity: writeQueueCapacity,
            readinessTimeout: readinessTimeout,
            onClose: { [weak self] identifier in
                self?.connectionClosed(identifier: identifier)
            }
        )
        let installed = condition.withLock { () -> Bool in
            guard running, activeConnection == nil else { return false }
            activeConnection = connection
            return true
        }
        guard installed else {
            rejectSecondClient(clientDescriptor)
            return
        }
        connection.start()
    }

    private func connectionClosed(identifier: UUID) {
        condition.withLock {
            guard activeConnection?.identifier == identifier else { return }
            activeConnection = nil
        }
    }

    private func rejectSecondClient(_ descriptor: Int32) {
        if let data = serverErrorFrame(
            code: "capture_conflict",
            message: "已有客户端正在采集",
            retryable: false
        ) {
            _ = try? writeAll(data, descriptor: descriptor)
        }
        _ = shutdown(descriptor, SHUT_RDWR)
        Darwin.close(descriptor)
    }
}

private final class ClientConnection: @unchecked Sendable {
    let identifier: UUID

    private let descriptorLock = NSLock()
    private var descriptor: Int32
    private var gracefulClose = false
    private var closed = false
    private var controllerDisconnected = false

    private let captureToken: String
    private let catalog: DeviceCatalog
    private let engine: any CaptureEngine
    private let readinessTimeout: TimeInterval
    private let queue: BoundedWriteQueue
    private let onClose: @Sendable (UUID) -> Void

    private lazy var controller = CaptureController(
        captureToken: captureToken,
        catalog: catalog,
        engine: engine,
        readinessTimeout: readinessTimeout,
        emit: { [weak self] output in self?.handle(output) }
    )

    init(
        identifier: UUID,
        descriptor: Int32,
        captureToken: String,
        catalog: DeviceCatalog,
        engine: any CaptureEngine,
        writeQueueCapacity: Int,
        readinessTimeout: TimeInterval,
        onClose: @escaping @Sendable (UUID) -> Void
    ) {
        self.identifier = identifier
        self.descriptor = descriptor
        self.captureToken = captureToken
        self.catalog = catalog
        self.engine = engine
        self.readinessTimeout = readinessTimeout
        queue = BoundedWriteQueue(capacity: writeQueueCapacity)
        self.onClose = onClose
    }

    func start() {
        _ = controller
        Thread.detachNewThread { [weak self] in self?.writerLoop() }
        Thread.detachNewThread { [weak self] in self?.readerLoop() }
    }

    func closeImmediately() {
        disconnectControllerOnce()
        queue.finish(drain: false)
        finishClose()
    }

    private func handle(_ output: CaptureControllerOutput) {
        switch output {
        case let .control(message):
            guard let data = try? WireEncoder.encode(message),
                  queue.enqueue(.control(data))
            else {
                closeImmediately()
                return
            }
        case let .pcm(frame):
            guard let data = try? WireEncoder.encode(frame) else {
                DispatchQueue.global(qos: .utility).async { [weak self] in
                    self?.closeImmediately()
                }
                return
            }
            _ = queue.enqueue(.pcm(sequence: frame.sequence, data: data))
        case .closeConnection:
            requestGracefulClose()
        }
    }

    private func readerLoop() {
        let decoder = WireDecoder()
        var storage = [UInt8](repeating: 0, count: 65_536)
        while !isClosing {
            let currentDescriptor = descriptorLock.withLock { descriptor }
            guard currentDescriptor >= 0 else { break }
            let count = storage.withUnsafeMutableBytes { bytes in
                Darwin.read(currentDescriptor, bytes.baseAddress, bytes.count)
            }
            if count == 0 { break }
            if count < 0 {
                if errno == EINTR { continue }
                break
            }
            do {
                let messages = try decoder.feed(Data(storage.prefix(count)))
                for message in messages {
                    guard !isClosing else { break }
                    switch message {
                    case let .control(control):
                        controller.receive(control)
                    case .pcm:
                        protocolViolation()
                    }
                }
            } catch {
                protocolViolation()
            }
        }
        readerEnded()
    }

    private func writerLoop() {
        var succeeded = true
        while let frame = queue.take() {
            let currentDescriptor = descriptorLock.withLock { descriptor }
            guard currentDescriptor >= 0 else {
                succeeded = false
                break
            }
            do {
                try writeAll(frame.data, descriptor: currentDescriptor)
            } catch {
                succeeded = false
                break
            }
        }
        if !succeeded {
            queue.finish(drain: false)
        }
        disconnectControllerOnce()
        finishClose()
    }

    private func protocolViolation() {
        if let frame = serverErrorFrame(
            code: "invalid_message",
            message: "请求消息无效",
            retryable: false
        ) {
            _ = queue.enqueue(.control(frame))
        }
        requestGracefulClose()
    }

    private func requestGracefulClose() {
        let currentDescriptor = descriptorLock.withLock { () -> Int32 in
            guard !closed else { return -1 }
            gracefulClose = true
            return descriptor
        }
        if currentDescriptor >= 0 {
            _ = shutdown(currentDescriptor, SHUT_RD)
        }
        queue.finish(drain: true)
    }

    private func readerEnded() {
        disconnectControllerOnce()
        let isGraceful = descriptorLock.withLock { gracefulClose }
        if !isGraceful {
            queue.finish(drain: false)
            finishClose()
        }
    }

    private func disconnectControllerOnce() {
        let shouldDisconnect = descriptorLock.withLock { () -> Bool in
            guard !controllerDisconnected else { return false }
            controllerDisconnected = true
            return true
        }
        if shouldDisconnect {
            controller.clientDisconnected()
        }
    }

    private func finishClose() {
        let currentDescriptor = descriptorLock.withLock { () -> Int32 in
            guard !closed else { return -1 }
            closed = true
            let current = descriptor
            descriptor = -1
            return current
        }
        guard currentDescriptor >= 0 else { return }
        _ = shutdown(currentDescriptor, SHUT_RDWR)
        Darwin.close(currentDescriptor)
        onClose(identifier)
    }

    private var isClosing: Bool {
        descriptorLock.withLock { gracefulClose || closed }
    }
}

private func serverErrorFrame(
    code: String,
    message: String,
    retryable: Bool
) -> Data? {
    guard let control = try? ControlMessage(payload: [
        "type": .string("error"),
        "request_id": .null,
        "code": .string(code),
        "message": .string(message),
        "retryable": .boolean(retryable),
    ]) else { return nil }
    return try? WireEncoder.encode(control)
}

private func writeAll(_ data: Data, descriptor: Int32) throws {
    var offset = 0
    try data.withUnsafeBytes { bytes in
        while offset < bytes.count {
            let count = Darwin.write(
                descriptor,
                bytes.baseAddress!.advanced(by: offset),
                bytes.count - offset
            )
            if count < 0, errno == EINTR { continue }
            guard count > 0 else {
                throw UnixPeerError(.socketOperationFailed, "Socket 写入失败")
            }
            offset += count
        }
    }
}

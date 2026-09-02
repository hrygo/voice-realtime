import CoreAudio
import Foundation
@_spi(Testing) import SonaAudioCaptureCore

private let controllerToken = String(repeating: "a", count: 64)
private let controllerCaptureID = UUID(
    uuidString: "00000000-0000-0000-0000-0000000000c1"
)!
private let controllerOtherCaptureID = UUID(
    uuidString: "00000000-0000-0000-0000-0000000000c2"
)!

func captureControllerTests() -> [SelfTest] {
    [
        SelfTest("controller rejects a wrong token without leaking secrets") {
            let fixture = try ControllerFixture()

            fixture.controller.receive(try control([
                "type": .string("hello"),
                "request_id": .string("hello-1"),
                "protocol_major": .integer(1),
                "protocol_minor": .integer(0),
                "capture_token": .string(String(repeating: "b", count: 64)),
                "client_pid": .integer(123),
            ]))

            let outputs = fixture.outputs.snapshot()
            try expectEqual(errorCode(in: outputs), "authentication_failed")
            try expect(outputs.contains(.closeConnection), "connection stayed open")
            let rendered = renderedControls(outputs)
            try expect(!rendered.contains(controllerToken), "expected token leaked")
            try expect(!rendered.contains(String(repeating: "b", count: 64)), "received token leaked")
        },
        SelfTest("controller stays pre-commit silent and rejects early commit") {
            let fixture = try authenticatedFixture()
            fixture.controller.receive(try prepareMessage(captureID: controllerCaptureID))

            fixture.engine.emitFrame(sequence: 1)
            try expectEqual(fixture.outputs.pcmFrames().count, 0)

            fixture.controller.receive(try captureCommand(
                "commit_capture",
                captureID: controllerCaptureID,
                requestID: "commit-early"
            ))
            try expectEqual(errorCode(in: fixture.outputs.snapshot()), "invalid_state")

            fixture.engine.emitReady()
            try expectEqual(controlType(in: fixture.outputs.snapshot(), requestID: "prepare-1"), "ready")
            fixture.engine.emitFrame(sequence: 2)
            try expectEqual(fixture.outputs.pcmFrames().count, 0)

            fixture.controller.receive(try captureCommand(
                "commit_capture",
                captureID: controllerCaptureID,
                requestID: "commit-ok"
            ))
            fixture.engine.emitFrame(sequence: 3)

            try expectEqual(controlType(in: fixture.outputs.snapshot(), requestID: "commit-ok"), "ack")
            try expectEqual(fixture.outputs.pcmFrames().map(\.sequence), [3])
        },
        SelfTest("controller rejects capture ID mismatch and closes the client") {
            let fixture = try authenticatedFixture()
            fixture.controller.receive(try prepareMessage(captureID: controllerCaptureID))
            fixture.engine.emitReady()

            fixture.controller.receive(try captureCommand(
                "commit_capture",
                captureID: controllerOtherCaptureID,
                requestID: "commit-conflict"
            ))

            let outputs = fixture.outputs.snapshot()
            try expectEqual(errorCode(in: outputs, requestID: "commit-conflict"), "capture_conflict")
            try expect(outputs.contains(.closeConnection), "capture conflict did not close")
        },
        SelfTest("controller stop is idempotent for the same capture") {
            let fixture = try authenticatedFixture()
            fixture.controller.receive(try prepareMessage(captureID: controllerCaptureID))
            fixture.engine.emitReady()
            fixture.controller.receive(try captureCommand(
                "stop_capture",
                captureID: controllerCaptureID,
                requestID: "stop-1"
            ))
            fixture.controller.receive(try captureCommand(
                "stop_capture",
                captureID: controllerCaptureID,
                requestID: "stop-2"
            ))

            try expectEqual(fixture.engine.stopCount, 1)
            try expectEqual(controlType(in: fixture.outputs.snapshot(), requestID: "stop-1"), "ack")
            try expectEqual(controlType(in: fixture.outputs.snapshot(), requestID: "stop-2"), "ack")
        },
        SelfTest("controller disconnect stops a prepared engine exactly once") {
            let fixture = try authenticatedFixture()
            fixture.controller.receive(try prepareMessage(captureID: controllerCaptureID))

            fixture.controller.clientDisconnected()
            fixture.controller.clientDisconnected()

            try expectEqual(fixture.engine.stopCount, 1)
            try expectEqual(fixture.outputs.pcmFrames().count, 0)
        },
        SelfTest("controller validates strict JSON fields and redacts private failures") {
            let fixture = try authenticatedFixture()
            fixture.controller.receive(try control([
                "type": .string("list_devices"),
                "request_id": .string("bad-list"),
                "unexpected": .string("private-device-uid"),
            ]))
            try expectEqual(
                errorCode(in: fixture.outputs.snapshot(), requestID: "bad-list"),
                "invalid_message"
            )

            fixture.controller.receive(try prepareMessage(captureID: controllerCaptureID))
            fixture.engine.emitFailure(TapCaptureError(.permissionDenied))

            let outputs = fixture.outputs.snapshot()
            try expectEqual(errorCode(in: outputs, requestID: "prepare-1"), "permission_denied")
            let rendered = renderedControls(outputs)
            try expect(!rendered.contains("private-device-uid"), "device UID leaked")
            try expect(!rendered.contains(controllerToken), "capture token leaked")
        },
        SelfTest("controller times out a silent prepare and stops its engine") {
            let fixture = try authenticatedFixture(readinessTimeout: 0.02)
            fixture.controller.receive(try prepareMessage(captureID: controllerCaptureID))

            try fixture.outputs.waitForError("callback_timeout")

            try expectEqual(fixture.engine.stopCount, 1)
            try expectEqual(
                errorCode(in: fixture.outputs.snapshot(), requestID: "prepare-1"),
                "callback_timeout"
            )
        },
    ]
}

private func authenticatedFixture(
    readinessTimeout: TimeInterval = 5
) throws -> ControllerFixture {
    let fixture = try ControllerFixture(readinessTimeout: readinessTimeout)
    fixture.controller.receive(try control([
        "type": .string("hello"),
        "request_id": .string("hello-ok"),
        "protocol_major": .integer(1),
        "protocol_minor": .integer(0),
        "capture_token": .string(controllerToken),
        "client_pid": .integer(123),
    ]))
    try expectEqual(
        controlType(in: fixture.outputs.snapshot(), requestID: "hello-ok"),
        "hello_ack"
    )
    fixture.outputs.removeAll()
    return fixture
}

private final class ControllerFixture {
    let engine = FakeCaptureEngine()
    let outputs = ControllerOutputRecorder()
    let controller: CaptureController

    init(readinessTimeout: TimeInterval = 5) throws {
        let reader = ControllerDeviceReader()
        let catalog = try DeviceCatalog(
            propertyReader: reader,
            referenceDeriver: DeviceReferenceDeriver(
                keyData: Data(repeating: 0x5A, count: 32)
            )
        )
        controller = CaptureController(
            captureToken: controllerToken,
            catalog: catalog,
            engine: engine,
            readinessTimeout: readinessTimeout,
            emit: outputs.record
        )
    }
}

private final class FakeCaptureEngine: CaptureEngine, @unchecked Sendable {
    private let lock = NSLock()
    private var onReady: (@Sendable () -> Void)?
    private var onFrame: (@Sendable (PCMFrame) -> Void)?
    private var onFailure: (@Sendable (TapCaptureError) -> Void)?
    private var request: TapCaptureRequest?
    private var prepareCount = 0
    private var stops = 0

    var stopCount: Int { lock.withLock { stops } }

    func prepare(
        request: TapCaptureRequest,
        onReady: @escaping @Sendable () -> Void,
        onFrame: @escaping @Sendable (PCMFrame) -> Void,
        onFailure: @escaping @Sendable (TapCaptureError) -> Void
    ) throws {
        lock.withLock {
            prepareCount += 1
            self.request = request
            self.onReady = onReady
            self.onFrame = onFrame
            self.onFailure = onFailure
        }
    }

    func stop() throws {
        lock.withLock {
            if prepareCount > stops {
                stops += 1
            }
            onReady = nil
            onFrame = nil
            onFailure = nil
            request = nil
        }
    }

    func emitReady() {
        lock.withLock { onReady }?()
    }

    func emitFrame(sequence: UInt64) {
        let snapshot = lock.withLock { (request, onFrame) }
        guard let request = snapshot.0 else { return }
        snapshot.1?(pcmFrame(request: request, sequence: sequence))
    }

    func emitFailure(_ error: TapCaptureError) {
        lock.withLock { onFailure }?(error)
    }
}

private final class ControllerOutputRecorder: @unchecked Sendable {
    private let condition = NSCondition()
    private var values: [CaptureControllerOutput] = []

    func record(_ output: CaptureControllerOutput) {
        condition.withLock {
            values.append(output)
            condition.broadcast()
        }
    }

    func snapshot() -> [CaptureControllerOutput] {
        condition.withLock { values }
    }

    func pcmFrames() -> [PCMFrame] {
        snapshot().compactMap {
            if case let .pcm(frame) = $0 { return frame }
            return nil
        }
    }

    func removeAll() {
        condition.withLock { values.removeAll() }
    }

    func waitForError(_ code: String) throws {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(2)
        while !values.contains(where: { output in
            guard case let .control(message) = output else { return false }
            return message.string(for: "type") == "error" &&
                message.string(for: "code") == code
        }) {
            guard condition.wait(until: deadline) else {
                throw SelfTestFailure("controller error timed out")
            }
        }
    }
}

private final class ControllerDeviceReader: OutputDevicePropertyReading {
    func deviceIDs() throws -> [UInt32] { [1] }
    func defaultOutputDeviceID() throws -> UInt32? { 1 }
    func isAlive(deviceID _: UInt32) throws -> Bool { true }
    func outputChannelCount(deviceID _: UInt32) throws -> UInt32 { 2 }
    func name(deviceID _: UInt32) throws -> String { "Built-in Output" }
    func uid(deviceID _: UInt32) throws -> String { "private-device-uid" }
    func transportType(deviceID _: UInt32) throws -> UInt32 {
        kAudioDeviceTransportTypeBuiltIn
    }
}

private func prepareMessage(captureID: UUID) throws -> ControlMessage {
    try control([
        "type": .string("prepare_capture"),
        "request_id": .string("prepare-1"),
        "capture_id": .string(captureID.uuidString.lowercased()),
        "follow_default_output": .boolean(true),
        "device_ref": .null,
        "exclude_pids": .array([]),
    ])
}

private func captureCommand(
    _ command: String,
    captureID: UUID,
    requestID: String
) throws -> ControlMessage {
    try control([
        "type": .string(command),
        "request_id": .string(requestID),
        "capture_id": .string(captureID.uuidString.lowercased()),
    ])
}

private func control(_ payload: [String: JSONValue]) throws -> ControlMessage {
    try ControlMessage(payload: payload)
}

private func pcmFrame(request: TapCaptureRequest, sequence: UInt64) -> PCMFrame {
    PCMFrame(
        captureID: request.captureID,
        sourceID: request.sourceID,
        deviceGeneration: request.deviceGeneration,
        sequence: sequence,
        hostTimeNanoseconds: sequence + 1,
        sampleRate: 16_000,
        samplesPerChannel: 512,
        channels: 1,
        sampleWidth: 2,
        flags: [],
        payload: Data(repeating: 0, count: 1_024)
    )
}

private func controlType(
    in outputs: [CaptureControllerOutput],
    requestID: String
) -> String? {
    outputs.compactMap { output -> String? in
        guard case let .control(message) = output,
              message.string(for: "request_id") == requestID
        else { return nil }
        return message.string(for: "type")
    }.last
}

private func errorCode(
    in outputs: [CaptureControllerOutput],
    requestID: String? = nil
) -> String? {
    outputs.compactMap { output -> String? in
        guard case let .control(message) = output,
              message.string(for: "type") == "error"
        else { return nil }
        if let requestID, message.string(for: "request_id") != requestID {
            return nil
        }
        return message.string(for: "code")
    }.last
}

private func renderedControls(_ outputs: [CaptureControllerOutput]) -> String {
    outputs.compactMap { output -> Data? in
        guard case let .control(message) = output else { return nil }
        return try? WireEncoder.encode(message)
    }.map { String(decoding: $0, as: UTF8.self) }.joined()
}

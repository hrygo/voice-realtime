import CoreAudio
import Darwin
import Foundation
@_spi(Testing) import VRAudioCaptureCore

private let serverToken = String(repeating: "c", count: 64)
private let serverCaptureID = UUID(
    uuidString: "00000000-0000-0000-0000-0000000000e1"
)!

func captureServerTests() -> [SelfTest] {
    [
        SelfTest("Unix peer and private directory checks fail closed") {
            try expect(
                CaptureTokenSecurity.constantTimeEqual(serverToken, serverToken),
                "equal tokens were rejected"
            )
            try expect(
                !CaptureTokenSecurity.constantTimeEqual(
                    String(repeating: "c", count: 1_024),
                    serverToken
                ),
                "oversized token was accepted"
            )
            try withRuntimeDirectory { directory in
                try UnixPeerSecurity.validateSocketParent(directory)
                try expect(
                    UnixPeerSecurity.peerIsAuthorized(
                        peerUID: geteuid(),
                        expectedUID: geteuid()
                    ),
                    "same UID was rejected"
                )
                try expect(
                    !UnixPeerSecurity.peerIsAuthorized(
                        peerUID: geteuid() &+ 1,
                        expectedUID: geteuid()
                    ),
                    "different UID was accepted"
                )

                try FileManager.default.setAttributes(
                    [.posixPermissions: 0o750],
                    ofItemAtPath: directory.path
                )
                try expectThrows(UnixPeerError.self) {
                    try UnixPeerSecurity.validateSocketParent(directory)
                }
            }

            try withRuntimeDirectory { directory in
                let link = directory.deletingLastPathComponent()
                    .appendingPathComponent("vrac-link-(UUID().uuidString)")
                try FileManager.default.createSymbolicLink(
                    at: link,
                    withDestinationURL: directory
                )
                defer { try? FileManager.default.removeItem(at: link) }
                try expectThrows(UnixPeerError.self) {
                    try UnixPeerSecurity.validateSocketParent(link)
                }
            }
        },
        SelfTest("bounded writer drops oldest PCM and preserves controls") {
            let queue = BoundedWriteQueue(capacity: 3)
            try expect(queue.enqueue(.pcm(sequence: 1, data: Data([1]))), "enqueue 1")
            try expect(queue.enqueue(.control(Data([9]))), "enqueue control")
            try expect(queue.enqueue(.pcm(sequence: 2, data: Data([2]))), "enqueue 2")
            try expect(queue.enqueue(.pcm(sequence: 3, data: Data([3]))), "enqueue 3")

            queue.finish(drain: true)
            let frames = [queue.take(), queue.take(), queue.take()].compactMap { $0 }

            try expectEqual(queue.droppedPCMFrames, 1)
            try expectEqual(frames.filter(\.isControl).count, 1)
            try expectEqual(frames.compactMap(\.pcmSequence), [2, 3])
        },
        SelfTest("server creates a 0600 socket and rejects a second client") {
            try withServer { fixture in
                let first = try connectUnixSocket(fixture.socketPath)
                defer { Darwin.close(first) }
                try sendControl(helloMessage(token: serverToken, requestID: "hello-first"), to: first)
                let helloResponse = try readControl(from: first)
                try expectEqual(
                    helloResponse.string(for: "type"),
                    "hello_ack"
                )

                var metadata = stat()
                try expectEqual(lstat(fixture.socketPath.path, &metadata), 0)
                try expectEqual(metadata.st_mode & 0o777, 0o600)

                let second = try connectUnixSocket(fixture.socketPath)
                defer { Darwin.close(second) }
                let conflict = try readControl(from: second)
                try expectEqual(conflict.string(for: "type"), "error")
                try expectEqual(conflict.string(for: "code"), "capture_conflict")

                try sendControl(
                    try ControlMessage(payload: [
                        "type": .string("list_devices"),
                        "request_id": .string("list-first"),
                    ]),
                    to: first
                )
                let devicesResponse = try readControl(from: first)
                try expectEqual(devicesResponse.string(for: "type"), "devices")
            }
        },
        SelfTest("server rejects oversized JSON from the prefix") {
            try withServer { fixture in
                let client = try connectUnixSocket(fixture.socketPath)
                defer { Darwin.close(client) }
                try sendControl(helloMessage(token: serverToken, requestID: "hello-limit"), to: client)
                _ = try readControl(from: client)

                try writeAll(oversizedControlPrefix(), to: client)
                let response = try readControl(from: client)
                try expectEqual(response.string(for: "type"), "error")
                try expectEqual(response.string(for: "code"), "invalid_message")
            }
        },
        SelfTest("client disconnect stops prepared capture exactly once") {
            try withServer { fixture in
                let client = try connectUnixSocket(fixture.socketPath)
                try sendControl(helloMessage(token: serverToken, requestID: "hello-stop"), to: client)
                _ = try readControl(from: client)
                try sendControl(prepareServerMessage(), to: client)
                try fixture.engine.waitUntilPrepared()
                fixture.engine.emitReady()
                let readyResponse = try readControl(from: client)
                try expectEqual(readyResponse.string(for: "type"), "ready")

                Darwin.close(client)
                try fixture.engine.waitForStopCount(1)
                try expectEqual(fixture.engine.stopCount, 1)
            }
        },
    ]
}

private struct ServerFixture {
    let server: CaptureServer
    let engine: ServerFakeCaptureEngine
    let socketPath: URL
}

private func withServer(_ body: (ServerFixture) throws -> Void) throws {
    try withRuntimeDirectory { directory in
        let socketPath = directory.appendingPathComponent("capture.sock")
        let engine = ServerFakeCaptureEngine()
        let catalog = try serverDeviceCatalog()
        let server = try CaptureServer(
            socketPath: socketPath,
            captureToken: serverToken,
            catalog: catalog,
            engine: engine,
            writeQueueCapacity: 4
        )
        try server.start()
        defer { server.stop() }
        try body(ServerFixture(server: server, engine: engine, socketPath: socketPath))
    }
}

private func withRuntimeDirectory(_ body: (URL) throws -> Void) throws {
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("vrac-(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(
        at: directory,
        withIntermediateDirectories: false,
        attributes: [.posixPermissions: 0o700]
    )
    defer { try? FileManager.default.removeItem(at: directory) }
    try body(directory)
}

private final class ServerFakeCaptureEngine: CaptureEngine, @unchecked Sendable {
    private let condition = NSCondition()
    private var request: TapCaptureRequest?
    private var onReady: (@Sendable () -> Void)?
    private var onFrame: (@Sendable (PCMFrame) -> Void)?
    private var onFailure: (@Sendable (TapCaptureError) -> Void)?
    private var stops = 0

    var stopCount: Int { condition.withLock { stops } }

    func prepare(
        request: TapCaptureRequest,
        onReady: @escaping @Sendable () -> Void,
        onFrame: @escaping @Sendable (PCMFrame) -> Void,
        onFailure: @escaping @Sendable (TapCaptureError) -> Void
    ) throws {
        condition.withLock {
            self.request = request
            self.onReady = onReady
            self.onFrame = onFrame
            self.onFailure = onFailure
            condition.broadcast()
        }
    }

    func stop() throws {
        condition.withLock {
            if request != nil {
                stops += 1
            }
            request = nil
            onReady = nil
            onFrame = nil
            onFailure = nil
            condition.broadcast()
        }
    }

    func emitReady() {
        condition.withLock { onReady }?()
    }

    func waitUntilPrepared() throws {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(2)
        while request == nil {
            guard condition.wait(until: deadline) else {
                throw SelfTestFailure("capture was not prepared")
            }
        }
    }

    func waitForStopCount(_ expected: Int) throws {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(2)
        while stops < expected {
            guard condition.wait(until: deadline) else {
                throw SelfTestFailure("capture was not stopped")
            }
        }
    }
}

private final class ServerDeviceReader: OutputDevicePropertyReading {
    func deviceIDs() throws -> [UInt32] { [7] }
    func defaultOutputDeviceID() throws -> UInt32? { 7 }
    func isAlive(deviceID _: UInt32) throws -> Bool { true }
    func outputChannelCount(deviceID _: UInt32) throws -> UInt32 { 2 }
    func name(deviceID _: UInt32) throws -> String { "Test Output" }
    func uid(deviceID _: UInt32) throws -> String { "private-server-device-uid" }
    func transportType(deviceID _: UInt32) throws -> UInt32 {
        kAudioDeviceTransportTypeBuiltIn
    }
}

private func serverDeviceCatalog() throws -> DeviceCatalog {
    try DeviceCatalog(
        propertyReader: ServerDeviceReader(),
        referenceDeriver: DeviceReferenceDeriver(
            keyData: Data(repeating: 0x6A, count: 32)
        )
    )
}

private func helloMessage(token: String, requestID: String) throws -> ControlMessage {
    try ControlMessage(payload: [
        "type": .string("hello"),
        "request_id": .string(requestID),
        "protocol_major": .integer(1),
        "protocol_minor": .integer(0),
        "capture_token": .string(token),
        "client_pid": .integer(Int64(getpid())),
    ])
}

private func prepareServerMessage() throws -> ControlMessage {
    try ControlMessage(payload: [
        "type": .string("prepare_capture"),
        "request_id": .string("prepare-server"),
        "capture_id": .string(serverCaptureID.uuidString.lowercased()),
        "follow_default_output": .boolean(true),
        "device_ref": .null,
        "exclude_pids": .array([]),
    ])
}

private func connectUnixSocket(_ path: URL) throws -> Int32 {
    let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    guard descriptor >= 0 else { throw SelfTestFailure("socket failed") }
    do {
        var address = try unixAddress(path.path)
        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(
                    descriptor,
                    $0,
                    socklen_t(MemoryLayout<sockaddr_un>.size)
                )
            }
        }
        guard result == 0 else { throw SelfTestFailure("connect failed: \(errno)") }
        return descriptor
    } catch {
        Darwin.close(descriptor)
        throw error
    }
}

private func unixAddress(_ path: String) throws -> sockaddr_un {
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8CString)
    let capacity = MemoryLayout.size(ofValue: address.sun_path)
    guard bytes.count <= capacity else { throw SelfTestFailure("socket path too long") }
    withUnsafeMutablePointer(to: &address.sun_path) { pointer in
        pointer.withMemoryRebound(to: CChar.self, capacity: capacity) { destination in
            for (index, byte) in bytes.enumerated() {
                destination[index] = byte
            }
        }
    }
    return address
}

private func sendControl(_ message: ControlMessage, to descriptor: Int32) throws {
    try writeAll(WireEncoder.encode(message), to: descriptor)
}

private func writeAll(_ data: Data, to descriptor: Int32) throws {
    var offset = 0
    try data.withUnsafeBytes { bytes in
        while offset < bytes.count {
            let count = Darwin.write(
                descriptor,
                bytes.baseAddress!.advanced(by: offset),
                bytes.count - offset
            )
            guard count > 0 else { throw SelfTestFailure("write failed: \(errno)") }
            offset += count
        }
    }
}

private func readControl(from descriptor: Int32) throws -> ControlMessage {
    let decoder = WireDecoder()
    var storage = [UInt8](repeating: 0, count: 4_096)
    let deadline = Date().addingTimeInterval(3)
    while Date() < deadline {
        let count = Darwin.read(descriptor, &storage, storage.count)
        guard count > 0 else { throw SelfTestFailure("connection closed before response") }
        let messages = try decoder.feed(Data(storage.prefix(count)))
        for message in messages {
            if case let .control(control) = message { return control }
        }
    }
    throw SelfTestFailure("response timed out")
}

private func oversizedControlPrefix() -> Data {
    var data = Data("VRAC".utf8)
    data.append(contentsOf: [0, 16, 1, 0, 1, 0, 0, 0])
    let bodyLength = UInt32(WireProtocol.maximumControlBodyBytes + 1).bigEndian
    withUnsafeBytes(of: bodyLength) { data.append(contentsOf: $0) }
    return data
}

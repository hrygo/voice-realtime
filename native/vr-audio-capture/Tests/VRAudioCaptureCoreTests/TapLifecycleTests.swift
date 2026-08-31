import Darwin
import Foundation
@_spi(Testing) import VRAudioCaptureCore

func tapLifecycleTests() -> [SelfTest] {
    [
        SelfTest("tap resources start and stop in dependency order") {
            let hal = FakeTapCaptureHAL()
            hal.processObjects = [101: 1_001, 202: nil]
            let engine = TapCaptureEngine(hal: hal)

            try engine.prepare(
                request: captureRequest(excludePIDs: [101, 202]),
                onReady: {},
                onFrame: { _ in },
                onFailure: { _ in }
            )
            try engine.stop()
            try engine.stop()

            try expectEqual(hal.excludedProcessObjectIDs, [1_001])
            try expectEqual(hal.lifecycleEvents, [
                "create_tap",
                "create_aggregate",
                "create_io",
                "start_io",
                "stop_io",
                "destroy_io",
                "destroy_aggregate",
                "destroy_tap",
            ])
        },
        SelfTest("callback data crosses the ring and emits a canonical PCM frame") {
            let hal = FakeTapCaptureHAL(
                tapFormat: CaptureAudioFormat(
                    sampleRate: 16_000,
                    channels: 1,
                    bytesPerFrame: 4,
                    isFloat32: true,
                    isInterleaved: true
                ),
                maximumInputFrames: 512
            )
            let engine = TapCaptureEngine(hal: hal)
            let state = CapturedFrameState()
            defer { try? engine.stop() }
            try engine.prepare(
                request: captureRequest(),
                onReady: { state.markReady() },
                onFrame: { state.append(frame: $0) },
                onFailure: { state.append(error: $0) }
            )

            hal.emit(
                samples: Array(repeating: Float(0.25), count: 512),
                hostTimeNanoseconds: 1_000_000_000
            )
            let snapshot = state.waitForFrame()

            try expect(snapshot.completed, "worker did not emit a frame")
            try expectEqual(snapshot.readyCount, 1)
            try expect(snapshot.errors.isEmpty, "worker failed: \(snapshot.errors)")
            let frame = try requiredFrame(snapshot.frames.first)
            try expectEqual(frame.captureID, captureRequest().captureID)
            try expectEqual(frame.sourceID, captureRequest().sourceID)
            try expectEqual(frame.sampleRate, 16_000)
            try expectEqual(frame.samplesPerChannel, 512)
            try expectEqual(frame.channels, 1)
            try expectEqual(frame.sampleWidth, 2)
            try expectEqual(frame.payload.count, 1_024)
            try expect(frame.payload.contains(where: { $0 != 0 }), "PCM is silent")
        },
        SelfTest("start failure rolls back prepared resources in reverse order") {
            let hal = FakeTapCaptureHAL(
                failurePoint: .startIO,
                failure: .failed
            )
            let engine = TapCaptureEngine(hal: hal)

            try expectTapError(.ioFailed) {
                try engine.prepare(
                    request: captureRequest(),
                    onReady: {},
                    onFrame: { _ in },
                    onFailure: { _ in }
                )
            }

            try expectEqual(hal.lifecycleEvents, [
                "create_tap",
                "create_aggregate",
                "create_io",
                "start_io",
                "stop_io",
                "destroy_io",
                "destroy_aggregate",
                "destroy_tap",
            ])
        },
        SelfTest("aggregate failure destroys the tap without widening scope") {
            let hal = FakeTapCaptureHAL(failurePoint: .createAggregate)
            let engine = TapCaptureEngine(hal: hal)

            try expectTapError(.unsupportedDeviceScope) {
                try engine.prepare(
                    request: captureRequest(),
                    onReady: {},
                    onFrame: { _ in },
                    onFailure: { _ in }
                )
            }

            try expectEqual(hal.lifecycleEvents, [
                "create_tap",
                "create_aggregate",
                "destroy_tap",
            ])
        },
        SelfTest("permission failures map to stable redacted errors") {
            let hal = FakeTapCaptureHAL(
                failurePoint: .createTap,
                failure: .permissionDenied
            )
            let engine = TapCaptureEngine(hal: hal)
            let privateUID = "private-device-uid"

            do {
                try engine.prepare(
                    request: captureRequest(deviceUID: privateUID),
                    onReady: {},
                    onFrame: { _ in },
                    onFailure: { _ in }
                )
                throw SelfTestFailure("expected permission error")
            } catch let error as TapCaptureError {
                try expectEqual(error.code, .permissionDenied)
                try expect(!error.description.contains(privateUID), "UID leaked")
            }
        },
        SelfTest("cleanup continues after one HAL release fails") {
            let hal = FakeTapCaptureHAL(
                failurePoint: .destroyIO,
                failure: .failed
            )
            let engine = TapCaptureEngine(hal: hal)
            try engine.prepare(
                request: captureRequest(),
                onReady: {},
                onFrame: { _ in },
                onFailure: { _ in }
            )

            try expectTapError(.ioFailed) {
                try engine.stop()
            }

            try expectEqual(Array(hal.lifecycleEvents.suffix(4)), [
                "stop_io",
                "destroy_io",
                "destroy_aggregate",
                "destroy_tap",
            ])
        },
    ]
}

private func requiredFrame(_ frame: PCMFrame?) throws -> PCMFrame {
    guard let frame else {
        throw SelfTestFailure("missing PCM frame")
    }
    return frame
}

private func captureRequest(
    deviceUID: String = "private-device-uid",
    excludePIDs: [Int32] = []
) -> TapCaptureRequest {
    TapCaptureRequest(
        captureID: UUID(uuidString: "00000000-0000-0000-0000-000000000011")!,
        sourceID: UUID(uuidString: "00000000-0000-0000-0000-000000000012")!,
        deviceUID: deviceUID,
        deviceGeneration: 3,
        excludedProcessIDs: excludePIDs
    )
}

private func expectTapError<Result>(
    _ code: TapCaptureError.Code,
    _ body: () throws -> Result
) throws {
    do {
        _ = try body()
    } catch let error as TapCaptureError {
        try expectEqual(error.code, code)
        return
    }
    throw SelfTestFailure("expected tap error \(code)")
}

private final class FakeTapCaptureHAL: TapCaptureHAL, @unchecked Sendable {
    enum FailurePoint {
        case createTap
        case createAggregate
        case createIO
        case startIO
        case destroyIO
    }

    private let failurePoint: FailurePoint?
    private let failure: HALCaptureError
    private let tapFormat: CaptureAudioFormat
    private let maximumInputFrames: UInt32
    private var inputSink: (any TapInputSink)?
    var processObjects: [Int32: UInt32?] = [:]
    private(set) var excludedProcessObjectIDs: [UInt32] = []
    private(set) var lifecycleEvents: [String] = []

    init(
        failurePoint: FailurePoint? = nil,
        failure: HALCaptureError = .unsupportedDeviceScope,
        tapFormat: CaptureAudioFormat = CaptureAudioFormat(
            sampleRate: 48_000,
            channels: 1,
            bytesPerFrame: 4,
            isFloat32: true,
            isInterleaved: true
        ),
        maximumInputFrames: UInt32 = 1_024
    ) {
        self.failurePoint = failurePoint
        self.failure = failure
        self.tapFormat = tapFormat
        self.maximumInputFrames = maximumInputFrames
    }

    func processObjectID(forPID pid: Int32) throws -> UInt32? {
        processObjects[pid] ?? nil
    }

    func createTap(
        deviceUID _: String,
        excludedProcessObjectIDs: [UInt32]
    ) throws -> HALTapResource {
        lifecycleEvents.append("create_tap")
        self.excludedProcessObjectIDs = excludedProcessObjectIDs
        try fail(if: .createTap)
        return HALTapResource(
            objectID: 11,
            uid: "synthetic-tap-uid",
            format: tapFormat
        )
    }

    func createAggregate(tap _: HALTapResource) throws -> HALAggregateResource {
        lifecycleEvents.append("create_aggregate")
        try fail(if: .createAggregate)
        return HALAggregateResource(
            objectID: 22,
            maximumInputFrames: maximumInputFrames
        )
    }

    func createIOProc(
        aggregate _: HALAggregateResource,
        format _: CaptureAudioFormat,
        sink: any TapInputSink
    ) throws -> HALIOProcResource {
        lifecycleEvents.append("create_io")
        try fail(if: .createIO)
        inputSink = sink
        return HALIOProcResource(identifier: 33)
    }

    func startIO(
        aggregate _: HALAggregateResource,
        ioProc _: HALIOProcResource
    ) throws {
        lifecycleEvents.append("start_io")
        try fail(if: .startIO)
    }

    func stopIO(
        aggregate _: HALAggregateResource,
        ioProc _: HALIOProcResource
    ) throws {
        lifecycleEvents.append("stop_io")
    }

    func destroyIOProc(
        aggregate _: HALAggregateResource,
        ioProc _: HALIOProcResource
    ) throws {
        lifecycleEvents.append("destroy_io")
        try fail(if: .destroyIO)
    }

    func destroyAggregate(_ aggregate: HALAggregateResource) throws {
        lifecycleEvents.append("destroy_aggregate")
    }

    func destroyTap(_ tap: HALTapResource) throws {
        lifecycleEvents.append("destroy_tap")
    }

    private func fail(if point: FailurePoint) throws {
        if failurePoint == point {
            throw failure
        }
    }

    func emit(samples: [Float], hostTimeNanoseconds: UInt64) {
        samples.withUnsafeBytes { bytes in
            inputSink?.consume(
                hostTimeNanoseconds: hostTimeNanoseconds,
                frameCount: UInt32(samples.count / Int(tapFormat.channels)),
                bytes: bytes
            )
        }
    }
}

private final class CapturedFrameState: @unchecked Sendable {
    private let condition = NSCondition()
    private var readyCount = 0
    private var frames: [PCMFrame] = []
    private var errors: [String] = []

    func markReady() {
        condition.withLock {
            readyCount += 1
            condition.broadcast()
        }
    }

    func append(frame: PCMFrame) {
        condition.withLock {
            frames.append(frame)
            condition.broadcast()
        }
    }

    func append(error: TapCaptureError) {
        condition.withLock {
            errors.append(error.code.rawValue)
            condition.broadcast()
        }
    }

    func waitForFrame() -> (
        readyCount: Int,
        frames: [PCMFrame],
        errors: [String],
        completed: Bool
    ) {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(3)
        while frames.isEmpty, errors.isEmpty {
            if !condition.wait(until: deadline) {
                break
            }
        }
        return (readyCount, frames, errors, !frames.isEmpty)
    }
}

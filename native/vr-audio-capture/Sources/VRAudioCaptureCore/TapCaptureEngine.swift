import Darwin
import Foundation

public struct TapCaptureError: Error, Equatable, CustomStringConvertible, Sendable {
    public enum Code: String, Equatable, Sendable {
        case unsupportedOS = "unsupported_os"
        case permissionDenied = "permission_denied"
        case unsupportedDeviceScope = "unsupported_device_scope"
        case conversionFailed = "conversion_failed"
        case ioFailed = "io_failed"
        case invalidState = "invalid_state"
    }

    public let code: Code
    public let description: String
    public let retryable: Bool

    @_spi(Testing)
    public init(_ code: Code) {
        self.code = code
        switch code {
        case .unsupportedOS:
            description = "当前 macOS 版本不支持系统输出采集"
            retryable = false
        case .permissionDenied:
            description = "系统音频录制权限未授权"
            retryable = true
        case .unsupportedDeviceScope:
            description = "该输出设备不支持安全的设备范围采集"
            retryable = false
        case .conversionFailed:
            description = "输出音频格式无法转换"
            retryable = true
        case .ioFailed:
            description = "系统输出采集 I/O 失败"
            retryable = true
        case .invalidState:
            description = "采集状态不允许执行该操作"
            retryable = false
        }
    }
}

@_spi(Testing)
public struct TapCaptureRequest: Sendable {
    public let captureID: UUID
    public let sourceID: UUID
    public let deviceUID: String
    public let deviceGeneration: UInt32
    public let excludedProcessIDs: [Int32]

    public init(
        captureID: UUID,
        sourceID: UUID,
        deviceUID: String,
        deviceGeneration: UInt32,
        excludedProcessIDs: [Int32]
    ) {
        self.captureID = captureID
        self.sourceID = sourceID
        self.deviceUID = deviceUID
        self.deviceGeneration = deviceGeneration
        self.excludedProcessIDs = excludedProcessIDs
    }
}

@_spi(Testing)
public final class TapCaptureEngine: @unchecked Sendable {
    private let hal: any TapCaptureHAL
    private let stateLock = NSLock()
    private var preparing = false
    private var resources: ActiveCaptureResources?

    public convenience init() {
        self.init(hal: CoreAudioHAL())
    }

    public init(hal: any TapCaptureHAL) {
        self.hal = hal
    }

    deinit {
        try? stop()
    }

    public func prepare(
        request: TapCaptureRequest,
        onReady: @escaping @Sendable () -> Void,
        onFrame: @escaping @Sendable (PCMFrame) -> Void,
        onFailure: @escaping @Sendable (TapCaptureError) -> Void
    ) throws {
        let mayPrepare = stateLock.withLock { () -> Bool in
            guard !preparing, resources == nil else {
                return false
            }
            preparing = true
            return true
        }
        guard mayPrepare else {
            throw TapCaptureError(.invalidState)
        }
        defer {
            stateLock.withLock {
                preparing = false
            }
        }

        var stage = PreparationStage.processes
        var tap: HALTapResource?
        var aggregate: HALAggregateResource?
        var ioProc: HALIOProcResource?
        var worker: CaptureWorker?
        var startAttempted = false

        do {
            let excludedObjects = try excludedProcessObjects(
                requestedPIDs: request.excludedProcessIDs
            )
            stage = .tap
            let createdTap = try hal.createTap(
                deviceUID: request.deviceUID,
                excludedProcessObjectIDs: excludedObjects
            )
            tap = createdTap

            stage = .aggregate
            let createdAggregate = try hal.createAggregate(tap: createdTap)
            aggregate = createdAggregate

            stage = .conversion
            let byteCount = try maximumPayloadByteCount(
                format: createdTap.format,
                frameCount: createdAggregate.maximumInputFrames
            )
            let ring = try SPSCRingBuffer(
                capacity: 64,
                maximumPayloadBytes: byteCount
            )
            let normalizer = try AudioNormalizer(
                sourceFormat: createdTap.format,
                maximumInputFrames: createdAggregate.maximumInputFrames
            )
            let createdSink = try RingTapInputSink(
                ring: ring,
                format: createdTap.format
            )
            let createdWorker = CaptureWorker(
                ring: ring,
                normalizer: normalizer,
                captureID: request.captureID,
                sourceID: request.sourceID,
                deviceGeneration: request.deviceGeneration,
                onReady: onReady,
                onFrame: onFrame,
                onFailure: onFailure
            )
            worker = createdWorker

            stage = .io
            let createdIOProc = try hal.createIOProc(
                aggregate: createdAggregate,
                format: createdTap.format,
                sink: createdSink
            )
            ioProc = createdIOProc
            createdWorker.start()
            startAttempted = true
            try hal.startIO(
                aggregate: createdAggregate,
                ioProc: createdIOProc
            )

            stateLock.withLock {
                resources = ActiveCaptureResources(
                    tap: createdTap,
                    aggregate: createdAggregate,
                    ioProc: createdIOProc,
                    sink: createdSink,
                    worker: createdWorker
                )
            }
        } catch {
            _ = cleanup(
                tap: tap,
                aggregate: aggregate,
                ioProc: ioProc,
                worker: worker,
                stopIO: startAttempted
            )
            throw mapPreparationError(error, stage: stage)
        }
    }

    public func stop() throws {
        let active = try stateLock.withLock { () throws -> ActiveCaptureResources? in
            guard !preparing else {
                throw TapCaptureError(.invalidState)
            }
            defer { resources = nil }
            return resources
        }
        guard let active else {
            return
        }
        let failed = cleanup(
            tap: active.tap,
            aggregate: active.aggregate,
            ioProc: active.ioProc,
            worker: active.worker,
            stopIO: true
        )
        _ = active.sink
        if failed {
            throw TapCaptureError(.ioFailed)
        }
    }

    private func excludedProcessObjects(
        requestedPIDs: [Int32]
    ) throws -> [UInt32] {
        var observedPIDs = Set<Int32>()
        var observedObjects = Set<UInt32>()
        var objectIDs: [UInt32] = []
        for pid in [Int32(getpid())] + requestedPIDs where pid > 0 {
            guard observedPIDs.insert(pid).inserted else {
                continue
            }
            guard let objectID = try hal.processObjectID(forPID: pid) else {
                continue
            }
            if observedObjects.insert(objectID).inserted {
                objectIDs.append(objectID)
            }
        }
        return objectIDs
    }

    private func cleanup(
        tap: HALTapResource?,
        aggregate: HALAggregateResource?,
        ioProc: HALIOProcResource?,
        worker: CaptureWorker?,
        stopIO: Bool
    ) -> Bool {
        var failed = false
        if stopIO, let aggregate, let ioProc {
            do {
                try hal.stopIO(aggregate: aggregate, ioProc: ioProc)
            } catch {
                failed = true
            }
        }
        worker?.stop()
        if let aggregate, let ioProc {
            do {
                try hal.destroyIOProc(aggregate: aggregate, ioProc: ioProc)
            } catch {
                failed = true
            }
        }
        if let aggregate {
            do {
                try hal.destroyAggregate(aggregate)
            } catch {
                failed = true
            }
        }
        if let tap {
            do {
                try hal.destroyTap(tap)
            } catch {
                failed = true
            }
        }
        return failed
    }
}

private enum PreparationStage {
    case processes
    case tap
    case aggregate
    case conversion
    case io
}

private struct ActiveCaptureResources {
    let tap: HALTapResource
    let aggregate: HALAggregateResource
    let ioProc: HALIOProcResource
    let sink: RingTapInputSink
    let worker: CaptureWorker
}

private final class RingTapInputSink: TapInputSink, @unchecked Sendable {
    private let ring: SPSCRingBuffer
    private let sampleRate: UInt32
    private let channels: UInt16
    private let bytesPerFrame: Int
    private var nextSequence: UInt64 = 0

    init(ring: SPSCRingBuffer, format: CaptureAudioFormat) throws {
        let roundedSampleRate = format.sampleRate.rounded()
        guard
            roundedSampleRate == format.sampleRate,
            roundedSampleRate > 0,
            roundedSampleRate <= Double(UInt32.max),
            format.channels > 0,
            format.channels <= UInt32(UInt16.max),
            format.bytesPerFrame > 0
        else {
            throw AudioNormalizerError.invalidFormat
        }
        self.ring = ring
        sampleRate = UInt32(roundedSampleRate)
        channels = UInt16(format.channels)
        bytesPerFrame = Int(format.bytesPerFrame)
    }

    func consume(
        hostTimeNanoseconds: UInt64,
        frameCount: UInt32,
        bytes: UnsafeRawBufferPointer
    ) {
        guard
            frameCount > 0,
            Int(frameCount) <= Int.max / bytesPerFrame,
            bytes.count == Int(frameCount) * bytesPerFrame
        else {
            return
        }
        let sequence = nextSequence
        nextSequence &+= 1
        _ = ring.push(
            metadata: RingRecordMetadata(
                sequence: sequence,
                hostTimeNanoseconds: hostTimeNanoseconds,
                sampleRate: sampleRate,
                frameCount: frameCount,
                channels: channels,
                bytesPerSample: 4
            ),
            payload: bytes
        )
    }
}

private final class CaptureWorker: @unchecked Sendable {
    private let ring: SPSCRingBuffer
    private let normalizer: AudioNormalizer
    private let accumulator = FrameAccumulator()
    private let captureID: UUID
    private let sourceID: UUID
    private let deviceGeneration: UInt32
    private let onReady: @Sendable () -> Void
    private let onFrame: @Sendable (PCMFrame) -> Void
    private let onFailure: @Sendable (TapCaptureError) -> Void
    private let stateLock = NSLock()
    private let completion = DispatchGroup()
    private var stopping = false
    private var started = false

    init(
        ring: SPSCRingBuffer,
        normalizer: AudioNormalizer,
        captureID: UUID,
        sourceID: UUID,
        deviceGeneration: UInt32,
        onReady: @escaping @Sendable () -> Void,
        onFrame: @escaping @Sendable (PCMFrame) -> Void,
        onFailure: @escaping @Sendable (TapCaptureError) -> Void
    ) {
        self.ring = ring
        self.normalizer = normalizer
        self.captureID = captureID
        self.sourceID = sourceID
        self.deviceGeneration = deviceGeneration
        self.onReady = onReady
        self.onFrame = onFrame
        self.onFailure = onFailure
    }

    func start() {
        let shouldStart = stateLock.withLock { () -> Bool in
            guard !started else {
                return false
            }
            started = true
            stopping = false
            return true
        }
        guard shouldStart else {
            return
        }
        completion.enter()
        Thread { [self] in
            run()
            completion.leave()
        }.start()
    }

    func stop() {
        let shouldWait = stateLock.withLock { () -> Bool in
            guard started else {
                return false
            }
            stopping = true
            return true
        }
        if shouldWait {
            completion.wait()
        }
        ring.clear()
    }

    private func run() {
        var storage = [UInt8](repeating: 0, count: ring.maximumPayloadBytes)
        var lastInputSequence: UInt64?
        var announcedReady = false

        while !stateLock.withLock({ stopping }) {
            let result = storage.withUnsafeMutableBytes { ring.pop(into: $0) }
            guard let result else {
                usleep(1_000)
                continue
            }
            do {
                let expectedSequence = lastInputSequence.map { $0 &+ 1 }
                let discontinuity = expectedSequence.map {
                    result.record.sequence != $0
                } ?? (result.record.sequence != 0)
                lastInputSequence = result.record.sequence
                if discontinuity {
                    normalizer.reset()
                }
                let samples = try storage.withUnsafeBytes { storageBytes in
                    try normalizer.convert(
                        rawInterleavedFloat32: UnsafeRawBufferPointer(
                            rebasing: storageBytes.prefix(result.payloadByteCount)
                        ),
                        frameCount: result.record.frameCount
                    )
                }
                if !announcedReady {
                    announcedReady = true
                    onReady()
                }
                let frames = accumulator.append(
                    samples: samples,
                    hostTimeNanoseconds: result.record.hostTimeNanoseconds,
                    deviceGeneration: deviceGeneration,
                    discontinuity: discontinuity
                )
                for frame in frames {
                    onFrame(makePCMFrame(frame))
                }
            } catch {
                let failure = TapCaptureError(.conversionFailed)
                DispatchQueue.global(qos: .userInitiated).async { [onFailure] in
                    onFailure(failure)
                }
                return
            }
        }
    }

    private func makePCMFrame(_ frame: AccumulatedAudioFrame) -> PCMFrame {
        let littleEndianSamples = frame.samples.map(\.littleEndian)
        let payload = littleEndianSamples.withUnsafeBytes { Data($0) }
        return PCMFrame(
            captureID: captureID,
            sourceID: sourceID,
            deviceGeneration: frame.deviceGeneration,
            sequence: frame.sequence,
            hostTimeNanoseconds: frame.hostTimeNanoseconds,
            sampleRate: UInt32(FrameAccumulator.sampleRate),
            samplesPerChannel: UInt16(FrameAccumulator.samplesPerFrame),
            channels: 1,
            sampleWidth: 2,
            flags: frame.flags,
            payload: payload
        )
    }
}

private func maximumPayloadByteCount(
    format: CaptureAudioFormat,
    frameCount: UInt32
) throws -> Int {
    let maximumBytes = 1_048_576
    guard
        frameCount > 0,
        format.bytesPerFrame > 0,
        Int(frameCount) <= maximumBytes / Int(format.bytesPerFrame)
    else {
        throw AudioNormalizerError.invalidFormat
    }
    return Int(frameCount) * Int(format.bytesPerFrame)
}

private func mapPreparationError(
    _ error: Error,
    stage: PreparationStage
) -> TapCaptureError {
    if error is RingBufferError || error is AudioNormalizerError {
        return TapCaptureError(.conversionFailed)
    }
    guard let error = error as? HALCaptureError else {
        return TapCaptureError(.ioFailed)
    }
    switch error {
    case .unsupportedOS:
        return TapCaptureError(.unsupportedOS)
    case .permissionDenied:
        return TapCaptureError(.permissionDenied)
    case .unsupportedFormat:
        return TapCaptureError(.conversionFailed)
    case .unsupportedDeviceScope:
        if stage == .tap || stage == .aggregate {
            return TapCaptureError(.unsupportedDeviceScope)
        }
        return TapCaptureError(.ioFailed)
    case .failed:
        return TapCaptureError(.ioFailed)
    }
}

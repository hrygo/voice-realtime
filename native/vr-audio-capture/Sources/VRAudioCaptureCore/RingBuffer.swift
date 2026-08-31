import VRAudioCaptureRing

public struct RingRecordMetadata: Equatable, Sendable {
    public let sequence: UInt64
    public let hostTimeNanoseconds: UInt64
    public let sampleRate: UInt32
    public let frameCount: UInt32
    public let channels: UInt16
    public let bytesPerSample: UInt16

    public init(
        sequence: UInt64,
        hostTimeNanoseconds: UInt64,
        sampleRate: UInt32,
        frameCount: UInt32,
        channels: UInt16,
        bytesPerSample: UInt16
    ) {
        self.sequence = sequence
        self.hostTimeNanoseconds = hostTimeNanoseconds
        self.sampleRate = sampleRate
        self.frameCount = frameCount
        self.channels = channels
        self.bytesPerSample = bytesPerSample
    }
}

public struct RingPopResult: Equatable, Sendable {
    public let record: RingRecordMetadata
    public let payloadByteCount: Int
}

public enum RingPushResult: Equatable, Sendable {
    case stored
    case storedDroppingOldest
    case payloadTooLarge
    case droppedIncoming
}

public enum RingBufferError: Error, Equatable, Sendable {
    case invalidCapacity
    case allocationFailed
}

public final class SPSCRingBuffer: @unchecked Sendable {
    private static let maximumCapacity = 65_536
    private static let maximumAllocationBytes = 64 * 1_024 * 1_024
    private let handle: OpaquePointer

    public init(capacity: Int, maximumPayloadBytes: Int) throws {
        guard
            capacity > 0,
            maximumPayloadBytes > 0,
            capacity <= Self.maximumCapacity,
            maximumPayloadBytes <= Self.maximumAllocationBytes,
            capacity <= Self.maximumAllocationBytes / maximumPayloadBytes,
            capacity <= Int(UInt32.max),
            maximumPayloadBytes <= Int(UInt32.max)
        else {
            throw RingBufferError.invalidCapacity
        }
        guard let handle = vr_audio_capture_ring_create(
            UInt32(capacity),
            UInt32(maximumPayloadBytes)
        ) else {
            throw RingBufferError.allocationFailed
        }
        self.handle = handle
    }

    deinit {
        vr_audio_capture_ring_destroy(handle)
    }

    public var capacity: Int {
        Int(vr_audio_capture_ring_capacity(handle))
    }

    public var maximumPayloadBytes: Int {
        Int(vr_audio_capture_ring_maximum_payload_bytes(handle))
    }

    public var count: Int {
        Int(vr_audio_capture_ring_count(handle))
    }

    public var droppedRecords: UInt64 {
        vr_audio_capture_ring_dropped_records(handle)
    }

    public func push(
        metadata: RingRecordMetadata,
        payload: UnsafeRawBufferPointer
    ) -> RingPushResult {
        guard payload.count <= Int(UInt32.max) else {
            return .payloadTooLarge
        }
        var rawMetadata = VRAudioCaptureRingMetadata(
            sequence: metadata.sequence,
            host_time_nanoseconds: metadata.hostTimeNanoseconds,
            sample_rate: metadata.sampleRate,
            frame_count: metadata.frameCount,
            channels: metadata.channels,
            bytes_per_sample: metadata.bytesPerSample
        )
        let result = vr_audio_capture_ring_push(
            handle,
            &rawMetadata,
            payload.baseAddress,
            UInt32(payload.count)
        )
        switch Int(result) {
        case VR_AUDIO_CAPTURE_RING_PUSH_STORED:
            return .stored
        case VR_AUDIO_CAPTURE_RING_PUSH_STORED_DROPPING_OLDEST:
            return .storedDroppingOldest
        case VR_AUDIO_CAPTURE_RING_PUSH_DROPPED_INCOMING:
            return .droppedIncoming
        default:
            return .payloadTooLarge
        }
    }

    public func pop(into destination: UnsafeMutableRawBufferPointer) -> RingPopResult? {
        guard destination.count <= Int(UInt32.max) else {
            return nil
        }
        var rawRecord = VRAudioCaptureRingRecord()
        guard vr_audio_capture_ring_pop(
            handle,
            destination.baseAddress,
            UInt32(destination.count),
            &rawRecord
        ) else {
            return nil
        }
        return RingPopResult(
            record: RingRecordMetadata(
                sequence: rawRecord.metadata.sequence,
                hostTimeNanoseconds: rawRecord.metadata.host_time_nanoseconds,
                sampleRate: rawRecord.metadata.sample_rate,
                frameCount: rawRecord.metadata.frame_count,
                channels: rawRecord.metadata.channels,
                bytesPerSample: rawRecord.metadata.bytes_per_sample
            ),
            payloadByteCount: Int(rawRecord.payload_byte_count)
        )
    }

    public func clear() {
        vr_audio_capture_ring_clear(handle)
    }
}

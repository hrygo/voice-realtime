import Darwin
import Foundation
import VRAudioCaptureCore

func ringBufferTests() -> [SelfTest] {
    [
        SelfTest("full ring drops the oldest record") {
            let ring = try SPSCRingBuffer(capacity: 2, maximumPayloadBytes: 8)

            try expectEqual(push([1], sequence: 1, into: ring), .stored)
            try expectEqual(push([2], sequence: 2, into: ring), .stored)
            try expectEqual(
                push([3], sequence: 3, into: ring),
                .storedDroppingOldest
            )
            try expectEqual(ring.count, 2)
            try expectEqual(ring.droppedRecords, 1)

            let first = pop(from: ring)
            let second = pop(from: ring)
            try expectEqual(first?.metadata.sequence, 2)
            try expectEqual(first?.payload, [2])
            try expectEqual(second?.metadata.sequence, 3)
            try expectEqual(second?.payload, [3])
        },
        SelfTest("sequence gaps remain observable") {
            let ring = try SPSCRingBuffer(capacity: 3, maximumPayloadBytes: 8)
            try expectEqual(push([1], sequence: 10, into: ring), .stored)
            try expectEqual(push([3], sequence: 12, into: ring), .stored)

            try expectEqual(pop(from: ring)?.metadata.sequence, 10)
            try expectEqual(pop(from: ring)?.metadata.sequence, 12)
        },
        SelfTest("clear removes records without retaining payload") {
            let ring = try SPSCRingBuffer(capacity: 2, maximumPayloadBytes: 8)
            try expectEqual(
                push([0xA5, 0x5A], sequence: 1, into: ring),
                .stored
            )

            ring.clear()

            try expectEqual(ring.count, 0)
            try expect(pop(from: ring) == nil, "ring retained a record after clear")
            try expectEqual(ring.droppedRecords, 0)
        },
        SelfTest("payload larger than the fixed slot is rejected") {
            let ring = try SPSCRingBuffer(capacity: 2, maximumPayloadBytes: 2)

            try expectEqual(
                push([1, 2, 3], sequence: 1, into: ring),
                .payloadTooLarge
            )
            try expectEqual(ring.count, 0)
        },
        SelfTest("oversized ring allocation is rejected") {
            try expectThrows(RingBufferError.self) {
                try SPSCRingBuffer(
                    capacity: 1,
                    maximumPayloadBytes: (64 * 1_024 * 1_024) + 1
                )
            }
        },
        SelfTest("concurrent producer and consumer never tear a record") {
            let ring = try SPSCRingBuffer(capacity: 16, maximumPayloadBytes: 8)
            let producerState = ProducerState()
            let producer = Thread {
                for sequence in UInt64(0) ..< 50_000 {
                    var payload = sequence.littleEndian
                    let metadata = RingRecordMetadata(
                        sequence: sequence,
                        hostTimeNanoseconds: sequence + 100,
                        sampleRate: 48_000,
                        frameCount: 1,
                        channels: 2,
                        bytesPerSample: 4
                    )
                    let result = withUnsafeBytes(of: &payload) {
                        ring.push(metadata: metadata, payload: $0)
                    }
                    if result == .payloadTooLarge {
                        producerState.finish(error: "fixed payload was rejected")
                        return
                    }
                }
                producerState.finish()
            }
            producer.start()

            var storage = [UInt8](repeating: 0, count: 8)
            var lastSequence: UInt64?
            var recordsRead = 0
            var failure: String?
            while !producerState.isFinished || ring.count > 0 {
                let record = storage.withUnsafeMutableBytes { ring.pop(into: $0) }
                guard let record else {
                    sched_yield()
                    continue
                }
                recordsRead += 1
                let payloadSequence = decodeLittleEndianUInt64(storage)
                if record.payloadByteCount != 8 {
                    failure = "unexpected payload length \(record.payloadByteCount)"
                } else if payloadSequence != record.record.sequence {
                    failure = "payload and metadata sequence were torn"
                } else if let lastSequence,
                          record.record.sequence <= lastSequence {
                    failure = "sequence did not increase"
                }
                lastSequence = record.record.sequence
            }

            try expect(producerState.error == nil, producerState.error ?? "")
            try expect(failure == nil, failure ?? "")
            try expect(recordsRead > 0, "consumer did not receive any record")
        },
    ]
}

private func push(
    _ payload: [UInt8],
    sequence: UInt64,
    into ring: SPSCRingBuffer
) -> RingPushResult {
    let metadata = RingRecordMetadata(
        sequence: sequence,
        hostTimeNanoseconds: sequence + 100,
        sampleRate: 48_000,
        frameCount: 1,
        channels: 2,
        bytesPerSample: 4
    )
    return payload.withUnsafeBytes { ring.push(metadata: metadata, payload: $0) }
}

private func pop(
    from ring: SPSCRingBuffer
) -> (metadata: RingRecordMetadata, payload: [UInt8])? {
    var storage = [UInt8](repeating: 0, count: ring.maximumPayloadBytes)
    let metadata = storage.withUnsafeMutableBytes { buffer in
        ring.pop(into: buffer)
    }
    guard let metadata else {
        return nil
    }
    return (metadata.record, Array(storage.prefix(metadata.payloadByteCount)))
}

private func decodeLittleEndianUInt64(_ bytes: [UInt8]) -> UInt64 {
    bytes.enumerated().reduce(into: UInt64(0)) { value, element in
        value |= UInt64(element.element) << UInt64(element.offset * 8)
    }
}

private final class ProducerState: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false
    private var failure: String?

    var isFinished: Bool {
        lock.withLock { finished }
    }

    var error: String? {
        lock.withLock { failure }
    }

    func finish(error: String? = nil) {
        lock.withLock {
            failure = error
            finished = true
        }
    }
}

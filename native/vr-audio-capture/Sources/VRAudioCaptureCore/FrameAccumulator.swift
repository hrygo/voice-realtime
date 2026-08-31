import Foundation

@_spi(Testing)
public struct AccumulatedAudioFrame: Equatable, Sendable {
    public let sequence: UInt64
    public let hostTimeNanoseconds: UInt64
    public let deviceGeneration: UInt32
    public let flags: PCMFrameFlags
    public let samples: [Int16]
}

@_spi(Testing)
public final class FrameAccumulator {
    public static let sampleRate = 16_000
    public static let samplesPerFrame = 512
    public static let frameDurationNanoseconds = UInt64(32_000_000)

    private var pending: [Int16] = []
    private var pendingOffset = 0
    private var pendingStartNanoseconds: UInt64?
    private var activeGeneration: UInt32?
    private var nextSequence: UInt64 = 0
    private var markNextDiscontinuity = false
    private var lastEmittedHostTime: UInt64?

    public init() {}

    public func append(
        samples: [Int16],
        hostTimeNanoseconds: UInt64,
        deviceGeneration: UInt32,
        discontinuity: Bool = false
    ) -> [AccumulatedAudioFrame] {
        let generationChanged = activeGeneration.map { $0 != deviceGeneration } ?? false
        if generationChanged || discontinuity {
            clearPending()
            markNextDiscontinuity = true
        }
        activeGeneration = deviceGeneration

        guard !samples.isEmpty else {
            return []
        }
        if pendingCount == 0 {
            var start = hostTimeNanoseconds
            if let lastEmittedHostTime, start <= lastEmittedHostTime {
                start = lastEmittedHostTime &+ Self.frameDurationNanoseconds
                markNextDiscontinuity = true
            }
            pendingStartNanoseconds = start
        }
        pending.append(contentsOf: samples)

        var frames: [AccumulatedAudioFrame] = []
        while pendingCount >= Self.samplesPerFrame {
            let startIndex = pendingOffset
            let endIndex = startIndex + Self.samplesPerFrame
            let frameSamples = Array(pending[startIndex ..< endIndex])
            let timestamp = pendingStartNanoseconds ?? hostTimeNanoseconds
            let flags: PCMFrameFlags = markNextDiscontinuity ? [.discontinuity] : []
            frames.append(AccumulatedAudioFrame(
                sequence: nextSequence,
                hostTimeNanoseconds: timestamp,
                deviceGeneration: deviceGeneration,
                flags: flags,
                samples: frameSamples
            ))
            nextSequence &+= 1
            pendingOffset = endIndex
            lastEmittedHostTime = timestamp
            pendingStartNanoseconds = timestamp &+ Self.frameDurationNanoseconds
            markNextDiscontinuity = false
        }
        compactPendingIfNeeded()
        return frames
    }

    public func reset(markDiscontinuity: Bool = true) {
        clearPending()
        activeGeneration = nil
        markNextDiscontinuity = markDiscontinuity
    }

    private var pendingCount: Int {
        pending.count - pendingOffset
    }

    private func clearPending() {
        pending.removeAll(keepingCapacity: true)
        pendingOffset = 0
        pendingStartNanoseconds = nil
    }

    private func compactPendingIfNeeded() {
        if pendingOffset == pending.count {
            clearPending()
        } else if pendingOffset >= Self.samplesPerFrame * 4 {
            pending.removeFirst(pendingOffset)
            pendingOffset = 0
        }
    }
}

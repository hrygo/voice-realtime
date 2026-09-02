import Foundation

public struct PCMFrameFlags: OptionSet, Equatable, Sendable {
    public let rawValue: UInt32

    public init(rawValue: UInt32) {
        self.rawValue = rawValue
    }

    public static let discontinuity = PCMFrameFlags(rawValue: 1 << 0)
    public static let silenceFill = PCMFrameFlags(rawValue: 1 << 1)
    public static let endOfStream = PCMFrameFlags(rawValue: 1 << 2)
    static let knownMask: UInt32 =
        discontinuity.rawValue | silenceFill.rawValue | endOfStream.rawValue
}

public struct PCMFrame: Equatable, Sendable {
    public let captureID: UUID
    public let sourceID: UUID
    public let deviceGeneration: UInt32
    public let sequence: UInt64
    public let hostTimeNanoseconds: UInt64
    public let sampleRate: UInt32
    public let samplesPerChannel: UInt16
    public let channels: UInt8
    public let sampleWidth: UInt8
    public let flags: PCMFrameFlags
    public let payload: Data

    public init(
        captureID: UUID,
        sourceID: UUID,
        deviceGeneration: UInt32,
        sequence: UInt64,
        hostTimeNanoseconds: UInt64,
        sampleRate: UInt32,
        samplesPerChannel: UInt16,
        channels: UInt8,
        sampleWidth: UInt8,
        flags: PCMFrameFlags,
        payload: Data
    ) {
        self.captureID = captureID
        self.sourceID = sourceID
        self.deviceGeneration = deviceGeneration
        self.sequence = sequence
        self.hostTimeNanoseconds = hostTimeNanoseconds
        self.sampleRate = sampleRate
        self.samplesPerChannel = samplesPerChannel
        self.channels = channels
        self.sampleWidth = sampleWidth
        self.flags = flags
        self.payload = payload
    }
}

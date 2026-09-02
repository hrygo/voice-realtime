@preconcurrency import AVFAudio
import Foundation

@_spi(Testing)
public struct CaptureAudioFormat: Equatable, Sendable {
    public let sampleRate: Double
    public let channels: UInt32
    public let bytesPerFrame: UInt32
    public let isFloat32: Bool
    public let isInterleaved: Bool

    public init(
        sampleRate: Double,
        channels: UInt32,
        bytesPerFrame: UInt32,
        isFloat32: Bool,
        isInterleaved: Bool
    ) {
        self.sampleRate = sampleRate
        self.channels = channels
        self.bytesPerFrame = bytesPerFrame
        self.isFloat32 = isFloat32
        self.isInterleaved = isInterleaved
    }
}

public enum AudioNormalizerError: Error, Equatable, Sendable {
    case invalidFormat
    case invalidInput
    case conversionFailed
}

@_spi(Testing)
public final class AudioNormalizer {
    private static let outputSampleRate = 16_000.0
    private let sourceFormat: CaptureAudioFormat
    private let maximumInputFrames: Int
    private let converter: AVAudioConverter
    private let inputBuffer: AVAudioPCMBuffer
    private let outputBuffer: AVAudioPCMBuffer
    private let inputState: ConverterInputState

    init(
        sourceFormat: CaptureAudioFormat,
        maximumInputFrames: UInt32
    ) throws {
        guard
            sourceFormat.isFloat32,
            sourceFormat.sampleRate.isFinite,
            sourceFormat.sampleRate > 0,
            sourceFormat.channels > 0,
            sourceFormat.channels <= 32,
            sourceFormat.bytesPerFrame == sourceFormat.channels * 4,
            sourceFormat.isInterleaved || sourceFormat.channels == 1,
            maximumInputFrames > 0,
            maximumInputFrames <= 65_536
        else {
            throw AudioNormalizerError.invalidFormat
        }
        guard let inputFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sourceFormat.sampleRate,
            channels: AVAudioChannelCount(sourceFormat.channels),
            interleaved: true
        ), let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: Self.outputSampleRate,
            channels: 1,
            interleaved: false
        ), let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            throw AudioNormalizerError.invalidFormat
        }
        let outputCapacityDouble = ceil(
            Double(maximumInputFrames) * Self.outputSampleRate / sourceFormat.sampleRate
        ) + 256
        guard outputCapacityDouble <= Double(UInt32.max) else {
            throw AudioNormalizerError.invalidFormat
        }
        guard let inputBuffer = AVAudioPCMBuffer(
            pcmFormat: inputFormat,
            frameCapacity: AVAudioFrameCount(maximumInputFrames)
        ), let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: outputFormat,
            frameCapacity: AVAudioFrameCount(outputCapacityDouble)
        ) else {
            throw AudioNormalizerError.invalidFormat
        }

        self.sourceFormat = sourceFormat
        self.maximumInputFrames = Int(maximumInputFrames)
        self.converter = converter
        self.inputBuffer = inputBuffer
        self.outputBuffer = outputBuffer
        inputState = ConverterInputState(buffer: inputBuffer)
        converter.downmix = true
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        converter.primeMethod = .none
    }

    public static func testing(
        sampleRate: Double,
        channels: UInt32,
        maximumInputFrames: UInt32
    ) throws -> AudioNormalizer {
        try AudioNormalizer(
            sourceFormat: CaptureAudioFormat(
                sampleRate: sampleRate,
                channels: channels,
                bytesPerFrame: channels * 4,
                isFloat32: true,
                isInterleaved: true
            ),
            maximumInputFrames: maximumInputFrames
        )
    }

    public func convert(
        interleavedFloat32 samples: [Float],
        frameCount: Int
    ) throws -> [Int16] {
        guard
            frameCount >= 0,
            frameCount <= maximumInputFrames,
            samples.count == frameCount * Int(sourceFormat.channels)
        else {
            throw AudioNormalizerError.invalidInput
        }
        return try samples.withUnsafeBytes {
            try convert(rawInterleavedFloat32: $0, frameCount: UInt32(frameCount))
        }
    }

    func convert(
        rawInterleavedFloat32 bytes: UnsafeRawBufferPointer,
        frameCount: UInt32
    ) throws -> [Int16] {
        let sampleCount = Int(frameCount) * Int(sourceFormat.channels)
        guard
            frameCount > 0,
            Int(frameCount) <= maximumInputFrames,
            sampleCount <= Int.max / MemoryLayout<Float>.size,
            bytes.count == sampleCount * MemoryLayout<Float>.size,
            let sourceBase = bytes.baseAddress,
            let destinationBase = inputBuffer.mutableAudioBufferList.pointee.mBuffers.mData
        else {
            throw AudioNormalizerError.invalidInput
        }

        let source = sourceBase.assumingMemoryBound(to: Float.self)
        let destination = destinationBase.assumingMemoryBound(to: Float.self)
        for index in 0 ..< sampleCount {
            let value = source[index]
            destination[index] = value.isFinite ? min(1, max(-1, value)) : 0
        }
        inputBuffer.frameLength = AVAudioFrameCount(frameCount)
        outputBuffer.frameLength = 0

        inputState.supplied = false
        var conversionError: NSError?
        let status = converter.convert(
            to: outputBuffer,
            error: &conversionError
        ) { [inputState] _, inputStatus in
            guard !inputState.supplied else {
                inputStatus.pointee = .noDataNow
                return nil
            }
            inputState.supplied = true
            inputStatus.pointee = .haveData
            return inputState.buffer
        }
        guard
            status != .error,
            conversionError == nil,
            let output = outputBuffer.floatChannelData?[0]
        else {
            throw AudioNormalizerError.conversionFailed
        }

        return (0 ..< Int(outputBuffer.frameLength)).map { index in
            let value = output[index]
            guard value.isFinite else {
                return 0
            }
            if value >= 1 {
                return Int16.max
            }
            if value <= -1 {
                return Int16.min
            }
            return Int16((value * Float(Int16.max)).rounded())
        }
    }

    func reset() {
        converter.reset()
    }
}

private final class ConverterInputState: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
    var supplied = false

    init(buffer: AVAudioPCMBuffer) {
        self.buffer = buffer
    }
}

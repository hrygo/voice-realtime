import Foundation
@_spi(Testing) import SonaAudioCaptureCore

func audioNormalizerTests() -> [SelfTest] {
    [
        SelfTest("48 kHz stereo is downmixed and resampled to 16 kHz mono") {
            let normalizer = try AudioNormalizer.testing(
                sampleRate: 48_000,
                channels: 2,
                maximumInputFrames: 1_024
            )
            let frameCount = 480
            let samples = Array(repeating: [Float(0.5), Float(0.5)], count: frameCount)
                .flatMap { $0 }

            let output = try normalizer.convert(
                interleavedFloat32: samples,
                frameCount: frameCount
            )

            try expect(
                (158 ... 162).contains(output.count),
                "unexpected output count \(output.count)"
            )
            let mean = output.reduce(into: Int64(0)) { $0 += Int64($1) }
                / Int64(output.count)
            try expect(
                (14_500 ... 17_200).contains(Int(mean)),
                "downmix level changed to \(mean)"
            )
        },
        SelfTest("44.1 kHz input keeps duration at 16 kHz") {
            let normalizer = try AudioNormalizer.testing(
                sampleRate: 44_100,
                channels: 1,
                maximumInputFrames: 4_410
            )
            let samples = Array(repeating: Float(0.25), count: 4_410)

            var output: [Int16] = []
            for _ in 0 ..< 10 {
                output += try normalizer.convert(
                    interleavedFloat32: samples,
                    frameCount: samples.count
                )
            }

            try expect(
                (15_850 ... 16_010).contains(output.count),
                "duration drifted to \(output.count) samples"
            )
            try expect(output.contains(where: { $0 != 0 }), "converter produced silence")
        },
        SelfTest("normalizer clips non-finite and out-of-range samples") {
            let normalizer = try AudioNormalizer.testing(
                sampleRate: 16_000,
                channels: 1,
                maximumInputFrames: 8
            )

            let output = try normalizer.convert(
                interleavedFloat32: [2, -2, .nan, .infinity, 1, -1, 0, 0.5],
                frameCount: 8
            )

            try expectEqual(output[0], Int16.max)
            try expectEqual(output[1], Int16.min)
            try expectEqual(output[2], 0)
            try expectEqual(output[3], 0)
            try expectEqual(output[4], Int16.max)
            try expectEqual(output[5], Int16.min)
        },
    ]
}

@_spi(Testing) import SonaAudioCaptureCore

func frameAccumulatorTests() -> [SelfTest] {
    [
        SelfTest("accumulator emits exact 32 ms frames with increasing host time") {
            let accumulator = FrameAccumulator()
            let start = UInt64(1_000_000_000)

            let first = accumulator.append(
                samples: Array(repeating: 1, count: 300),
                hostTimeNanoseconds: start,
                deviceGeneration: 7
            )
            let second = accumulator.append(
                samples: Array(repeating: 2, count: 724),
                hostTimeNanoseconds: start + 18_750_000,
                deviceGeneration: 7
            )

            try expectEqual(first.count, 0)
            try expectEqual(second.count, 2)
            try expectEqual(second.map(\.samples.count), [512, 512])
            try expectEqual(second.map(\.sequence), [0, 1])
            try expectEqual(second.map(\.hostTimeNanoseconds), [
                start,
                start + 32_000_000,
            ])
            try expectEqual(second.map(\.deviceGeneration), [7, 7])
        },
        SelfTest("generation change flushes partial audio and marks discontinuity") {
            let accumulator = FrameAccumulator()
            _ = accumulator.append(
                samples: Array(repeating: 1, count: 200),
                hostTimeNanoseconds: 1_000,
                deviceGeneration: 1
            )

            let frames = accumulator.append(
                samples: Array(repeating: 2, count: 512),
                hostTimeNanoseconds: 2_000,
                deviceGeneration: 2
            )

            try expectEqual(frames.count, 1)
            try expectEqual(frames[0].deviceGeneration, 2)
            try expect(frames[0].flags.contains(.discontinuity), "flag missing")
            try expect(frames[0].samples.allSatisfy { $0 == 2 }, "old samples leaked")
        },
        SelfTest("host time regression remains monotonic and observable") {
            let accumulator = FrameAccumulator()
            let first = accumulator.append(
                samples: Array(repeating: 1, count: 512),
                hostTimeNanoseconds: 2_000_000_000,
                deviceGeneration: 1
            )[0]
            let second = accumulator.append(
                samples: Array(repeating: 2, count: 512),
                hostTimeNanoseconds: 1_000_000_000,
                deviceGeneration: 1
            )[0]

            try expect(second.hostTimeNanoseconds > first.hostTimeNanoseconds, "time regressed")
            try expect(second.flags.contains(.discontinuity), "regression was hidden")
        },
    ]
}

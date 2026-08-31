import CoreAudio
@_spi(Testing) import VRAudioCaptureCore

func coreAudioHALTests() -> [SelfTest] {
    [
        SelfTest("Tap description stays private, unmuted, and device scoped") {
            let privateUID = "private-scoped-device-uid"
            let description = try makeDeviceScopedTapDescription(
                deviceUID: privateUID,
                excludedProcessObjectIDs: [7, 8]
            )

            try expectEqual(description.deviceUID, privateUID)
            try expectEqual(description.stream, 0)
            try expectEqual(description.processes, [7, 8])
            try expect(description.isPrivate, "Tap is public")
            try expect(description.isExclusive, "Tap does not exclude processes")
            try expect(description.isMixdown, "Tap is not mixed down")
            try expect(description.isMono, "Tap is not mono")
            try expectEqual(description.muteBehavior.rawValue, 0)
        },
        SelfTest("aggregate description is private and contains only the Tap") {
            let tapUID = "synthetic-private-tap-uid"
            let description = makePrivateAggregateDescription(tapUID: tapUID)
            let tapList = description[kAudioAggregateDeviceTapListKey]
                as? [[String: String]]

            try expectEqual(
                description[kAudioAggregateDeviceIsPrivateKey] as? Bool,
                true
            )
            try expectEqual(
                description[kAudioAggregateDeviceTapAutoStartKey] as? Bool,
                false
            )
            try expectEqual(tapList, [[kAudioSubTapUIDKey: tapUID]])
            try expect(
                description[kAudioAggregateDeviceSubDeviceListKey] == nil,
                "unexpected hardware subdevice widened aggregate scope"
            )
        },
        SelfTest("Tap format boundary only accepts native packed Float32 PCM") {
            let valid = floatPCMDescription()

            let format = try captureAudioFormat(valid)

            try expectEqual(format.sampleRate, 48_000)
            try expectEqual(format.channels, 2)
            try expectEqual(format.bytesPerFrame, 8)

            var bigEndian = valid
            bigEndian.mFormatFlags |= kAudioFormatFlagIsBigEndian
            try expectThrows(HALCaptureError.self) {
                try captureAudioFormat(bigEndian)
            }

            var unpacked = valid
            unpacked.mFormatFlags &= ~kAudioFormatFlagIsPacked
            try expectThrows(HALCaptureError.self) {
                try captureAudioFormat(unpacked)
            }

            var planarStereo = valid
            planarStereo.mFormatFlags |= kAudioFormatFlagIsNonInterleaved
            planarStereo.mBytesPerFrame = 4
            planarStereo.mBytesPerPacket = 4
            try expectThrows(HALCaptureError.self) {
                try captureAudioFormat(planarStereo)
            }
        },
        SelfTest("host clock converts valid Core Audio host timestamps") {
            let hostTime = AudioGetCurrentHostTime()
            var timestamp = AudioTimeStamp()
            timestamp.mHostTime = hostTime
            timestamp.mFlags = [.hostTimeValid]

            try expectEqual(
                HostClock.nanoseconds(for: timestamp),
                AudioConvertHostTimeToNanos(hostTime)
            )
        },
    ]
}

private func floatPCMDescription() -> AudioStreamBasicDescription {
    AudioStreamBasicDescription(
        mSampleRate: 48_000,
        mFormatID: kAudioFormatLinearPCM,
        mFormatFlags: kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked,
        mBytesPerPacket: 8,
        mFramesPerPacket: 1,
        mBytesPerFrame: 8,
        mChannelsPerFrame: 2,
        mBitsPerChannel: 32,
        mReserved: 0
    )
}

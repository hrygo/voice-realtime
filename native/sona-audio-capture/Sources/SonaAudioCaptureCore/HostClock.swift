import AudioToolbox
import CoreAudio

@_spi(Testing)
public enum HostClock {
    public static func nanoseconds(for timestamp: AudioTimeStamp) -> UInt64 {
        let hostTime: UInt64
        if timestamp.mFlags.contains(.hostTimeValid),
           timestamp.mHostTime != 0 {
            hostTime = timestamp.mHostTime
        } else {
            hostTime = AudioGetCurrentHostTime()
        }
        return AudioConvertHostTimeToNanos(hostTime)
    }
}

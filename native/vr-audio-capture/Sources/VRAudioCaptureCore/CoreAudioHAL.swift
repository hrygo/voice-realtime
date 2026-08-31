import AudioToolbox
import CoreAudio
import Foundation

@_spi(Testing)
public struct HALTapResource: Sendable {
    public let objectID: UInt32
    public let uid: String
    public let format: CaptureAudioFormat

    public init(objectID: UInt32, uid: String, format: CaptureAudioFormat) {
        self.objectID = objectID
        self.uid = uid
        self.format = format
    }
}

@_spi(Testing)
public struct HALAggregateResource: Equatable, Sendable {
    public let objectID: UInt32
    public let maximumInputFrames: UInt32

    public init(objectID: UInt32, maximumInputFrames: UInt32) {
        self.objectID = objectID
        self.maximumInputFrames = maximumInputFrames
    }
}

@_spi(Testing)
public struct HALIOProcResource: Equatable, Sendable {
    public let identifier: UInt64

    public init(identifier: UInt64) {
        self.identifier = identifier
    }
}

@_spi(Testing)
public enum HALCaptureError: Error, Equatable, Sendable {
    case unsupportedOS
    case permissionDenied
    case unsupportedDeviceScope
    case unsupportedFormat
    case failed
}

@_spi(Testing)
public protocol TapInputSink: AnyObject, Sendable {
    func consume(
        hostTimeNanoseconds: UInt64,
        frameCount: UInt32,
        bytes: UnsafeRawBufferPointer
    )
}

@_spi(Testing)
public protocol TapCaptureHAL: AnyObject, Sendable {
    func processObjectID(forPID pid: Int32) throws -> UInt32?
    func createTap(
        deviceUID: String,
        excludedProcessObjectIDs: [UInt32]
    ) throws -> HALTapResource
    func createAggregate(tap: HALTapResource) throws -> HALAggregateResource
    func createIOProc(
        aggregate: HALAggregateResource,
        format: CaptureAudioFormat,
        sink: any TapInputSink
    ) throws -> HALIOProcResource
    func startIO(
        aggregate: HALAggregateResource,
        ioProc: HALIOProcResource
    ) throws
    func stopIO(
        aggregate: HALAggregateResource,
        ioProc: HALIOProcResource
    ) throws
    func destroyIOProc(
        aggregate: HALAggregateResource,
        ioProc: HALIOProcResource
    ) throws
    func destroyAggregate(_ aggregate: HALAggregateResource) throws
    func destroyTap(_ tap: HALTapResource) throws
}

final class CoreAudioHAL: TapCaptureHAL, @unchecked Sendable {
    private static let maximumFramesPerCallback: UInt32 = 65_536
    private let ioProcLock = NSLock()
    private var nextIOProcIdentifier: UInt64 = 1
    private var ioProcs: [UInt64: AudioDeviceIOProcID] = [:]

    func processObjectID(forPID pid: Int32) throws -> UInt32? {
        guard pid > 0 else {
            return nil
        }
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var qualifier = pid_t(pid)
        var objectID = AudioObjectID(kAudioObjectUnknown)
        var byteCount = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = withUnsafePointer(to: &qualifier) { qualifierPointer in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                UInt32(MemoryLayout<pid_t>.size),
                qualifierPointer,
                &byteCount,
                &objectID
            )
        }
        guard status == noErr, byteCount == MemoryLayout<AudioObjectID>.size else {
            throw mapStatus(status, unsupportedScope: false)
        }
        return objectID == kAudioObjectUnknown ? nil : UInt32(objectID)
    }

    func createTap(
        deviceUID: String,
        excludedProcessObjectIDs: [UInt32]
    ) throws -> HALTapResource {
        guard #available(macOS 14.2, *) else {
            throw HALCaptureError.unsupportedOS
        }
        let description = try makeDeviceScopedTapDescription(
            deviceUID: deviceUID,
            excludedProcessObjectIDs: excludedProcessObjectIDs
        )

        var tapID = AudioObjectID(kAudioObjectUnknown)
        let status = AudioHardwareCreateProcessTap(description, &tapID)
        guard status == noErr, tapID != kAudioObjectUnknown else {
            throw mapStatus(status, unsupportedScope: true)
        }
        do {
            let uid = try stringProperty(
                objectID: tapID,
                selector: kAudioTapPropertyUID
            )
            let streamDescription = try tapFormat(tapID: tapID)
            return HALTapResource(
                objectID: UInt32(tapID),
                uid: uid,
                format: try captureAudioFormat(streamDescription)
            )
        } catch {
            _ = AudioHardwareDestroyProcessTap(tapID)
            throw error
        }
    }

    func createAggregate(tap: HALTapResource) throws -> HALAggregateResource {
        let aggregateDescription = makePrivateAggregateDescription(tapUID: tap.uid)
        var aggregateID = AudioObjectID(kAudioObjectUnknown)
        let status = AudioHardwareCreateAggregateDevice(
            aggregateDescription as CFDictionary,
            &aggregateID
        )
        guard status == noErr, aggregateID != kAudioObjectUnknown else {
            throw mapStatus(status, unsupportedScope: true)
        }
        do {
            let frameCount = try maximumInputFrames(deviceID: aggregateID)
            return HALAggregateResource(
                objectID: UInt32(aggregateID),
                maximumInputFrames: frameCount
            )
        } catch {
            _ = AudioHardwareDestroyAggregateDevice(aggregateID)
            throw error
        }
    }

    func createIOProc(
        aggregate: HALAggregateResource,
        format: CaptureAudioFormat,
        sink: any TapInputSink
    ) throws -> HALIOProcResource {
        var ioProcID: AudioDeviceIOProcID?
        let status = AudioDeviceCreateIOProcIDWithBlock(
            &ioProcID,
            AudioObjectID(aggregate.objectID),
            nil
        ) { _, inputData, inputTime, _, _ in
            guard
                inputData.pointee.mNumberBuffers == 1,
                format.bytesPerFrame > 0
            else {
                return
            }
            let buffer = inputData.pointee.mBuffers
            let byteCount = Int(buffer.mDataByteSize)
            guard
                buffer.mNumberChannels == format.channels,
                byteCount > 0,
                byteCount % Int(format.bytesPerFrame) == 0,
                let data = buffer.mData
            else {
                return
            }
            let frameCount = byteCount / Int(format.bytesPerFrame)
            guard frameCount <= Int(UInt32.max) else {
                return
            }
            sink.consume(
                hostTimeNanoseconds: HostClock.nanoseconds(for: inputTime.pointee),
                frameCount: UInt32(frameCount),
                bytes: UnsafeRawBufferPointer(start: data, count: byteCount)
            )
        }
        guard status == noErr, let ioProcID else {
            throw mapStatus(status, unsupportedScope: false)
        }
        let identifier = ioProcLock.withLock { () -> UInt64 in
            let identifier = nextIOProcIdentifier
            nextIOProcIdentifier &+= 1
            ioProcs[identifier] = ioProcID
            return identifier
        }
        return HALIOProcResource(identifier: identifier)
    }

    func startIO(
        aggregate: HALAggregateResource,
        ioProc: HALIOProcResource
    ) throws {
        let rawIOProc = try resolvedIOProc(ioProc)
        let status = AudioDeviceStart(AudioObjectID(aggregate.objectID), rawIOProc)
        guard status == noErr else {
            throw mapStatus(status, unsupportedScope: false)
        }
    }

    func stopIO(
        aggregate: HALAggregateResource,
        ioProc: HALIOProcResource
    ) throws {
        let rawIOProc = try resolvedIOProc(ioProc)
        let status = AudioDeviceStop(AudioObjectID(aggregate.objectID), rawIOProc)
        guard status == noErr || status == kAudioHardwareNotRunningError else {
            throw mapStatus(status, unsupportedScope: false)
        }
    }

    func destroyIOProc(
        aggregate: HALAggregateResource,
        ioProc: HALIOProcResource
    ) throws {
        let rawIOProc = try resolvedIOProc(ioProc)
        let status = AudioDeviceDestroyIOProcID(
            AudioObjectID(aggregate.objectID),
            rawIOProc
        )
        guard status == noErr else {
            throw mapStatus(status, unsupportedScope: false)
        }
        _ = ioProcLock.withLock {
            ioProcs.removeValue(forKey: ioProc.identifier)
        }
    }

    func destroyAggregate(_ aggregate: HALAggregateResource) throws {
        let status = AudioHardwareDestroyAggregateDevice(
            AudioObjectID(aggregate.objectID)
        )
        guard status == noErr else {
            throw mapStatus(status, unsupportedScope: false)
        }
    }

    func destroyTap(_ tap: HALTapResource) throws {
        guard #available(macOS 14.2, *) else {
            throw HALCaptureError.unsupportedOS
        }
        let status = AudioHardwareDestroyProcessTap(AudioObjectID(tap.objectID))
        guard status == noErr else {
            throw mapStatus(status, unsupportedScope: false)
        }
    }

    private func resolvedIOProc(
        _ resource: HALIOProcResource
    ) throws -> AudioDeviceIOProcID {
        guard let ioProc = ioProcLock.withLock({ ioProcs[resource.identifier] }) else {
            throw HALCaptureError.failed
        }
        return ioProc
    }

    private func maximumInputFrames(deviceID: AudioObjectID) throws -> UInt32 {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyBufferFrameSize,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        var frameCount: UInt32 = 0
        var byteCount = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(
            deviceID,
            &address,
            0,
            nil,
            &byteCount,
            &frameCount
        )
        guard
            status == noErr,
            byteCount == MemoryLayout<UInt32>.size,
            frameCount > 0,
            frameCount <= Self.maximumFramesPerCallback
        else {
            throw mapStatus(status, unsupportedScope: false)
        }
        return frameCount
    }

    private func tapFormat(tapID: AudioObjectID) throws -> AudioStreamBasicDescription {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var format = AudioStreamBasicDescription()
        var byteCount = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let status = AudioObjectGetPropertyData(
            tapID,
            &address,
            0,
            nil,
            &byteCount,
            &format
        )
        guard
            status == noErr,
            byteCount == MemoryLayout<AudioStreamBasicDescription>.size
        else {
            throw HALCaptureError.unsupportedFormat
        }
        return format
    }

    private func stringProperty(
        objectID: AudioObjectID,
        selector: AudioObjectPropertySelector
    ) throws -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: Unmanaged<CFString>?
        var byteCount = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(
            objectID,
            &address,
            0,
            nil,
            &byteCount,
            &value
        )
        guard
            status == noErr,
            byteCount == MemoryLayout<Unmanaged<CFString>?>.size,
            let value
        else {
            throw HALCaptureError.failed
        }
        return value.takeRetainedValue() as String
    }
}

@_spi(Testing)
public func makeDeviceScopedTapDescription(
    deviceUID: String,
    excludedProcessObjectIDs: [UInt32]
) throws -> CATapDescription {
    guard !deviceUID.isEmpty else {
        throw HALCaptureError.unsupportedDeviceScope
    }
    let description = CATapDescription(
        excludingProcesses: excludedProcessObjectIDs.map { AudioObjectID($0) },
        deviceUID: deviceUID,
        stream: 0
    )
    description.name = "VoiceRealtime Private Output Capture"
    description.isPrivate = true
    description.muteBehavior = CATapMuteBehavior(rawValue: 0)
        ?? description.muteBehavior
    description.isExclusive = true
    description.isMixdown = true
    description.isMono = true
    return description
}

@_spi(Testing)
public func makePrivateAggregateDescription(tapUID: String) -> [String: Any] {
    [
        kAudioAggregateDeviceNameKey: "VoiceRealtime Private Capture",
        kAudioAggregateDeviceUIDKey: "voice-realtime.capture.\(UUID().uuidString)",
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceTapAutoStartKey: false,
        kAudioAggregateDeviceTapListKey: [
            [kAudioSubTapUIDKey: tapUID],
        ],
    ]
}

@_spi(Testing)
public func captureAudioFormat(
    _ format: AudioStreamBasicDescription
) throws -> CaptureAudioFormat {
    let flags = format.mFormatFlags
    let isFloat32 = format.mFormatID == kAudioFormatLinearPCM &&
        flags & kAudioFormatFlagIsFloat != 0 &&
        flags & kAudioFormatFlagIsPacked != 0 &&
        flags & kAudioFormatFlagIsBigEndian == 0 &&
        format.mBitsPerChannel == 32
    let isInterleaved = flags & kAudioFormatFlagIsNonInterleaved == 0
    guard
        isFloat32,
        format.mSampleRate.isFinite,
        format.mSampleRate > 0,
        format.mChannelsPerFrame > 0,
        format.mChannelsPerFrame <= 32,
        format.mFramesPerPacket == 1,
        format.mBytesPerPacket == format.mBytesPerFrame,
        format.mBytesPerFrame > 0,
        isInterleaved || format.mChannelsPerFrame == 1
    else {
        throw HALCaptureError.unsupportedFormat
    }
    let expectedBytesPerFrame = isInterleaved
        ? format.mChannelsPerFrame * 4
        : 4
    guard format.mBytesPerFrame == expectedBytesPerFrame else {
        throw HALCaptureError.unsupportedFormat
    }
    return CaptureAudioFormat(
        sampleRate: format.mSampleRate,
        channels: format.mChannelsPerFrame,
        bytesPerFrame: format.mBytesPerFrame,
        isFloat32: true,
        isInterleaved: isInterleaved
    )
}

private func mapStatus(
    _ status: OSStatus,
    unsupportedScope: Bool
) -> HALCaptureError {
    if status == kAudioDevicePermissionsError {
        return .permissionDenied
    }
    if status == kAudioDeviceUnsupportedFormatError {
        return .unsupportedFormat
    }
    if unsupportedScope,
       status == kAudioHardwareUnsupportedOperationError ||
       status == kAudioHardwareBadDeviceError ||
       status == kAudioHardwareBadStreamError ||
       status == kAudioHardwareIllegalOperationError {
        return .unsupportedDeviceScope
    }
    return .failed
}

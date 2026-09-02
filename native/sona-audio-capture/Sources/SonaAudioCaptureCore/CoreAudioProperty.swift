import AudioToolbox
import CoreAudio
import Foundation

struct CoreAudioPropertyError: Error, Sendable {
    let operation: String
}

final class CoreAudioHALPropertyReader: OutputDevicePropertyReading, @unchecked Sendable {
    func deviceIDs() throws -> [UInt32] {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let byteCount = try propertyByteCount(
            objectID: AudioObjectID(kAudioObjectSystemObject),
            address: &address,
            operation: "list_devices_size"
        )
        guard byteCount % UInt32(MemoryLayout<AudioObjectID>.size) == 0 else {
            throw CoreAudioPropertyError(operation: "list_devices_shape")
        }
        if byteCount == 0 {
            return []
        }
        var devices = [AudioObjectID](
            repeating: kAudioObjectUnknown,
            count: Int(byteCount) / MemoryLayout<AudioObjectID>.size
        )
        var mutableByteCount = byteCount
        let status = devices.withUnsafeMutableBytes { buffer in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                0,
                nil,
                &mutableByteCount,
                buffer.baseAddress!
            )
        }
        guard status == noErr, mutableByteCount == byteCount else {
            throw CoreAudioPropertyError(operation: "list_devices")
        }
        return devices
    }

    func defaultOutputDeviceID() throws -> UInt32? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let value: AudioObjectID = try scalar(
            objectID: AudioObjectID(kAudioObjectSystemObject),
            address: &address,
            operation: "default_output"
        )
        return value == kAudioObjectUnknown ? nil : UInt32(value)
    }

    func isAlive(deviceID: UInt32) throws -> Bool {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsAlive,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let value: UInt32 = try scalar(
            objectID: AudioObjectID(deviceID),
            address: &address,
            operation: "device_alive"
        )
        return value != 0
    }

    func outputChannelCount(deviceID: UInt32) throws -> UInt32 {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeOutput,
            mElement: kAudioObjectPropertyElementMain
        )
        let byteCount = try propertyByteCount(
            objectID: AudioObjectID(deviceID),
            address: &address,
            operation: "output_channels_size"
        )
        guard byteCount >= MemoryLayout<AudioBufferList>.size else {
            return 0
        }
        let storage = UnsafeMutableRawPointer.allocate(
            byteCount: Int(byteCount),
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { storage.deallocate() }
        storage.initializeMemory(as: UInt8.self, repeating: 0, count: Int(byteCount))
        var mutableByteCount = byteCount
        let status = AudioObjectGetPropertyData(
            AudioObjectID(deviceID),
            &address,
            0,
            nil,
            &mutableByteCount,
            storage
        )
        guard
            status == noErr,
            mutableByteCount >= MemoryLayout<AudioBufferList>.size,
            mutableByteCount <= byteCount
        else {
            throw CoreAudioPropertyError(operation: "output_channels")
        }
        let audioBufferList = storage.assumingMemoryBound(to: AudioBufferList.self)
        let headerBytes = MemoryLayout<AudioBufferList>.size - MemoryLayout<AudioBuffer>.size
        let availableBufferBytes = Int(mutableByteCount) - headerBytes
        let maximumBufferCount = availableBufferBytes / MemoryLayout<AudioBuffer>.stride
        guard audioBufferList.pointee.mNumberBuffers <= maximumBufferCount else {
            throw CoreAudioPropertyError(operation: "output_channels_shape")
        }
        let channelCount = UnsafeMutableAudioBufferListPointer(audioBufferList).reduce(
            into: UInt64(0)
        ) {
            $0 += UInt64($1.mNumberChannels)
        }
        guard channelCount <= UInt32.max else {
            throw CoreAudioPropertyError(operation: "output_channels_shape")
        }
        return UInt32(channelCount)
    }

    func name(deviceID: UInt32) throws -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        return try stringProperty(
            objectID: AudioObjectID(deviceID),
            address: &address,
            operation: "device_name"
        )
    }

    func uid(deviceID: UInt32) throws -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        return try stringProperty(
            objectID: AudioObjectID(deviceID),
            address: &address,
            operation: "device_uid"
        )
    }

    func transportType(deviceID: UInt32) throws -> UInt32 {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyTransportType,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        return try scalar(
            objectID: AudioObjectID(deviceID),
            address: &address,
            operation: "device_transport"
        )
    }

    private func scalar<Value: FixedWidthInteger>(
        objectID: AudioObjectID,
        address: inout AudioObjectPropertyAddress,
        operation: String
    ) throws -> Value {
        var value = Value.zero
        var byteCount = UInt32(MemoryLayout<Value>.size)
        let status = withUnsafeMutablePointer(to: &value) { pointer in
            AudioObjectGetPropertyData(
                objectID,
                &address,
                0,
                nil,
                &byteCount,
                pointer
            )
        }
        guard status == noErr, byteCount == MemoryLayout<Value>.size else {
            throw CoreAudioPropertyError(operation: operation)
        }
        return value
    }

    private func stringProperty(
        objectID: AudioObjectID,
        address: inout AudioObjectPropertyAddress,
        operation: String
    ) throws -> String {
        var value: Unmanaged<CFString>?
        var byteCount = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = withUnsafeMutablePointer(to: &value) { pointer in
            AudioObjectGetPropertyData(
                objectID,
                &address,
                0,
                nil,
                &byteCount,
                pointer
            )
        }
        guard
            status == noErr,
            byteCount == MemoryLayout<Unmanaged<CFString>?>.size,
            let value
        else {
            throw CoreAudioPropertyError(operation: operation)
        }
        return value.takeRetainedValue() as String
    }

    private func propertyByteCount(
        objectID: AudioObjectID,
        address: inout AudioObjectPropertyAddress,
        operation: String
    ) throws -> UInt32 {
        var byteCount: UInt32 = 0
        let status = AudioObjectGetPropertyDataSize(
            objectID,
            &address,
            0,
            nil,
            &byteCount
        )
        guard status == noErr else {
            throw CoreAudioPropertyError(operation: operation)
        }
        return byteCount
    }
}

import CoreAudio
import Foundation

@_spi(Testing)
public protocol OutputDevicePropertyReading: Sendable {
    func deviceIDs() throws -> [UInt32]
    func defaultOutputDeviceID() throws -> UInt32?
    func isAlive(deviceID: UInt32) throws -> Bool
    func outputChannelCount(deviceID: UInt32) throws -> UInt32
    func name(deviceID: UInt32) throws -> String
    func uid(deviceID: UInt32) throws -> String
    func transportType(deviceID: UInt32) throws -> UInt32
}

public enum OutputDeviceTransport: String, Codable, Equatable, Sendable {
    case builtIn = "built_in"
    case bluetooth
    case usb
    case hdmi
    case display
    case airPlay = "airplay"
    case virtual
    case other

    @_spi(Testing)
    public init(coreAudioValue: UInt32) {
        switch coreAudioValue {
        case kAudioDeviceTransportTypeBuiltIn:
            self = .builtIn
        case kAudioDeviceTransportTypeBluetooth,
             kAudioDeviceTransportTypeBluetoothLE:
            self = .bluetooth
        case kAudioDeviceTransportTypeUSB:
            self = .usb
        case kAudioDeviceTransportTypeHDMI:
            self = .hdmi
        case kAudioDeviceTransportTypeDisplayPort:
            self = .display
        case kAudioDeviceTransportTypeAirPlay:
            self = .airPlay
        case kAudioDeviceTransportTypeAggregate,
             kAudioDeviceTransportTypeAutoAggregate,
             kAudioDeviceTransportTypeVirtual:
            self = .virtual
        default:
            self = .other
        }
    }
}

public struct OutputDeviceDescriptor: Codable, Equatable, Sendable {
    public let deviceReference: String
    public let label: String
    public let transport: OutputDeviceTransport
    public let isDefault: Bool

    enum CodingKeys: String, CodingKey {
        case deviceReference = "device_ref"
        case label
        case transport
        case isDefault = "is_default"
    }
}

public struct DeviceCatalogError: Error, Equatable, CustomStringConvertible, Sendable {
    public enum Code: String, Equatable, Sendable {
        case enumerationFailed = "device_enumeration_failed"
        case defaultDeviceUnavailable = "default_device_unavailable"
        case unknownDevice = "unknown_device"
    }

    public let code: Code
    public let description: String

    init(_ code: Code, _ description: String) {
        self.code = code
        self.description = description
    }
}

struct ResolvedOutputDevice: Sendable {
    let objectID: UInt32
    let uid: String
    let descriptor: OutputDeviceDescriptor
}

public struct DeviceCatalog: Sendable {
    private let propertyReader: any OutputDevicePropertyReading
    private let referenceDeriver: any DeviceReferenceProviding

    public static func live() throws -> DeviceCatalog {
        DeviceCatalog(
            propertyReader: CoreAudioHALPropertyReader(),
            referenceDeriver: try DeviceReferenceStore.live()
        )
    }

    @_spi(Testing)
    public init(
        propertyReader: any OutputDevicePropertyReading,
        referenceDeriver: any DeviceReferenceProviding
    ) {
        self.propertyReader = propertyReader
        self.referenceDeriver = referenceDeriver
    }

    public func devices() throws -> [OutputDeviceDescriptor] {
        try resolvedDevices().map(\.descriptor)
    }

    public func defaultDevice() throws -> OutputDeviceDescriptor {
        guard let device = try resolvedDevices().first(where: { $0.descriptor.isDefault }) else {
            throw DeviceCatalogError(
                .defaultDeviceUnavailable,
                "默认输出设备不可用"
            )
        }
        return device.descriptor
    }

    public func device(reference rawReference: String) throws -> OutputDeviceDescriptor {
        guard let reference = DeviceReference(rawValue: rawReference) else {
            throw unknownDeviceError()
        }
        guard let device = try resolvedDevices().first(where: {
            $0.descriptor.deviceReference == reference.rawValue
        }) else {
            throw unknownDeviceError()
        }
        return device.descriptor
    }

    func resolvedDefaultDevice() throws -> ResolvedOutputDevice {
        guard let device = try resolvedDevices().first(where: { $0.descriptor.isDefault }) else {
            throw DeviceCatalogError(
                .defaultDeviceUnavailable,
                "默认输出设备不可用"
            )
        }
        return device
    }

    func resolvedDevice(reference: DeviceReference) throws -> ResolvedOutputDevice {
        guard let device = try resolvedDevices().first(where: {
            $0.descriptor.deviceReference == reference.rawValue
        }) else {
            throw unknownDeviceError()
        }
        return device
    }

    private func resolvedDevices() throws -> [ResolvedOutputDevice] {
        let deviceIDs: [UInt32]
        let defaultDeviceID: UInt32?
        do {
            deviceIDs = try propertyReader.deviceIDs()
            defaultDeviceID = try propertyReader.defaultOutputDeviceID()
        } catch {
            throw DeviceCatalogError(
                .enumerationFailed,
                "无法枚举输出设备"
            )
        }

        var devices: [ResolvedOutputDevice] = []
        devices.reserveCapacity(deviceIDs.count)
        for deviceID in deviceIDs {
            guard
                (try? propertyReader.isAlive(deviceID: deviceID)) == true,
                (try? propertyReader.outputChannelCount(deviceID: deviceID)) ?? 0 > 0,
                let rawUID = try? propertyReader.uid(deviceID: deviceID)
            else {
                continue
            }
            guard !rawUID.isEmpty else {
                continue
            }
            let uid = rawUID
            let reference = referenceDeriver.reference(forDeviceUID: uid)
            let rawName = (try? propertyReader.name(deviceID: deviceID)) ?? ""
            let rawTransport = (
                try? propertyReader.transportType(deviceID: deviceID)
            ) ?? kAudioDeviceTransportTypeUnknown
            devices.append(ResolvedOutputDevice(
                objectID: deviceID,
                uid: uid,
                descriptor: OutputDeviceDescriptor(
                    deviceReference: reference.rawValue,
                    label: sanitizedDeviceLabel(rawName),
                    transport: OutputDeviceTransport(coreAudioValue: rawTransport),
                    isDefault: deviceID == defaultDeviceID
                )
            ))
        }
        return devices.sorted {
            if $0.descriptor.label == $1.descriptor.label {
                return $0.descriptor.deviceReference < $1.descriptor.deviceReference
            }
            return $0.descriptor.label < $1.descriptor.label
        }
    }
}

private func sanitizedDeviceLabel(_ rawValue: String) -> String {
    let normalized = rawValue.unicodeScalars.map { scalar -> Character in
        if CharacterSet.whitespacesAndNewlines.contains(scalar) ||
            CharacterSet.controlCharacters.contains(scalar) {
            return " "
        }
        return Character(String(scalar))
    }
    let collapsed = String(normalized)
        .split(whereSeparator: \.isWhitespace)
        .joined(separator: " ")
    if collapsed.isEmpty {
        return "未命名输出设备"
    }
    return String(collapsed.prefix(128))
}

private func unknownDeviceError() -> DeviceCatalogError {
    DeviceCatalogError(.unknownDevice, "输出设备不存在或已断开")
}

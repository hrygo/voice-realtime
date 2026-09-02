import CoreAudio
import Foundation
@_spi(Testing) import SonaAudioCaptureCore

func deviceCatalogTests() -> [SelfTest] {
    [
        SelfTest("catalog only exposes alive output devices") {
            let reader = FakeOutputDeviceReader(
                devices: [
                    1: FakeOutputDevice(
                        alive: true,
                        outputChannels: 2,
                        name: "  Mac\nSpeakers\u{0007} ",
                        uid: "private-built-in-uid",
                        transport: kAudioDeviceTransportTypeBuiltIn
                    ),
                    2: FakeOutputDevice(
                        alive: false,
                        outputChannels: 2,
                        name: "Dead Device",
                        uid: "private-dead-uid",
                        transport: kAudioDeviceTransportTypeUSB
                    ),
                    3: FakeOutputDevice(
                        alive: true,
                        outputChannels: 0,
                        name: "Input Only",
                        uid: "private-input-uid",
                        transport: kAudioDeviceTransportTypeUSB
                    ),
                    4: FakeOutputDevice(
                        alive: true,
                        outputChannels: 2,
                        name: "Bluetooth Headset",
                        uid: "private-bluetooth-uid",
                        transport: kAudioDeviceTransportTypeBluetooth
                    ),
                ],
                defaultOutputDeviceID: 1
            )
            let catalog = try makeCatalog(reader: reader)

            let devices = try catalog.devices()

            try expectEqual(devices.count, 2)
            try expectEqual(devices.map(\.label), [
                "Bluetooth Headset",
                "Mac Speakers",
            ])
            try expectEqual(devices.map(\.transport), [.bluetooth, .builtIn])
            try expectEqual(devices.filter(\.isDefault).map(\.label), ["Mac Speakers"])
        },
        SelfTest("empty device names never fall back to private identifiers") {
            let uid = "private-fallback-uid"
            let reader = FakeOutputDeviceReader(
                devices: [
                    9: FakeOutputDevice(
                        alive: true,
                        outputChannels: 2,
                        name: "\u{0000}\n\t",
                        uid: uid,
                        transport: kAudioDeviceTransportTypeUnknown
                    ),
                ],
                defaultOutputDeviceID: 9
            )

            let device = try makeCatalog(reader: reader).devices()[0]

            try expectEqual(device.label, "未命名输出设备")
            try expect(!device.label.contains(uid), "UID leaked through label fallback")
        },
        SelfTest("Core Audio device UIDs remain opaque") {
            let opaqueUID = " private-opaque-uid "
            let reader = FakeOutputDeviceReader(
                devices: [
                    10: FakeOutputDevice(
                        alive: true,
                        outputChannels: 2,
                        name: "Output",
                        uid: opaqueUID,
                        transport: kAudioDeviceTransportTypeBuiltIn
                    ),
                ],
                defaultOutputDeviceID: 10
            )
            let expected = try DeviceReferenceDeriver(
                keyData: Data(repeating: 0x5A, count: 32)
            ).reference(forDeviceUID: opaqueUID)

            let actual = try makeCatalog(reader: reader).devices()[0]

            try expectEqual(actual.deviceReference, expected.rawValue)
        },
        SelfTest("Core Audio transports map to stable public categories") {
            let cases: [(UInt32, OutputDeviceTransport)] = [
                (kAudioDeviceTransportTypeBuiltIn, .builtIn),
                (kAudioDeviceTransportTypeBluetoothLE, .bluetooth),
                (kAudioDeviceTransportTypeUSB, .usb),
                (kAudioDeviceTransportTypeHDMI, .hdmi),
                (kAudioDeviceTransportTypeDisplayPort, .display),
                (kAudioDeviceTransportTypeAirPlay, .airPlay),
                (kAudioDeviceTransportTypeAggregate, .virtual),
                (kAudioDeviceTransportTypeThunderbolt, .other),
                (kAudioDeviceTransportTypeAVB, .other),
                (kAudioDeviceTransportTypeUnknown, .other),
            ]

            for testCase in cases {
                try expectEqual(
                    OutputDeviceTransport(coreAudioValue: testCase.0),
                    testCase.1
                )
            }
        },
        SelfTest("unknown and missing-default devices return stable errors") {
            let reader = FakeOutputDeviceReader(
                devices: [
                    1: FakeOutputDevice(
                        alive: true,
                        outputChannels: 2,
                        name: "Output",
                        uid: "private-output-uid",
                        transport: kAudioDeviceTransportTypeUSB
                    ),
                ],
                defaultOutputDeviceID: nil
            )
            let catalog = try makeCatalog(reader: reader)

            try expectCatalogError(.unknownDevice) {
                try catalog.device(reference: "vrdev1_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
            }
            try expectCatalogError(.defaultDeviceUnavailable) {
                try catalog.defaultDevice()
            }
        },
        SelfTest("device JSON exposes only sanitized public fields") {
            let rawUID = "private-json-device-uid"
            let reader = FakeOutputDeviceReader(
                devices: [
                    7: FakeOutputDevice(
                        alive: true,
                        outputChannels: 2,
                        name: " USB\tDAC ",
                        uid: rawUID,
                        transport: kAudioDeviceTransportTypeUSB
                    ),
                ],
                defaultOutputDeviceID: 7
            )
            let descriptor = try makeCatalog(reader: reader).devices()[0]

            let data = try JSONEncoder().encode(descriptor)
            let object = try JSONSerialization.jsonObject(with: data)
            guard let dictionary = object as? [String: Any] else {
                throw SelfTestFailure("device JSON is not an object")
            }

            try expectEqual(Set(dictionary.keys), [
                "device_ref",
                "is_default",
                "label",
                "transport",
            ])
            try expect(!String(decoding: data, as: UTF8.self).contains(rawUID), "UID leaked")
        },
    ]
}

private func makeCatalog(reader: FakeOutputDeviceReader) throws -> DeviceCatalog {
    try DeviceCatalog(
        propertyReader: reader,
        referenceDeriver: DeviceReferenceDeriver(
            keyData: Data(repeating: 0x5A, count: 32)
        )
    )
}

private func expectCatalogError<Result>(
    _ code: DeviceCatalogError.Code,
    _ body: () throws -> Result
) throws {
    do {
        _ = try body()
    } catch let error as DeviceCatalogError {
        try expectEqual(error.code, code)
        return
    }
    throw SelfTestFailure("expected device catalog error \(code)")
}

private struct FakeOutputDevice {
    let alive: Bool
    let outputChannels: UInt32
    let name: String
    let uid: String
    let transport: UInt32
}

private final class FakeOutputDeviceReader: OutputDevicePropertyReading {
    private let records: [UInt32: FakeOutputDevice]
    private let defaultID: UInt32?

    init(
        devices: [UInt32: FakeOutputDevice],
        defaultOutputDeviceID: UInt32?
    ) {
        records = devices
        defaultID = defaultOutputDeviceID
    }

    func deviceIDs() throws -> [UInt32] {
        records.keys.sorted()
    }

    func defaultOutputDeviceID() throws -> UInt32? {
        defaultID
    }

    func isAlive(deviceID: UInt32) throws -> Bool {
        try record(deviceID).alive
    }

    func outputChannelCount(deviceID: UInt32) throws -> UInt32 {
        try record(deviceID).outputChannels
    }

    func name(deviceID: UInt32) throws -> String {
        try record(deviceID).name
    }

    func uid(deviceID: UInt32) throws -> String {
        try record(deviceID).uid
    }

    func transportType(deviceID: UInt32) throws -> UInt32 {
        try record(deviceID).transport
    }

    private func record(_ deviceID: UInt32) throws -> FakeOutputDevice {
        guard let record = records[deviceID] else {
            throw SelfTestFailure("unknown fake device")
        }
        return record
    }
}

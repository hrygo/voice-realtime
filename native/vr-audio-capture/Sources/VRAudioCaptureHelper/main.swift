import Darwin
import Foundation
import VRAudioCaptureCore

private let arguments = Array(CommandLine.arguments.dropFirst())

if arguments == ["--list-devices-json"] {
    do {
        let devices = try DeviceCatalog.live().devices()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(devices)
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    } catch {
        FileHandle.standardError.write(Data("device_enumeration_failed\n".utf8))
        exit(1)
    }
} else if !arguments.isEmpty {
    FileHandle.standardError.write(
        Data("usage: vr-audio-capture-helper [--list-devices-json]\n".utf8)
    )
    exit(64)
}

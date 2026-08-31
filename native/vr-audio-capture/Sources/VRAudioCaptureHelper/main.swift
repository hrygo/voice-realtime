import Darwin
import Foundation
import VRAudioCaptureCore

private let arguments = Array(CommandLine.arguments.dropFirst())
private let tokenEnvironmentKey = "VR_AUDIO_CAPTURE_TOKEN"

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
} else if arguments.count == 2, arguments[0] == "--socket" {
    guard let captureToken = ProcessInfo.processInfo.environment[tokenEnvironmentKey] else {
        FileHandle.standardError.write(Data("helper_configuration_invalid\n".utf8))
        exit(78)
    }
    unsetenv(tokenEnvironmentKey)
    do {
        let server = try CaptureServer(
            socketPath: URL(fileURLWithPath: arguments[1]),
            captureToken: captureToken
        )
        signal(SIGTERM, SIG_IGN)
        signal(SIGINT, SIG_IGN)
        let terminationSource = DispatchSource.makeSignalSource(
            signal: SIGTERM,
            queue: .global(qos: .utility)
        )
        let interruptSource = DispatchSource.makeSignalSource(
            signal: SIGINT,
            queue: .global(qos: .utility)
        )
        terminationSource.setEventHandler { server.stop() }
        interruptSource.setEventHandler { server.stop() }
        terminationSource.resume()
        interruptSource.resume()
        try server.start()
        server.waitUntilStopped()
        terminationSource.cancel()
        interruptSource.cancel()
    } catch {
        FileHandle.standardError.write(Data("helper_start_failed\n".utf8))
        exit(1)
    }
} else if !arguments.isEmpty {
    FileHandle.standardError.write(
        Data(
            "usage: vr-audio-capture-helper [--list-devices-json | --socket PATH]\n".utf8
        )
    )
    exit(64)
}

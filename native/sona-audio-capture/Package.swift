// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "sona-audio-capture",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(name: "SonaAudioCaptureCore", targets: ["SonaAudioCaptureCore"]),
        .executable(
            name: "sona-audio-capture-helper",
            targets: ["SonaAudioCaptureHelper"]
        ),
        .executable(
            name: "sona-audio-capture-selftest",
            targets: ["SonaAudioCaptureCoreTests"]
        ),
    ],
    targets: [
        .target(
            name: "SonaAudioCaptureRing",
            publicHeadersPath: "include"
        ),
        .target(
            name: "SonaAudioCaptureCore",
            dependencies: ["SonaAudioCaptureRing"]
        ),
        .executableTarget(
            name: "SonaAudioCaptureHelper",
            dependencies: ["SonaAudioCaptureCore"]
        ),
        .executableTarget(
            name: "SonaAudioCaptureCoreTests",
            dependencies: ["SonaAudioCaptureCore"],
            path: "Tests/SonaAudioCaptureCoreTests"
        ),
    ],
    swiftLanguageModes: [.v6],
    cLanguageStandard: .c11
)

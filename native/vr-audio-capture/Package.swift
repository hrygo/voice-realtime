// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "vr-audio-capture",
    platforms: [
        .macOS(.v14),
    ],
    products: [
        .library(name: "VRAudioCaptureCore", targets: ["VRAudioCaptureCore"]),
        .executable(
            name: "vr-audio-capture-helper",
            targets: ["VRAudioCaptureHelper"]
        ),
        .executable(
            name: "vr-audio-capture-selftest",
            targets: ["VRAudioCaptureCoreTests"]
        ),
    ],
    targets: [
        .target(
            name: "VRAudioCaptureRing",
            publicHeadersPath: "include"
        ),
        .target(
            name: "VRAudioCaptureCore",
            dependencies: ["VRAudioCaptureRing"]
        ),
        .executableTarget(
            name: "VRAudioCaptureHelper",
            dependencies: ["VRAudioCaptureCore"]
        ),
        .executableTarget(
            name: "VRAudioCaptureCoreTests",
            dependencies: ["VRAudioCaptureCore"],
            path: "Tests/VRAudioCaptureCoreTests"
        ),
    ],
    swiftLanguageModes: [.v6],
    cLanguageStandard: .c11
)

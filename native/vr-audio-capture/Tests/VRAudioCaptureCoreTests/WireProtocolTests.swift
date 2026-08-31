import Foundation
import VRAudioCaptureCore

func wireProtocolTests() -> [SelfTest] {
    [
        SelfTest("PCM header matches the shared Python golden fixture") {
            let captureID = UUID(
                uuidString: "00000000-0000-0000-0000-000000000001"
            )!
            let sourceID = UUID(
                uuidString: "00000000-0000-0000-0000-000000000002"
            )!
            let expectedHeader = try Data(hexadecimal: String(
                contentsOf: fixtureURL("pcm-header.hex"),
                encoding: .utf8
            ))
            let frame = PCMFrame(
                captureID: captureID,
                sourceID: sourceID,
                deviceGeneration: 3,
                sequence: 4,
                hostTimeNanoseconds: 5,
                sampleRate: 16_000,
                samplesPerChannel: 512,
                channels: 1,
                sampleWidth: 2,
                flags: [.discontinuity],
                payload: Data(repeating: 0, count: 1_024)
            )

            let encoded = try WireEncoder.encode(frame)
            try expectEqual(
                Data(encoded.prefix(WireProtocol.pcmHeaderLength)),
                expectedHeader
            )

            let decoder = WireDecoder()
            let messages = try decoder.feed(
                expectedHeader + Data(repeating: 0, count: 1_024)
            )
            try expectEqual(messages, [.pcm(frame)])
        },
        SelfTest("control fixture round-trips one byte at a time") {
            let fixture = try Data(contentsOf: fixtureURL("hello.json"))
            let control = try ControlMessage(jsonData: fixture)
            let encoded = try WireEncoder.encode(control)
            let decoder = WireDecoder()
            var decoded: [WireMessage] = []

            for byte in encoded {
                decoded.append(contentsOf: try decoder.feed(Data([byte])))
            }

            try expectEqual(decoded, [.control(control)])
        },
        SelfTest("future minor header extension is skipped") {
            let body = Data(#"{"type":"list_devices","request_id":"future"}"#.utf8)
            let extensionBytes = Data("future!!".utf8)
            var frame = Data("VRAC".utf8)
            frame.appendBigEndian(
                UInt16(WireProtocol.commonHeaderLength + extensionBytes.count)
            )
            frame.append(1)
            frame.append(1)
            frame.append(WireMessageType.control.rawValue)
            frame.append(0)
            frame.appendBigEndian(UInt16(0))
            frame.appendBigEndian(UInt32(body.count))
            frame.append(extensionBytes)
            frame.append(body)

            let messages = try WireDecoder().feed(frame)
            try expectEqual(messages.count, 1)
            guard case let .control(message) = messages[0] else {
                throw SelfTestFailure("expected a control message")
            }
            try expectEqual(message.string(for: "type"), "list_devices")
            try expectEqual(message.string(for: "request_id"), "future")
        },
        SelfTest("oversized JSON is rejected from its prefix") {
            var frame = Data("VRAC".utf8)
            frame.appendBigEndian(UInt16(WireProtocol.commonHeaderLength))
            frame.append(1)
            frame.append(0)
            frame.append(WireMessageType.control.rawValue)
            frame.append(0)
            frame.appendBigEndian(UInt16(0))
            frame.appendBigEndian(
                UInt32(WireProtocol.maximumControlBodyBytes + 1)
            )

            try expectThrows(WireProtocolError.self) {
                try WireDecoder().feed(frame)
            }
        },
        SelfTest("invalid common header fields fail closed") {
            let control = try ControlMessage(payload: [
                "type": .string("list_devices"),
                "request_id": .string("boundary"),
            ])
            let valid = try WireEncoder.encode(control)
            let cases: [(offset: Int, value: UInt8, code: String)] = [
                (0, 0, "invalid_magic"),
                (6, 2, "unsupported_protocol"),
                (8, 0xFF, "unsupported_message_type"),
                (9, 1, "invalid_header"),
                (11, 1, "invalid_header"),
            ]
            for testCase in cases {
                var candidate = valid
                candidate[candidate.startIndex + testCase.offset] = testCase.value
                try expectWireError(testCase.code, decoding: candidate)
            }
        },
        SelfTest("invalid PCM length and format are rejected") {
            let frame = PCMFrame(
                captureID: UUID(),
                sourceID: UUID(),
                deviceGeneration: 0,
                sequence: 0,
                hostTimeNanoseconds: 0,
                sampleRate: 16_000,
                samplesPerChannel: 512,
                channels: 1,
                sampleWidth: 2,
                flags: [],
                payload: Data(repeating: 0, count: 1_024)
            )
            let valid = try WireEncoder.encode(frame)

            var wrongFormat = valid
            wrongFormat[wrongFormat.startIndex + 68] = 1
            try expectWireError("invalid_pcm_format", decoding: wrongFormat)

            var wrongLength = valid
            wrongLength[wrongLength.startIndex + 83] = 1
            try expectWireError("invalid_pcm", decoding: wrongLength)
        },
        SelfTest("protocol errors never echo untrusted JSON") {
            let secret = "sensitive-capture-token"
            let body = Data(
                ("{\"type\":\"hello\",\"capture_token\":\"" + secret).utf8
            )
            var frame = Data("VRAC".utf8)
            frame.appendBigEndian(UInt16(WireProtocol.commonHeaderLength))
            frame.append(1)
            frame.append(0)
            frame.append(WireMessageType.control.rawValue)
            frame.append(0)
            frame.appendBigEndian(UInt16(0))
            frame.appendBigEndian(UInt32(body.count))
            frame.append(body)

            do {
                _ = try WireDecoder().feed(frame)
                throw SelfTestFailure("malformed JSON was accepted")
            } catch let error as WireProtocolError {
                try expect(
                    !error.description.contains(secret),
                    "protocol error echoed untrusted JSON"
                )
            }
        },
    ]
}

private func expectWireError(_ code: String, decoding frame: Data) throws {
    do {
        _ = try WireDecoder().feed(frame)
    } catch let error as WireProtocolError {
        try expectEqual(error.code, code)
        return
    }
    throw SelfTestFailure("expected wire error \(code)")
}

private func fixtureURL(_ name: String) -> URL {
    let testFile = URL(fileURLWithPath: #filePath)
    let packageRoot = testFile
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let repositoryRoot = packageRoot
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    return repositoryRoot
        .appending(path: "contracts/audio-capture/v1/fixtures")
        .appending(path: name)
}

private extension Data {
    init(hexadecimal: String) throws {
        let characters = Array(hexadecimal.filter { !$0.isWhitespace })
        guard characters.count.isMultiple(of: 2) else {
            throw FixtureError.invalidHex
        }
        self.init()
        reserveCapacity(characters.count / 2)
        for index in stride(from: 0, to: characters.count, by: 2) {
            guard let byte = UInt8(
                String(characters[index ... index + 1]),
                radix: 16
            ) else {
                throw FixtureError.invalidHex
            }
            append(byte)
        }
    }

    mutating func appendBigEndian<T: FixedWidthInteger>(_ value: T) {
        var bigEndian = value.bigEndian
        Swift.withUnsafeBytes(of: &bigEndian) { append(contentsOf: $0) }
    }
}

private enum FixtureError: Error {
    case invalidHex
}

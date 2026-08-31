import Foundation

public enum WireProtocol {
    public static let major: UInt8 = 1
    public static let minor: UInt8 = 0
    public static let commonHeaderLength = 16
    public static let pcmHeaderLength = 84
    public static let maximumHeaderLength = 256
    public static let maximumControlBodyBytes = 65_536
    public static let maximumFrameBytes = 1_048_576
    public static let pcmPayloadBytes = 1_024
    static let magic = Data("VRAC".utf8)
}

public enum WireMessageType: UInt8, Sendable {
    case control = 1
    case pcm = 2
}

public struct WireProtocolError: Error, Equatable, CustomStringConvertible, Sendable {
    public let code: String
    public let description: String

    init(_ code: String, _ description: String) {
        self.code = code
        self.description = description
    }
}

public enum JSONValue: Equatable, Codable, Sendable {
    case string(String)
    case integer(Int64)
    case number(Double)
    case boolean(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .boolean(value)
        } else if let value = try? container.decode(Int64.self) {
            self = .integer(value)
        } else if let value = try? container.decode(Double.self) {
            guard value.isFinite else {
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "non-finite JSON number"
                )
            }
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "unsupported JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case let .string(value):
            try container.encode(value)
        case let .integer(value):
            try container.encode(value)
        case let .number(value):
            guard value.isFinite else {
                throw EncodingError.invalidValue(
                    value,
                    EncodingError.Context(
                        codingPath: encoder.codingPath,
                        debugDescription: "non-finite JSON number"
                    )
                )
            }
            try container.encode(value)
        case let .boolean(value):
            try container.encode(value)
        case let .object(value):
            try container.encode(value)
        case let .array(value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }
}

public struct ControlMessage: Equatable, Sendable {
    public let payload: [String: JSONValue]

    public init(payload: [String: JSONValue]) throws {
        try Self.validate(payload)
        self.payload = payload
    }

    public init(jsonData: Data) throws {
        do {
            let payload = try JSONDecoder().decode(
                [String: JSONValue].self,
                from: jsonData
            )
            try Self.validate(payload)
            self.payload = payload
        } catch let error as WireProtocolError {
            throw error
        } catch {
            throw WireProtocolError(
                "invalid_control",
                "control body is not valid JSON"
            )
        }
    }

    public func string(for key: String) -> String? {
        guard case let .string(value) = payload[key] else {
            return nil
        }
        return value
    }

    private static func validate(_ payload: [String: JSONValue]) throws {
        guard
            case let .string(type)? = payload["type"],
            (1 ... 64).contains(type.count)
        else {
            throw WireProtocolError("invalid_control", "control type is invalid")
        }
        if let requestID = payload["request_id"] {
            guard
                case let .string(value) = requestID,
                (1 ... 64).contains(value.count)
            else {
                throw WireProtocolError(
                    "invalid_control",
                    "request_id is invalid"
                )
            }
        }
    }
}

public enum WireMessage: Equatable, Sendable {
    case control(ControlMessage)
    case pcm(PCMFrame)
}

public enum WireEncoder {
    public static func encode(_ message: ControlMessage) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        let body: Data
        do {
            body = try encoder.encode(message.payload)
        } catch {
            throw WireProtocolError(
                "invalid_control",
                "control message is not valid JSON"
            )
        }
        guard body.count <= WireProtocol.maximumControlBodyBytes else {
            throw WireProtocolError("frame_too_large", "control frame too large")
        }
        var frame = commonHeader(
            type: .control,
            headerLength: WireProtocol.commonHeaderLength,
            bodyLength: body.count
        )
        frame.append(body)
        return frame
    }

    public static func encode(_ frame: PCMFrame) throws -> Data {
        try validatePCM(frame)
        var encoded = commonHeader(
            type: .pcm,
            headerLength: WireProtocol.pcmHeaderLength,
            bodyLength: frame.payload.count
        )
        encoded.appendUUID(frame.captureID)
        encoded.appendUUID(frame.sourceID)
        encoded.appendBigEndian(frame.deviceGeneration)
        encoded.appendBigEndian(frame.sequence)
        encoded.appendBigEndian(frame.hostTimeNanoseconds)
        encoded.appendBigEndian(frame.sampleRate)
        encoded.appendBigEndian(frame.samplesPerChannel)
        encoded.append(frame.channels)
        encoded.append(frame.sampleWidth)
        encoded.appendBigEndian(frame.flags.rawValue)
        encoded.appendBigEndian(UInt32(frame.payload.count))
        encoded.append(frame.payload)
        return encoded
    }

    private static func commonHeader(
        type: WireMessageType,
        headerLength: Int,
        bodyLength: Int
    ) -> Data {
        var data = WireProtocol.magic
        data.appendBigEndian(UInt16(headerLength))
        data.append(WireProtocol.major)
        data.append(WireProtocol.minor)
        data.append(type.rawValue)
        data.append(0)
        data.appendBigEndian(UInt16(0))
        data.appendBigEndian(UInt32(bodyLength))
        return data
    }

    private static func validatePCM(_ frame: PCMFrame) throws {
        guard
            frame.sampleRate == 16_000,
            frame.samplesPerChannel == 512,
            frame.channels == 1,
            frame.sampleWidth == 2
        else {
            throw WireProtocolError("invalid_pcm_format", "unsupported PCM format")
        }
        guard frame.payload.count == WireProtocol.pcmPayloadBytes else {
            throw WireProtocolError("invalid_pcm", "invalid PCM payload length")
        }
        guard frame.flags.rawValue & ~PCMFrameFlags.knownMask == 0 else {
            throw WireProtocolError(
                "invalid_frame_flags",
                "unknown PCM frame flags"
            )
        }
    }
}

public final class WireDecoder {
    private var buffer = Data()

    public init() {}

    public var bufferedBytes: Int {
        buffer.count
    }

    public func feed(_ data: Data) throws -> [WireMessage] {
        buffer.append(data)
        var messages: [WireMessage] = []
        while buffer.count >= WireProtocol.commonHeaderLength {
            guard buffer.prefix(4) == WireProtocol.magic else {
                throw WireProtocolError("invalid_magic", "invalid wire magic")
            }
            let headerLength = Int(buffer.readUInt16(at: 4))
            let major = buffer.byte(at: 6)
            let minor = buffer.byte(at: 7)
            let rawMessageType = buffer.byte(at: 8)
            let prefixFlags = buffer.byte(at: 9)
            let reserved = buffer.readUInt16(at: 10)
            let bodyLength = Int(buffer.readUInt32(at: 12))

            guard major == WireProtocol.major else {
                throw WireProtocolError(
                    "unsupported_protocol",
                    "unsupported protocol major"
                )
            }
            guard let messageType = WireMessageType(rawValue: rawMessageType) else {
                throw WireProtocolError(
                    "unsupported_message_type",
                    "unsupported wire message type"
                )
            }
            guard prefixFlags == 0, reserved == 0 else {
                throw WireProtocolError(
                    "invalid_header",
                    "reserved header fields must be zero"
                )
            }

            let minimumHeaderLength = messageType == .control
                ? WireProtocol.commonHeaderLength
                : WireProtocol.pcmHeaderLength
            guard
                headerLength >= minimumHeaderLength,
                headerLength <= WireProtocol.maximumHeaderLength,
                minor != WireProtocol.minor || headerLength == minimumHeaderLength
            else {
                throw WireProtocolError("invalid_header", "invalid wire header length")
            }
            if messageType == .control,
               bodyLength > WireProtocol.maximumControlBodyBytes {
                throw WireProtocolError("frame_too_large", "control frame too large")
            }
            if messageType == .pcm,
               bodyLength != WireProtocol.pcmPayloadBytes {
                throw WireProtocolError("invalid_pcm", "invalid PCM payload length")
            }
            let frameLength = headerLength + bodyLength
            guard frameLength <= WireProtocol.maximumFrameBytes else {
                throw WireProtocolError("frame_too_large", "wire frame too large")
            }
            guard buffer.count >= frameLength else {
                break
            }

            let frame = Data(buffer.prefix(frameLength))
            let message = try decode(
                frame,
                type: messageType,
                headerLength: headerLength,
                bodyLength: bodyLength
            )
            buffer.removeFirst(frameLength)
            messages.append(message)
        }
        return messages
    }

    private func decode(
        _ frame: Data,
        type: WireMessageType,
        headerLength: Int,
        bodyLength: Int
    ) throws -> WireMessage {
        switch type {
        case .control:
            let body = frame.subdata(
                in: headerLength ..< headerLength + bodyLength
            )
            return .control(try ControlMessage(jsonData: body))
        case .pcm:
            let payloadLength = Int(frame.readUInt32(at: 80))
            guard payloadLength == bodyLength else {
                throw WireProtocolError(
                    "invalid_pcm",
                    "PCM length fields do not match"
                )
            }
            let sampleRate = frame.readUInt32(at: 68)
            let samplesPerChannel = frame.readUInt16(at: 72)
            let channels = frame.byte(at: 74)
            let sampleWidth = frame.byte(at: 75)
            guard
                sampleRate == 16_000,
                samplesPerChannel == 512,
                channels == 1,
                sampleWidth == 2
            else {
                throw WireProtocolError(
                    "invalid_pcm_format",
                    "unsupported PCM format"
                )
            }
            let rawFlags = frame.readUInt32(at: 76)
            guard rawFlags & ~PCMFrameFlags.knownMask == 0 else {
                throw WireProtocolError(
                    "invalid_frame_flags",
                    "unknown PCM frame flags"
                )
            }
            return .pcm(PCMFrame(
                captureID: frame.readUUID(at: 16),
                sourceID: frame.readUUID(at: 32),
                deviceGeneration: frame.readUInt32(at: 48),
                sequence: frame.readUInt64(at: 52),
                hostTimeNanoseconds: frame.readUInt64(at: 60),
                sampleRate: sampleRate,
                samplesPerChannel: samplesPerChannel,
                channels: channels,
                sampleWidth: sampleWidth,
                flags: PCMFrameFlags(rawValue: rawFlags),
                payload: frame.subdata(
                    in: headerLength ..< headerLength + bodyLength
                )
            ))
        }
    }
}

private extension Data {
    func byte(at offset: Int) -> UInt8 {
        self[index(startIndex, offsetBy: offset)]
    }

    func readUInt16(at offset: Int) -> UInt16 {
        UInt16(byte(at: offset)) << 8 |
            UInt16(byte(at: offset + 1))
    }

    func readUInt32(at offset: Int) -> UInt32 {
        UInt32(byte(at: offset)) << 24 |
            UInt32(byte(at: offset + 1)) << 16 |
            UInt32(byte(at: offset + 2)) << 8 |
            UInt32(byte(at: offset + 3))
    }

    func readUInt64(at offset: Int) -> UInt64 {
        UInt64(readUInt32(at: offset)) << 32 |
            UInt64(readUInt32(at: offset + 4))
    }

    func readUUID(at offset: Int) -> UUID {
        UUID(uuid: (
            byte(at: offset),
            byte(at: offset + 1),
            byte(at: offset + 2),
            byte(at: offset + 3),
            byte(at: offset + 4),
            byte(at: offset + 5),
            byte(at: offset + 6),
            byte(at: offset + 7),
            byte(at: offset + 8),
            byte(at: offset + 9),
            byte(at: offset + 10),
            byte(at: offset + 11),
            byte(at: offset + 12),
            byte(at: offset + 13),
            byte(at: offset + 14),
            byte(at: offset + 15)
        ))
    }

    mutating func appendBigEndian<T: FixedWidthInteger>(_ value: T) {
        var bigEndian = value.bigEndian
        Swift.withUnsafeBytes(of: &bigEndian) { append(contentsOf: $0) }
    }

    mutating func appendUUID(_ value: UUID) {
        var raw = value.uuid
        Swift.withUnsafeBytes(of: &raw) { append(contentsOf: $0) }
    }
}

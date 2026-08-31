import CryptoKit
import Foundation

public struct DeviceReference: Hashable, Sendable {
    public let rawValue: String

    init(trustedRawValue: String) {
        rawValue = trustedRawValue
    }

    public init?(rawValue: String) {
        let prefix = "vrdev1_"
        guard rawValue.hasPrefix(prefix) else {
            return nil
        }
        let suffix = rawValue.dropFirst(prefix.count)
        let allowed = CharacterSet(
            charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        guard
            suffix.count == 43,
            suffix.unicodeScalars.allSatisfy(allowed.contains)
        else {
            return nil
        }
        self.rawValue = rawValue
    }
}

public enum DeviceReferenceError: Error, Equatable, Sendable {
    case invalidKey
    case keyUnavailable
    case insecureStorage
}

public protocol DeviceReferenceProviding: Sendable {
    func reference(forDeviceUID uid: String) -> DeviceReference
}

public struct DeviceReferenceDeriver: DeviceReferenceProviding, Sendable {
    private let key: SymmetricKey

    public init(keyData: Data) throws {
        guard keyData.count == 32 else {
            throw DeviceReferenceError.invalidKey
        }
        key = SymmetricKey(data: keyData)
    }

    public func reference(forDeviceUID uid: String) -> DeviceReference {
        let authenticationCode = HMAC<SHA256>.authenticationCode(
            for: Data(uid.utf8),
            using: key
        )
        let encoded = Data(authenticationCode)
            .base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
        return DeviceReference(trustedRawValue: "vrdev1_\(encoded)")
    }
}

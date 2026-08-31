import Darwin
import Foundation

@_spi(Testing)
public struct UnixPeerError: Error, Equatable, CustomStringConvertible, Sendable {
    public enum Code: String, Equatable, Sendable {
        case invalidRuntimeDirectory = "invalid_runtime_directory"
        case invalidSocketPath = "invalid_socket_path"
        case socketOperationFailed = "socket_operation_failed"
        case peerCredentialFailed = "peer_credential_failed"
        case invalidCaptureToken = "invalid_capture_token"
    }

    public let code: Code
    public let description: String

    init(_ code: Code, _ description: String) {
        self.code = code
        self.description = description
    }
}

@_spi(Testing)
public enum CaptureTokenSecurity {
    public static func isValid(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy {
            (48 ... 57).contains($0) || (97 ... 102).contains($0)
        }
    }

    public static func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
        let left = Array(lhs.utf8)
        let right = Array(rhs.utf8)
        var difference = UInt64(left.count ^ right.count)
        for index in 0 ..< max(left.count, right.count) {
            let leftByte = index < left.count ? left[index] : 0
            let rightByte = index < right.count ? right[index] : 0
            difference |= UInt64(leftByte ^ rightByte)
        }
        return difference == 0 && isValid(lhs) && isValid(rhs)
    }
}

@_spi(Testing)
public enum UnixPeerSecurity {
    public static func validateSocketParent(_ directory: URL) throws {
        guard directory.isFileURL,
              directory.path.hasPrefix("/"),
              directory.standardizedFileURL.path == directory.path
        else {
            throw UnixPeerError(
                .invalidRuntimeDirectory,
                "运行目录无效"
            )
        }

        var metadata = stat()
        guard lstat(directory.path, &metadata) == 0,
              metadata.st_mode & S_IFMT == S_IFDIR,
              metadata.st_uid == geteuid(),
              metadata.st_mode & 0o777 == 0o700
        else {
            throw UnixPeerError(
                .invalidRuntimeDirectory,
                "运行目录必须由当前用户独占"
            )
        }
    }

    public static func peerIsAuthorized(
        peerUID: uid_t,
        expectedUID: uid_t
    ) -> Bool {
        peerUID == expectedUID
    }

    static func effectivePeerUID(descriptor: Int32) throws -> uid_t {
        var effectiveUID = uid_t.max
        var effectiveGID = gid_t.max
        guard getpeereid(descriptor, &effectiveUID, &effectiveGID) == 0 else {
            throw UnixPeerError(
                .peerCredentialFailed,
                "无法校验客户端身份"
            )
        }
        return effectiveUID
    }

    static func makeListeningSocket(at socketPath: URL) throws -> Int32 {
        let parent = socketPath.deletingLastPathComponent()
        try validateSocketParent(parent)
        guard socketPath.isFileURL,
              socketPath.path.hasPrefix(parent.path + "/"),
              socketPath.deletingLastPathComponent().path == parent.path,
              socketPath.standardizedFileURL.path == socketPath.path,
              !socketPath.lastPathComponent.isEmpty,
              !socketPath.path.utf8.contains(0)
        else {
            throw UnixPeerError(.invalidSocketPath, "Socket 路径无效")
        }

        var existing = stat()
        if lstat(socketPath.path, &existing) == 0 || errno != ENOENT {
            throw UnixPeerError(.invalidSocketPath, "Socket 路径已存在")
        }

        var address = try unixAddress(socketPath.path)
        let descriptor = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw UnixPeerError(.socketOperationFailed, "无法创建 Socket")
        }
        var didBind = false
        do {
            try configureConnectedSocket(descriptor)
            let bindResult = withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.bind(
                        descriptor,
                        $0,
                        socklen_t(MemoryLayout<sockaddr_un>.size)
                    )
                }
            }
            guard bindResult == 0 else {
                throw UnixPeerError(.socketOperationFailed, "无法绑定 Socket")
            }
            didBind = true
            guard chmod(socketPath.path, 0o600) == 0 else {
                throw UnixPeerError(.socketOperationFailed, "无法设置 Socket 权限")
            }
            try validateBoundSocket(socketPath)
            guard Darwin.listen(descriptor, 4) == 0 else {
                throw UnixPeerError(.socketOperationFailed, "无法监听 Socket")
            }
            return descriptor
        } catch {
            Darwin.close(descriptor)
            if didBind {
                _ = unlink(socketPath.path)
            }
            throw error
        }
    }

    static func configureConnectedSocket(_ descriptor: Int32) throws {
        var enabled: Int32 = 1
        guard setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_NOSIGPIPE,
            &enabled,
            socklen_t(MemoryLayout<Int32>.size)
        ) == 0 else {
            throw UnixPeerError(.socketOperationFailed, "无法配置 Socket")
        }
        var timeout = timeval(tv_sec: 1, tv_usec: 0)
        guard setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_SNDTIMEO,
            &timeout,
            socklen_t(MemoryLayout<timeval>.size)
        ) == 0 else {
            throw UnixPeerError(.socketOperationFailed, "无法配置 Socket")
        }
    }

    static func removeOwnedSocket(at socketPath: URL) {
        var metadata = stat()
        guard lstat(socketPath.path, &metadata) == 0,
              metadata.st_mode & S_IFMT == S_IFSOCK,
              metadata.st_uid == geteuid()
        else { return }
        _ = unlink(socketPath.path)
    }

    private static func validateBoundSocket(_ socketPath: URL) throws {
        var metadata = stat()
        guard lstat(socketPath.path, &metadata) == 0,
              metadata.st_mode & S_IFMT == S_IFSOCK,
              metadata.st_uid == geteuid(),
              metadata.st_mode & 0o777 == 0o600
        else {
            throw UnixPeerError(.socketOperationFailed, "Socket 权限校验失败")
        }
    }

    private static func unixAddress(_ path: String) throws -> sockaddr_un {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8CString)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        guard bytes.count <= capacity else {
            throw UnixPeerError(.invalidSocketPath, "Socket 路径过长")
        }
        withUnsafeMutablePointer(to: &address.sun_path) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: capacity) { destination in
                for (index, byte) in bytes.enumerated() {
                    destination[index] = byte
                }
            }
        }
        return address
    }
}

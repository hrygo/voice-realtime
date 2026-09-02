import Darwin
import Foundation

public struct DeviceReferenceStore: DeviceReferenceProviding, Sendable {
    private static let keyByteCount = 32
    private static let keyFileName = "device-reference.key"
    private let deriver: DeviceReferenceDeriver

    public static func live() throws -> DeviceReferenceStore {
        guard let applicationSupport = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first else {
            throw DeviceReferenceError.keyUnavailable
        }
        let directory = applicationSupport
            .appending(path: "VoiceRealtime", directoryHint: .isDirectory)
            .appending(path: "AudioCapture", directoryHint: .isDirectory)
        return try load(storageDirectory: directory)
    }

    @_spi(Testing)
    public static func testingStore(
        storageDirectory: URL
    ) throws -> DeviceReferenceStore {
        try load(storageDirectory: storageDirectory)
    }

    public func reference(forDeviceUID uid: String) -> DeviceReference {
        deriver.reference(forDeviceUID: uid)
    }

    private static func load(storageDirectory: URL) throws -> DeviceReferenceStore {
        try ensurePrivateDirectory(storageDirectory)
        let keyURL = storageDirectory.appending(path: keyFileName)
        let keyData: Data
        do {
            keyData = try readKey(at: keyURL)
        } catch KeyFileState.missing {
            keyData = try createKey(at: keyURL)
        } catch let error as DeviceReferenceError {
            throw error
        } catch {
            throw DeviceReferenceError.keyUnavailable
        }
        return DeviceReferenceStore(
            deriver: try DeviceReferenceDeriver(keyData: keyData)
        )
    }

    private static func ensurePrivateDirectory(_ directory: URL) throws {
        do {
            try FileManager.default.createDirectory(
                at: directory.deletingLastPathComponent(),
                withIntermediateDirectories: true,
                attributes: nil
            )
        } catch {
            throw DeviceReferenceError.keyUnavailable
        }
        let createStatus = directory.path.withCString { path in
            mkdir(path, mode_t(0o700))
        }
        guard createStatus == 0 || errno == EEXIST else {
            throw DeviceReferenceError.keyUnavailable
        }
        var info = stat()
        let status = directory.path.withCString { path in
            lstat(path, &info)
        }
        guard
            status == 0,
            (info.st_mode & S_IFMT) == S_IFDIR,
            info.st_uid == geteuid(),
            (info.st_mode & 0o777) == 0o700
        else {
            throw DeviceReferenceError.insecureStorage
        }
    }

    private static func readKey(at url: URL) throws -> Data {
        let descriptor = url.path.withCString { path in
            open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        }
        if descriptor < 0 {
            if errno == ENOENT {
                throw KeyFileState.missing
            }
            throw DeviceReferenceError.keyUnavailable
        }
        defer { close(descriptor) }

        var info = stat()
        guard
            fstat(descriptor, &info) == 0,
            (info.st_mode & S_IFMT) == S_IFREG,
            info.st_uid == geteuid(),
            (info.st_mode & 0o777) == 0o600,
            info.st_nlink == 1,
            info.st_size == keyByteCount
        else {
            throw DeviceReferenceError.insecureStorage
        }
        return try readExactly(keyByteCount, from: descriptor)
    }

    private static func createKey(at url: URL) throws -> Data {
        var generator = SystemRandomNumberGenerator()
        let bytes = (0 ..< keyByteCount).map { _ in
            UInt8.random(in: .min ... .max, using: &generator)
        }
        let keyData = Data(bytes)
        let temporaryURL = url.deletingLastPathComponent().appending(
            path: ".device-reference.\(UUID().uuidString).tmp"
        )
        var descriptor = temporaryURL.path.withCString { path in
            open(
                path,
                O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
                mode_t(0o600)
            )
        }
        if descriptor < 0 {
            throw DeviceReferenceError.keyUnavailable
        }
        defer {
            if descriptor >= 0 {
                close(descriptor)
            }
            _ = temporaryURL.path.withCString { unlink($0) }
        }
        guard fchmod(descriptor, mode_t(0o600)) == 0 else {
            throw DeviceReferenceError.keyUnavailable
        }
        try writeAll(keyData, to: descriptor)
        guard fsync(descriptor) == 0 else {
            throw DeviceReferenceError.keyUnavailable
        }
        guard close(descriptor) == 0 else {
            throw DeviceReferenceError.keyUnavailable
        }
        descriptor = -1

        let renameStatus = temporaryURL.path.withCString { source in
            url.path.withCString { destination in
                renamex_np(source, destination, UInt32(RENAME_EXCL))
            }
        }
        if renameStatus != 0 {
            if errno == EEXIST {
                return try readKey(at: url)
            }
            throw DeviceReferenceError.keyUnavailable
        }
        return keyData
    }

    private static func readExactly(_ count: Int, from descriptor: Int32) throws -> Data {
        var bytes = [UInt8](repeating: 0, count: count)
        var offset = 0
        while offset < count {
            let readCount = bytes.withUnsafeMutableBytes { buffer in
                read(
                    descriptor,
                    buffer.baseAddress?.advanced(by: offset),
                    count - offset
                )
            }
            if readCount < 0, errno == EINTR {
                continue
            }
            guard readCount > 0 else {
                throw DeviceReferenceError.keyUnavailable
            }
            offset += readCount
        }
        return Data(bytes)
    }

    private static func writeAll(_ data: Data, to descriptor: Int32) throws {
        var offset = 0
        while offset < data.count {
            let writeCount = data.withUnsafeBytes { buffer in
                write(
                    descriptor,
                    buffer.baseAddress?.advanced(by: offset),
                    data.count - offset
                )
            }
            if writeCount < 0, errno == EINTR {
                continue
            }
            guard writeCount > 0 else {
                throw DeviceReferenceError.keyUnavailable
            }
            offset += writeCount
        }
    }
}

private enum KeyFileState: Error {
    case missing
}

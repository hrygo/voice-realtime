import Foundation
@_spi(Testing) import VRAudioCaptureCore

func deviceReferenceTests() -> [SelfTest] {
    [
        SelfTest("device references are stable opaque HMAC values") {
            let uid = "private-device-uid"
            let first = try DeviceReferenceDeriver(
                keyData: Data(repeating: 0x11, count: 32)
            )
            let sameKey = try DeviceReferenceDeriver(
                keyData: Data(repeating: 0x11, count: 32)
            )
            let otherKey = try DeviceReferenceDeriver(
                keyData: Data(repeating: 0x22, count: 32)
            )

            let reference = first.reference(forDeviceUID: uid)

            try expectEqual(reference, sameKey.reference(forDeviceUID: uid))
            try expect(
                reference != otherKey.reference(forDeviceUID: uid),
                "key did not scope ref"
            )
            try expect(!reference.rawValue.contains(uid), "device UID leaked into ref")
            try expect(reference.rawValue.hasPrefix("vrdev1_"), "ref prefix is unstable")
            try expectEqual(reference.rawValue.count, 50)
        },
        SelfTest("install key is private and stable across reload") {
            let directory = FileManager.default.temporaryDirectory
                .appending(path: "vr-device-ref-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: directory) }

            let firstStore = try DeviceReferenceStore.testingStore(
                storageDirectory: directory
            )
            let firstReference = firstStore.reference(
                forDeviceUID: "private-persisted-uid"
            )
            let secondStore = try DeviceReferenceStore.testingStore(
                storageDirectory: directory
            )
            let secondReference = secondStore.reference(
                forDeviceUID: "private-persisted-uid"
            )
            let keyURL = directory.appending(path: "device-reference.key")
            let attributes = try FileManager.default.attributesOfItem(
                atPath: keyURL.path
            )
            let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue
            let directoryAttributes = try FileManager.default.attributesOfItem(
                atPath: directory.path
            )
            let directoryPermissions = (
                directoryAttributes[.posixPermissions] as? NSNumber
            )?.intValue
            let persistedKey = try Data(contentsOf: keyURL)

            try expectEqual(firstReference, secondReference)
            try expectEqual(permissions, 0o600)
            try expectEqual(directoryPermissions, 0o700)
            try expectEqual(persistedKey.count, 32)
        },
        SelfTest("install key symlinks are rejected") {
            let root = FileManager.default.temporaryDirectory
                .appending(path: "vr-device-ref-symlink-\(UUID().uuidString)")
            let storage = root.appending(path: "storage")
            defer { try? FileManager.default.removeItem(at: root) }
            try FileManager.default.createDirectory(
                at: storage,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let target = root.appending(path: "target.key")
            try Data(repeating: 0, count: 32).write(to: target)
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: target.path
            )
            try FileManager.default.createSymbolicLink(
                at: storage.appending(path: "device-reference.key"),
                withDestinationURL: target
            )

            try expectThrows(DeviceReferenceError.self) {
                try DeviceReferenceStore.testingStore(storageDirectory: storage)
            }
        },
        SelfTest("group-readable key directories are rejected") {
            let directory = FileManager.default.temporaryDirectory
                .appending(path: "vr-device-ref-mode-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: directory) }
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            try FileManager.default.setAttributes(
                [.posixPermissions: 0o750],
                ofItemAtPath: directory.path
            )

            try expectThrows(DeviceReferenceError.self) {
                try DeviceReferenceStore.testingStore(storageDirectory: directory)
            }
        },
        SelfTest("concurrent first launch observes one complete install key") {
            let directory = FileManager.default.temporaryDirectory
                .appending(path: "vr-device-ref-race-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: directory) }
            let state = ConcurrentStoreState()
            let workerCount = 8

            for _ in 0 ..< workerCount {
                Thread {
                    do {
                        let store = try DeviceReferenceStore.testingStore(
                            storageDirectory: directory
                        )
                        state.complete(
                            reference: store.reference(forDeviceUID: "race-device-uid")
                        )
                    } catch {
                        state.complete(error: error)
                    }
                }.start()
            }
            let snapshot = state.waitForCompletions(workerCount)

            try expect(snapshot.completed, "concurrent store workers timed out")
            try expect(
                snapshot.errors.isEmpty,
                "concurrent store errors: \(snapshot.errors)"
            )
            try expectEqual(Set(snapshot.references).count, 1)
            try expectEqual(snapshot.references.count, workerCount)
        },
    ]
}

private final class ConcurrentStoreState: @unchecked Sendable {
    private let condition = NSCondition()
    private var references: [DeviceReference] = []
    private var errors: [String] = []

    func complete(reference: DeviceReference) {
        condition.withLock {
            references.append(reference)
            condition.broadcast()
        }
    }

    func complete(error: Error) {
        condition.withLock {
            errors.append(String(describing: error))
            condition.broadcast()
        }
    }

    func waitForCompletions(
        _ expectedCount: Int
    ) -> (references: [DeviceReference], errors: [String], completed: Bool) {
        condition.lock()
        defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(5)
        while references.count + errors.count < expectedCount {
            if !condition.wait(until: deadline) {
                break
            }
        }
        return (
            references,
            errors,
            references.count + errors.count == expectedCount
        )
    }
}

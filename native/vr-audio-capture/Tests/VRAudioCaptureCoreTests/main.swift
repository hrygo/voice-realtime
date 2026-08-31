import Darwin
import Foundation

let tests = wireProtocolTests()
    + ringBufferTests()
    + deviceReferenceTests()
    + deviceCatalogTests()
var failures = 0

for test in tests {
    do {
        try test.run()
        print("PASS \(test.name)")
    } catch {
        failures += 1
        let message = "FAIL \(test.name): \(error)\n"
        FileHandle.standardError.write(Data(message.utf8))
    }
}

if failures > 0 {
    exit(1)
}
print("\(tests.count) self-tests passed")

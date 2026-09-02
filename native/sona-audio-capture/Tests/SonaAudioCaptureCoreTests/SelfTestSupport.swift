struct SelfTest {
    let name: String
    let run: () throws -> Void

    init(_ name: String, run: @escaping () throws -> Void) {
        self.name = name
        self.run = run
    }
}

struct SelfTestFailure: Error, CustomStringConvertible {
    let description: String

    init(_ description: String) {
        self.description = description
    }
}

func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else {
        throw SelfTestFailure(message)
    }
}

func expectEqual<T: Equatable>(
    _ actual: @autoclosure () -> T,
    _ expected: @autoclosure () -> T
) throws {
    let actualValue = actual()
    let expectedValue = expected()
    guard actualValue == expectedValue else {
        throw SelfTestFailure("expected \(expectedValue), got \(actualValue)")
    }
}

func expectThrows<E: Error, Result>(
    _ errorType: E.Type,
    _ body: () throws -> Result
) throws {
    do {
        _ = try body()
    } catch is E {
        return
    } catch {
        throw SelfTestFailure("expected \(errorType), got \(type(of: error))")
    }
    throw SelfTestFailure("expected \(errorType) to be thrown")
}

import Foundation

/// The ~/.splash layout the app touches (SPEC §5). Mirrors manager/splash_gui/paths.py:
/// the base is `SPLASH_GUI_HOME` when set, else `~/.splash`.
public struct HomePaths: Sendable, Equatable {
    public static let homeEnv = "SPLASH_GUI_HOME"

    public var base: URL

    public init(base: URL) {
        self.base = base
    }

    public static func fromEnvironment(
        _ env: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> HomePaths {
        if let override = env[homeEnv], !override.isEmpty {
            let expanded = (override as NSString).expandingTildeInPath
            return HomePaths(base: URL(fileURLWithPath: expanded, isDirectory: true).standardizedFileURL)
        }
        return HomePaths(base: home.appendingPathComponent(".splash", isDirectory: true))
    }

    public var settingsFile: URL { base.appendingPathComponent("settings.json") }
    public var runDir: URL { base.appendingPathComponent("run", isDirectory: true) }
    public var cliTokenFile: URL { runDir.appendingPathComponent("cli.token") }
    public var logsDir: URL { base.appendingPathComponent("logs", isDirectory: true) }
    public var managerLog: URL { logsDir.appendingPathComponent("manager.log") }
    /// stdout/stderr of a manager the app spawned itself (dev mode) or via launchd.
    public var managerLaunchLog: URL { logsDir.appendingPathComponent("manager.launch.log") }

    /// Splash hardcodes its crash trace directory (server/crash_trace.py,
    /// docs/spec-drift.md #4); it does not follow SPLASH_GUI_HOME.
    public static var crashTraceDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Splash/crash", isDirectory: true)
    }

    /// Reads settings.json directly (used before the manager answers, to find its port).
    public func readLocalSettings() -> AppSettings {
        guard let data = try? Data(contentsOf: settingsFile), let json = try? JSONValue.parse(data) else {
            return AppSettings()
        }
        return AppSettings(response: json)
    }
}

/// Reads `~/.splash/run/cli.token` (docs/api.md §1.2). The manager creates it at startup and
/// never rotates it, but it may not exist yet when the app starts, so it is re-read lazily.
public final class CLITokenReader: Sendable {
    private let file: URL
    private let cache = LockedBox<String?>(nil)

    public init(file: URL) {
        self.file = file
    }

    public init(paths: HomePaths) {
        self.file = paths.cliTokenFile
    }

    /// The token, trimmed; nil when the file is missing or empty.
    public func token() -> String? {
        if let cached = cache.value { return cached }
        return reload()
    }

    /// Drops the cached value (after a 401) and reads the file again.
    @discardableResult
    public func reload() -> String? {
        let value = Self.read(file)
        cache.value = value
        return value
    }

    public static func read(_ file: URL) -> String? {
        guard let data = try? Data(contentsOf: file) else { return nil }
        let token = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? nil : token
    }
}

/// A tiny lock-protected box for state shared across isolation domains.
public final class LockedBox<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: Value

    public init(_ value: Value) {
        _value = value
    }

    public var value: Value {
        get { lock.withLock { _value } }
        set { lock.withLock { _value = newValue } }
    }

    public func withValue<T>(_ body: (inout Value) -> T) -> T {
        lock.withLock { body(&_value) }
    }
}

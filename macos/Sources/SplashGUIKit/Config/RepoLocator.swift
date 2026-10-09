import Foundation

/// Finds the Splashboard source checkout and `uv` for the development bundle (the app runs
/// the manager from the source tree with `uv run --project <repo>/manager splash-gui-manager`).
/// The packaged app never gets here: `BundledRuntime` runs the manager from inside the bundle first.
///
/// Repo resolution order:
/// 1. env `SPLASH_GUI_REPO`
/// 2. UserDefaults `SplashGUIRepoPath` (`defaults write <bundle id> SplashGUIRepoPath /path`; the
///    defaults domain is the app's bundle id, from packaging/identity.env, e.g. `io.github.arnavprabhu.splashboard`
///    or `io.github.arnavprabhu.splashboard.verify` for the verify variant)
/// 3. Info.plist `SplashGUIRepoPath` (baked in by scripts/bundle.sh)
/// 4. walking up from the executable (works for `swift run`, `.build/…`, and `macos/build/*.app`)
/// 5. `~/Desktop/Projects/Splash-GUI`
/// A candidate counts only if it contains `manager/pyproject.toml`.
public struct RepoLocator: Sendable {
    public static let envKey = "SPLASH_GUI_REPO"
    public static let defaultsKey = "SplashGUIRepoPath"

    public var environment: [String: String]
    public var defaultsValue: String?
    public var infoPlistValue: String?
    public var executableURL: URL?
    public var home: URL
    public var fileExists: @Sendable (String) -> Bool

    public init(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaultsValue: String? = UserDefaults.standard.string(forKey: RepoLocator.defaultsKey),
        infoPlistValue: String? = Bundle.main.object(forInfoDictionaryKey: RepoLocator.defaultsKey) as? String,
        executableURL: URL? = Bundle.main.executableURL,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        fileExists: @escaping @Sendable (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) {
        self.environment = environment
        self.defaultsValue = defaultsValue
        self.infoPlistValue = infoPlistValue
        self.executableURL = executableURL
        self.home = home
        self.fileExists = fileExists
    }

    public func isRepo(_ url: URL) -> Bool {
        fileExists(url.appendingPathComponent("manager/pyproject.toml").path)
    }

    public func repoURL() -> URL? {
        var candidates: [URL] = []
        for raw in [environment[Self.envKey], defaultsValue, infoPlistValue] {
            if let raw, !raw.isEmpty {
                candidates.append(URL(fileURLWithPath: (raw as NSString).expandingTildeInPath, isDirectory: true))
            }
        }
        if let exe = executableURL?.resolvingSymlinksInPath() {
            var dir = exe.deletingLastPathComponent()
            for _ in 0..<10 {
                candidates.append(dir)
                let parent = dir.deletingLastPathComponent()
                if parent.path == dir.path { break }
                dir = parent
            }
        }
        candidates.append(home.appendingPathComponent("Desktop/Projects/Splash-GUI", isDirectory: true))
        return candidates.first(where: isRepo)?.standardizedFileURL
    }

    /// `uv`, from PATH, then Homebrew, then the standalone installer's location.
    public func uvURL() -> URL? {
        var dirs = (environment["PATH"] ?? "").split(separator: ":").map(String.init)
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", home.appendingPathComponent(".local/bin").path,
                 home.appendingPathComponent(".cargo/bin").path]
        for dir in dirs where !dir.isEmpty {
            let path = (dir as NSString).appendingPathComponent("uv")
            if fileExists(path) { return URL(fileURLWithPath: path) }
        }
        return nil
    }

    /// The command line that starts the manager: `uv run --project <repo>/manager splash-gui-manager`.
    public func managerCommand() -> [String]? {
        guard let uv = uvURL(), let repo = repoURL() else { return nil }
        return [uv.path, "run", "--project", repo.appendingPathComponent("manager").path, "splash-gui-manager"]
    }

    /// PATH for the manager process: GUI apps get a minimal PATH from launchd, but the manager
    /// needs brew (engine discovery) and uv's Python.
    public func managerPATH() -> String {
        var parts = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
                     home.appendingPathComponent(".local/bin").path]
        parts += (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
        var seen = Set<String>()
        return parts.filter { seen.insert($0).inserted }.joined(separator: ":")
    }
}

/// The Python runtime that `Splashboard.app` carries:
/// `Contents/Resources/manager/python/bin/python3`, beside `Contents/Resources/web`.
/// When it exists the manager starts with it, and neither `uv` nor the repo is consulted.
public struct BundledRuntime: Sendable, Equatable {
    /// Arguments after the interpreter: the manager as a module, isolated, and no bytecode writes
    /// into the signed bundle (the same flags the CLI shim uses).
    public static let managerArguments = ["-I", "-B", "-m", "splash_gui.manager"]

    /// The bundled interpreter, or nil when the app carries none (a development bundle, `swift run`).
    public let python: URL?

    /// - Parameters:
    ///   - resources: the app's Resources folder (`Bundle.main.resourceURL` in the app).
    ///   - fileExists: injected for tests.
    public init(
        resources: URL? = Bundle.main.resourceURL,
        fileExists: @Sendable (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }
    ) {
        let candidate = resources?.appendingPathComponent("manager/python/bin/python3")
        if let candidate, fileExists(candidate.path) {
            python = candidate
        } else {
            python = nil
        }
    }

    /// The full command that starts the manager with the bundled interpreter, or nil.
    public func managerCommand() -> [String]? {
        guard let python else { return nil }
        return [python.path] + Self.managerArguments
    }
}

import Foundation

/// A spawned child process (dev mode). Mocked in tests.
public protocol LaunchedProcess: AnyObject, Sendable {
    var isRunning: Bool { get }
    var processIdentifier: Int32 { get }
    func interrupt()
    func terminate()
}

public protocol ProcessLaunching: Sendable {
    func launch(arguments: [String], environment: [String: String], logFile: URL) throws -> LaunchedProcess
}

/// `Foundation.Process` implementation; stdout/stderr go to `logFile` (appended).
public struct FoundationProcessLauncher: ProcessLaunching {
    public init() {}

    public func launch(arguments: [String], environment: [String: String], logFile: URL) throws -> LaunchedProcess {
        precondition(!arguments.isEmpty)
        let fm = FileManager.default
        try? fm.createDirectory(at: logFile.deletingLastPathComponent(), withIntermediateDirectories: true,
                                attributes: [.posixPermissions: 0o700])
        if !fm.fileExists(atPath: logFile.path) {
            fm.createFile(atPath: logFile.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let handle = try FileHandle(forWritingTo: logFile)
        handle.seekToEndOfFile()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: arguments[0])
        p.arguments = Array(arguments.dropFirst())
        p.environment = environment
        p.standardOutput = handle
        p.standardError = handle
        p.standardInput = FileHandle.nullDevice
        try p.run()
        return ProcessBox(p)
    }

    final class ProcessBox: LaunchedProcess, @unchecked Sendable {
        let process: Process
        init(_ p: Process) { process = p }
        var isRunning: Bool { process.isRunning }
        var processIdentifier: Int32 { process.processIdentifier }
        func interrupt() { process.interrupt() }
        func terminate() { process.terminate() }
    }
}

/// How the manager the app talks to came to run.
public enum ManagerOwnership: String, Sendable, Equatable {
    /// Already running when the app looked (CLI `splash start`, a developer's terminal). Never stopped by the app.
    case external
    /// The LaunchAgent registered through SMAppService (bundle mode).
    case agent
    /// A child `Process` the app spawned (`swift run`, or when the agent cannot be used).
    case child
}

/// Finds or starts the manager (SPEC §4.2, D30 dev mode) and stops what the app started.
@MainActor
public final class ManagerController {
    public private(set) var ownership: ManagerOwnership?
    public var startTimeout: Double = 20

    private let api: AdminAPI
    private let paths: HomePaths
    private let repo: RepoLocator
    private let agent: AppService?
    private let bundled: BundledRuntime
    private let launcher: ProcessLaunching
    private let sleep: @Sendable (Double) async -> Void
    private var child: LaunchedProcess?

    public init(
        api: AdminAPI, paths: HomePaths, repo: RepoLocator, agent: AppService?,
        bundled: BundledRuntime = BundledRuntime(),
        launcher: ProcessLaunching = FoundationProcessLauncher(),
        sleep: @escaping @Sendable (Double) async -> Void = { try? await Task.sleep(for: .seconds($0)) }
    ) {
        self.api = api
        self.paths = paths
        self.repo = repo
        self.agent = agent
        self.bundled = bundled
        self.launcher = launcher
        self.sleep = sleep
    }

    /// Makes sure a manager answers `/health`. Order: already running → LaunchAgent (bundle) →
    /// child process. Returns the ownership or a human-readable failure.
    public func ensureRunning() async -> Result<ManagerOwnership, ManagerStartError> {
        switch await api.probe() {
        case .manager:
            if ownership == nil {
                ownership = (agent?.status == .enabled) ? .agent : .external
            }
            return .success(ownership!)
        case .foreign:
            // A manager started now could not bind the port; never treat the stranger as ours.
            return .failure(.portTaken)
        case .down:
            break
        }
        if let agent {
            do {
                if agent.status == .enabled {
                    Self.kickstartAgent()
                } else {
                    try agent.register()
                }
                if agent.status == .enabled, await waitForHealth(timeout: startTimeout / 2) {
                    ownership = .agent
                    return .success(.agent)
                }
            } catch {
                // Fall through to the child process (e.g. ad-hoc signature not accepted).
            }
        }
        return await startChild()
    }

    private func startChild() async -> Result<ManagerOwnership, ManagerStartError> {
        if let child, child.isRunning {
            if await waitForHealth(timeout: startTimeout) {
                ownership = .child
                return .success(.child)
            }
            return .failure(.timedOut(log: paths.managerLaunchLog))
        }
        // Packaged app: the bundled interpreter, no uv, no repo (PKG-4). Development: uv run in the repo.
        let command: [String]
        if let bundledCommand = bundled.managerCommand() {
            command = bundledCommand
        } else if let repoCommand = repo.managerCommand() {
            command = repoCommand
        } else {
            if repo.uvURL() == nil { return .failure(.uvMissing) }
            return .failure(.repoMissing)
        }
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = repo.managerPATH()
        env["PYTHONUNBUFFERED"] = "1"
        do {
            child = try launcher.launch(arguments: command, environment: env, logFile: paths.managerLaunchLog)
        } catch {
            return .failure(.launchFailed(error.localizedDescription))
        }
        if await waitForHealth(timeout: startTimeout) {
            ownership = .child
            return .success(.child)
        }
        return .failure(child?.isRunning == false ? .exited(log: paths.managerLaunchLog) : .timedOut(log: paths.managerLaunchLog))
    }

    private func waitForHealth(timeout: Double) async -> Bool {
        let step = 0.5
        var waited = 0.0
        while waited < timeout {
            if await api.health() { return true }
            if let child, !child.isRunning { return false }
            await sleep(step)
            waited += step
        }
        return await api.health()
    }

    /// Stops the manager only if the app started it (agent or child); an external manager is
    /// left alone. Unregistering the agent is a launchd bootout (SIGTERM).
    public func stopOwned() async {
        switch ownership {
        case .agent:
            try? agent?.unregister()
        case .child:
            if let child, child.isRunning {
                child.interrupt()  // SIGINT: uvicorn's graceful shutdown
                var waited = 0.0
                while child.isRunning, waited < 10 {
                    await sleep(0.25)
                    waited += 0.25
                }
                if child.isRunning { child.terminate() }
            }
            child = nil
        case .external, nil:
            break
        }
        ownership = nil
    }

    /// `launchctl kickstart gui/<uid>/ai.splashgui.manager` for a registered but stopped agent.
    nonisolated static func kickstartAgent() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/\(ManagerAgent.label)"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
    }
}

public enum ManagerStartError: Error, Sendable, Equatable, CustomStringConvertible {
    case uvMissing
    case repoMissing
    case launchFailed(String)
    case exited(log: URL)
    case timedOut(log: URL)
    /// Another server (not Splash GUI) answers on the manager's port.
    case portTaken

    public var description: String {
        switch self {
        case .uvMissing: return "uv was not found (PATH, /opt/homebrew/bin, ~/.local/bin)."
        case .repoMissing: return "The Splash GUI source checkout was not found. Set SPLASH_GUI_REPO."
        case .launchFailed(let why): return "Couldn't start the manager: \(why)"
        case .exited(let log): return "The manager exited during startup. See \(log.path)."
        case .timedOut: return "The manager did not answer within 20 s."
        case .portTaken:
            return "Another server (not Splash GUI) is using the manager's port. Quit it, or change "
                + "server.port in ~/.splash/settings.json."
        }
    }
}

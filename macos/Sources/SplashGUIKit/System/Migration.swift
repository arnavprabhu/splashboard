import Foundation

/// Moves a development install to the packaged app (docs/plans/packaging.md, PKG-16).
///
/// The development bundle (`macos/scripts/bundle.sh`) registers a manager agent that runs `uv run --project
/// <repo>`. When the packaged app (it carries `Contents/Resources/manager/python`) starts and finds its own label
/// running that program, it unregisters the agent and registers its own plist, so launchd runs the bundled
/// interpreter; the manager rewrites the shim when it starts. Labels from an earlier identity (D62's rename) that
/// are still loaded are booted out. The decision is pure (`plan`) so it is tested without launchd.
public enum DevAgentMigration {
    public enum Action: Equatable, Sendable {
        /// The app's own label runs the development program: unregister and register again.
        case reregister
        /// A label from an earlier identity is loaded: `launchctl bootout gui/<uid>/<label>`.
        case bootout(String)
    }

    /// The `arguments = { … }` block of `launchctl print gui/<uid>/<label>`, one argument per line.
    public static func arguments(fromLaunchctlPrint text: String) -> [String] {
        var inside = false
        var out: [String] = []
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if !inside {
                if line == "arguments = {" { inside = true }
                continue
            }
            if line == "}" { break }
            if !line.isEmpty { out.append(line) }
        }
        return out
    }

    /// True for the development agent: `uv run --project <repo> …`.
    public static func isDevelopmentProgram(_ arguments: [String]) -> Bool {
        guard let first = arguments.first, first == "uv" || first.hasSuffix("/uv") else { return false }
        return arguments.contains("--project")
    }

    /// What to do at launch. `printer` returns `launchctl print` output for a label, or nil when it is not loaded.
    public static func plan(currentLabel: String, legacyLabels: [String], packaged: Bool,
                            printer: (String) -> String?) -> [Action] {
        guard packaged else { return [] }
        var actions: [Action] = []
        if let text = printer(currentLabel), isDevelopmentProgram(arguments(fromLaunchctlPrint: text)) {
            actions.append(.reregister)
        }
        for label in legacyLabels where label != currentLabel && printer(label) != nil {
            actions.append(.bootout(label))
        }
        return actions
    }

    /// Labels of earlier identities. Empty until the D62 switch, which adds `ai.splashgui.manager`.
    public static let legacyLabels: [String] = []

    /// `launchctl print gui/<uid>/<label>`, or nil when launchd has no such job.
    public static func launchctlPrint(_ label: String) -> String? {
        run(["print", "gui/\(getuid())/\(label)"])
    }

    /// Applies the plan: re-registers through the app's own agent service, boots legacy labels out.
    @MainActor
    public static func apply(_ actions: [Action], agent: AppService?) {
        for action in actions {
            switch action {
            case .reregister:
                try? agent?.unregister()
                try? agent?.register()
            case .bootout(let label):
                _ = run(["bootout", "gui/\(getuid())/\(label)"])
            }
        }
    }

    private static func run(_ arguments: [String]) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return process.terminationStatus == 0 ? String(decoding: data, as: UTF8.self) : nil
    }
}

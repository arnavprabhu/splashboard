import AppKit
import Foundation

/// One menu bar app per user: a second copy of `Splashboard.app`, e.g. another build
/// path, activates the running one and exits instead of adding a second status item.
public enum SingleInstance {
    public struct Candidate: Sendable, Equatable {
        public var pid: Int32
        public var bundleID: String?
        public var launchDate: Date?
        public var terminated: Bool

        public init(pid: Int32, bundleID: String?, launchDate: Date?, terminated: Bool = false) {
            self.pid = pid
            self.bundleID = bundleID
            self.launchDate = launchDate
            self.terminated = terminated
        }
    }

    /// The running instance this launch should defer to, or nil to keep running. Unbundled runs
    /// (`swift run`, no bundle id) never defer. When two copies start together, the earlier launch
    /// (then the lower pid) stays, so exactly one survives.
    public static func instanceToDeferTo(bundleID: String?, me: Candidate, running: [Candidate]) -> Candidate? {
        guard let bundleID, !bundleID.isEmpty else { return nil }
        let rank = { (c: Candidate) in (c.launchDate ?? .distantPast, c.pid) }
        let others = running.filter { $0.pid != me.pid && !$0.terminated && $0.bundleID == bundleID }
        let older = others.filter { rank($0) < rank(me) }
        return older.min { rank($0) < rank($1) }
    }

    /// Checks the live process list; activates the instance to defer to and returns true when this
    /// process should exit.
    @MainActor
    public static func deferToRunningInstance(bundle: Bundle = .main) -> Bool {
        guard let bundleID = bundle.bundleIdentifier else { return false }
        let current = NSRunningApplication.current
        let me = Candidate(pid: current.processIdentifier, bundleID: bundleID, launchDate: current.launchDate ?? Date())
        let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).map {
            Candidate(pid: $0.processIdentifier, bundleID: $0.bundleIdentifier, launchDate: $0.launchDate, terminated: $0.isTerminated)
        }
        guard let other = instanceToDeferTo(bundleID: bundleID, me: me, running: running) else { return false }
        NSRunningApplication(processIdentifier: other.pid)?.activate()
        return true
    }
}

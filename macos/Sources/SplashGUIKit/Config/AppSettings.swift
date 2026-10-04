import Foundation

/// The settings the menu bar app reads (SPEC §8.2: `server.*`, `lifecycle.*`, `menubar.*`,
/// `notifications.*`, `wizard.completed`, `ui.theme`, `routing.default_model`).
///
/// Built from the `global` object of a SettingsDocument, either from
/// `GET /api/admin/settings` (`settings.global`) or straight from `~/.splash/settings.json`
/// before the manager answers. Every key has the documented default.
public struct AppSettings: Sendable, Equatable {
    public var serverHost: String = "127.0.0.1"
    public var serverPort: Int = 8000
    public var launchAtLogin: Bool = true
    public var stopOnQuit: Bool = true
    public var showTokps: Bool = false
    public var showMemory: Bool = false
    public var showGPU: Bool = false
    public var showSwitcher: Bool = false
    /// `notifications.<kind>` toggles; absent keys default to on.
    public var notifications: [String: Bool] = [
        "download_done": true, "engine_failed": true, "memory_critical": true,
        "update_available": true, "disk_cache_errors": true,
    ]
    public var wizardCompleted: Bool = false
    public var theme: String = "light"
    public var defaultModel: String?

    public init() {}

    /// `global` = the SettingsDocument's `global` object.
    public init(global: JSONValue) {
        if let v = global[path: "server.host"]?.string, !v.isEmpty { serverHost = v }
        if let v = global[path: "server.port"]?.int, (1...65535).contains(v) { serverPort = v }
        if let v = global[path: "lifecycle.launch_at_login"]?.bool { launchAtLogin = v }
        if let v = global[path: "lifecycle.stop_on_quit"]?.bool { stopOnQuit = v }
        if let v = global[path: "menubar.show_tokps"]?.bool { showTokps = v }
        if let v = global[path: "menubar.show_memory"]?.bool { showMemory = v }
        if let v = global[path: "menubar.show_gpu"]?.bool { showGPU = v }
        if let v = global[path: "menubar.show_switcher"]?.bool { showSwitcher = v }
        if let toggles = global["notifications"]?.object {
            for (k, v) in toggles { if let b = v.bool { notifications[k] = b } }
        }
        if let v = global[path: "wizard.completed"]?.bool { wizardCompleted = v }
        if let v = global[path: "ui.theme"]?.string { theme = v }
        defaultModel = global[path: "routing.default_model"]?.string
    }

    /// From a `GET /api/admin/settings` response (`{settings: {global: …}, …}`) or a bare
    /// settings.json document (`{version, global, models}`).
    public init(response: JSONValue) {
        if let global = response[path: "settings.global"] {
            self.init(global: global)
        } else if let global = response["global"] {
            self.init(global: global)
        } else {
            self.init()
        }
    }

    public func notificationEnabled(_ toggle: String) -> Bool {
        notifications[toggle] ?? true
    }

    /// Where to reach the manager. A wildcard bind is reached over loopback; the CLI token is
    /// accepted only from loopback clients (docs/api.md §1.2).
    public var managerBaseURL: URL {
        let host: String
        switch serverHost {
        case "0.0.0.0", "", "localhost": host = "127.0.0.1"
        case "::", "::1", "[::]", "[::1]": host = "[::1]"
        default: host = serverHost.contains(":") && !serverHost.hasPrefix("[") ? "[\(serverHost)]" : serverHost
        }
        return URL(string: "http://\(host):\(serverPort)")!
    }

    /// `127.0.0.1:8000` for the menu's version line.
    public var endpointLabel: String {
        let url = managerBaseURL
        return "\(url.host(percentEncoded: false) ?? "127.0.0.1"):\(serverPort)"
    }
}

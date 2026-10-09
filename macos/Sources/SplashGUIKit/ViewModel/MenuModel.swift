import Foundation

/// Every action a menu item can trigger.
public enum MenuCommand: Sendable, Equatable, Hashable {
    case startManager
    case showManagerLog
    case stopServer
    case cancelLoading
    case loadModel(String)
    case unloadModel
    case restartEngine
    case openAdmin(String)  // path under the manager, e.g. "/admin/status"
    case connectIntegration(String)
    case disconnectIntegration(String)
    case restoreIntegration(String)
    case openTerminal(String)
    case checkForUpdates
    case about
    case continueSetup
    case openNotificationSettings
    case quit(stopServer: Bool)
}

public struct MenuItem: Sendable, Equatable {
    public var title: String
    public var command: MenuCommand
    public var enabled: Bool = true
    /// Key equivalent with ⌘ (`"o"` → ⌘O).
    public var shortcut: Character?
    public var subtitle: String?
    public var checked: Bool = false
    public var help: String?
    /// Shown instead while ⌥ is held (D-10-5).
    public var alternate: MenuAlternate?

    public init(
        _ title: String, _ command: MenuCommand, enabled: Bool = true, shortcut: Character? = nil,
        subtitle: String? = nil, checked: Bool = false, help: String? = nil, alternate: MenuAlternate? = nil
    ) {
        self.title = title
        self.command = command
        self.enabled = enabled
        self.shortcut = shortcut
        self.subtitle = subtitle
        self.checked = checked
        self.help = help
        self.alternate = alternate
    }
}

public struct MenuAlternate: Sendable, Equatable {
    public var title: String
    public var command: MenuCommand
}

/// A menu row. Disabled informational lines are `.text`.
public indirect enum MenuEntry: Sendable, Equatable {
    case text(String)
    case button(MenuItem)
    case submenu(title: String, enabled: Bool, entries: [MenuEntry])
    case separator

    /// Snapshot form for tests: titles with flags (`[x]` disabled, `✓` checked, `▸` submenu).
    public var snapshot: String {
        switch self {
        case .text(let t): return "[x] \(t)"
        case .button(let b):
            var s = b.enabled ? "" : "[x] "
            if b.checked { s += "✓ " }
            s += b.title
            if let k = b.shortcut { s += " ⌘\(k.uppercased())" }
            return s
        case .submenu(let title, let enabled, _): return (enabled ? "" : "[x] ") + title + " ▸"
        case .separator: return "---"
        }
    }

    public var submenuEntries: [MenuEntry]? {
        if case .submenu(_, _, let e) = self { return e }
        return nil
    }
}

/// Everything the menu depends on.
public struct MenuInput: Sendable {
    public var manager: ManagerPhase
    public var engine: EngineView?
    public var settings: AppSettings
    public var models: [InstalledModel]?
    public var integrations: IntegrationsView?
    public var metrics: LiveMetrics?
    public var usageToday: UsageToday?
    public var downloads: [DownloadItem]
    public var notificationsDenied: Bool
    public var setupUnfinished: Bool

    public init(
        manager: ManagerPhase, engine: EngineView? = nil, settings: AppSettings = AppSettings(),
        models: [InstalledModel]? = nil, integrations: IntegrationsView? = nil, metrics: LiveMetrics? = nil,
        usageToday: UsageToday? = nil, downloads: [DownloadItem] = [], notificationsDenied: Bool = false,
        setupUnfinished: Bool = false
    ) {
        self.manager = manager
        self.engine = engine
        self.settings = settings
        self.models = models
        self.integrations = integrations
        self.metrics = metrics
        self.usageToday = usageToday
        self.downloads = downloads
        self.notificationsDenied = notificationsDenied
        self.setupUnfinished = setupUnfinished
    }
}

/// Builds the menu exactly as docs/ui/10-menubar.md §3 lists it, per state.
public enum MenuModel {
    public static func build(_ input: MenuInput) -> [MenuEntry] {
        var entries: [MenuEntry] = []
        let managerUp = input.manager == .running && input.engine != nil

        // One-time header lines (§4 notifications off, §5 setup not finished).
        var extras: [MenuEntry] = []
        if managerUp, input.setupUnfinished {
            extras += [.text("Setup not finished"), .button(MenuItem("Continue Setup…", .continueSetup))]
        }
        if input.notificationsDenied {
            extras += [.text("Notifications are off — enable them in System Settings"),
                       .button(MenuItem("Open Notification Settings", .openNotificationSettings))]
        }
        if !extras.isEmpty { entries += extras + [.separator] }

        let header = Presentation.header(engine: input.engine, manager: managerUp ? .running : input.manager,
                                         liveTps: liveTps(input))
        guard managerUp, let engine = input.engine else {
            entries.append(.text(header))
            switch input.manager {
            case .starting:
                entries.append(.button(MenuItem("Starting…", .startManager, enabled: false)))
            case .failedToStart:
                entries.append(.button(MenuItem("Show Log", .showManagerLog)))
                entries.append(.button(MenuItem("Try Again", .startManager)))
            default:
                entries.append(.button(MenuItem("Start Splashboard", .startManager)))
            }
            entries += tail(input, managerUp: false)
            return entries
        }

        entries.append(.text(header))
        entries.append(.text(Presentation.versionLine(engine: engine, endpoint: input.settings.endpointLabel)))
        let stats = servingStats(input, enabled: engine.state.isServing)

        switch engine.state {
        case .stopped, .unknown:
            let loadable = (input.models ?? []).filter(\.isLoadable)
            if input.models != nil, loadable.isEmpty, (input.models ?? []).isEmpty {
                entries.append(.button(MenuItem("Download a Model…", .openAdmin("/admin/models/downloader"))))
            } else if let def = input.settings.defaultModel, !def.isEmpty {
                entries.append(.button(MenuItem("Start Server", .loadModel(def))))
                if input.settings.showSwitcher {
                    entries.append(.submenu(title: "Load Model", enabled: true, entries: switcher(input, running: false)))
                }
            } else {
                // D-10-2: shown even when the switcher is off, because starting needs a model.
                entries.append(.submenu(title: "Start Server", enabled: true, entries: switcher(input, running: false)))
            }
            entries.append(stats)

        case .ready, .busy, .idleReleased, .recovering:
            if engine.state == .recovering, let err = engine.transportError, !err.isEmpty {
                entries.insert(.text(Format.truncate(err, to: 60)), at: entries.count)
            }
            entries.append(.button(MenuItem("Stop Server", .stopServer)))
            if engine.state == .recovering {
                entries.append(.button(MenuItem("Open Logs", .openAdmin("/admin/logs"))))
            }
            if input.settings.showSwitcher {
                entries.append(.submenu(title: "Load Model", enabled: true, entries: switcher(input, running: true)))
            }
            entries.append(stats)

        case .starting:
            let download = input.downloads.first { $0.model == engine.model && $0.isActive }
            entries.append(.text(Presentation.progressLine(phase: engine.phase, download: download, install: engine.install)))
            if let install = engine.install {
                entries.append(.text(Format.truncate(Presentation.installLine(install), to: 80)))
                entries.append(.text(Presentation.installWarning))
            }
            entries.append(.button(MenuItem("Cancel Loading", .cancelLoading)))
            entries.append(stats)

        case .stopping:
            entries.append(.button(MenuItem("Stopping…", .stopServer, enabled: false)))
            entries.append(stats)

        case .crashed:
            entries.append(.text("Splash exited unexpectedly. Restarting (attempt \(max(1, engine.restartAttempt)))…"))
            entries.append(.button(MenuItem("Stop Server", .stopServer)))
            entries.append(stats)

        case .engineFailed, .failed:
            if let msg = engine.error?.message ?? engine.transportError, !msg.isEmpty {
                entries.append(.text(Format.truncate(msg, to: 80)))
            }
            if engine.error?.kind == "budget_refusal" {
                entries.append(.button(MenuItem("Open Memory Settings", .openAdmin("/admin/settings/memory"))))
            } else {
                entries.append(.button(MenuItem("Restart Engine", .restartEngine)))
            }
            entries.append(.button(MenuItem("Open Logs", .openAdmin("/admin/logs"))))
            entries.append(stats)
        }

        entries += tail(input, managerUp: true)
        return entries
    }

    static func liveTps(_ input: MenuInput) -> Double? {
        guard input.engine?.state == .busy else { return nil }
        return input.metrics?.decodeTps
    }

    /// Open Admin … Quit (shared by every state).
    static func tail(_ input: MenuInput, managerUp: Bool) -> [MenuEntry] {
        let stop = input.settings.stopOnQuit
        let quit = MenuItem(
            "Quit Splashboard", .quit(stopServer: stop), shortcut: "q",
            alternate: MenuAlternate(title: stop ? "Quit and Keep Running" : "Quit and Stop Server",
                                     command: .quit(stopServer: !stop)))
        return [
            .separator,
            .button(MenuItem("Open Admin Panel", .openAdmin("/admin/status"), enabled: managerUp, shortcut: "o")),
            .button(MenuItem("Chat", .openAdmin("/admin/chat"), enabled: managerUp, shortcut: "k")),
            .submenu(title: "Integrations", enabled: managerUp, entries: managerUp ? integrations(input) : []),
            .separator,
            .button(MenuItem("Preferences…", .openAdmin("/admin/settings"), enabled: managerUp, shortcut: ",",
                               help: managerUp ? nil : "Start Splashboard first")),
            .button(MenuItem("Check for Updates…", .checkForUpdates)),
            .button(MenuItem("About Splashboard", .about)),
            .button(quit),
        ]
    }

    /// Quick switcher (§3.3). `running` adds Unload Model and checks the active model.
    public static func switcher(_ input: MenuInput, running: Bool) -> [MenuEntry] {
        var rows: [MenuEntry] = []
        let active = running ? input.engine?.model : nil
        if let models = input.models {
            if models.isEmpty { rows.append(.text("No models installed")) }
            for m in models {
                let isActive = m.id == active
                let subtitle = m.isLoadable
                    ? "\(m.formatLabel) · \(Format.bytes(m.sizeBytes))"
                    : m.statusLabel
                rows.append(.button(MenuItem(
                    m.id, .loadModel(m.id), enabled: m.isLoadable && !isActive, subtitle: subtitle, checked: isActive)))
            }
        } else {
            rows.append(.text("Models unavailable"))
        }
        if running {
            rows += [.separator, .button(MenuItem("Unload Model", .unloadModel))]
        }
        rows += [.separator, .button(MenuItem("Manage Models…", .openAdmin("/admin/models")))]
        return rows
    }

    /// Serving Stats (§3.4, D-10-3). `—` for unknown values, never 0.
    static func servingStats(_ input: MenuInput, enabled: Bool) -> MenuEntry {
        guard enabled else { return .submenu(title: "Serving Stats", enabled: false, entries: []) }
        let m = input.metrics
        let decode = Format.tokPerSec(m?.decodeTps)
        let ttft = "\(Format.seconds(fromMs: m?.ttftP50Ms)) / \(Format.seconds(fromMs: m?.ttftP95Ms))"
        var tokens = Format.integer(input.usageToday?.totalTokens)
        if let cached = input.usageToday?.cachedTokens, input.usageToday?.totalTokens != nil {
            tokens += " (\(Format.integer(cached)) cached)"
        }
        var memory = Format.unknown
        if let c = m?.memoryCurrentBytes {
            memory = Format.bytes(c)
            if let l = m?.memoryLimitBytes { memory += " of \(Format.bytes(l))" }
            if let p = m?.systemPressure { memory += " · pressure \(p)" }
        }
        let inFlight = input.engine?.requestsInFlight
        let requests = "\(Format.integer(m?.requestsCompleted)) completed · \(Format.integer(m?.requestsFailed)) failed · \(Format.integer(inFlight)) in flight"
        return .submenu(title: "Serving Stats", enabled: true, entries: [
            .text("Decode  \(decode)"),
            .text("TTFT p50 / p95  \(ttft)"),
            .text("Tokens today  \(tokens)"),
            .text("Cache hit rate  \(Format.percent(m?.cacheHitRate))"),
            .text("Memory  \(memory)"),
            .text("Requests  \(requests)"),
            .separator,
            .button(MenuItem("Open Status Page…", .openAdmin("/admin/status"))),
        ])
    }

    /// Integrations (§3.5, D-10-4).
    static func integrations(_ input: MenuInput) -> [MenuEntry] {
        var rows: [MenuEntry] = []
        guard let view = input.integrations else {
            return [.text("Integrations unavailable"), .separator,
                    .button(MenuItem("Open Integrations Page…", .openAdmin("/admin/integrations")))]
        }
        for d in view.desktop {
            guard d.detected else {
                rows.append(.text("\(d.label) — not installed"))
                continue
            }
            rows.append(.text("\(d.label) — \(d.stateLabel)"))
            switch d.state {
            case "connected":
                rows.append(.button(MenuItem("Disconnect \(d.label)", .disconnectIntegration(d.name))))
            case "connecting":
                rows.append(.button(MenuItem("Connecting…", .connectIntegration(d.name), enabled: false)))
            case "restoring":
                rows.append(.button(MenuItem("Restoring…", .restoreIntegration(d.name), enabled: false)))
            case "needs_restore":
                rows.append(.button(MenuItem("Restore now", .restoreIntegration(d.name))))
                rows.append(.button(MenuItem("Reconnect", .connectIntegration(d.name))))
            default:
                rows.append(.button(MenuItem("Connect \(d.label)…", .connectIntegration(d.name))))
            }
        }
        let clis = view.cli.filter(\.installed)
        if !clis.isEmpty {
            if !rows.isEmpty { rows.append(.separator) }
            for c in clis {
                rows.append(.button(MenuItem("Launch \(c.label) in Terminal", .openTerminal(c.name))))
            }
        }
        if !rows.isEmpty { rows.append(.separator) }
        rows.append(.button(MenuItem("Open Integrations Page…", .openAdmin("/admin/integrations"))))
        return rows
    }
}

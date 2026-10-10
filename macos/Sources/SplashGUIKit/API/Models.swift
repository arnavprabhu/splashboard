import Foundation

// Typed views of the manager's payloads (pydantic models in
// manager/splash_gui/schemas.py). Each is built from a `JSONValue` with lenient accessors,
// so missing fields fall back to "unknown" instead of failing the whole object.

// MARK: Engine

/// `EngineView.state`. Unknown values from a newer manager map to `.unknown`.
public enum EngineState: String, Sendable, CaseIterable {
    case stopped, starting, ready, busy
    case idleReleased = "idle_released"
    case recovering
    case engineFailed = "engine_failed"
    case stopping, crashed, failed
    case unknown

    public init(raw: String?) {
        self = raw.flatMap(EngineState.init(rawValue:)) ?? .unknown
    }

    /// The engine answers requests (Serving Stats enabled, Stop Server shown).
    public var isServing: Bool {
        switch self {
        case .ready, .busy, .idleReleased, .recovering: return true
        default: return false
        }
    }

    /// A process exists (or is being started): Stop/Cancel makes sense, MEM gauge shown.
    public var hasProcess: Bool {
        switch self {
        case .stopped, .failed, .unknown: return false
        default: return true
        }
    }
}

public enum EnginePhase: String, Sendable {
    case installing, loading, warming
}

public struct EngineErrorInfo: Sendable, Equatable {
    public var kind: String
    public var code: String
    public var message: String

    public init(kind: String, code: String = "", message: String) {
        self.kind = kind
        self.code = code
        self.message = message
    }

    public init?(json: JSONValue?) {
        guard let json, json.object != nil else { return nil }
        kind = json["kind"]?.string ?? "other"
        code = json["code"]?.string ?? ""
        message = json["message"]?.string ?? ""
    }
}

/// What `splash serve` downloads before it loads (`starting.installing`, `EngineView.install`).
/// Splash cannot resume a file, so interrupting restarts the file in progress.
public struct EngineInstall: Sendable, Equatable {
    public var repo: String
    public var revision: String
    public var files: Int
    public var totalBytes: Int
    public var doneBytes: Int
    public var speedBps: Double?
    public var etaS: Double?

    public init(repo: String, revision: String = "", files: Int = 0, totalBytes: Int = 0, doneBytes: Int = 0,
                speedBps: Double? = nil, etaS: Double? = nil) {
        self.repo = repo
        self.revision = revision
        self.files = files
        self.totalBytes = totalBytes
        self.doneBytes = doneBytes
        self.speedBps = speedBps
        self.etaS = etaS
    }

    public init?(json: JSONValue?) {
        guard let json, let repo = json["repo"]?.string else { return nil }
        self.init(repo: repo, revision: json["revision"]?.string ?? "", files: json["files"]?.int ?? 0,
                  totalBytes: json["total_bytes"]?.int ?? 0, doneBytes: json["done_bytes"]?.int ?? 0,
                  speedBps: json["speed_bps"]?.double, etaS: json["eta_s"]?.double)
    }

    /// 0…1, or nil when the total is unknown.
    public var fraction: Double? { totalBytes > 0 ? min(1, Double(doneBytes) / Double(totalBytes)) : nil }
}

public struct EngineView: Sendable, Equatable {
    public var state: EngineState
    public var phase: EnginePhase?
    public var model: String?
    public var uptimeS: Double?
    public var requestsInFlight: Int
    public var queued: Int
    public var transportError: String?
    public var restartAttempt: Int
    public var error: EngineErrorInfo?
    public var engineVersion: String?
    public var engineFound: Bool?
    public var engineSource: String?
    /// Set while `starting.installing` downloads files.
    public var install: EngineInstall?

    public init(
        state: EngineState, phase: EnginePhase? = nil, model: String? = nil, uptimeS: Double? = nil,
        requestsInFlight: Int = 0, queued: Int = 0, transportError: String? = nil, restartAttempt: Int = 0,
        error: EngineErrorInfo? = nil, engineVersion: String? = nil, engineFound: Bool? = nil,
        engineSource: String? = nil, install: EngineInstall? = nil
    ) {
        self.state = state
        self.phase = phase
        self.model = model
        self.uptimeS = uptimeS
        self.requestsInFlight = requestsInFlight
        self.queued = queued
        self.transportError = transportError
        self.restartAttempt = restartAttempt
        self.error = error
        self.engineVersion = engineVersion
        self.engineFound = engineFound
        self.engineSource = engineSource
        self.install = install
    }

    public init(json: JSONValue) {
        state = EngineState(raw: json["state"]?.string)
        phase = json["phase"]?.string.flatMap(EnginePhase.init(rawValue:))
        model = json["model"]?.string
        uptimeS = json["uptime_s"]?.double
        requestsInFlight = json["requests_in_flight"]?.int ?? 0
        queued = json["queued"]?.int ?? 0
        transportError = json["transport"]?["error"]?.string
        restartAttempt = json["restart"]?["attempt"]?.int ?? 0
        error = EngineErrorInfo(json: json["error"])
        engineVersion = json["engine"]?["version"]?.string
        engineFound = json["engine"]?["found"]?.bool
        engineSource = json["engine"]?["source"]?.string
        install = phase == .installing ? EngineInstall(json: json["install"]) : nil
    }

    /// Splash is downloading files for the model it is starting (a Load or Restart interrupts it).
    public var isInstalling: Bool { state == .starting && phase == .installing }
}

// MARK: Live metrics

public struct LiveMetrics: Sendable, Equatable {
    public var t: Double?
    public var engineState: EngineState
    public var model: String?
    public var decodeTps: Double?
    public var ttftP50Ms: Double?
    public var ttftP95Ms: Double?
    public var cacheHitRate: Double?
    public var memoryCurrentBytes: Int?
    public var memoryLimitBytes: Int?
    public var systemPressure: String?
    public var requestsCompleted: Int?
    public var requestsFailed: Int?
    public var decoding: Int?
    public var prefilling: Int?

    public init(
        engineState: EngineState = .unknown, decodeTps: Double? = nil, memoryCurrentBytes: Int? = nil,
        memoryLimitBytes: Int? = nil
    ) {
        self.engineState = engineState
        self.decodeTps = decodeTps
        self.memoryCurrentBytes = memoryCurrentBytes
        self.memoryLimitBytes = memoryLimitBytes
    }

    public init(json: JSONValue) {
        t = json["t"]?.double
        engineState = EngineState(raw: json["engine_state"]?.string)
        model = json["model"]?.string
        decodeTps = json[path: "throughput.decode_tps"]?.double
        ttftP50Ms = json[path: "latency.ttft_p50_ms"]?.double
        ttftP95Ms = json[path: "latency.ttft_p95_ms"]?.double
        cacheHitRate = json[path: "cache.hit_rate"]?.double
        memoryCurrentBytes = json[path: "memory.current_bytes"]?.int
        memoryLimitBytes = json[path: "memory.limit_bytes"]?.int
        systemPressure = json[path: "memory.system_pressure"]?.string
        requestsCompleted = json[path: "totals.requests_completed"]?.int
        requestsFailed = json[path: "totals.requests_failed"]?.int
        decoding = json[path: "scheduler.decoding"]?.int
        prefilling = json[path: "scheduler.prefilling"]?.int
    }

    /// current / limit, or nil when either is unknown.
    public var memoryRatio: Double? {
        guard let c = memoryCurrentBytes, let l = memoryLimitBytes, l > 0 else { return nil }
        return Double(c) / Double(l)
    }
}

// MARK: Models

public struct InstalledModel: Sendable, Equatable, Identifiable {
    public var id: String
    public var format: String?
    public var sizeBytes: Int?
    public var status: String?
    public var progress: Double?

    public init(id: String, format: String? = nil, sizeBytes: Int? = nil, status: String? = "ready") {
        self.id = id
        self.format = format
        self.sizeBytes = sizeBytes
        self.status = status
    }

    public init?(json: JSONValue) {
        guard let id = json["id"]?.string, !id.isEmpty else { return nil }
        self.id = id
        format = json["format"]?.string
        sizeBytes = json["size_bytes"]?.int
        status = json["status"]?.string
        progress = json["progress"]?.double
    }

    /// Downloading / paused / verifying / broken rows, and Splash packages Splash no longer
    /// loads (`unsupported`), are listed but not loadable.
    public var isLoadable: Bool {
        switch status {
        case "downloading", "paused", "verifying", "broken", "unsupported": return false
        default: return true
        }
    }

    public var formatLabel: String {
        switch format {
        case "mlx": return "MLX"
        case "gguf": return "GGUF"
        case "legacy": return "Splash package"
        case let other?: return other.uppercased()
        case nil: return Format.unknown
        }
    }

    public var statusLabel: String {
        switch status {
        case "downloading":
            if let p = progress { return "Downloading \(Int((p * 100).rounded())) %" }
            return "Downloading"
        case "paused": return "Paused"
        case "verifying": return "Verifying"
        case "broken": return "Broken"
        case "unsupported": return "No longer loads"
        default: return status?.capitalized ?? ""
        }
    }

    public static func list(json: JSONValue) -> [InstalledModel] {
        (json["models"]?.array ?? []).compactMap(InstalledModel.init(json:))
    }
}

// MARK: Downloads

public struct DownloadItem: Sendable, Equatable {
    public var id: String
    public var model: String
    public var state: String
    public var progress: Double?
    public var speedBps: Double?

    public init(id: String, model: String, state: String, progress: Double? = nil, speedBps: Double? = nil) {
        self.id = id
        self.model = model
        self.state = state
        self.progress = progress
        self.speedBps = speedBps
    }

    public init?(json: JSONValue) {
        guard let id = json["id"]?.string, let model = json["model"]?.string else { return nil }
        self.id = id
        self.model = model
        state = json["state"]?.string ?? "queued"
        progress = json["progress"]?.double
        speedBps = json["speed_bps"]?.double
    }

    public var isActive: Bool { state == "queued" || state == "running" || state == "verifying" }
}

// MARK: Integrations

public struct DesktopIntegration: Sendable, Equatable {
    public var name: String
    public var label: String
    public var detected: Bool
    public var running: Bool
    public var state: String

    public init(name: String, label: String, detected: Bool, running: Bool = false, state: String) {
        self.name = name
        self.label = label
        self.detected = detected
        self.running = running
        self.state = state
    }

    public init?(json: JSONValue) {
        guard let name = json["name"]?.string else { return nil }
        self.name = name
        label = json["label"]?.string ?? name
        detected = json["detected"]?.bool ?? false
        running = json["running"]?.bool ?? false
        state = json["state"]?.string ?? "not_connected"
    }

    public var stateLabel: String {
        switch state {
        case "connected": return "Connected"
        case "connecting": return "Connecting…"
        case "restoring": return "Restoring…"
        case "needs_restore": return "Needs restore"
        default: return "Not connected"
        }
    }
}

public struct CliIntegration: Sendable, Equatable {
    public var name: String
    public var label: String
    public var installed: Bool

    public init(name: String, label: String, installed: Bool) {
        self.name = name
        self.label = label
        self.installed = installed
    }

    public init?(json: JSONValue) {
        guard let name = json["name"]?.string else { return nil }
        self.name = name
        label = json["label"]?.string ?? name
        installed = json["installed"]?.bool ?? false
    }
}

public struct IntegrationsView: Sendable, Equatable {
    public var cli: [CliIntegration]
    public var desktop: [DesktopIntegration]
    public var uncleanShutdown: Bool

    public init(cli: [CliIntegration] = [], desktop: [DesktopIntegration] = [], uncleanShutdown: Bool = false) {
        self.cli = cli
        self.desktop = desktop
        self.uncleanShutdown = uncleanShutdown
    }

    public init(json: JSONValue) {
        cli = (json["cli"]?.array ?? []).compactMap(CliIntegration.init(json:))
        desktop = (json["desktop"]?.array ?? []).compactMap(DesktopIntegration.init(json:))
        uncleanShutdown = json["unclean_shutdown"]?.bool ?? false
    }
}

// MARK: Notifications

/// `NotificationAction` = an admin API call `{id, label, method, path, body}`.
public struct NotificationAction: Sendable, Equatable {
    public var id: String
    public var label: String
    public var method: String
    public var path: String
    public var body: JSONValue?

    public init(id: String, label: String, method: String = "POST", path: String, body: JSONValue? = nil) {
        self.id = id
        self.label = label
        self.method = method
        self.path = path
        self.body = body
    }

    public init?(json: JSONValue) {
        guard let id = json["id"]?.string, !id.isEmpty, let path = json["path"]?.string else { return nil }
        self.id = id
        label = json["label"]?.string ?? id
        method = (json["method"]?.string ?? "POST").uppercased()
        self.path = path
        let b = json["body"]
        body = (b == nil || b?.isNull == true) ? nil : b
    }

    public var json: JSONValue {
        var o: [String: JSONValue] = ["id": .string(id), "label": .string(label), "method": .string(method), "path": .string(path)]
        o["body"] = body ?? .null
        return .object(o)
    }

    /// Paths under `/admin` are pages to open in the browser, not API calls.
    public var opensPage: Bool { path.hasPrefix("/admin") }
}

/// `Notification` event payload: `{id, kind, title, body, ts, actions}`.
public struct NotificationPayload: Sendable, Equatable {
    public var id: String
    public var kind: String
    public var title: String
    public var body: String
    public var actions: [NotificationAction]
    public var href: String?

    public init(id: String, kind: String, title: String, body: String, actions: [NotificationAction] = [], href: String? = nil) {
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.actions = actions
        self.href = href
    }

    public init?(json: JSONValue) {
        guard let title = json["title"]?.string, !title.isEmpty else { return nil }
        id = json["id"]?.string ?? UUID().uuidString
        kind = json["kind"]?.string ?? "info"
        self.title = title
        body = json["body"]?.string ?? ""
        actions = (json["actions"]?.array ?? []).compactMap(NotificationAction.init(json:))
        href = json["href"]?.string
    }
}

// MARK: Versions, usage

public struct VersionsInfo: Sendable, Equatable {
    public var gui: String?
    public var manager: String?
    public var engineVersion: String?
    public var engineSource: String?
    public var engineFound: Bool
    public var statusSchemaVersion: Int?

    public init(json: JSONValue) {
        gui = json["gui"]?.string
        manager = json["manager"]?.string
        engineVersion = json[path: "engine.version"]?.string
        engineSource = json[path: "engine.source"]?.string
        engineFound = json[path: "engine.found"]?.bool ?? false
        statusSchemaVersion = json["status_schema_version"]?.int
    }
}

public struct UsageToday: Sendable, Equatable {
    public var totalTokens: Int?
    public var cachedTokens: Int?

    public init(totalTokens: Int?, cachedTokens: Int?) {
        self.totalTokens = totalTokens
        self.cachedTokens = cachedTokens
    }

    public init(json: JSONValue) {
        cachedTokens = json["cached_tokens"]?.int
        if let total = json["total_tokens"]?.int {
            totalTokens = total
        } else if let p = json["prompt_tokens"]?.int, let c = json["completion_tokens"]?.int {
            totalTokens = p + c
        } else {
            totalTokens = nil
        }
    }
}

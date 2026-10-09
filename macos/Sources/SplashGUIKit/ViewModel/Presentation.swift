import Foundation

/// Whether the app can reach the manager.
public enum ManagerPhase: Sendable, Equatable {
    case unknown
    case starting
    case running
    case down
    case failedToStart(String)
}

/// The glyph family per state.
public enum IconKind: String, Sendable, CaseIterable {
    case stopped, loading, ready, generating, recovering, error, offline
}

/// A concrete status-item image: the kind plus its animation frame.
public enum StatusIcon: Sendable, Equatable, Hashable {
    case stopped
    case loading(frame: Int)  // 0…11
    case ready
    case generating(dotOn: Bool)
    case recovering(dotOn: Bool)
    case error
    case offline

    public static let loadingFrameCount = 12
    /// 10 fps for the loading dash; 0.9 s for the activity dot.
    public static let loadingFrameInterval = 0.1
    public static let dotBlinkInterval = 0.9

    /// Asset names (`menubar-loading-01…12`, `menubar-generating-a/b`).
    public var assetName: String {
        switch self {
        case .stopped: return "menubar-stopped"
        case .loading(let f): return String(format: "menubar-loading-%02d", (f % Self.loadingFrameCount) + 1)
        case .ready: return "menubar-ready"
        case .generating(let on): return on ? "menubar-generating-a" : "menubar-generating-b"
        case .recovering: return "menubar-recovering"
        case .error: return "menubar-error"
        case .offline: return "menubar-offline"
        }
    }

    public var kind: IconKind {
        switch self {
        case .stopped: return .stopped
        case .loading: return .loading
        case .ready: return .ready
        case .generating: return .generating
        case .recovering: return .recovering
        case .error: return .error
        case .offline: return .offline
        }
    }
}

public enum Presentation {
    /// Icon family for an engine state; nil engine = manager unreachable = offline.
    public static func iconKind(engine: EngineState?, manager: ManagerPhase) -> IconKind {
        if manager == .starting { return .loading }
        guard manager == .running, let engine else { return .offline }
        switch engine {
        case .stopped, .idleReleased, .unknown: return .stopped
        case .starting, .stopping, .crashed: return .loading
        case .ready: return .ready
        case .busy: return .generating
        case .recovering: return .recovering
        case .engineFailed, .failed: return .error
        }
    }

    /// The icon for a tick of the animation clock. Under Reduce Motion the loading image holds
    /// frame 01 and the dot stays on.
    public static func icon(kind: IconKind, tick: Int, reduceMotion: Bool) -> StatusIcon {
        switch kind {
        case .stopped: return .stopped
        case .loading: return .loading(frame: reduceMotion ? 0 : tick % StatusIcon.loadingFrameCount)
        case .ready: return .ready
        case .generating: return .generating(dotOn: reduceMotion ? true : dotOn(tick: tick))
        case .recovering: return .recovering(dotOn: reduceMotion ? true : dotOn(tick: tick))
        case .error: return .error
        case .offline: return .offline
        }
    }

    /// The animation clock ticks at 10 fps; the dot toggles every 0.9 s = 9 ticks.
    static func dotOn(tick: Int) -> Bool { (tick / 9) % 2 == 0 }

    public static func animates(_ kind: IconKind) -> Bool {
        kind == .loading || kind == .generating || kind == .recovering
    }

    /// Chip label.
    public static func chipLabel(_ engine: EngineView?) -> String {
        guard let engine else { return "Offline" }
        switch engine.state {
        case .stopped: return "Stopped"
        case .starting: return engine.phase == .installing ? "Preparing" : "Loading"
        case .ready: return "Ready"
        case .busy: return "Generating"
        case .idleReleased: return "Idle"
        case .recovering: return "Recovering"
        case .engineFailed, .failed: return "Failed"
        case .stopping: return "Stopping"
        case .crashed: return "Restarting"
        case .unknown: return "Unknown"
        }
    }

    /// "Splashboard: Ready".
    public static func accessibilityLabel(_ engine: EngineView?, manager: ManagerPhase) -> String {
        if manager != .running { return "Splashboard: Offline" }
        return "Splashboard: \(chipLabel(engine))"
    }

    /// Sub-state text while starting.
    public static func startingText(_ phase: EnginePhase?) -> String {
        switch phase {
        case .installing: return "Preparing model…"
        case .loading: return "Loading weights…"
        case .warming: return "Warming up…"
        case nil: return "Loading…"
        }
    }

    /// The header line.
    public static func header(engine: EngineView?, manager: ManagerPhase, liveTps: Double?) -> String {
        switch manager {
        case .unknown, .down: return "Splashboard is not running"
        case .starting: return "Starting Splashboard…"
        case .failedToStart: return "Splashboard failed to start"
        case .running: break
        }
        guard let engine else { return "Splashboard is not running" }
        let name = Format.shortModelName(engine.model) ?? "model"
        switch engine.state {
        case .stopped, .unknown:
            return "○ Splash is idle — no model loaded"
        case .ready:
            return "● Splash is running — \(name) (Ready)"
        case .busy:
            if let tps = liveTps { return "● Splash is running — \(name) (Generating · \(Format.integerTokPerSec(tps)))" }
            return "● Splash is running — \(name) (Generating)"
        case .idleReleased:
            return "● Splash is running — \(name) (Idle — weights released)"
        case .recovering:
            return "● Splash is running — \(name) (Recovering)"
        case .starting:
            return "◐ Loading \(name) — \(startingText(engine.phase))"
        case .stopping:
            return "◐ Stopping \(name)…"
        case .crashed:
            return "◐ Restarting \(name)…"
        case .engineFailed, .failed:
            if engine.error?.kind == "budget_refusal" { return "! \(name) does not fit in memory" }
            return "! Splash stopped after repeated failures"
        }
    }

    /// `Splash 1.2.0 · 127.0.0.1:8000 · up 2 h 05 m`.
    public static func versionLine(engine: EngineView?, endpoint: String) -> String {
        var parts: [String] = []
        if let v = engine?.engineVersion { parts.append("Splash \(v)") } else if engine?.engineFound == false {
            parts.append("Splash not found")
        }
        parts.append(endpoint)
        if let engine, engine.state.hasProcess, let up = Format.uptime(engine.uptimeS) { parts.append(up) }
        return parts.joined(separator: " · ")
    }

    /// `Preparing model ✓ · Loading weights… · Warming up`. While Splash
    /// installs, the bracket carries the install's percent, speed and ETA.
    public static func progressLine(phase: EnginePhase?, download: DownloadItem?, install: EngineInstall? = nil) -> String {
        var preparing = "Preparing model"
        if phase == .installing, let i = install {
            var bits: [String] = []
            if let p = i.fraction { bits.append("\(Int((p * 100).rounded())) %") }
            if let s = i.speedBps, s > 0 { bits.append("\(Format.bytes(Int(s), base: 1000))/s") }
            if let eta = i.etaS { bits.append("\(Format.duration(eta)) left") }
            if !bits.isEmpty { preparing += " (\(bits.joined(separator: " · ")))" }
        } else if phase == .installing || phase == nil, let d = download, d.isActive {
            var bits: [String] = []
            if let p = d.progress { bits.append("\(Int((p * 100).rounded())) %") }
            if let s = d.speedBps { bits.append("\(Format.bytes(Int(s)))/s") }
            if !bits.isEmpty { preparing += " (\(bits.joined(separator: " · ")))" }
        }
        switch phase {
        case .installing, nil: return "\(preparing)… · Loading weights · Warming up"
        case .loading: return "Preparing model ✓ · Loading weights… · Warming up"
        case .warming: return "Preparing model ✓ · Loading weights ✓ · Warming up…"
        }
    }
}

public extension Presentation {
    /// `mlx-community/Qwen3.8-27B-4bit · 9 files · 8.6 GB of 19.9 GB` under the progress line.
    static func installLine(_ i: EngineInstall) -> String {
        let files = i.files == 1 ? "1 file" : "\(i.files) files"
        return "\(i.repo) · \(files) · \(Format.bytes(i.doneBytes, base: 1000)) of \(Format.bytes(i.totalBytes, base: 1000))"
    }

    /// Splash cannot resume a file.
    static let installWarning = "Stopping now restarts the file in progress from zero."

    /// The alert before a Load or Restart interrupts an install.
    static func interruptInstall(_ engine: EngineView?) -> (message: String, info: String, confirmTitle: String) {
        let repo = engine?.install?.repo ?? engine?.model ?? "the model"
        return ("Splash is downloading \(repo).",
                "Interrupt it? Finished files are kept, but the file in progress starts again from zero; Splash can't resume it.",
                "Interrupt")
    }
}

/// Text and gauges right of the glyph. Order: glyph · t/s · MEM · GPU.
public struct StatusTitle: Sendable, Equatable {
    public var tokps: String?
    /// 0…5 filled cells, nil = hidden.
    public var memoryCells: Int?
    public var gpuCells: Int?
    /// Reduce Motion while loading: the static frame gets a "…" title.
    public var loadingEllipsis: Bool = false

    public init(tokps: String? = nil, memoryCells: Int? = nil, gpuCells: Int? = nil, loadingEllipsis: Bool = false) {
        self.tokps = tokps
        self.memoryCells = memoryCells
        self.gpuCells = gpuCells
        self.loadingEllipsis = loadingEllipsis
    }

    public var isEmpty: Bool { tokps == nil && memoryCells == nil && gpuCells == nil && !loadingEllipsis }

    /// How long `t/s` stays after generation ends.
    public static let tokpsLinger: Double = 2

    public static func cells(_ ratio: Double) -> Int {
        guard ratio.isFinite, ratio > 0 else { return 0 }
        return min(5, max(1, Int((ratio * 5).rounded())))
    }

    public static func make(
        settings: AppSettings, engine: EngineView?, manager: ManagerPhase, metrics: LiveMetrics?,
        lastTps: Double?, busyEndedAt: Date?, now: Date, gpu: Int?, reduceMotion: Bool
    ) -> StatusTitle {
        var title = StatusTitle()
        let running = manager == .running
        let state = engine?.state
        if settings.showTokps, running, let tps = lastTps {
            let busy = state == .busy
            let lingering = busyEndedAt.map { now.timeIntervalSince($0) < tokpsLinger } ?? false
            if busy || lingering { title.tokps = Format.menuBarTokPerSec(tps) }
        }
        if settings.showMemory, running, let state, state.hasProcess, let ratio = metrics?.memoryRatio {
            title.memoryCells = cells(ratio)
        }
        if settings.showGPU, let gpu {
            title.gpuCells = cells(Double(gpu) / 100)
        }
        if reduceMotion, Presentation.iconKind(engine: state, manager: manager) == .loading {
            title.loadingEllipsis = true
        }
        return title
    }

    /// Plain-text rendering, used for accessibility and tests: `74 t/s MEM ▮▮▮▯▯ GPU ▮▮▯▯▯`.
    public var text: String {
        var parts: [String] = []
        if loadingEllipsis { parts.append("…") }
        if let tokps { parts.append(tokps) }
        if let m = memoryCells { parts.append("MEM " + Self.gauge(m)) }
        if let g = gpuCells { parts.append("GPU " + Self.gauge(g)) }
        return parts.joined(separator: " ")
    }

    public static func gauge(_ filled: Int) -> String {
        String(repeating: "▮", count: filled) + String(repeating: "▯", count: 5 - filled)
    }
}

import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Icon and header per engine state (10-menubar §2.1, §3; acceptance 1–3)")
struct PresentationTests {
    static let table: [(EngineState, IconKind, String)] = [
        (.stopped, .stopped, "menubar-stopped"),
        (.idleReleased, .stopped, "menubar-stopped"),
        (.starting, .loading, "menubar-loading-01"),
        (.stopping, .loading, "menubar-loading-01"),
        (.crashed, .loading, "menubar-loading-01"),
        (.ready, .ready, "menubar-ready"),
        (.busy, .generating, "menubar-generating-a"),
        (.recovering, .recovering, "menubar-recovering"),
        (.engineFailed, .error, "menubar-error"),
        (.failed, .error, "menubar-error"),
    ]

    @Test(arguments: table)
    func iconPerState(state: EngineState, kind: IconKind, asset: String) {
        #expect(Presentation.iconKind(engine: state, manager: .running) == kind)
        #expect(Presentation.icon(kind: kind, tick: 0, reduceMotion: false).assetName == asset)
    }

    @Test func everyStateIsMapped() {
        let mapped = Set(Self.table.map(\.0))
        for s in EngineState.allCases where s != .unknown { #expect(mapped.contains(s), "\(s)") }
    }

    @Test func nilEngineAndManagerDownAreOffline() {
        #expect(Presentation.iconKind(engine: nil, manager: .running) == .offline)
        #expect(Presentation.iconKind(engine: .ready, manager: .down) == .offline)
        #expect(Presentation.iconKind(engine: nil, manager: .failedToStart("x")) == .offline)
        #expect(StatusIcon.offline.assetName == "menubar-offline")
        #expect(Presentation.iconKind(engine: nil, manager: .starting) == .loading)
    }

    @Test func loadingCyclesTwelveFramesAt10fps() {
        let names = (0..<13).map { Presentation.icon(kind: .loading, tick: $0, reduceMotion: false).assetName }
        #expect(names[0] == "menubar-loading-01")
        #expect(names[11] == "menubar-loading-12")
        #expect(names[12] == "menubar-loading-01")
    }

    @Test func generatingDotBlinksEvery900ms() {
        #expect(Presentation.icon(kind: .generating, tick: 0, reduceMotion: false) == .generating(dotOn: true))
        #expect(Presentation.icon(kind: .generating, tick: 9, reduceMotion: false) == .generating(dotOn: false))
        #expect(Presentation.icon(kind: .generating, tick: 18, reduceMotion: false) == .generating(dotOn: true))
    }

    @Test func reduceMotionHoldsFrameOneAndDot() {
        for tick in [0, 5, 9, 13] {
            #expect(Presentation.icon(kind: .loading, tick: tick, reduceMotion: true).assetName == "menubar-loading-01")
            #expect(Presentation.icon(kind: .generating, tick: tick, reduceMotion: true) == .generating(dotOn: true))
            #expect(Presentation.icon(kind: .recovering, tick: tick, reduceMotion: true) == .recovering(dotOn: true))
        }
    }

    @Test func chipLabels() {
        #expect(Presentation.chipLabel(nil) == "Offline")
        #expect(Presentation.chipLabel(engine("ready")) == "Ready")
        #expect(Presentation.chipLabel(engine("busy")) == "Generating")
        #expect(Presentation.chipLabel(engine("starting", phase: "installing")) == "Preparing")
        #expect(Presentation.chipLabel(engine("starting", phase: "loading")) == "Loading")
        #expect(Presentation.chipLabel(engine("crashed")) == "Restarting")
        #expect(Presentation.chipLabel(engine("idle_released")) == "Idle")
        #expect(Presentation.accessibilityLabel(engine("ready"), manager: .running) == "Splashboard: Ready")
        #expect(Presentation.accessibilityLabel(nil, manager: .down) == "Splashboard: Offline")
    }

    @Test func headers() {
        let gguf = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
        #expect(Presentation.header(engine: engine("ready"), manager: .running, liveTps: nil)
            == "● Splash is running — Qwen3.8-27B-4bit (Ready)")
        #expect(Presentation.header(engine: engine("busy"), manager: .running, liveTps: 74.2)
            == "● Splash is running — Qwen3.8-27B-4bit (Generating · 74 tok/s)")
        #expect(Presentation.header(engine: engine("idle_released", model: gguf), manager: .running, liveTps: nil)
            == "● Splash is running — Qwen3.8-27B-GGUF:UD-Q4_K_M (Idle — weights released)")
        #expect(Presentation.header(engine: engine("stopped", model: nil), manager: .running, liveTps: nil)
            == "○ Splash is idle — no model loaded")
        #expect(Presentation.header(engine: engine("starting", phase: "loading"), manager: .running, liveTps: nil)
            == "◐ Loading Qwen3.8-27B-4bit — Loading weights…")
        #expect(Presentation.header(engine: engine("recovering"), manager: .running, liveTps: nil)
            == "● Splash is running — Qwen3.8-27B-4bit (Recovering)")
        #expect(Presentation.header(engine: engine("engine_failed"), manager: .running, liveTps: nil)
            == "! Splash stopped after repeated failures")
        let refusal = engine("failed", extra: ["error": ["kind": "budget_refusal", "code": "x", "message": "m"]])
        #expect(Presentation.header(engine: refusal, manager: .running, liveTps: nil)
            == "! Qwen3.8-27B-4bit does not fit in memory")
        #expect(Presentation.header(engine: nil, manager: .down, liveTps: nil) == "Splashboard is not running")
        #expect(Presentation.header(engine: nil, manager: .starting, liveTps: nil) == "Starting Splashboard…")
        #expect(Presentation.header(engine: nil, manager: .failedToStart("x"), liveTps: nil) == "Splashboard failed to start")
    }

    @Test func versionAndProgressLines() {
        let e = engine("ready", extra: ["uptime_s": 7500])
        #expect(Presentation.versionLine(engine: e, endpoint: "127.0.0.1:8000") == "Splash 1.2.0 · 127.0.0.1:8000 · up 2 h 05 m")
        #expect(Presentation.progressLine(phase: .loading, download: nil) == "Preparing model ✓ · Loading weights… · Warming up")
        #expect(Presentation.progressLine(phase: .warming, download: nil) == "Preparing model ✓ · Loading weights ✓ · Warming up…")
        let d = DownloadItem(id: "d", model: "m", state: "running", progress: 0.42, speedBps: 84 * 1024 * 1024)
        #expect(Presentation.progressLine(phase: .installing, download: d)
            == "Preparing model (42 % · 84.0 MB/s)… · Loading weights · Warming up")
    }

    // UI QA pass 2026-10-04, bug 4: install progress in the menu.
    @Test func installProgress() {
        let e = engine("starting", phase: "installing", extra: ["install": ["repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "4c0d1e2a9b", "files": 9, "total_bytes": 19930000000, "done_bytes": 8600000000, "speed_bps": 84000000, "eta_s": 135]])
        let i = e.install!
        #expect(Presentation.progressLine(phase: .installing, download: nil, install: i)
            == "Preparing model (43 % · 84.0 MB/s · 2 m 15 s left)… · Loading weights · Warming up")
        #expect(Presentation.installLine(i) == "mlx-community/Qwen3.8-27B-4bit · 9 files · 8.6 GB of 19.9 GB")
        #expect(Presentation.interruptInstall(e).message == "Splash is downloading mlx-community/Qwen3.8-27B-4bit.")
        #expect(engine("starting", phase: "loading", extra: ["install": ["repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "4c0d1e2a9b", "files": 9, "total_bytes": 19930000000, "done_bytes": 8600000000, "speed_bps": 84000000, "eta_s": 135]]).install == nil)
        #expect(Format.duration(45) == "45 s")
        #expect(Format.duration(3725) == "1 h 02 m")
    }
}

@Suite("Title text and gauges (10-menubar §2.2; acceptance 2)")
struct StatusTitleTests {
    func settings(tokps: Bool = false, mem: Bool = false, gpu: Bool = false) -> AppSettings {
        var s = AppSettings()
        s.showTokps = tokps
        s.showMemory = mem
        s.showGPU = gpu
        return s
    }

    @Test func tokpsWhileBusy() {
        let t = StatusTitle.make(settings: settings(tokps: true), engine: engine("busy"), manager: .running,
                                 metrics: nil, lastTps: 74.2, busyEndedAt: nil, now: Date(), gpu: nil, reduceMotion: false)
        #expect(t.tokps == "74 t/s")
        #expect(t.text == "74 t/s")
    }

    @Test func tokpsLingersTwoSecondsAfterBusy() {
        let ended = Date()
        let s = settings(tokps: true)
        let within = StatusTitle.make(settings: s, engine: engine("ready"), manager: .running, metrics: nil, lastTps: 74,
                                      busyEndedAt: ended, now: ended.addingTimeInterval(1.5), gpu: nil, reduceMotion: false)
        #expect(within.tokps == "74 t/s")
        let after = StatusTitle.make(settings: s, engine: engine("ready"), manager: .running, metrics: nil, lastTps: 74,
                                     busyEndedAt: ended, now: ended.addingTimeInterval(2.0), gpu: nil, reduceMotion: false)
        #expect(after.tokps == nil)
        #expect(after.isEmpty)
    }

    @Test func allTogglesOffMeansNoTitle() {
        let m = LiveMetrics(engineState: .busy, decodeTps: 80, memoryCurrentBytes: 10, memoryLimitBytes: 20)
        let t = StatusTitle.make(settings: settings(), engine: engine("busy"), manager: .running, metrics: m,
                                 lastTps: 80, busyEndedAt: nil, now: Date(), gpu: 50, reduceMotion: false)
        #expect(t.isEmpty)
        #expect(t.text == "")
    }

    @Test func memoryGaugeHiddenWhenStopped() {
        let m = LiveMetrics(engineState: .ready, memoryCurrentBytes: 24, memoryLimitBytes: 48)
        let running = StatusTitle.make(settings: settings(mem: true), engine: engine("ready"), manager: .running,
                                       metrics: m, lastTps: nil, busyEndedAt: nil, now: Date(), gpu: nil, reduceMotion: false)
        #expect(running.memoryCells == 3)  // 0.5 × 5 = 2.5 → 3
        #expect(running.text == "MEM ▮▮▮▯▯")
        let stopped = StatusTitle.make(settings: settings(mem: true), engine: engine("stopped"), manager: .running,
                                       metrics: m, lastTps: nil, busyEndedAt: nil, now: Date(), gpu: nil, reduceMotion: false)
        #expect(stopped.memoryCells == nil)
    }

    @Test func gpuGaugeHiddenWhenUnavailable() {
        let none = StatusTitle.make(settings: settings(gpu: true), engine: engine("ready"), manager: .running, metrics: nil,
                                    lastTps: nil, busyEndedAt: nil, now: Date(), gpu: nil, reduceMotion: false)
        #expect(none.gpuCells == nil)
        let some = StatusTitle.make(settings: settings(gpu: true), engine: engine("ready"), manager: .running, metrics: nil,
                                    lastTps: nil, busyEndedAt: nil, now: Date(), gpu: 40, reduceMotion: false)
        #expect(some.gpuCells == 2)
    }

    @Test func orderAndCells() {
        let m = LiveMetrics(engineState: .busy, decodeTps: 210, memoryCurrentBytes: 48, memoryLimitBytes: 48)
        let t = StatusTitle.make(settings: settings(tokps: true, mem: true, gpu: true), engine: engine("busy"),
                                 manager: .running, metrics: m, lastTps: 210.4, busyEndedAt: nil, now: Date(),
                                 gpu: 3, reduceMotion: false)
        #expect(t.text == "210 t/s MEM ▮▮▮▮▮ GPU ▮▯▯▯▯")
        #expect(StatusTitle.cells(0) == 0)
        #expect(StatusTitle.cells(0.01) == 1)
        #expect(StatusTitle.cells(1.5) == 5)
    }

    @Test func reduceMotionLoadingAddsEllipsis() {
        let t = StatusTitle.make(settings: settings(), engine: engine("starting"), manager: .running, metrics: nil,
                                 lastTps: nil, busyEndedAt: nil, now: Date(), gpu: nil, reduceMotion: true)
        #expect(t.loadingEllipsis)
        #expect(t.text == "…")
    }

    @Test func formatting() {
        #expect(Format.tokPerSec(74.25) == "74.2 tok/s" || Format.tokPerSec(74.25) == "74.3 tok/s")
        #expect(Format.tokPerSec(210) == "210 tok/s")
        #expect(Format.tokPerSec(nil) == "—")
        #expect(Format.menuBarTokPerSec(73.6) == "74 t/s")
        #expect(Format.bytes(22_870_000_000) == "21.3 GB")
        #expect(Format.bytes(0) == "0 B")
        #expect(Format.bytes(nil) == "—")
        #expect(Format.integer(1_204_311) == "1,204,311")
        #expect(Format.percent(0.87) == "87 %")
        #expect(Format.shortModelName("unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M") == "Qwen3.8-27B-GGUF:UD-Q4_K_M")
        #expect(Format.truncate(String(repeating: "a", count: 70), to: 60).count == 60)
    }
}

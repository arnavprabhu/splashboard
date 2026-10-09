import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Menu per state (10-menubar §3; acceptance 4, 6)")
struct MenuModelTests {
    static let tail = [
        "---", "Open Admin Panel ⌘O", "Chat ⌘K", "Integrations ▸", "---",
        "Preferences… ⌘,", "Check for Updates…", "About Splashboard", "Quit Splashboard ⌘Q",
    ]
    static let tailDisabled = [
        "---", "[x] Open Admin Panel ⌘O", "[x] Chat ⌘K", "[x] Integrations ▸", "---",
        "[x] Preferences… ⌘,", "Check for Updates…", "About Splashboard", "Quit Splashboard ⌘Q",
    ]
    static let models = [
        InstalledModel(id: "mlx-community/Qwen3.8-27B-4bit", format: "mlx", sizeBytes: 22_870_000_000),
        InstalledModel(id: "mlx-community/Qwen3.6-35B-A3B-4bit", format: "mlx", sizeBytes: 25_769_803_776),
        InstalledModel(id: "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M", format: "gguf", sizeBytes: 17_287_243_366,
                       status: "downloading"),
    ]

    func snap(_ input: MenuInput) -> [String] { MenuModel.build(input).map(\.snapshot) }

    func running(_ e: EngineView, switcher: Bool = false, defaultModel: String? = nil,
                 models: [InstalledModel]? = MenuModelTests.models) -> MenuInput {
        var s = AppSettings()
        s.showSwitcher = switcher
        s.defaultModel = defaultModel
        return MenuInput(manager: .running, engine: e, settings: s, models: models)
    }

    @Test func managerDown() {
        #expect(snap(MenuInput(manager: .down)) == ["[x] Splashboard is not running", "Start Splashboard"] + Self.tailDisabled)
        let prefs = MenuModel.build(MenuInput(manager: .down)).compactMap { entry -> MenuItem? in
            if case .button(let b) = entry, b.title == "Preferences…" { return b }
            return nil
        }.first
        #expect(prefs?.help == "Start Splashboard first")
    }

    @Test func managerStartingAndFailed() {
        #expect(snap(MenuInput(manager: .starting)) == ["[x] Starting Splashboard…", "[x] Starting…"] + Self.tailDisabled)
        #expect(snap(MenuInput(manager: .failedToStart("x")))
            == ["[x] Splashboard failed to start", "Show Log", "Try Again"] + Self.tailDisabled)
    }

    @Test func stoppedShowsStartServerSubmenuEvenWithoutSwitcher() {
        let entries = MenuModel.build(running(engine("stopped", model: nil)))
        #expect(entries.map(\.snapshot) == [
            "[x] ○ Splash is idle — no model loaded", "[x] Splash 1.2.0 · 127.0.0.1:8000",
            "Start Server ▸", "[x] Serving Stats ▸",
        ] + Self.tail)
        let sub = entries[2].submenuEntries!.map(\.snapshot)
        #expect(sub == [
            "mlx-community/Qwen3.8-27B-4bit", "mlx-community/Qwen3.6-35B-A3B-4bit",
            "[x] unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M", "---", "Manage Models…",
        ])
    }

    @Test func stoppedWithDefaultModel() {
        let s = snap(running(engine("stopped", model: nil), switcher: true, defaultModel: "mlx-community/Qwen3.8-27B-4bit"))
        #expect(Array(s[2...4]) == ["Start Server", "Load Model ▸", "[x] Serving Stats ▸"])
        let noSwitcher = snap(running(engine("stopped", model: nil), defaultModel: "mlx-community/Qwen3.8-27B-4bit"))
        #expect(Array(noSwitcher[2...3]) == ["Start Server", "[x] Serving Stats ▸"])
    }

    @Test func stoppedWithoutModelsOffersDownload() {
        let s = snap(running(engine("stopped", model: nil), models: []))
        #expect(s[2] == "Download a Model…")
    }

    @Test func readyReferenceLayout() {
        let s = snap(running(engine("ready", extra: ["uptime_s": 7500]), switcher: true))
        #expect(s == [
            "[x] ● Splash is running — Qwen3.8-27B-4bit (Ready)",
            "[x] Splash 1.2.0 · 127.0.0.1:8000 · up 2 h 05 m",
            "Stop Server", "Load Model ▸", "Serving Stats ▸",
        ] + Self.tail)
        let without = snap(running(engine("ready")))
        #expect(!without.contains("Load Model ▸"))
    }

    @Test func switcherChecksActiveModel() {
        let entries = MenuModel.build(running(engine("ready"), switcher: true))
        let sub = entries[3].submenuEntries!
        #expect(sub.map(\.snapshot) == [
            "[x] ✓ mlx-community/Qwen3.8-27B-4bit", "mlx-community/Qwen3.6-35B-A3B-4bit",
            "[x] unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M", "---", "Unload Model", "---", "Manage Models…",
        ])
        guard case .button(let other) = sub[1], case .button(let gguf) = sub[2] else { Issue.record("rows"); return }
        #expect(other.command == .loadModel("mlx-community/Qwen3.6-35B-A3B-4bit"))
        #expect(other.subtitle == "MLX · 24.0 GB")
        #expect(gguf.subtitle == "Downloading")
    }

    @Test func generatingHeaderUsesLiveTps() {
        var input = running(engine("busy"))
        input.metrics = LiveMetrics(engineState: .busy, decodeTps: 74.2)
        #expect(snap(input)[0] == "[x] ● Splash is running — Qwen3.8-27B-4bit (Generating · 74 tok/s)")
    }

    @Test func loading() {
        let s = snap(running(engine("starting", phase: "loading")))
        #expect(s == [
            "[x] ◐ Loading Qwen3.8-27B-4bit — Loading weights…", "[x] Splash 1.2.0 · 127.0.0.1:8000",
            "[x] Preparing model ✓ · Loading weights… · Warming up", "Cancel Loading", "[x] Serving Stats ▸",
        ] + Self.tail)
    }

    // UI QA pass 2026-10-04, bug 4: the install's progress, size and the can't-resume warning.
    @Test func installing() {
        let e = engine("starting", phase: "installing", extra: ["install": ["repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "4c0d1e2", "files": 9, "total_bytes": 19930000000, "done_bytes": 8600000000, "speed_bps": 84000000, "eta_s": 135]])
        let s = snap(running(e))
        #expect(Array(s[2...5]) == [
            "[x] Preparing model (43 % · 84.0 MB/s · 2 m 15 s left)… · Loading weights · Warming up",
            "[x] mlx-community/Qwen3.8-27B-4bit · 9 files · 8.6 GB of 19.9 GB",
            "[x] Stopping now restarts the file in progress from zero.",
            "Cancel Loading",
        ])
    }

    @Test func recovering() {
        let e = engine("recovering", extra: ["transport": ["recovering": true, "error": "Metal command buffer failed"]])
        let s = snap(running(e))
        #expect(Array(s[0...5]) == [
            "[x] ● Splash is running — Qwen3.8-27B-4bit (Recovering)", "[x] Splash 1.2.0 · 127.0.0.1:8000",
            "[x] Metal command buffer failed", "Stop Server", "Open Logs", "Serving Stats ▸",
        ])
    }

    @Test func failed() {
        let e = engine("engine_failed", extra: ["error": ["kind": "other", "code": "e", "message": "GPU hang"]])
        #expect(snap(running(e)) == [
            "[x] ! Splash stopped after repeated failures", "[x] Splash 1.2.0 · 127.0.0.1:8000",
            "[x] GPU hang", "Restart Engine", "Open Logs", "[x] Serving Stats ▸",
        ] + Self.tail)
        let refusal = engine("failed", extra: ["error": ["kind": "budget_refusal", "code": "b", "message": "needs 40 GB"]])
        #expect(Array(snap(running(refusal))[0...4]) == [
            "[x] ! Qwen3.8-27B-4bit does not fit in memory", "[x] Splash 1.2.0 · 127.0.0.1:8000",
            "[x] needs 40 GB", "Open Memory Settings", "Open Logs",
        ])
    }

    @Test func idleReleased() {
        #expect(snap(running(engine("idle_released")))[0] == "[x] ● Splash is running — Qwen3.8-27B-4bit (Idle — weights released)")
    }

    @Test func servingStatsUsesDashForUnknown() {
        var input = running(engine("ready", inFlight: 2))
        input.metrics = LiveMetrics(json: [
            "engine_state": "ready", "throughput": ["decode_tps": 74.2],
            "latency": ["ttft_p50_ms": 310, "ttft_p95_ms": 920], "cache": ["hit_rate": 0.87],
            "memory": ["current_bytes": 24_803_628_236, "limit_bytes": 51_539_607_552, "system_pressure": "normal"],
            "totals": ["requests_completed": 1204, "requests_failed": 3],
        ])
        let stats = MenuModel.build(input).first { $0.snapshot == "Serving Stats ▸" }!.submenuEntries!.map(\.snapshot)
        #expect(stats == [
            "[x] Decode  74.2 tok/s", "[x] TTFT p50 / p95  0.31 s / 0.92 s", "[x] Tokens today  —",
            "[x] Cache hit rate  87 %", "[x] Memory  23.1 GB of 48.0 GB · pressure normal",
            "[x] Requests  1,204 completed · 3 failed · 2 in flight", "---", "Open Status Page…",
        ])
    }

    @Test func integrationsSubmenu() {
        var input = running(engine("ready"))
        input.integrations = IntegrationsView(
            cli: [CliIntegration(name: "claude", label: "Claude Code", installed: true),
                  CliIntegration(name: "codex", label: "Codex CLI", installed: true),
                  CliIntegration(name: "pi", label: "Pi", installed: false)],
            desktop: [DesktopIntegration(name: "claude-desktop", label: "Claude Desktop", detected: true, state: "connected"),
                      DesktopIntegration(name: "codex-app", label: "Codex app", detected: true, state: "not_connected")])
        let sub = MenuModel.build(input).first { $0.snapshot == "Integrations ▸" }!.submenuEntries!
        #expect(sub.map(\.snapshot) == [
            "[x] Claude Desktop — Connected", "Disconnect Claude Desktop",
            "[x] Codex app — Not connected", "Connect Codex app…", "---",
            "Launch Claude Code in Terminal", "Launch Codex CLI in Terminal", "---", "Open Integrations Page…",
        ])
        guard case .button(let connect) = sub[3] else { Issue.record("row"); return }
        #expect(connect.command == .connectIntegration("codex-app"))
    }

    @Test func extraHeaderLines() {
        var input = running(engine("ready"))
        input.notificationsDenied = true
        input.setupUnfinished = true
        let s = snap(input)
        #expect(Array(s[0...4]) == [
            "[x] Setup not finished", "Continue Setup…",
            "[x] Notifications are off — enable them in System Settings", "Open Notification Settings", "---",
        ])
    }

    @Test func quitAlternateFlipsStopOnQuit() {
        func quitItem(_ stop: Bool) -> MenuItem? {
            var s = AppSettings()
            s.stopOnQuit = stop
            return MenuModel.build(MenuInput(manager: .down, settings: s)).compactMap { e -> MenuItem? in
                if case .button(let b) = e, b.title == "Quit Splashboard" { return b }
                return nil
            }.first
        }
        #expect(quitItem(true)?.command == .quit(stopServer: true))
        #expect(quitItem(true)?.alternate?.title == "Quit and Keep Running")
        #expect(quitItem(true)?.alternate?.command == .quit(stopServer: false))
        #expect(quitItem(false)?.command == .quit(stopServer: false))
        #expect(quitItem(false)?.alternate?.title == "Quit and Stop Server")
    }
}

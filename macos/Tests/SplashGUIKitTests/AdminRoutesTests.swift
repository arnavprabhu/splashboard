import Foundation
import Testing
@testable import SplashGUIKit

/// Every admin page the app opens must be a route the web admin has (web/src/routes.tsx,
/// web/src/routes/tabs.ts SETTINGS_SECTIONS). A `#fragment` on /admin/settings used to be
/// dropped by the redirect to /admin/settings/server, so settings links name the section.
@Suite("Admin links point at real web routes")
struct AdminRoutesTests {
    static let settingsSlugs: Set<String> = [
        "server", "security", "storage", "memory", "cache", "performance", "requests", "sampling", "routing",
        "hf", "chat", "lifecycle", "menubar", "notifications", "data", "advanced", "about",
    ]
    static let pages: Set<String> = [
        "/admin", "/admin/status", "/admin/status/history", "/admin/models", "/admin/models/downloader", "/admin/chat",
        "/admin/tools/playground", "/admin/tools/tokenizer", "/admin/tools/judgments", "/admin/tools/benchmark",
        "/admin/integrations", "/admin/logs", "/admin/logs/diagnostics", "/admin/settings", "/admin/welcome",
    ]

    static func isRoute(_ href: String) -> Bool {
        let path = String(href.split(separator: "?").first ?? "").split(separator: "#").first.map(String.init) ?? ""
        if pages.contains(path) { return !(path == "/admin/settings" && href.contains("#")) }
        if path.hasPrefix("/admin/settings/") { return settingsSlugs.contains(String(path.dropFirst("/admin/settings/".count))) }
        return false
    }

    @Test func notificationTargetsAreRoutes() {
        let kinds = [
            "download_done", "download_failed", "engine_failed", "crash_loop", "update_available",
            "unclean_integration_shutdown", "capacity_exhausted", "resource_timeout", "queue_full",
            "memory_critical", "engine_recovering", "disk_tier_failures", "write_behind_refused", "benchmark_done",
        ]
        for kind in kinds {
            let path = NotificationMapper.defaultPath(for: kind)
            #expect(Self.isRoute(path), "\(kind) → \(path)")
        }
    }

    @Test func menuLinksAreRoutes() {
        func collect(_ entries: [MenuEntry]) -> [String] {
            entries.flatMap { e -> [String] in
                switch e {
                case .button(let b):
                    if case .openAdmin(let p) = b.command { return [p] }
                    return []
                case .submenu(_, _, let children): return collect(children)
                default: return []
                }
            }
        }
        let failedFit = EngineView(json: [
            "state": "failed", "model": "mlx-community/Qwen3.8-27B-4bit",
            "error": ["kind": "budget_refusal", "code": "budget", "message": "does not fit"],
        ])
        let inputs: [MenuInput] = [
            MenuInput(manager: .running, engine: EngineView(json: ["state": "stopped"]), models: []),
            MenuInput(manager: .running, engine: EngineView(json: ["state": "ready", "model": "a/b"])),
            MenuInput(manager: .running, engine: failedFit),
        ]
        for input in inputs {
            for path in collect(MenuModel.build(input)) {
                #expect(Self.isRoute(path), "\(path)")
            }
        }
    }

    /// The two lists above are copies; keep them honest against the web sources so a renamed or
    /// removed web route fails here instead of passing against a stale list.
    @Test func listsMatchTheWebSources() throws {
        let web = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("web/src")
        let routes = try String(contentsOf: web.appendingPathComponent("routes.tsx"), encoding: .utf8)
        let tabs = try String(contentsOf: web.appendingPathComponent("routes/tabs.ts"), encoding: .utf8)
        func captures(_ pattern: String, in text: String) throws -> Set<String> {
            let re = try NSRegularExpression(pattern: pattern)
            let range = NSRange(text.startIndex..., in: text)
            return Set(re.matches(in: text, range: range).compactMap { m in
                Range(m.range(at: 1), in: text).map { String(text[$0]) }
            })
        }
        let webPaths = try captures(#"\{\s*path:\s*'([^']+)'"#, in: routes)
            .map { $0.replacingOccurrences(of: "/:cid?", with: "") }
        let webSlugs = try captures(#"slug:\s*'([^']+)'"#, in: tabs)
        #expect(!webPaths.isEmpty && !webSlugs.isEmpty)
        #expect(webSlugs == Self.settingsSlugs)
        for page in Self.pages where page != "/admin" {
            #expect(webPaths.contains(String(page.dropFirst("/admin".count))), "\(page) is not in web/src/routes.tsx")
        }
    }

    @Test func guardRejectsFragmentOnSettingsRoot() {
        #expect(!Self.isRoute("/admin/settings#about"))
        #expect(Self.isRoute("/admin/settings/about#licenses"))
        #expect(!Self.isRoute("/admin/settings/nope"))
    }
}

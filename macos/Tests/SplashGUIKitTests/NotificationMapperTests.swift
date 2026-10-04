import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Notification mapping (10-menubar §4; acceptance 7–8)")
struct NotificationMapperTests {
    static let loadNow: JSONValue = [
        "id": "n1", "kind": "download_done", "title": "Qwen3.8-27B-4bit downloaded",
        "body": "21.3 GB · verified. Load it now?", "ts": "2026-10-03T19:24:00+00:00",
        "actions": [
            ["id": "load", "label": "Load now", "method": "POST", "path": "/api/admin/engine/load",
             "body": ["model": "mlx-community/Qwen3.8-27B-4bit"]],
            ["id": "show", "label": "Show in Models", "method": "GET", "path": "/admin/models", "body": nil],
        ],
    ]

    @Test func downloadDone() throws {
        let n = try #require(NotificationPayload(json: Self.loadNow))
        let m = try #require(NotificationMapper.map(n, settings: AppSettings()))
        #expect(m.identifier == "n1")
        #expect(m.title == "Qwen3.8-27B-4bit downloaded")
        #expect(m.body == "21.3 GB · verified. Load it now?")
        #expect(m.categoryIdentifier == "splashgui.download_done.load+show")
        #expect(m.threadIdentifier == "splashgui.download_done")
        #expect(m.interruption == .active)
        #expect(m.actions.map(\.identifier) == ["load", "show"])
        #expect(m.actions.map(\.title) == ["Load now", "Show in Models"])
        #expect(m.openPath == "/admin/models")
    }

    @Test func toggledOffKindProducesNothing() throws {
        var s = AppSettings()
        s.notifications["download_done"] = false
        let n = try #require(NotificationPayload(json: Self.loadNow))
        #expect(NotificationMapper.map(n, settings: s) == nil)
    }

    @Test func diskTierUsesDiskCacheErrorsToggle() {
        var s = AppSettings()
        s.notifications["disk_cache_errors"] = false
        let n = NotificationPayload(id: "d", kind: "disk_tier_failures", title: "SSD cache write failed", body: "x")
        #expect(NotificationMapper.map(n, settings: s) == nil)
    }

    @Test func untoggledKindsAlwaysDelivered() {
        var s = AppSettings()
        for k in s.notifications.keys { s.notifications[k] = false }
        for kind in ["download_failed", "crash_loop", "queue_full", "unclean_integration_shutdown", "info"] {
            let n = NotificationPayload(id: kind, kind: kind, title: "T", body: "B")
            #expect(NotificationMapper.map(n, settings: s) != nil, "\(kind)")
        }
    }

    @Test func criticalKindsAreTimeSensitive() {
        for kind in ["engine_failed", "crash_loop", "memory_critical"] {
            let m = NotificationMapper.map(NotificationPayload(id: "c", kind: kind, title: "T", body: "B"), settings: AppSettings())
            #expect(m?.interruption == .timeSensitive)
        }
    }

    @Test func criticalKindsBypassTogglesByDefault() {
        var s = AppSettings()
        for k in s.notifications.keys { s.notifications[k] = false }
        for kind in ["engine_failed", "crash_loop", "memory_critical"] {
            let n = NotificationPayload(id: kind, kind: kind, title: "T", body: "B")
            #expect(NotificationMapper.map(n, settings: s) != nil, "\(kind)")
        }
        let failed = NotificationPayload(id: "e", kind: "engine_failed", title: "T", body: "")
        #expect(NotificationMapper.map(failed, settings: s, criticalBypassesToggle: false) == nil)
        // Non-critical toggled kinds are still suppressed.
        let update = NotificationPayload(id: "u", kind: "update_available", title: "T", body: "")
        #expect(NotificationMapper.map(update, settings: s) == nil)
    }

    @Test func hrefOverridesDefaultPath() {
        let n = NotificationPayload(id: "x", kind: "info", title: "T", body: "", href: "/admin/tools/benchmark")
        #expect(NotificationMapper.map(n, settings: AppSettings())?.openPath == "/admin/tools/benchmark")
    }

    @Test func resolveResponses() throws {
        let n = try #require(NotificationPayload(json: Self.loadNow))
        let info = try #require(NotificationMapper.map(n, settings: AppSettings())).userInfo
        #expect(NotificationMapper.resolve(actionIdentifier: NotificationMapper.defaultActionIdentifier, userInfo: info)
            == .openPage("/admin/models"))
        #expect(NotificationMapper.resolve(actionIdentifier: NotificationMapper.dismissActionIdentifier, userInfo: info) == .none)
        #expect(NotificationMapper.resolve(actionIdentifier: "load", userInfo: info) == .call(NotificationAction(
            id: "load", label: "Load now", method: "POST", path: "/api/admin/engine/load",
            body: ["model": "mlx-community/Qwen3.8-27B-4bit"])))
        #expect(NotificationMapper.resolve(actionIdentifier: "show", userInfo: info) == .openPage("/admin/models"))
        #expect(NotificationMapper.resolve(actionIdentifier: "nope", userInfo: info) == .none)
    }

    @Test func failureFollowUp() {
        let a = NotificationAction(id: "restart", label: "Restart engine", path: "/api/admin/engine/restart")
        let m = NotificationMapper.failureNotification(for: a, error: .http(status: 409, code: "model_switch_busy", message: "Busy"))
        #expect(m.title == "Couldn't restart engine")
        #expect(m.body == "Busy")
    }

    @Test func toleratesMissingFields() {
        #expect(NotificationPayload(json: ["kind": "info"]) == nil)
        let n = NotificationPayload(json: ["title": "Hi", "actions": [["label": "no id"], ["id": "a", "path": "/x"]]])
        #expect(n?.kind == "info")
        #expect(n?.actions.map(\.id) == ["a"])
        #expect(n?.actions.first?.method == "POST")
    }
}

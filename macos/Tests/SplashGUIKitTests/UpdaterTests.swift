import Foundation
import Testing
@testable import SplashGUIKit

@MainActor
final class FakeUpdater: Updater {
    var canCheckForUpdates = true
    var checks = 0
    func checkForUpdates() { checks += 1 }
}

@MainActor
@Suite("Check for Updates runs Sparkle (PKG-9)")
struct UpdaterTests {
    func make(_ updater: Updater?) -> (MenuBarViewModel, MockHost, MockAPI) {
        let api = MockAPI()
        api.set("POST", "/api/admin/auth/link", .success(LoginLinkTests.minted))
        let vm = MenuBarViewModel(api: api, settings: AppSettings(), paths: HomePaths(base: URL(fileURLWithPath: "/tmp/sg")),
                                  notifier: MockNotifier(), gpu: StubGPU(value: nil))
        let host = MockHost()
        vm.host = host
        vm.updater = updater
        return (vm, host, api)
    }

    @Test func theMenuItemRunsTheUpdaterAndOpensNothing() async {
        let updater = FakeUpdater()
        let (vm, host, api) = make(updater)
        await vm.perform(.checkForUpdates)
        #expect(updater.checks == 1)
        #expect(host.opened.isEmpty && host.aboutShown == 0)
        #expect(!api.posted("/api/admin/auth/link"))
    }

    @Test func theWebAboutEventRunsTheUpdater() async {
        let updater = FakeUpdater()
        let (vm, _, _) = make(updater)
        await vm.handle(SSEEvent(event: "app.check_updates", data: "{}"))
        #expect(updater.checks == 1)
    }

    @Test func aCheckAlreadyShowingIsNotStartedAgain() async {
        let updater = FakeUpdater()
        updater.canCheckForUpdates = false
        let (vm, host, _) = make(updater)
        await vm.perform(.checkForUpdates)
        await vm.handle(SSEEvent(event: "app.check_updates", data: "{}"))
        #expect(updater.checks == 0)
        #expect(host.opened.isEmpty && host.aboutShown == 0)
    }

    @Test func withoutAnUpdaterTheManagerlessMenuShowsAbout() async {
        let (vm, host, _) = make(nil)
        await vm.perform(.checkForUpdates)
        #expect(host.aboutShown == 1)
        await vm.handle(SSEEvent(event: "app.check_updates", data: "{}"))
        #expect(host.aboutShown == 2)
    }

    @Test func withoutAnUpdaterARunningManagerOpensSettingsAbout() async {
        let (vm, host, api) = make(nil)
        api.set("GET", "/api/admin/engine", .success(engineJSON("ready")))
        await vm.refreshEngine()
        await vm.perform(.checkForUpdates)
        #expect(host.opened.map(\.absoluteString)
            == ["http://127.0.0.1:8000/admin/login?code=abc123&next=%2Fadmin%2Fsettings%2Fabout"])
    }
}

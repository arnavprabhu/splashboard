import Foundation
import Testing
@testable import SplashGUIKit

@MainActor
@Suite("Remove Splashboard Data…")
struct RemoveDataTests {
    static let plan: JSONValue = [
        "home": "/Users/u/.splash", "data_bytes": 2048, "models_bytes": 61_200_000_000, "cache_bytes": 9_600_000_000,
        "items": [["path": "/Volumes/big/hf", "kind": "models", "bytes": 1, "deletable": false]],
        "steps": ["Restore Claude Desktop and the Codex app", "Stop the manager"],
    ]
    let paths = HomePaths(base: FileManager.default.temporaryDirectory.appendingPathComponent("sg-\(UUID().uuidString)"))
    let repo = RepoLocator(
        environment: ["SPLASH_GUI_REPO": "/r", "PATH": "/bin"], defaultsValue: nil, infoPlistValue: nil,
        executableURL: nil, home: URL(fileURLWithPath: "/Users/x"),
        fileExists: { ["/r/manager/pyproject.toml", "/opt/homebrew/bin/uv"].contains($0) })

    func make(agent: MockService?, mainApp: MockService) -> (MenuBarViewModel, MockHost, MockAPI) {
        let api = MockAPI()
        api.set("POST", "/api/admin/uninstall/plan", .success(Self.plan))
        api.set("POST", "/api/admin/uninstall", .success(["deleted": [], "kept": []]))
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: agent, launcher: MockLauncher(), sleep: { _ in })
        let vm = MenuBarViewModel(api: api, settings: AppSettings(), paths: paths, notifier: MockNotifier(),
                                  managerController: mc, loginItems: LoginItemController(mainApp: mainApp),
                                  gpu: StubGPU(value: nil))
        let host = MockHost()
        vm.host = host
        return (vm, host, api)
    }

    func body(_ api: MockAPI) -> JSONValue? {
        api.calls.value.first { $0.method == "POST" && $0.path == "/api/admin/uninstall" }?.body
    }

    @Test func thePlanReachesTheSheetWithSizesAndKeptFolders() async {
        let (vm, host, _) = make(agent: nil, mainApp: MockService())
        await vm.removeData()
        #expect(host.removalPlans == [UninstallSummary(
            home: "/Users/u/.splash", dataBytes: 2048, modelsBytes: 61_200_000_000, cacheBytes: 9_600_000_000,
            kept: ["/Volumes/big/hf"], steps: ["Restore Claude Desktop and the Codex app", "Stop the manager"])])
    }

    @Test func cancelRemovesNothing() async {
        let agent = MockService(.enabled)
        let (vm, host, api) = make(agent: agent, mainApp: MockService(.enabled))
        host.removalChoice = nil
        await vm.removeData()
        #expect(body(api) == nil)
        #expect(agent.unregisterCount.value == 0 && !host.terminated)
    }

    @Test func removeAsksTheManagerThenUnregistersTheLoginItemAndAgentAndQuits() async {
        let agent = MockService(.enabled)
        let mainApp = MockService(.enabled)
        let (vm, host, api) = make(agent: agent, mainApp: mainApp)
        host.removalChoice = RemovalChoice(deleteModels: false, deleteCache: true)
        await vm.removeData()
        #expect(body(api) == ["delete_data": true, "delete_models": false, "delete_cache": true, "stop": false])
        #expect(mainApp.unregisterCount.value == 1)
        #expect(agent.unregisterCount.value == 1)
        #expect(!api.posted("/api/admin/shutdown"), "unregistering the agent stops the manager")
        #expect(host.terminated)
    }

    @Test func aManagerTheAppDidNotStartIsStoppedThroughTheAPI() async {
        let (vm, host, api) = make(agent: MockService(.notRegistered), mainApp: MockService())
        host.removalChoice = RemovalChoice(deleteModels: false, deleteCache: false)
        await vm.removeData()
        #expect(api.posted("/api/admin/shutdown"))
        #expect(host.terminated)
    }

    @Test func aFailedRemovalKeepsTheLoginItemAgentAndApp() async {
        let agent = MockService(.enabled)
        let mainApp = MockService(.enabled)
        let (vm, host, api) = make(agent: agent, mainApp: mainApp)
        api.set("POST", "/api/admin/uninstall",
                .failure(.http(status: 409, code: "restore_incomplete", message: "Could not restore codex-app")))
        host.removalChoice = RemovalChoice(deleteModels: false, deleteCache: false)
        await vm.removeData()
        #expect(mainApp.unregisterCount.value == 0 && agent.unregisterCount.value == 0)
        #expect(!host.terminated && host.errors.count == 1)
    }

    @Test func aFailedAgentUnregisterStopsTheManagerAndKeepsTheAppOpen() async {
        let agent = MockService(.enabled)
        agent.failUnregister = true
        let (vm, host, api) = make(agent: agent, mainApp: MockService(.enabled))
        host.removalChoice = RemovalChoice(deleteModels: false, deleteCache: false)
        await vm.removeData()
        #expect(api.posted("/api/admin/shutdown"), "the agent is still registered, so the manager is stopped explicitly")
        #expect(!host.terminated)
        #expect(host.errors.count == 1 && host.errors[0].contains("background item"))
    }

    @Test func aFailedLoginItemUnregisterKeepsTheAppOpen() async {
        let mainApp = MockService(.enabled)
        mainApp.failUnregister = true
        let agent = MockService(.enabled)
        let (vm, host, _) = make(agent: agent, mainApp: mainApp)
        host.removalChoice = RemovalChoice(deleteModels: false, deleteCache: false)
        await vm.removeData()
        #expect(agent.unregisterCount.value == 1, "the agent is still removed")
        #expect(!host.terminated)
        #expect(host.errors.count == 1 && host.errors[0].contains("login item"))
    }
}

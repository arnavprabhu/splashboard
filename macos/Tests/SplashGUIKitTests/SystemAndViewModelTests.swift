import Foundation
import Testing
@testable import SplashGUIKit

/// The interpreter a packaged app carries (PKG-4), as a path under a fake `.app`.
private let packagedPython = "/Applications/Test.app/Contents/Resources/manager/python/bin/python3"

@Suite("SMAppService wrappers (mocked)")
struct LoginItemTests {
    @Test func registersWhenEnabled() {
        let svc = MockService(.notRegistered)
        let out = LoginItemController(mainApp: svc).apply(launchAtLogin: true)
        #expect(svc.registerCount.value == 1)
        #expect(out.status == .enabled)
        #expect(out.error == nil)
    }

    @Test func idempotent() {
        let svc = MockService(.enabled)
        LoginItemController(mainApp: svc).apply(launchAtLogin: true)
        #expect(svc.registerCount.value == 0)
        let pending = MockService(.requiresApproval)
        LoginItemController(mainApp: pending).apply(launchAtLogin: true)
        #expect(pending.registerCount.value == 0)
    }

    @Test func unregistersWhenDisabled() {
        let svc = MockService(.enabled)
        let out = LoginItemController(mainApp: svc).apply(launchAtLogin: false)
        #expect(svc.unregisterCount.value == 1)
        #expect(out.status == .notRegistered)
        let off = MockService(.notRegistered)
        LoginItemController(mainApp: off).apply(launchAtLogin: false)
        #expect(off.unregisterCount.value == 0)
    }

    @Test func reportsErrors() {
        let svc = MockService(.notRegistered)
        svc.failRegister = true
        #expect(LoginItemController(mainApp: svc).apply(launchAtLogin: true).error != nil)
    }
}

@MainActor
@Suite("Manager detection and start")
struct ManagerControllerTests {
    let paths = HomePaths(base: FileManager.default.temporaryDirectory.appendingPathComponent("sg-\(UUID().uuidString)"))
    let repo = RepoLocator(
        environment: ["SPLASH_GUI_REPO": "/r", "PATH": "/bin"], defaultsValue: nil, infoPlistValue: nil,
        executableURL: nil, home: URL(fileURLWithPath: "/Users/x"),
        fileExists: { ["/r/manager/pyproject.toml", "/opt/homebrew/bin/uv"].contains($0) })

    @Test func alreadyRunningIsExternal() async {
        let api = MockAPI()
        let launcher = MockLauncher()
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: nil, launcher: launcher, sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.external))
        #expect(launcher.launches.value.isEmpty)
        await mc.stopOwned()
        #expect(!launcher.process.interrupted.value)
    }

    @Test func spawnsChildWithUvWhenDown() async {
        let api = MockAPI()
        api.healthy.value = false
        let launcher = MockLauncher()
        launcher.onLaunch = { api.healthy.value = true }
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: nil, launcher: launcher, sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.child))
        #expect(launcher.launches.value == [["/opt/homebrew/bin/uv", "run", "--project", "/r/manager", "splash-gui-manager"]])
        await mc.stopOwned()
        #expect(launcher.process.interrupted.value)
        #expect(mc.ownership == nil)
    }

    @Test func usesAgentInBundleMode() async {
        let api = MockAPI()
        api.healthy.value = false
        let agent = MockService(.notRegistered)
        let launcher = MockLauncher()
        // Registering the agent makes launchd start the manager.
        final class Hook: AppService, @unchecked Sendable {
            let inner: MockService
            let api: MockAPI
            init(_ i: MockService, _ a: MockAPI) { inner = i; api = a }
            var status: AppServiceStatus { inner.status }
            func register() throws { try inner.register(); api.healthy.value = true }
            func unregister() throws { try inner.unregister() }
        }
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: Hook(agent, api), launcher: launcher,
                                   sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.agent))
        #expect(agent.registerCount.value == 1)
        #expect(launcher.launches.value.isEmpty)
        await mc.stopOwned()
        #expect(agent.unregisterCount.value == 1)
    }

    @Test func fallsBackToChildWhenAgentNeedsApproval() async {
        let api = MockAPI()
        api.healthy.value = false
        let agent = MockService(.notRegistered)
        agent.statusAfterRegister = .requiresApproval
        let launcher = MockLauncher()
        launcher.onLaunch = { api.healthy.value = true }
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: agent, launcher: launcher, sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.child))
    }

    @Test func anotherServerOnThePortIsNotTheManager() async {
        let api = MockAPI()
        api.probeOverride.value = .foreign
        let agent = MockService(.notRegistered)
        let launcher = MockLauncher()
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: agent, launcher: launcher, sleep: { _ in })
        let result = await mc.ensureRunning()
        #expect(result == .failure(.portTaken))
        #expect(launcher.launches.value.isEmpty)
        #expect(agent.registerCount.value == 0)
        #expect(mc.ownership == nil)
        if case .failure(let error) = result { #expect(error.description.contains("not Splash GUI")) }
    }

    @Test func healthClassification() {
        let ours = Data(#"{"status":"ok","service":"splash-gui-manager","version":"0.1.0"}"#.utf8)
        #expect(HTTPAdminAPI.classifyHealth(status: 200, data: ours) == .manager)
        // oMLX and other servers answer /health too.
        #expect(HTTPAdminAPI.classifyHealth(status: 200, data: Data(#"{"status":"ok"}"#.utf8)) == .foreign)
        #expect(HTTPAdminAPI.classifyHealth(status: 200, data: Data("OK".utf8)) == .foreign)
        #expect(HTTPAdminAPI.classifyHealth(status: 404, data: ours) == .foreign)
    }

    @Test func reportsMissingUv() async {
        let api = MockAPI()
        api.healthy.value = false
        let noUv = RepoLocator(environment: [:], defaultsValue: nil, infoPlistValue: nil, executableURL: nil,
                               home: URL(fileURLWithPath: "/nowhere"), fileExists: { _ in false })
        let mc = ManagerController(api: api, paths: paths, repo: noUv, agent: nil, launcher: MockLauncher(), sleep: { _ in })
        #expect(await mc.ensureRunning() == .failure(.uvMissing))
    }

    @Test func bundledRuntimeBeatsUvAndTheRepo() async {
        // PKG-4: a packaged app starts the manager with its own interpreter; uv and the repo are not consulted.
        let api = MockAPI()
        api.healthy.value = false
        let launcher = MockLauncher()
        launcher.onLaunch = { api.healthy.value = true }
        let bundled = BundledRuntime(resources: URL(fileURLWithPath: "/Applications/Test.app/Contents/Resources"),
                                     fileExists: { $0 == packagedPython })
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: nil, bundled: bundled,
                                   launcher: launcher, sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.child))
        #expect(launcher.launches.value == [[packagedPython, "-I", "-B", "-m", "splash_gui.manager"]])
        await mc.stopOwned()
    }

    @Test func bundledRuntimeWorksWithoutUvOrRepo() async {
        let api = MockAPI()
        api.healthy.value = false
        let launcher = MockLauncher()
        launcher.onLaunch = { api.healthy.value = true }
        let noUv = RepoLocator(environment: [:], defaultsValue: nil, infoPlistValue: nil, executableURL: nil,
                               home: URL(fileURLWithPath: "/nowhere"), fileExists: { _ in false })
        let bundled = BundledRuntime(resources: URL(fileURLWithPath: "/Applications/Test.app/Contents/Resources"),
                                     fileExists: { $0 == packagedPython })
        let mc = ManagerController(api: api, paths: paths, repo: noUv, agent: nil, bundled: bundled,
                                   launcher: launcher, sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.child))
        await mc.stopOwned()
    }

    @Test func noBundledRuntimeKeepsTheDevelopmentPath() async {
        // A development bundle has no Resources/manager: the same uv command as before PKG-4.
        let api = MockAPI()
        api.healthy.value = false
        let launcher = MockLauncher()
        launcher.onLaunch = { api.healthy.value = true }
        let bundled = BundledRuntime(resources: URL(fileURLWithPath: "/Applications/Test.app/Contents/Resources"),
                                     fileExists: { _ in false })
        #expect(bundled.python == nil)
        let mc = ManagerController(api: api, paths: paths, repo: repo, agent: nil, bundled: bundled,
                                   launcher: launcher, sleep: { _ in })
        #expect(await mc.ensureRunning() == .success(.child))
        #expect(launcher.launches.value == [["/opt/homebrew/bin/uv", "run", "--project", "/r/manager", "splash-gui-manager"]])
        await mc.stopOwned()
    }
}

@MainActor
final class StopRecorder: ManagerStopping {
    var stopped = 0
    func stopOwned() async { stopped += 1 }
}

@MainActor
@Suite("Quit flow honours lifecycle.stop_on_quit (acceptance 9)")
struct QuitTests {
    @Test func stopOnQuitStopsEngineRestoresAndStopsManager() async {
        let api = MockAPI()
        let manager = StopRecorder()
        let q = QuitCoordinator(api: api, manager: manager) { _ in true }
        #expect(await q.quit(stopServer: true, requestsInFlight: 0) == .terminate(stoppedServer: true))
        #expect(api.mutating.map(\.path) == ["/api/admin/engine/stop", "/api/admin/integrations/restore-all"])
        #expect(manager.stopped == 1)
        #expect(q.steps == [.engineStopped, .integrationsRestored, .managerStopped])
    }

    @Test func keepRunningPostsNothing() async {
        let api = MockAPI()
        let manager = StopRecorder()
        let q = QuitCoordinator(api: api, manager: manager) { _ in true }
        #expect(await q.quit(stopServer: false, requestsInFlight: 3) == .terminate(stoppedServer: false))
        #expect(api.calls.value.isEmpty)
        #expect(manager.stopped == 0)
    }

    @Test func inFlightAsksAndCanCancel() async {
        let api = MockAPI(engine: engineJSON("busy", inFlight: 3))
        var asked: [Int] = []
        let q = QuitCoordinator(api: api, manager: nil) { n in asked.append(n); return false }
        #expect(await q.quit(stopServer: true, requestsInFlight: 0) == .cancelled)
        #expect(asked == [3])
        #expect(api.mutating.isEmpty)
        #expect(QuitCoordinator.confirmationText(inFlight: 3).message == "3 requests are running.")
    }

    @Test func stubRestoreIsIgnored() async {
        let api = MockAPI()
        api.set("POST", "/api/admin/integrations/restore-all", .failure(.http(status: 501, code: "not_implemented", message: "x")))
        let q = QuitCoordinator(api: api, manager: nil) { _ in true }
        #expect(await q.quit(stopServer: true, requestsInFlight: 0) == .terminate(stoppedServer: true))
        #expect(q.steps == [.engineStopped])
    }
}

@MainActor
@Suite("View model")
struct ViewModelTests {
    func make(api: MockAPI = MockAPI(), settings: AppSettings = AppSettings(), notifier: MockNotifier = MockNotifier())
        -> (MenuBarViewModel, MockHost)
    {
        let vm = MenuBarViewModel(api: api, settings: settings, paths: HomePaths(base: URL(fileURLWithPath: "/tmp/sg")),
                                  notifier: notifier, gpu: StubGPU(value: nil))
        let host = MockHost()
        vm.host = host
        return (vm, host)
    }

    @Test func engineEventsDriveIcon() async {
        let (vm, _) = make()
        #expect(vm.iconKind == .offline)
        await vm.refreshEngine()
        #expect(vm.manager == .running)
        #expect(vm.iconKind == .stopped)
        await vm.handle(SSEEvent(event: "engine.state", data: engineJSON("busy").serializedString()))
        #expect(vm.iconKind == .generating)
        await vm.handle(SSEEvent(event: "engine.state", data: engineJSON("ready").serializedString()))
        #expect(vm.iconKind == .ready)
        #expect(vm.busyEndedAt != nil)
        await vm.handle(SSEEvent(event: "something.new", data: "{}"))
        #expect(vm.iconKind == .ready)
        vm.stop()
    }

    @Test func tokpsTitleFromLiveMetrics() async {
        var s = AppSettings()
        s.showTokps = true
        let (vm, _) = make(settings: s)
        await vm.refreshEngine()
        vm.applyEngine(engine("busy"))
        vm.applyMetrics(LiveMetrics(json: ["engine_state": "busy", "throughput": ["decode_tps": 74.4]]))
        #expect(vm.title.tokps == "74 t/s")
        #expect(vm.needsLiveMetrics)
        vm.stop()
    }

    @Test func notificationEventsHonourToggles() async {
        let notifier = MockNotifier()
        var s = AppSettings()
        s.notifications["update_available"] = false
        let (vm, _) = make(settings: s, notifier: notifier)
        let done: JSONValue = ["id": "1", "kind": "download_done", "title": "X downloaded", "body": "", "actions": []]
        let update: JSONValue = ["id": "2", "kind": "update_available", "title": "Splash 1.2.1 available", "body": ""]
        await vm.handle(SSEEvent(event: "notification", data: done.serializedString()))
        await vm.handle(SSEEvent(event: "notification", data: update.serializedString()))
        #expect(notifier.posted.value.map(\.identifier) == ["1"])
    }

    @Test func settingsChangedRefetches() async {
        let api = MockAPI()
        api.set("GET", "/api/admin/settings", .success(["settings": ["global": ["menubar": ["show_switcher": true]]]]))
        let (vm, _) = make(api: api)
        await vm.handle(SSEEvent(event: "settings.changed", data: #"{"changed":[],"restart_required":false}"#))
        #expect(vm.settings.showSwitcher)
    }

    @Test func stopServerWithRequestsInFlightAsksFirst() async {
        let api = MockAPI(engine: engineJSON("busy", inFlight: 3))
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        host.confirmAnswer = false
        await vm.perform(.stopServer)
        #expect(host.confirms.first?.message == "3 requests are running.")
        #expect(host.confirms.first?.info == "Stop the engine anyway? Clients will get an error.")
        #expect(!api.posted("/api/admin/engine/stop"))
        host.confirmAnswer = true
        await vm.perform(.stopServer)
        #expect(api.posted("/api/admin/engine/stop"))
    }

    @Test func stopServerWithoutRequestsStopsDirectly() async {
        let api = MockAPI(engine: engineJSON("ready"))
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.stopServer)
        #expect(host.confirms.isEmpty)
        #expect(api.posted("/api/admin/engine/stop"))
    }

    @Test func loadModelPostsFullID() async {
        let api = MockAPI(engine: engineJSON("ready"))
        api.set("POST", "/api/admin/engine/load", .success(engineJSON("starting", model: "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M")))
        let (vm, _) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.loadModel("unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"))
        let call = api.mutating.first
        #expect(call?.path == "/api/admin/engine/load")
        #expect(call?.body == ["model": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"])
        #expect(vm.engine?.state == .starting)
    }

    @Test func loadNowNotificationAction() async throws {
        let api = MockAPI()
        let (vm, _) = make(api: api)
        let n = try #require(NotificationPayload(json: NotificationMapperTests.loadNow))
        let mapped = try #require(NotificationMapper.map(n, settings: AppSettings()))
        await vm.handleNotificationResponse(actionIdentifier: "load", userInfo: mapped.userInfo)
        #expect(api.mutating == [MockAPI.Call(method: "POST", path: "/api/admin/engine/load",
                                              body: ["model": "mlx-community/Qwen3.8-27B-4bit"])])
    }

    @Test func failedActionPostsFollowUp() async throws {
        let api = MockAPI()
        api.set("POST", "/api/admin/engine/load", .failure(.http(status: 404, code: "model_not_installed", message: "Not installed")))
        let notifier = MockNotifier()
        let (vm, _) = make(api: api, notifier: notifier)
        let n = try #require(NotificationPayload(json: NotificationMapperTests.loadNow))
        let mapped = try #require(NotificationMapper.map(n, settings: AppSettings()))
        await vm.handleNotificationResponse(actionIdentifier: "load", userInfo: mapped.userInfo)
        #expect(notifier.posted.value.first?.title == "Couldn't load now")
        #expect(notifier.posted.value.first?.body == "Not installed")
    }

    @Test func notificationBodyOpensAdminPage() async {
        let (vm, host) = make()
        await vm.handleNotificationResponse(actionIdentifier: NotificationMapper.defaultActionIdentifier,
                                            userInfo: ["openPath": "/admin/models"])
        #expect(host.opened == [URL(string: "http://127.0.0.1:8000/admin/models")!])
    }

    @Test func powerOffRestoresIntegrations() async {
        let api = MockAPI()
        let (vm, _) = make(api: api)
        await vm.restoreIntegrationsForPowerOff(timeout: 5)
        #expect(api.posted("/api/admin/integrations/restore-all"))
    }

    @Test func quitCommandTerminates() async {
        let api = MockAPI(engine: engineJSON("ready"))
        let (vm, host) = make(api: api)
        await vm.perform(.quit(stopServer: true))
        #expect(api.posted("/api/admin/engine/stop"))
        #expect(host.terminated)
        let api2 = MockAPI()
        let (vm2, host2) = make(api: api2)
        await vm2.perform(.quit(stopServer: false))
        #expect(api2.calls.value.isEmpty)
        #expect(host2.terminated)
    }

    @Test func managerDownAfterSilence() async {
        let api = MockAPI()
        api.set("GET", "/api/admin/engine", .failure(.unreachable("refused")))
        let (vm, _) = make(api: api)
        await vm.refreshEngine()
        #expect(vm.manager == .down)
        #expect(vm.menu.first?.snapshot == "[x] Splash GUI is not running")
    }

    @Test func welcomeShownWhenWizardIncomplete() async {
        let api = MockAPI()
        api.set("GET", "/api/admin/settings", .success(["settings": ["global": ["wizard": ["completed": false]]]]))
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        #expect(host.welcomeShown == 1)
        vm.welcomeClosed(completed: false)
        #expect(vm.setupUnfinished)
        vm.welcomeClosed(completed: true)
        #expect(!vm.setupUnfinished)
    }

    @Test func eventsStreamIdentifiesMenubarClient() async {
        let requested = LockedBox<[String]>([])
        let vm = MenuBarViewModel(api: MockAPI(), settings: AppSettings(), paths: HomePaths(base: URL(fileURLWithPath: "/tmp/sg")),
                                  streams: { path in requested.withValue { $0.append(path) }; return AsyncStream { $0.finish() } },
                                  notifier: MockNotifier(), gpu: StubGPU(value: nil))
        await vm.start()
        vm.stop()
        #expect(requested.value.contains("/api/admin/events?client=menubar"))
    }

    @Test func restartWithRequestsInFlightForces() async {
        let api = MockAPI(engine: engineJSON("ready", inFlight: 2))
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.restartEngine)
        #expect(host.confirms.first?.message == "2 requests are running.")
        #expect(api.posted("/api/admin/engine/restart?force=true"))
    }

    @Test func connectOpensAdminSheet() async {
        let (vm, host) = make()
        await vm.perform(.connectIntegration("claude-desktop"))
        #expect(host.opened.last?.absoluteString == "http://127.0.0.1:8000/admin/integrations?connect=claude-desktop")
    }

    // UI QA pass 2026-10-04, bug 4: a Load or Restart during an install asks first (SPEC Q24).
    static let installing = engineJSON("starting", phase: "installing", extra: ["install": ["repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "4c0d1e2a9b", "files": 9, "total_bytes": 19930000000, "done_bytes": 8600000000, "speed_bps": 84000000, "eta_s": 135]])

    @Test func loadDuringInstallAsksThenForces() async {
        let api = MockAPI(engine: Self.installing)
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.loadModel("mlx-community/Qwen3.6-35B-A3B-4bit"))
        #expect(host.confirms.first?.message == "Splash is downloading mlx-community/Qwen3.8-27B-4bit.")
        #expect(host.confirms.first?.title == "Interrupt")
        #expect(api.mutating.last?.body?["force"]?.bool == true)
    }

    @Test func loadDuringInstallCancelledSendsNothing() async {
        let api = MockAPI(engine: Self.installing)
        let (vm, host) = make(api: api)
        host.confirmAnswer = false
        await vm.refreshEngine()
        await vm.perform(.loadModel("mlx-community/Qwen3.6-35B-A3B-4bit"))
        #expect(host.confirms.count == 1)
        #expect(api.mutating.isEmpty)
    }

    @Test func restartDuringInstallAsksThenForces() async {
        let api = MockAPI(engine: Self.installing)
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.restartEngine)
        #expect(host.confirms.first?.title == "Interrupt")
        #expect(api.posted("/api/admin/engine/restart?force=true"))
    }

    @Test func installConflictAsksThenRetriesWithForce() async {
        let api = MockAPI(engine: engineJSON("ready"))
        api.set("POST", "/api/admin/engine/load", .failure(.http(status: 409, code: "install_in_progress", message: "installing")))
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.loadModel("mlx-community/Qwen3.6-35B-A3B-4bit"))
        #expect(host.confirms.count == 1)
        #expect(api.mutating.map { $0.body?["force"]?.bool ?? false } == [false, true])
    }

    @Test func sameModelInstallConflictIsNotAnErrorAndNeedsNoConfirm() async {
        let api = MockAPI(engine: engineJSON("ready"))
        api.set("POST", "/api/admin/engine/load", .failure(.http(status: 409, code: "install_in_progress", message: "installing",
                                                                   details: ["same_model": true])))
        let (vm, host) = make(api: api)
        await vm.refreshEngine()
        await vm.perform(.loadModel("mlx-community/Qwen3.8-27B-4bit"))
        #expect(host.confirms.isEmpty)
        #expect(host.errors.isEmpty)
        #expect(api.mutating.count == 1)
    }

    @Test func apiErrorCarriesDetails() {
        let data = Data(#"{"error":{"message":"m","type":"conflict_error","code":"install_in_progress","details":{"same_model":true}}}"#.utf8)
        #expect(APIError.from(status: 409, data: data).isSameModelInstall)
        #expect(!APIError.http(status: 409, code: "install_in_progress", message: "m").isSameModelInstall)
    }
}

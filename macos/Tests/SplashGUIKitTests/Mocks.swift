import Foundation
@testable import SplashGUIKit

/// Records every admin call; responses are keyed by "METHOD /path".
final class MockAPI: AdminAPI, @unchecked Sendable {
    struct Call: Equatable {
        var method: String
        var path: String
        var body: JSONValue?
    }

    let calls = LockedBox<[Call]>([])
    let responses = LockedBox<[String: Result<JSONValue, APIError>]>([:])
    let healthy = LockedBox<Bool>(true)
    /// Set to `.foreign` to simulate another server on the port.
    let probeOverride = LockedBox<HealthProbe?>(nil)

    init(engine: JSONValue = ["state": "stopped"]) {
        set("GET", "/api/admin/engine", .success(engine))
    }

    func set(_ method: String, _ path: String, _ result: Result<JSONValue, APIError>) {
        responses.withValue { $0["\(method) \(path)"] = result }
    }

    func health() async -> Bool { await probe() == .manager }
    func probe() async -> HealthProbe { probeOverride.value ?? (healthy.value ? .manager : .down) }

    func request(_ method: String, _ path: String, body: JSONValue?) async throws -> JSONValue {
        calls.withValue { $0.append(Call(method: method, path: path, body: body)) }
        switch responses.value["\(method) \(path)"] {
        case .success(let v)?: return v
        case .failure(let e)?: throw e
        case nil: return ["ok": true]
        }
    }

    var mutating: [Call] { calls.value.filter { $0.method != "GET" } }
    func posted(_ path: String) -> Bool { calls.value.contains { $0.method == "POST" && $0.path == path } }
}

final class MockNotifier: NotificationPosting, @unchecked Sendable {
    let posted = LockedBox<[MappedNotification]>([])
    let denied = LockedBox<Bool>(false)
    func requestAuthorization() async -> Bool { !denied.value }
    func isDenied() async -> Bool { denied.value }
    func post(_ notification: MappedNotification) async { posted.withValue { $0.append(notification) } }
}

final class MockService: AppService, @unchecked Sendable {
    let state: LockedBox<AppServiceStatus>
    let registerCount = LockedBox(0)
    let unregisterCount = LockedBox(0)
    var failRegister = false
    var statusAfterRegister: AppServiceStatus = .enabled

    init(_ status: AppServiceStatus = .notRegistered) { state = LockedBox(status) }
    var status: AppServiceStatus { state.value }
    func register() throws {
        registerCount.withValue { $0 += 1 }
        if failRegister { throw NSError(domain: "test", code: 1) }
        state.value = statusAfterRegister
    }
    func unregister() throws {
        unregisterCount.withValue { $0 += 1 }
        state.value = .notRegistered
    }
}

final class MockProcess: LaunchedProcess, @unchecked Sendable {
    let running = LockedBox(true)
    let interrupted = LockedBox(false)
    var isRunning: Bool { running.value }
    var processIdentifier: Int32 { 4242 }
    func interrupt() { interrupted.value = true; running.value = false }
    func terminate() { running.value = false }
}

final class MockLauncher: ProcessLaunching, @unchecked Sendable {
    let launches = LockedBox<[[String]]>([])
    let process = MockProcess()
    var onLaunch: (() -> Void)?
    func launch(arguments: [String], environment: [String: String], logFile: URL) throws -> LaunchedProcess {
        launches.withValue { $0.append(arguments) }
        onLaunch?()
        return process
    }
}

@MainActor
final class MockHost: MenuBarHost {
    var opened: [URL] = []
    var confirms: [(message: String, info: String, title: String)] = []
    var confirmAnswer = true
    var errors: [String] = []
    var terminated = false
    var welcomeShown = 0
    var aboutShown = 0
    func open(_ url: URL) { opened.append(url) }
    func openFile(_ url: URL) { opened.append(url) }
    func confirm(message: String, info: String, confirmTitle: String) async -> Bool {
        confirms.append((message, info, confirmTitle))
        return confirmAnswer
    }
    func showError(title: String, message: String) { errors.append(message) }
    func showAbout() { aboutShown += 1 }
    func showWelcome() { welcomeShown += 1 }
    func terminate() { terminated = true }
}

struct StubGPU: GPUSampling {
    var value: Int?
    func utilization() -> Int? { value }
}

func engineJSON(_ state: String, model: String? = "mlx-community/Qwen3.8-27B-4bit", phase: String? = nil,
                inFlight: Int = 0, extra: [String: JSONValue] = [:]) -> JSONValue {
    var o: [String: JSONValue] = ["state": .string(state), "requests_in_flight": .number(Double(inFlight)),
                                  "engine": ["found": true, "version": "1.2.0", "source": "brew"]]
    if let model { o["model"] = .string(model) }
    if let phase { o["phase"] = .string(phase) }
    for (k, v) in extra { o[k] = v }
    return .object(o)
}

func engine(_ state: String, model: String? = "mlx-community/Qwen3.8-27B-4bit", phase: String? = nil,
            inFlight: Int = 0, extra: [String: JSONValue] = [:]) -> EngineView {
    EngineView(json: engineJSON(state, model: model, phase: phase, inFlight: inFlight, extra: extra))
}

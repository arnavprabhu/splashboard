import Foundation

/// Stops whatever manager the app started. `ManagerController` conforms; tests mock it.
@MainActor
public protocol ManagerStopping: AnyObject {
    func stopOwned() async
}

extension ManagerController: ManagerStopping {}

/// The ⌘Q sequence (SPEC §4.2, docs/ui/10-menubar.md §6).
///
/// With "stop server when quitting":
///  1. `requests_in_flight > 0` → confirmation ("3 requests are running.");
///  2. `POST /api/admin/engine/stop`;
///  3. `POST /api/admin/integrations/restore-all` (SPEC §11.4; a 501 stub is ignored);
///  4. stop the manager if the app started it (LaunchAgent bootout or child SIGINT).
/// The doc's G20 `POST /api/admin/shutdown` is not in docs/api.md, so steps 2–4 replace it.
/// With "keep running": terminate at once and send nothing.
@MainActor
public final class QuitCoordinator {
    public enum Outcome: Sendable, Equatable {
        case cancelled
        case terminate(stoppedServer: Bool)
    }

    public enum Step: Sendable, Equatable {
        case confirmed(inFlight: Int)
        case engineStopped
        case engineStopFailed(String)
        case integrationsRestored
        case managerStopped
    }

    private let api: AdminAPI
    private weak var manager: ManagerStopping?
    /// Asked when requests are in flight; returns true to continue.
    private let confirm: @MainActor (Int) async -> Bool
    public private(set) var steps: [Step] = []

    public init(api: AdminAPI, manager: ManagerStopping?, confirm: @escaping @MainActor (Int) async -> Bool) {
        self.api = api
        self.manager = manager
        self.confirm = confirm
    }

    /// `stopServer` is the effective choice (`lifecycle.stop_on_quit`, flipped by ⌥).
    /// `requestsInFlight` is the last known value; it is refreshed from `/engine` when reachable.
    public func quit(stopServer: Bool, requestsInFlight: Int) async -> Outcome {
        steps = []
        guard stopServer else { return .terminate(stoppedServer: false) }
        var inFlight = requestsInFlight
        if let fresh = try? await api.engine() { inFlight = fresh.requestsInFlight }
        if inFlight > 0 {
            guard await confirm(inFlight) else { return .cancelled }
            steps.append(.confirmed(inFlight: inFlight))
        }
        do {
            try await api.stopEngine()
            steps.append(.engineStopped)
        } catch let e as APIError {
            steps.append(.engineStopFailed(e.description))
        } catch {
            steps.append(.engineStopFailed(error.localizedDescription))
        }
        if (try? await api.restoreAllIntegrations()) != nil {
            steps.append(.integrationsRestored)
        }
        if let manager {
            await manager.stopOwned()
            steps.append(.managerStopped)
        }
        return .terminate(stoppedServer: true)
    }

    /// The confirmation text (10-menubar §6).
    public static func confirmationText(inFlight: Int) -> (message: String, info: String) {
        let noun = inFlight == 1 ? "request is" : "requests are"
        return ("\(inFlight) \(noun) running.", "Quit and stop the server anyway?")
    }
}

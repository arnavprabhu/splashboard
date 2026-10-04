import Foundation
import ServiceManagement

/// SMAppService status, mirrored so tests do not need ServiceManagement.
public enum AppServiceStatus: String, Sendable {
    case notRegistered, enabled, requiresApproval, notFound
}

/// One registrable service (login item or LaunchAgent). Mocked in tests.
public protocol AppService: Sendable {
    var status: AppServiceStatus { get }
    func register() throws
    func unregister() throws
}

/// `SMAppService` behind `AppService`.
public struct SMAppServiceWrapper: AppService, @unchecked Sendable {
    private let service: SMAppService

    public init(_ service: SMAppService) {
        self.service = service
    }

    /// The menu bar app itself (SPEC §13.3).
    public static var mainApp: SMAppServiceWrapper { SMAppServiceWrapper(.mainApp) }

    /// The manager LaunchAgent; the plist must be in `Contents/Library/LaunchAgents/`.
    public static func agent(plistName: String = ManagerAgent.plistName) -> SMAppServiceWrapper {
        SMAppServiceWrapper(.agent(plistName: plistName))
    }

    public var status: AppServiceStatus {
        switch service.status {
        case .notRegistered: return .notRegistered
        case .enabled: return .enabled
        case .requiresApproval: return .requiresApproval
        case .notFound: return .notFound
        @unknown default: return .notFound
        }
    }

    public func register() throws { try service.register() }
    public func unregister() throws { try service.unregister() }
}

public enum ManagerAgent {
    public static let label = "ai.splashgui.manager"
    public static let plistName = "ai.splashgui.manager.plist"

    /// True when the running app bundle carries the agent plist (scripts/bundle.sh writes it).
    public static func plistInBundle(_ bundle: Bundle = .main) -> Bool {
        let url = bundle.bundleURL.appendingPathComponent("Contents/Library/LaunchAgents/\(plistName)")
        return bundle.bundlePath.hasSuffix(".app") && FileManager.default.fileExists(atPath: url.path)
    }
}

/// Applies `lifecycle.launch_at_login` (SPEC §8.2) to the app's login item.
///
/// The manager agent is registered when the app needs the manager (see `ManagerController`)
/// and unregistered when the app quits with "stop server" — so it comes back at login only
/// together with the app. This avoids killing a running manager when the toggle changes.
public final class LoginItemController: Sendable {
    public struct Outcome: Sendable, Equatable {
        public var status: AppServiceStatus
        public var error: String?
    }

    private let mainApp: AppService

    public init(mainApp: AppService) {
        self.mainApp = mainApp
    }

    @discardableResult
    public func apply(launchAtLogin: Bool) -> Outcome {
        let current = mainApp.status
        do {
            if launchAtLogin, current != .enabled, current != .requiresApproval {
                try mainApp.register()
            } else if !launchAtLogin, current == .enabled || current == .requiresApproval {
                try mainApp.unregister()
            }
            return Outcome(status: mainApp.status, error: nil)
        } catch {
            return Outcome(status: mainApp.status, error: error.localizedDescription)
        }
    }

    public var status: AppServiceStatus { mainApp.status }
}

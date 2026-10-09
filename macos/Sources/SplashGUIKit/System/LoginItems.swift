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
    /// Info.plist key that scripts/bundle.sh writes from packaging/identity.env (PKG-1).
    public static let labelInfoKey = "SplashGUIAgentLabel"
    /// The label when the bundle does not carry one (`swift run`, `swift test`).
    public static let fallbackLabel = "ai.splashgui.manager"

    /// The agent label from an Info.plist dictionary, or the fallback when the key is missing or empty.
    public static func resolveLabel(info: [String: Any]?) -> String {
        if let value = info?[labelInfoKey] as? String, !value.isEmpty { return value }
        return fallbackLabel
    }

    /// The agent plist name that scripts/bundle.sh writes: `<label>.plist`.
    public static func plistName(forLabel label: String) -> String { "\(label).plist" }

    public static let label: String = resolveLabel(info: Bundle.main.infoDictionary)
    public static let plistName: String = plistName(forLabel: label)

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
/// The toggle never registers or unregisters the agent (SPEC §4.2, D87; the keep-running residual is §22 Q42).
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

    /// Remove Splash GUI data (PKG-12): the app no longer opens at login, whatever the setting says.
    /// Returns nil when no login item is left, else why it could not be removed (re-read after the call).
    public func unregisterForRemoval() -> String? {
        guard mainApp.status == .enabled || mainApp.status == .requiresApproval else { return nil }
        do { try mainApp.unregister() } catch { return error.localizedDescription }
        let after = mainApp.status
        return after == .enabled || after == .requiresApproval ? "it is still registered" : nil
    }
}

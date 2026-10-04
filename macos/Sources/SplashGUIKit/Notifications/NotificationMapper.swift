import Foundation

/// A notification ready for `UNUserNotificationCenter`, produced by `NotificationMapper`
/// from a manager `notification` event (docs/api.md §4, docs/ui/10-menubar.md §4).
public struct MappedNotification: Sendable, Equatable {
    public enum Interruption: String, Sendable { case active, timeSensitive }

    public struct Action: Sendable, Equatable {
        /// `UNNotificationAction.identifier` = the NotificationAction id.
        public var identifier: String
        public var title: String
    }

    /// Request identifier = the notification id (a repeat replaces the previous banner).
    public var identifier: String
    public var title: String
    public var body: String
    /// One category per kind + action set: `splashgui.<kind>[.<id>+<id>…]`.
    public var categoryIdentifier: String
    /// Groups by kind so repeated download notifications collapse (10-menubar §4).
    public var threadIdentifier: String
    public var interruption: Interruption
    public var actions: [Action]
    /// Admin page opened when the body is tapped.
    public var openPath: String
    /// Carried in `userInfo` so a tapped action can be sent without the original event.
    public var userInfo: [String: String]
}

public enum NotificationMapper {
    public static let userInfoOpenPath = "openPath"
    public static let userInfoActions = "actions"
    public static let userInfoKind = "kind"

    /// `notifications.<toggle>` gating each kind (SPEC §8.2; 10-menubar §4 "⚙").
    /// Kinds without a toggle are always delivered.
    public static func toggleKey(for kind: String) -> String? {
        switch kind {
        case "download_done": return "download_done"
        case "engine_failed": return "engine_failed"
        case "memory_critical": return "memory_critical"
        case "update_available": return "update_available"
        case "disk_tier_failures": return "disk_cache_errors"
        default: return nil
        }
    }

    /// Critical kinds (SPEC §16.3) are posted time-sensitive (never `.critical`, D-10-7).
    public static func isCritical(_ kind: String) -> Bool {
        ["engine_failed", "crash_loop", "memory_critical"].contains(kind)
    }

    /// The admin page the body opens.
    public static func defaultPath(for kind: String) -> String {
        switch kind {
        case "download_done": return "/admin/models"
        case "download_failed": return "/admin/models/downloader"
        case "engine_failed", "crash_loop": return "/admin/logs"
        case "update_available": return "/admin/settings#about"
        case "unclean_integration_shutdown": return "/admin/integrations"
        case "capacity_exhausted", "resource_timeout": return "/admin/settings#memory_context"
        case "queue_full": return "/admin/settings#requests_limits"
        default: return "/admin/status"
        }
    }

    /// Maps an event to a notification, or nil when its toggle is off.
    ///
    /// Critical kinds (`engine_failed`, `crash_loop`, `memory_critical`) are delivered even when
    /// their `notifications.*` toggle is off (10-menubar §10 criterion 7; 00-foundations §11:
    /// critical = AlertBand + OS notification). Pass `criticalBypassesToggle: false` to honour
    /// every toggle instead.
    public static func map(
        _ n: NotificationPayload, settings: AppSettings, criticalBypassesToggle: Bool = true
    ) -> MappedNotification? {
        if let key = toggleKey(for: n.kind), !settings.notificationEnabled(key) {
            if !(criticalBypassesToggle && isCritical(n.kind)) { return nil }
        }
        let actions = n.actions.map { MappedNotification.Action(identifier: $0.id, title: $0.label) }
        var category = "splashgui.\(n.kind)"
        if !actions.isEmpty { category += "." + actions.map(\.identifier).joined(separator: "+") }
        let openPath: String = {
            if let href = n.href, href.hasPrefix("/") { return href }
            return defaultPath(for: n.kind)
        }()
        let actionsJSON = JSONValue.array(n.actions.map(\.json)).serializedString()
        return MappedNotification(
            identifier: n.id,
            title: n.title,
            body: n.body,
            categoryIdentifier: category,
            threadIdentifier: "splashgui.\(n.kind)",
            interruption: isCritical(n.kind) ? .timeSensitive : .active,
            actions: actions,
            openPath: openPath,
            userInfo: [userInfoOpenPath: openPath, userInfoActions: actionsJSON, userInfoKind: n.kind]
        )
    }

    /// What to do when the user responds to a delivered notification.
    public enum Response: Sendable, Equatable {
        case openPage(String)
        case call(NotificationAction)
        case none
    }

    public static let defaultActionIdentifier = "com.apple.UNNotificationDefaultActionIdentifier"
    public static let dismissActionIdentifier = "com.apple.UNNotificationDismissActionIdentifier"

    /// Resolves a response from the action identifier and the `userInfo` strings.
    public static func resolve(actionIdentifier: String, userInfo: [String: String]) -> Response {
        if actionIdentifier == dismissActionIdentifier { return .none }
        if actionIdentifier == defaultActionIdentifier {
            return .openPage(userInfo[userInfoOpenPath] ?? "/admin/status")
        }
        guard let raw = userInfo[userInfoActions], let json = try? JSONValue.parse(raw),
              let action = (json.array ?? []).compactMap(NotificationAction.init(json:))
                .first(where: { $0.id == actionIdentifier })
        else { return .none }
        return action.opensPage ? .openPage(action.path) : .call(action)
    }

    /// The follow-up shown when an action's API call fails ("Couldn't restart the engine").
    public static func failureNotification(for action: NotificationAction, error: APIError) -> MappedNotification {
        let verb = action.label.prefix(1).lowercased() + action.label.dropFirst()
        return MappedNotification(
            identifier: "action-failed-\(action.id)-\(UUID().uuidString)",
            title: "Couldn't \(verb)",
            body: error.userMessage,
            categoryIdentifier: "splashgui.action_failed",
            threadIdentifier: "splashgui.action_failed",
            interruption: .active,
            actions: [],
            openPath: "/admin/status",
            userInfo: [userInfoOpenPath: "/admin/status"]
        )
    }
}

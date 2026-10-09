import Foundation
import UserNotifications

/// Posts mapped notifications. Behind a protocol so the view model can be tested.
public protocol NotificationPosting: Sendable {
    func requestAuthorization() async -> Bool
    /// True when the user turned notifications off for the app.
    func isDenied() async -> Bool
    func post(_ notification: MappedNotification) async
}

/// `UNUserNotificationCenter` implementation. UN needs a real app bundle; outside one
/// (`swift run`) it falls back to `osascript display notification`, without actions.
public final class UserNotificationPoster: NotificationPosting {
    /// category id → actions; UNNotificationCategory is not Sendable, so categories are rebuilt.
    private let categories = LockedBox<[String: [MappedNotification.Action]]>([:])
    public let usesUserNotifications: Bool

    public init(bundle: Bundle = .main) {
        usesUserNotifications = bundle.bundleIdentifier != nil && bundle.bundlePath.hasSuffix(".app")
    }

    private var center: UNUserNotificationCenter { UNUserNotificationCenter.current() }

    public func requestAuthorization() async -> Bool {
        guard usesUserNotifications else { return true }
        return (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    public func isDenied() async -> Bool {
        guard usesUserNotifications else { return false }
        return await center.notificationSettings().authorizationStatus == .denied
    }

    public func post(_ n: MappedNotification) async {
        guard usesUserNotifications else {
            OSAScriptNotifier.post(title: n.title, body: n.body)
            return
        }
        registerCategoryIfNeeded(n)
        let content = UNMutableNotificationContent()
        content.title = n.title
        content.body = n.body
        content.categoryIdentifier = n.categoryIdentifier
        content.threadIdentifier = n.threadIdentifier
        content.interruptionLevel = n.interruption == .timeSensitive ? .timeSensitive : .active
        content.sound = n.interruption == .timeSensitive ? .default : nil
        content.userInfo = n.userInfo
        let request = UNNotificationRequest(identifier: n.identifier, content: content, trigger: nil)
        try? await center.add(request)
    }

    private func registerCategoryIfNeeded(_ n: MappedNotification) {
        let all: [String: [MappedNotification.Action]]? = categories.withValue { known in
            guard known[n.categoryIdentifier] == nil else { return nil }
            known[n.categoryIdentifier] = n.actions
            return known
        }
        guard let all else { return }
        let set = Set(all.map { id, actions in
            UNNotificationCategory(
                identifier: id,
                actions: actions.map { UNNotificationAction(identifier: $0.identifier, title: $0.title, options: []) },
                intentIdentifiers: [], options: [])
        })
        center.setNotificationCategories(set)
    }
}

/// Receives taps on delivered notifications and forwards them as plain strings.
public final class NotificationResponder: NSObject, UNUserNotificationCenterDelegate, Sendable {
    public typealias Handler = @Sendable (_ actionIdentifier: String, _ userInfo: [String: String]) async -> Void
    private let handler: Handler

    public init(handler: @escaping Handler) {
        self.handler = handler
    }

    public func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        let action = response.actionIdentifier
        var info: [String: String] = [:]
        for (k, v) in response.notification.request.content.userInfo {
            if let k = k as? String, let v = v as? String { info[k] = v }
        }
        await handler(action, info)
    }

    public func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list]
    }
}

/// `osascript -e 'display notification …'` (no actions); used outside an app bundle.
public enum OSAScriptNotifier {
    public static func script(title: String, body: String) -> String {
        "display notification \(AppleScript.quote(body)) with title \(AppleScript.quote(title))"
    }

    public static func post(title: String, body: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script(title: title, body: body)]
        try? p.run()
    }
}

/// AppleScript string quoting.
public enum AppleScript {
    /// `"…"` with backslashes and quotes escaped.
    public static func quote(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}

import Foundation

/*
 Welcome window ⇄ web wizard bridge (SPEC §13.3, docs/ui/10-menubar.md §5, docs/ui/04-welcome-wizard.md §8)
 ==========================================================================================================
 Contract agreed with the web wizard (frontend lead, 2026-10-03). Implemented exactly; no aliases.

 URL
   The Welcome window loads `http://<manager>/admin/welcome?host=app` in a WKWebView, adding
   `&theme=dark|light` only when `ui.theme` is "system" (the app's effective appearance;
   docs/ui/01-shell-and-nav.md §7).

 Detection (page side)
   `?host=app` in the URL and `window.webkit?.messageHandlers?.splashGUI` present.

 JS → native
   window.webkit.messageHandlers.splashGUI.postMessage({ type, requestId, ...payload })
   `requestId` is a string chosen by the page.

 Native → JS (one reply per message that carries a requestId)
   webView.evaluateJavaScript("window.splashGUIHost && window.splashGUIHost.resolve(<json requestId>, <json result>)")

 Types
 | type                   | payload                                       | result                                       |
 |------------------------|-----------------------------------------------|----------------------------------------------|
 | hello                  | {}                                            | {ok: true, version: "<app version>", features: ["pickFolder","openHomebrewInstaller","openTerminal","openURL","closeWelcome"]} |
 | pickFolder             | {target: "models"|"cache", current?: string}  | {path: "/abs/path"} or {cancelled: true}     |
 | openHomebrewInstaller  | {}                                            | {ok: true} or {error: "…"}                   |
 | openTerminal           | {command: string}                             | {ok: true} or {error: "…"}                   |
 | openURL                | {url: string}                                 | {ok: true} or {error: "…"}                   |
 | closeWelcome           | {completed: boolean}                          | {ok: true} (may not arrive: the window closes) |
 | (anything else)        |                                               | {error: "unknown type"}                      |

 Native-side safety rules (replies stay in the shapes above):
 - messages are accepted only from the main frame of the manager's own origin (others are ignored);
 - openTerminal runs only allow-listed commands (`TerminalCommandPolicy.allowed`: the Homebrew
   installer, `brew install|upgrade incoai/tap/splash`, `brew update`); others reply
   {error: "command not allowed"};
 - openURL accepts http, https and x-apple.systempreferences URLs.
 - pickFolder uses NSOpenPanel (directories only, can create), starting at `current` when given.
*/

/// A parsed bridge message.
public enum BridgeCommand: Sendable, Equatable {
    case hello
    case pickFolder(target: String?, current: String?)
    case openHomebrewInstaller
    case openTerminal(command: String)
    case openURL(URL)
    case closeWelcome(completed: Bool)
}

public struct BridgeRequest: Sendable, Equatable {
    public var type: String
    public var requestId: String?
    public var command: BridgeCommand
}

public enum BridgeParseError: Error, Sendable, Equatable {
    case notAnObject
    case unknownType(String)
    case invalidParams(String)

    /// The `{error}` reply for this failure.
    public var reply: JSONValue {
        switch self {
        case .notAnObject, .unknownType: return ["error": "unknown type"]
        case .invalidParams(let field): return ["error": .string("invalid \(field)")]
        }
    }
}

public enum WelcomeBridge {
    public static let handlerName = "splashGUI"
    public static let features = ["pickFolder", "openHomebrewInstaller", "openTerminal", "openURL", "closeWelcome"]

    /// Parses a message body (the `WKScriptMessage.body` converted with `JSONValue(any:)`).
    /// The requestId is returned even on failure so the error can still be delivered.
    public static func parse(_ body: JSONValue) -> (requestId: String?, result: Result<BridgeRequest, BridgeParseError>) {
        guard body.object != nil else { return (nil, .failure(.notAnObject)) }
        let requestId = body["requestId"]?.string ?? body["requestId"]?.int.map(String.init)
        let type = body["type"]?.string ?? ""
        let command: BridgeCommand
        switch type {
        case "hello":
            command = .hello
        case "pickFolder":
            command = .pickFolder(target: body["target"]?.string, current: body["current"]?.string)
        case "openHomebrewInstaller":
            command = .openHomebrewInstaller
        case "openTerminal":
            guard let c = body["command"]?.string, !c.trimmingCharacters(in: .whitespaces).isEmpty else {
                return (requestId, .failure(.invalidParams("command")))
            }
            command = .openTerminal(command: c)
        case "openURL":
            guard let s = body["url"]?.string, let url = URL(string: s),
                  ["http", "https", "x-apple.systempreferences"].contains(url.scheme?.lowercased() ?? "")
            else { return (requestId, .failure(.invalidParams("url"))) }
            command = .openURL(url)
        case "closeWelcome":
            command = .closeWelcome(completed: body["completed"]?.bool ?? false)
        default:
            return (requestId, .failure(.unknownType(type)))
        }
        return (requestId, .success(BridgeRequest(type: type, requestId: requestId, command: command)))
    }

    /// `hello` reply.
    public static func helloReply(appVersion: String) -> JSONValue {
        ["ok": true, "version": .string(appVersion), "features": .array(features.map { .string($0) })]
    }

    public static let okReply: JSONValue = ["ok": true]
    public static let cancelledReply: JSONValue = ["cancelled": true]

    public static func errorReply(_ message: String) -> JSONValue {
        ["error": .string(message)]
    }

    public static func pathReply(_ path: String) -> JSONValue {
        ["path": .string(path)]
    }

    /// The exact reply script: `window.splashGUIHost && window.splashGUIHost.resolve(<id>, <result>)`.
    public static func replyScript(requestId: String, result: JSONValue) -> String {
        let id = JSONValue.string(requestId).serializedString()
        return "window.splashGUIHost && window.splashGUIHost.resolve(\(id), \(result.serializedString()))"
    }

    /// Only the manager's own origin may use the bridge.
    public static func isAllowedOrigin(scheme: String?, host: String?, port: Int?, manager: URL) -> Bool {
        guard let scheme = scheme?.lowercased(), scheme == manager.scheme?.lowercased() else { return false }
        let loopback: Set<String> = ["127.0.0.1", "localhost", "::1", "[::1]"]
        let h = host?.lowercased() ?? ""
        let mh = manager.host(percentEncoded: false)?.lowercased() ?? ""
        let hostOK = h == mh || (loopback.contains(h) && loopback.contains(mh))
        let defaultPort = scheme == "https" ? 443 : 80
        let mPort = manager.port ?? defaultPort
        let p = (port ?? 0) == 0 ? defaultPort : port!
        return hostOK && p == mPort
    }

    /// Links that leave the manager's origin open in the default browser only for web and mail
    /// schemes; anything else (`file:`, `smb:`, app URL schemes) is dropped.
    public static func canOpenExternally(_ url: URL) -> Bool {
        ["http", "https", "mailto"].contains(url.scheme?.lowercased() ?? "")
    }

    /// The welcome URL: `/admin/welcome?host=app[&theme=…]`.
    public static func welcomeURL(manager: URL, theme: String?) -> URL {
        var s = manager.absoluteString
        if s.hasSuffix("/") { s.removeLast() }
        return URL(string: s + welcomePath(theme: theme))!
    }

    /// `/admin/welcome?host=app[&theme=…]`, the page the window opens (signed in through a
    /// one-time link when the manager can mint one, D58).
    public static func welcomePath(theme: String?) -> String {
        "/admin/welcome?host=app" + (theme.map { "&theme=\($0)" } ?? "")
    }

    /// `theme` query value: only when the preference is "system" (01-shell §7).
    public static func themeParameter(preference: String, systemIsDark: Bool) -> String? {
        preference == "system" ? (systemIsDark ? "dark" : "light") : nil
    }
}

/// Which commands the page may run in Terminal (docs/ui/04 §2 step 1).
public enum TerminalCommandPolicy {
    /// Homebrew's official installer one-liner (https://brew.sh).
    public static let homebrewInstall =
        "/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    public static let allowed: Set<String> = [
        homebrewInstall,
        "brew install incoai/tap/splash",
        "brew upgrade incoai/tap/splash",
        "brew update",
    ]

    public static func isAllowed(_ command: String) -> Bool {
        allowed.contains(command.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// AppleScript that opens Terminal and runs `command` in a new window.
    public static func appleScript(for command: String) -> String {
        """
        tell application "Terminal"
            activate
            do script \(AppleScript.quote(command))
        end tell
        """
    }

    /// Body of a `.command` file (fallback when Automation permission for Terminal is denied).
    public static func commandFile(for command: String) -> String {
        "#!/bin/zsh\n# Opened by Splash GUI\n\(command)\n"
    }
}

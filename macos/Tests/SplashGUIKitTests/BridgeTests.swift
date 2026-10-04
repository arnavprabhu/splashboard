import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Welcome window bridge (handler splashGUI)")
struct BridgeTests {
    func parse(_ body: JSONValue) -> (String?, Result<BridgeRequest, BridgeParseError>) {
        let r = WelcomeBridge.parse(body)
        return (r.requestId, r.result)
    }

    @Test func parsesEveryType() throws {
        func command(_ body: JSONValue) throws -> BridgeCommand { try parse(body).1.get().command }
        #expect(try command(["type": "hello", "requestId": "1"]) == .hello)
        #expect(try command(["type": "pickFolder", "requestId": "2", "target": "models", "current": "~/.splash/models"])
            == .pickFolder(target: "models", current: "~/.splash/models"))
        #expect(try command(["type": "pickFolder", "target": "cache"]) == .pickFolder(target: "cache", current: nil))
        #expect(try command(["type": "openHomebrewInstaller"]) == .openHomebrewInstaller)
        #expect(try command(["type": "openTerminal", "command": "brew install incoai/tap/splash"])
            == .openTerminal(command: "brew install incoai/tap/splash"))
        #expect(try command(["type": "openURL", "url": "https://github.com/incoai/splash"])
            == .openURL(URL(string: "https://github.com/incoai/splash")!))
        #expect(try command(["type": "closeWelcome", "completed": true]) == .closeWelcome(completed: true))
        #expect(try command(["type": "closeWelcome"]) == .closeWelcome(completed: false))
        #expect(parse(["type": "pickFolder", "requestId": "abc"]).0 == "abc")
    }

    @Test func unknownAndInvalid() {
        let (id, unknown) = parse(["type": "pick-folder", "requestId": "r9"])
        #expect(id == "r9")
        #expect(unknown == .failure(.unknownType("pick-folder")))
        #expect((try? unknown.get()) == nil)
        #expect(BridgeParseError.unknownType("x").reply == ["error": "unknown type"])
        #expect(parse("just a string").1 == .failure(.notAnObject))
        #expect(parse(["type": "openURL", "url": "file:///etc/passwd"]).1 == .failure(.invalidParams("url")))
        #expect(parse(["type": "openTerminal"]).1 == .failure(.invalidParams("command")))
    }

    @Test func replies() {
        #expect(WelcomeBridge.replyScript(requestId: "r1", result: ["path": "/Users/x/models"])
            == #"window.splashGUIHost && window.splashGUIHost.resolve("r1", {"path":"/Users/x/models"})"#)
        #expect(WelcomeBridge.replyScript(requestId: "a\"b", result: WelcomeBridge.cancelledReply)
            == #"window.splashGUIHost && window.splashGUIHost.resolve("a\"b", {"cancelled":true})"#)
        let hello = WelcomeBridge.helloReply(appVersion: "0.1.0")
        #expect(hello["ok"]?.bool == true)
        #expect(hello["version"]?.string == "0.1.0")
        #expect(hello["features"]?.array?.compactMap(\.string)
            == ["pickFolder", "openHomebrewInstaller", "openTerminal", "openURL", "closeWelcome"])
        #expect(WelcomeBridge.errorReply("x") == ["error": "x"])
        #expect(WelcomeBridge.okReply == ["ok": true])
    }

    @Test func originPolicy() {
        let manager = URL(string: "http://127.0.0.1:8000")!
        #expect(WelcomeBridge.isAllowedOrigin(scheme: "http", host: "127.0.0.1", port: 8000, manager: manager))
        #expect(WelcomeBridge.isAllowedOrigin(scheme: "http", host: "localhost", port: 8000, manager: manager))
        #expect(!WelcomeBridge.isAllowedOrigin(scheme: "http", host: "127.0.0.1", port: 8001, manager: manager))
        #expect(!WelcomeBridge.isAllowedOrigin(scheme: "https", host: "evil.example", port: 0, manager: manager))
        #expect(!WelcomeBridge.isAllowedOrigin(scheme: nil, host: nil, port: nil, manager: manager))
    }

    @Test func welcomeURLAndTheme() {
        let m = URL(string: "http://127.0.0.1:8000")!
        #expect(WelcomeBridge.welcomeURL(manager: m, theme: nil).absoluteString == "http://127.0.0.1:8000/admin/welcome?host=app")
        #expect(WelcomeBridge.welcomeURL(manager: m, theme: "dark").absoluteString
            == "http://127.0.0.1:8000/admin/welcome?host=app&theme=dark")
        #expect(WelcomeBridge.themeParameter(preference: "system", systemIsDark: true) == "dark")
        #expect(WelcomeBridge.themeParameter(preference: "system", systemIsDark: false) == "light")
        #expect(WelcomeBridge.themeParameter(preference: "light", systemIsDark: true) == nil)
    }

    @Test func terminalPolicy() {
        #expect(TerminalCommandPolicy.isAllowed(TerminalCommandPolicy.homebrewInstall))
        #expect(TerminalCommandPolicy.isAllowed(" brew install incoai/tap/splash\n"))
        #expect(!TerminalCommandPolicy.isAllowed("rm -rf ~"))
        #expect(TerminalCommandPolicy.homebrewInstall
            == #"/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)""#)
        let script = TerminalCommandPolicy.appleScript(for: TerminalCommandPolicy.homebrewInstall)
        #expect(script.contains(#"do script "/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"""#))
        #expect(AppleScript.quote(#"a\b"c"#) == #""a\\b\"c""#)
        #expect(OSAScriptNotifier.script(title: "T", body: "B") == #"display notification "B" with title "T""#)
    }
}

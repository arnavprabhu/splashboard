import Foundation
import Testing
@testable import SplashGUIKit

//Admin sign-in is on by default. Pages the menu bar opens go through a one-time link
/// (`POST /api/admin/auth/link`, CLI token only) so the browser lands signed in.
@Suite("One-time sign-in links")
struct LoginLinkTests {
    static let minted: JSONValue = ["url": "/admin/login?code=abc123", "expires_in": 60]

    @Test func parsesTheManagerResponse() throws {
        let link = try #require(LoginLink(json: Self.minted))
        #expect(link.path == "/admin/login?code=abc123")
        #expect(link.expiresIn == 60)
    }

    @Test func refusesAnythingButAnAdminPath() {
        for bad in ["https://evil.example/admin/login?code=x", "//evil.example/admin/login", "/v1/models",
                    "admin/login?code=x", "/admin/login?code=a b", "/admin\\login", ""] {
            #expect(LoginLink(json: ["url": .string(bad)]) == nil, "\(bad)")
        }
        #expect(LoginLink(json: ["expires_in": 60]) == nil)
    }

    @Test func urlCarriesNextEncoded() throws {
        let link = LoginLink(path: "/admin/login?code=abc123")
        let base = URL(string: "http://127.0.0.1:8140/")!
        let url = try #require(link.url(base: base, next: "/admin/integrations?connect=claude-desktop"))
        #expect(url.absoluteString
            == "http://127.0.0.1:8140/admin/login?code=abc123&next=%2Fadmin%2Fintegrations%3Fconnect%3Dclaude-desktop")
        let comps = try #require(URLComponents(url: url, resolvingAgainstBaseURL: false))
        #expect(comps.queryItems?.first { $0.name == "next" }?.value == "/admin/integrations?connect=claude-desktop")
        #expect(comps.queryItems?.first { $0.name == "code" }?.value == "abc123")
        #expect(link.url(base: base, next: nil)?.absoluteString == "http://127.0.0.1:8140/admin/login?code=abc123")
        #expect(LoginLink(path: "/admin/login").url(base: base, next: "/admin/status")?.absoluteString
            == "http://127.0.0.1:8140/admin/login?next=%2Fadmin%2Fstatus")
    }

    /// The Welcome window's `theme` reaches the login page, which paints first.
    @Test func themeInNextIsRepeatedOnTheLoginURL() throws {
        let link = LoginLink(path: "/admin/login?code=abc123")
        let base = URL(string: "http://127.0.0.1:8140")!
        let url = try #require(link.url(base: base, next: WelcomeBridge.welcomePath(theme: "dark")))
        #expect(url.absoluteString
            == "http://127.0.0.1:8140/admin/login?code=abc123&next=%2Fadmin%2Fwelcome%3Fhost%3Dapp%26theme%3Ddark&theme=dark")
        let light = try #require(link.url(base: base, next: WelcomeBridge.welcomePath(theme: "light")))
        #expect(light.absoluteString.hasSuffix("&theme=light"))
        // No theme, or one the page would not accept: nothing added.
        let none = try #require(link.url(base: base, next: WelcomeBridge.welcomePath(theme: nil)))
        #expect(!none.absoluteString.contains("&theme="))
        let odd = try #require(link.url(base: base, next: "/admin/welcome?theme=%22%3E%3Cscript%3E"))
        #expect(!odd.absoluteString.contains("&theme="))
    }

    @Test func welcomePathMatchesWelcomeURL() {
        let base = URL(string: "http://127.0.0.1:8000")!
        #expect(WelcomeBridge.welcomePath(theme: nil) == "/admin/welcome?host=app")
        #expect(WelcomeBridge.welcomePath(theme: "dark") == "/admin/welcome?host=app&theme=dark")
        #expect(WelcomeBridge.welcomeURL(manager: base, theme: "dark").absoluteString
            == "http://127.0.0.1:8000/admin/welcome?host=app&theme=dark")
    }
}

@MainActor
@Suite("Menu bar opens admin pages signed in")
struct OpenAdminSignedInTests {
    func make(_ api: MockAPI) -> (MenuBarViewModel, MockHost) {
        var settings = AppSettings()
        settings.serverPort = 8140
        let vm = MenuBarViewModel(api: api, settings: settings, paths: HomePaths(base: URL(fileURLWithPath: "/tmp/sg")),
                                  notifier: MockNotifier(), gpu: StubGPU(value: nil))
        let host = MockHost()
        vm.host = host
        return (vm, host)
    }

    @Test func openAdminPanelMintsALinkAndOpensIt() async {
        let api = MockAPI()
        api.set("POST", "/api/admin/auth/link", .success(LoginLinkTests.minted))
        let (vm, host) = make(api)
        await vm.perform(.openAdmin("/admin/status"))
        #expect(api.posted("/api/admin/auth/link"))
        #expect(host.opened.map(\.absoluteString)
            == ["http://127.0.0.1:8140/admin/login?code=abc123&next=%2Fadmin%2Fstatus"])
    }

    @Test func everyWebOpeningActionUsesALink() async {
        let api = MockAPI()
        api.set("GET", "/api/admin/engine", .success(engineJSON("ready")))
        api.set("POST", "/api/admin/auth/link", .success(LoginLinkTests.minted))
        let (vm, host) = make(api)
        await vm.refreshEngine()
        await vm.perform(.connectIntegration("claude-desktop"))
        await vm.perform(.checkForUpdates)
        await vm.handleNotificationResponse(actionIdentifier: NotificationMapper.defaultActionIdentifier,
                                            userInfo: ["openPath": "/admin/models"])
        #expect(host.opened.count == 3)
        #expect(host.opened.allSatisfy { $0.absoluteString.hasPrefix("http://127.0.0.1:8140/admin/login?code=abc123&next=") })
        #expect(api.calls.value.filter { $0.path == "/api/admin/auth/link" }.count == 3)
        let welcome = await vm.signedInURL(forAdminPath: WelcomeBridge.welcomePath(theme: nil))
        #expect(welcome.absoluteString == "http://127.0.0.1:8140/admin/login?code=abc123&next=%2Fadmin%2Fwelcome%3Fhost%3Dapp")
    }

    @Test func fallsBackToThePlainPageWhenMintingFails() async {
        let api = MockAPI()
        api.set("POST", "/api/admin/auth/link",
                .failure(.http(status: 401, code: "auth_required", message: "Sign in")))
        let (vm, host) = make(api)
        await vm.perform(.openAdmin("/admin/settings/security"))
        #expect(host.opened.map(\.absoluteString) == ["http://127.0.0.1:8140/admin/settings/security"])
    }

    @Test func fallsBackWhenTheManagerIsUnreachableOrAnswersOddly() async {
        let api = MockAPI()
        api.set("POST", "/api/admin/auth/link", .failure(.unreachable("connection refused")))
        let (vm, host) = make(api)
        await vm.perform(.openAdmin("/admin/chat"))
        api.set("POST", "/api/admin/auth/link", .success(["url": "https://evil.example/admin/login?code=x"]))
        await vm.perform(.openAdmin("/admin/logs"))
        #expect(host.opened.map(\.absoluteString)
            == ["http://127.0.0.1:8140/admin/chat", "http://127.0.0.1:8140/admin/logs"])
    }
}

@MainActor
@Suite("Read-only POSTs")
struct ReadOnlyPostTests {
    @Test func integrationsAreFetchedWithPost() async {
        let api = MockAPI()
        api.set("POST", "/api/admin/integrations", .success(["cli": [], "desktop": [["name": "claude-desktop", "label": "Claude Desktop", "connected": true]]]))
        let vm = MenuBarViewModel(api: api, settings: AppSettings(), paths: HomePaths(base: URL(fileURLWithPath: "/tmp/sg")),
                                  notifier: MockNotifier(), gpu: StubGPU(value: nil))
        await vm.refreshEngine()
        await vm.refreshInventory()
        #expect(api.posted("/api/admin/integrations"))
        #expect(!api.calls.value.contains { $0.method == "GET" && $0.path == "/api/admin/integrations" })
        #expect(vm.integrations?.desktop.first?.name == "claude-desktop")
    }
}

/// The real HTTP client against a URLProtocol stub: the link request carries the CLI token from
/// the token file, as every other admin call does.
@Suite("HTTPAdminAPI sends the CLI token", .serialized)
struct HTTPAdminAPITokenTests {
    final class StubProtocol: URLProtocol, @unchecked Sendable {
        nonisolated(unsafe) static var seen: [URLRequest] = []
        nonisolated(unsafe) static var respond: (URLRequest) -> (Int, Data) = { _ in (200, Data("{}".utf8)) }

        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            Self.seen.append(request)
            let (status, data) = Self.respond(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    func makeAPI(token: String) throws -> HTTPAdminAPI {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("sg-token-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("cli.token")
        try Data("\(token)\n".utf8).write(to: file)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return HTTPAdminAPI(baseURL: URL(string: "http://127.0.0.1:8140")!, tokens: CLITokenReader(file: file),
                            session: URLSession(configuration: config))
    }

    @Test func mintsWithTheBearerToken() async throws {
        StubProtocol.seen = []
        StubProtocol.respond = { req in
            req.url?.path == "/api/admin/auth/link"
                ? (200, Data(#"{"url":"/admin/login?code=zz9","expires_in":60}"#.utf8))
                : (404, Data(#"{"error":{"message":"nope","type":"not_found_error","code":"not_found"}}"#.utf8))
        }
        let api = try makeAPI(token: "tok-from-file")
        let link = try await api.mintLoginLink()
        #expect(link.path == "/admin/login?code=zz9")
        let req = try #require(StubProtocol.seen.first)
        #expect(req.httpMethod == "POST")
        #expect(req.url?.absoluteString == "http://127.0.0.1:8140/api/admin/auth/link")
        #expect(req.value(forHTTPHeaderField: "Authorization") == "Bearer tok-from-file")
    }

    @Test func aRefusedMintIsAnAPIError() async throws {
        StubProtocol.seen = []
        StubProtocol.respond = { _ in
            (401, Data(#"{"error":{"message":"CLI token required","type":"authentication_error","code":"auth_required"}}"#.utf8))
        }
        let api = try makeAPI(token: "tok")
        await #expect(throws: APIError.self) { try await api.mintLoginLink() }
        // A 401 re-reads the token file once and retries (the manager may have recreated it).
        #expect(StubProtocol.seen.count == 2)
        #expect(StubProtocol.seen.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer tok" })
    }
}

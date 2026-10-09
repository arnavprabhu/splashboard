import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Settings, paths, token, repo")
struct ConfigTests {
    func tempDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("splashgui-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    @Test func defaults() {
        let s = AppSettings()
        #expect(s.serverPort == 8000)
        #expect(s.stopOnQuit && s.launchAtLogin)
        #expect(!s.showTokps && !s.showMemory && !s.showGPU && !s.showSwitcher)
        #expect(s.notificationEnabled("download_done"))
        #expect(s.managerBaseURL.absoluteString == "http://127.0.0.1:8000")
    }

    @Test(arguments: ["127.0.0.1@evil.example", "evil.example", "127.0.0.1/x", "127.0.0.1?a", "a b", ""])
    func aHostThatIsNotAnAddressIsIgnored(host: String) throws {
        let s = AppSettings(global: try JSONValue.parse(#"{"server": {"host": "\#(host)", "port": 8123}}"#))
        #expect(s.managerBaseURL.absoluteString == "http://127.0.0.1:8123")
    }

    @Test(arguments: ["192.168.1.20", "::1", "localhost", "fe80::1"])
    func anAddressIsKept(host: String) {
        #expect(AppSettings.isBindHost(host))
    }

    @Test func fromSettingsResponse() throws {
        let json = try JSONValue.parse("""
        {"settings": {"version": 1, "global": {
          "server": {"host": "0.0.0.0", "port": 8765},
          "lifecycle": {"launch_at_login": false, "stop_on_quit": false},
          "menubar": {"show_tokps": true, "show_memory": true, "show_gpu": false, "show_switcher": true},
          "notifications": {"download_done": false},
          "wizard": {"completed": true}, "ui": {"theme": "system"},
          "routing": {"default_model": "mlx-community/Qwen3.8-27B-4bit"}}},
         "secrets": {}, "read_only": false}
        """)
        let s = AppSettings(response: json)
        #expect(s.serverPort == 8765)
        #expect(s.managerBaseURL.absoluteString == "http://127.0.0.1:8765")
        #expect(s.endpointLabel == "127.0.0.1:8765")
        #expect(!s.launchAtLogin && !s.stopOnQuit)
        #expect(s.showTokps && s.showMemory && !s.showGPU && s.showSwitcher)
        #expect(!s.notificationEnabled("download_done"))
        #expect(s.notificationEnabled("engine_failed"))
        #expect(s.wizardCompleted)
        #expect(s.theme == "system")
        #expect(s.defaultModel == "mlx-community/Qwen3.8-27B-4bit")
    }

    @Test func toleratesGarbage() throws {
        let s = AppSettings(response: try JSONValue.parse(#"{"global": {"server": {"port": "nope"}, "menubar": 3}}"#))
        #expect(s.serverPort == 8000)
        #expect(!s.showTokps)
    }

    @Test func homePathsRespectEnv() {
        let custom = HomePaths.fromEnvironment(["SPLASH_GUI_HOME": "/tmp/sg-home"])
        #expect(custom.cliTokenFile.path == "/tmp/sg-home/run/cli.token")
        #expect(custom.settingsFile.path == "/tmp/sg-home/settings.json")
        let home = URL(fileURLWithPath: "/Users/x")
        let def = HomePaths.fromEnvironment([:], home: home)
        #expect(def.base.path == "/Users/x/.splash")
        #expect(def.managerLog.path == "/Users/x/.splash/logs/manager.log")
        #expect(HomePaths.crashTraceDir.path.hasSuffix("Library/Logs/Splash/crash"))
    }

    @Test func readsLocalSettingsAndToken() throws {
        let dir = try tempDir()
        let paths = HomePaths(base: dir)
        #expect(paths.readLocalSettings() == AppSettings())
        try #"{"version":1,"global":{"server":{"host":"127.0.0.1","port":9001}}}"#.write(
            to: paths.settingsFile, atomically: true, encoding: .utf8)
        #expect(paths.readLocalSettings().serverPort == 9001)

        let tokens = CLITokenReader(paths: paths)
        #expect(tokens.token() == nil)
        try FileManager.default.createDirectory(at: paths.runDir, withIntermediateDirectories: true)
        try "  abc123token\n".write(to: paths.cliTokenFile, atomically: true, encoding: .utf8)
        #expect(tokens.token() == "abc123token")
        try "rotated".write(to: paths.cliTokenFile, atomically: true, encoding: .utf8)
        #expect(tokens.token() == "abc123token")  // cached
        #expect(tokens.reload() == "rotated")
    }

    @Test func authHeadersCarryBearerToken() throws {
        let dir = try tempDir()
        let paths = HomePaths(base: dir)
        try FileManager.default.createDirectory(at: paths.runDir, withIntermediateDirectories: true)
        try "tok".write(to: paths.cliTokenFile, atomically: true, encoding: .utf8)
        let api = HTTPAdminAPI(baseURL: URL(string: "http://127.0.0.1:8000")!, tokens: CLITokenReader(paths: paths))
        #expect(api.authHeaders()["Authorization"] == "Bearer tok")
        #expect(api.url(for: "/api/admin/models/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M/profiles").absoluteString
            == "http://127.0.0.1:8000/api/admin/models/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M/profiles")
        api.baseURL = URL(string: "http://127.0.0.1:9000")!
        #expect(api.url(for: "/health").absoluteString == "http://127.0.0.1:9000/health")
    }

    @Test func apiErrorShape() throws {
        let data = Data(#"{"error":{"message":"Busy","type":"conflict_error","code":"model_switch_busy"}}"#.utf8)
        #expect(throws: APIError.http(status: 409, code: "model_switch_busy", message: "Busy")) {
            try HTTPAdminAPI.decode(status: 409, data: data)
        }
        #expect(try HTTPAdminAPI.decode(status: 204, data: Data()) == .null)
        #expect(APIError.http(status: 501, code: "not_implemented", message: "x").isNotImplemented)
    }

    @Test func repoLocatorOrder() {
        let existing: Set<String> = ["/env/repo/manager/pyproject.toml", "/src/Splash-GUI/manager/pyproject.toml",
                                     "/Users/x/.local/bin/uv"]
        let exists: @Sendable (String) -> Bool = { existing.contains($0) }
        let home = URL(fileURLWithPath: "/Users/x")
        let fromEnv = RepoLocator(environment: ["SPLASH_GUI_REPO": "/env/repo", "PATH": "/usr/bin"], defaultsValue: nil,
                                  infoPlistValue: nil, executableURL: nil, home: home, fileExists: exists)
        #expect(fromEnv.repoURL()?.path == "/env/repo")
        let fromExe = RepoLocator(environment: ["PATH": "/usr/bin"], defaultsValue: "/missing", infoPlistValue: nil,
                                  executableURL: URL(fileURLWithPath: "/src/Splash-GUI/macos/.build/debug/SplashGUI"),
                                  home: home, fileExists: exists)
        #expect(fromExe.repoURL()?.path == "/src/Splash-GUI")
        #expect(fromExe.uvURL()?.path == "/Users/x/.local/bin/uv")
        #expect(fromExe.managerCommand() == ["/Users/x/.local/bin/uv", "run", "--project", "/src/Splash-GUI/manager",
                                             "splash-gui-manager"])
        #expect(fromExe.managerPATH().hasPrefix("/opt/homebrew/bin:"))
        let none = RepoLocator(environment: [:], defaultsValue: nil, infoPlistValue: nil, executableURL: nil,
                               home: home, fileExists: { _ in false })
        #expect(none.repoURL() == nil)
        #expect(none.managerCommand() == nil)
    }

    @Test func jsonValueRoundTrip() throws {
        let v = try JSONValue.parse(#"{"a":[1,2.5,true,null,"s"],"confirm_restart":true}"#)
        #expect(v["confirm_restart"]?.bool == true)
        #expect(v["a"]?[1]?.double == 2.5)
        #expect(v["a"]?[2]?.bool == true)
        #expect(v["a"]?[3]?.isNull == true)
        #expect(try JSONValue.parse(v.serialized()) == v)
        #expect(JSONValue(any: ["x": 1, "b": false] as [String: Any]) == ["x": 1, "b": false])
    }

    @Test func modelsTolerateMissingFields() {
        let e = EngineView(json: ["state": "warp_speed"])
        #expect(e.state == .unknown)
        #expect(e.requestsInFlight == 0)
        let list = InstalledModel.list(json: ["models": [["id": "a/b", "format": "gguf"], ["format": "mlx"]]])
        #expect(list.map(\.id) == ["a/b"])
        #expect(list[0].isLoadable)
        #expect(UsageToday(json: ["prompt_tokens": 10, "completion_tokens": 5]).totalTokens == 15)
    }

    @Test func gpuExtraction() {
        #expect(IOKitGPUSampler.extract(from: ["PerformanceStatistics": ["Device Utilization %": NSNumber(value: 37)]]) == 37)
        #expect(IOKitGPUSampler.extract(from: ["PerformanceStatistics": ["Other": 1]]) == nil)
        #expect(IOKitGPUSampler.extract(from: [:]) == nil)
        #expect(IOKitGPUSampler.extract(from: ["PerformanceStatistics": ["Device Utilization %": 140]]) == 100)
    }
}

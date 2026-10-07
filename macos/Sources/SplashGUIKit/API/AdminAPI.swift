import Foundation

/// An error from the admin API, in Splash's shape `{"error": {message, type, code, …}}`
/// (docs/api.md §1.3), or a transport failure.
public enum APIError: Error, Sendable, Equatable, CustomStringConvertible {
    /// `details` is the error's `details` object, when the manager sent one.
    case http(status: Int, code: String, message: String, details: JSONValue? = nil)
    case unreachable(String)
    case invalidResponse

    public var status: Int? {
        if case .http(let status, _, _, _) = self { return status }
        return nil
    }

    public var code: String? {
        if case .http(_, let code, _, _) = self { return code }
        return nil
    }

    public var isNotImplemented: Bool { status == 501 }

    /// A 409 `install_in_progress` for the model already being installed (`details.same_model`):
    /// the load changes nothing, so it is not an error and needs no confirmation.
    public var isSameModelInstall: Bool {
        guard case .http(409, "install_in_progress", _, let details) = self else { return false }
        return details?["same_model"]?.bool == true
    }

    public var description: String {
        switch self {
        case .http(let status, let code, let message, _): return "\(message) (\(status) \(code))"
        case .unreachable(let why): return "Splash GUI is not reachable: \(why)"
        case .invalidResponse: return "Unexpected response from Splash GUI"
        }
    }

    /// The human message to show in a follow-up notification.
    public var userMessage: String {
        switch self {
        case .http(_, _, let message, _): return message
        default: return description
        }
    }

    static func from(status: Int, data: Data) -> APIError {
        let json = try? JSONValue.parse(data)
        let err = json?["error"]
        let message = err?["message"]?.string
            ?? json?["detail"]?.string
            ?? HTTPURLResponse.localizedString(forStatusCode: status)
        return .http(status: status, code: err?["code"]?.string ?? "http_\(status)", message: message, details: err?["details"])
    }
}

/// What answers `GET /health` on the manager's port.
public enum HealthProbe: Sendable, Equatable {
    /// A Splash GUI manager (`{"status":"ok","service":"splash-gui-manager"}`).
    case manager
    /// Something else is listening there (oMLX, another app): never talk to it as the manager.
    case foreign
    /// Nothing answers.
    case down
}

/// The `service` value the manager's `/health` carries (manager/splash_gui/__init__.py).
public let managerHealthService = "splash-gui-manager"

/// The manager's admin API as the app uses it. The two primitives make mocking trivial; the
/// typed calls are extensions.
public protocol AdminAPI: Sendable {
    /// `GET /health` → true only when our manager answers (`.manager` from `probe()`).
    func health() async -> Bool
    /// `GET /health`, telling our manager from another server on the same port.
    func probe() async -> HealthProbe
    /// GET/POST/PUT/DELETE with a JSON body; returns the parsed JSON (`.null` for 204).
    func request(_ method: String, _ path: String, body: JSONValue?) async throws -> JSONValue
}

public extension AdminAPI {
    func probe() async -> HealthProbe {
        await health() ? .manager : .down
    }

    func get(_ path: String) async throws -> JSONValue {
        try await request("GET", path, body: nil)
    }

    func post(_ path: String, body: JSONValue? = nil) async throws -> JSONValue {
        try await request("POST", path, body: body)
    }

    func engine() async throws -> EngineView {
        EngineView(json: try await get("/api/admin/engine"))
    }

    func settingsResponse() async throws -> JSONValue {
        try await get("/api/admin/settings")
    }

    func models() async throws -> [InstalledModel] {
        InstalledModel.list(json: try await get("/api/admin/models"))
    }

    /// `POST /api/admin/integrations` (D58, was GET: it runs the clients' `--version`).
    func integrations() async throws -> IntegrationsView {
        IntegrationsView(json: try await post("/api/admin/integrations"))
    }

    func versions() async throws -> VersionsInfo {
        VersionsInfo(json: try await get("/api/admin/versions"))
    }

    /// G22: `scope=today` is not in the contract yet (only `all|session`); callers treat
    /// errors as "unknown".
    func usageToday() async throws -> UsageToday {
        UsageToday(json: try await get("/api/admin/usage/summary?scope=today"))
    }

    @discardableResult
    func loadModel(_ id: String, force: Bool = false) async throws -> EngineView {
        var body: [String: JSONValue] = ["model": .string(id)]
        if force { body["force"] = true }
        return EngineView(json: try await post("/api/admin/engine/load", body: .object(body)))
    }

    @discardableResult
    func stopEngine() async throws -> EngineView {
        EngineView(json: try await post("/api/admin/engine/stop"))
    }

    @discardableResult
    func restartEngine(force: Bool = false) async throws -> EngineView {
        EngineView(json: try await post("/api/admin/engine/restart" + (force ? "?force=true" : "")))
    }

    @discardableResult
    func restoreAllIntegrations() async throws -> JSONValue {
        try await post("/api/admin/integrations/restore-all")
    }

    @discardableResult
    func disconnectIntegration(_ name: String) async throws -> JSONValue {
        try await post("/api/admin/integrations/\(name)/disconnect")
    }

    @discardableResult
    func openTerminal(client: String, model: String? = nil) async throws -> JSONValue {
        let body: JSONValue = model.map { ["model": .string($0)] } ?? .object([:])
        return try await post("/api/admin/integrations/\(client)/open-terminal", body: body)
    }

    /// `POST /api/admin/auth/link` (D58): a one-time browser sign-in link. CLI token only.
    func mintLoginLink() async throws -> LoginLink {
        guard let link = LoginLink(json: try await post("/api/admin/auth/link")) else { throw APIError.invalidResponse }
        return link
    }

    /// Sends a `NotificationAction` (notification buttons, alert actions).
    @discardableResult
    func perform(_ action: NotificationAction) async throws -> JSONValue {
        try await request(action.method, action.path, body: action.body)
    }
}

/// URLSession implementation. Every request carries `Authorization: Bearer <cli.token>`,
/// which passes the manager's CSRF and admin-auth checks for loopback clients (§1.2).
public final class HTTPAdminAPI: AdminAPI {
    private let baseURLBox: LockedBox<URL>
    private let tokens: CLITokenReader
    private let session: URLSession

    public init(baseURL: URL, tokens: CLITokenReader, session: URLSession? = nil) {
        self.baseURLBox = LockedBox(baseURL)
        self.tokens = tokens
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 15
            config.requestCachePolicy = .reloadIgnoringLocalCacheData
            config.connectionProxyDictionary = [:]  // loopback only; never through a proxy
            self.session = URLSession(configuration: config)
        }
    }

    public var baseURL: URL {
        get { baseURLBox.value }
        set { baseURLBox.value = newValue }
    }

    public func url(for path: String) -> URL {
        // Paths carry model IDs literally (slashes, colons; docs/api.md §1.1), so join strings.
        let base = baseURL.absoluteString.hasSuffix("/") ? String(baseURL.absoluteString.dropLast()) : baseURL.absoluteString
        let p = path.hasPrefix("/") ? path : "/" + path
        return URL(string: base + p) ?? baseURL
    }

    /// Headers for admin calls (also used by the SSE client).
    public func authHeaders() -> [String: String] {
        var h = ["Accept": "application/json"]
        if let token = tokens.token() { h["Authorization"] = "Bearer \(token)" }
        return h
    }

    public func health() async -> Bool {
        await probe() == .manager
    }

    public func probe() async -> HealthProbe {
        var req = URLRequest(url: url(for: "/health"))
        req.timeoutInterval = 2
        guard let (data, response) = try? await session.data(for: req),
              let http = response as? HTTPURLResponse
        else { return .down }
        return Self.classifyHealth(status: http.statusCode, data: data)
    }

    /// Our manager answers 200 with `status: ok` and `service: splash-gui-manager`; any other
    /// answer means another server holds the port (oMLX also says `{"status":"ok"}`).
    public static func classifyHealth(status: Int, data: Data) -> HealthProbe {
        guard status == 200, let body = try? JSONValue.parse(data) else { return .foreign }
        return body["status"]?.string == "ok" && body["service"]?.string == managerHealthService ? .manager : .foreign
    }

    public func request(_ method: String, _ path: String, body: JSONValue?) async throws -> JSONValue {
        let (status, data) = try await send(method, path, body: body)
        if status == 401 {
            // The manager may have recreated the token file; read it again once.
            tokens.reload()
            let (status2, data2) = try await send(method, path, body: body)
            return try Self.decode(status: status2, data: data2)
        }
        return try Self.decode(status: status, data: data)
    }

    private func send(_ method: String, _ path: String, body: JSONValue?) async throws -> (Int, Data) {
        var req = URLRequest(url: url(for: path))
        req.httpMethod = method
        for (k, v) in authHeaders() { req.setValue(v, forHTTPHeaderField: k) }
        if let body {
            req.httpBody = body.serialized()
            req.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        } else if method != "GET" && method != "HEAD" && method != "DELETE" {
            req.httpBody = Data("{}".utf8)
            req.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        }
        do {
            let (data, response) = try await session.data(for: req)
            guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
            return (http.statusCode, data)
        } catch let e as APIError {
            throw e
        } catch {
            throw APIError.unreachable(error.localizedDescription)
        }
    }

    static func decode(status: Int, data: Data) throws -> JSONValue {
        guard (200..<300).contains(status) else { throw APIError.from(status: status, data: data) }
        if data.isEmpty { return .null }
        return (try? JSONValue.parse(data)) ?? .null
    }
}

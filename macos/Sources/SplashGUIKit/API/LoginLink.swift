import Foundation

/// A one-time sign-in link from `POST /api/admin/auth/link` (CLI token only):
/// `{"url": "/admin/login?code=<code>", "expires_in": 60}`. Opening it in a browser trades the
/// code for a session cookie (`POST /auth/exchange` from the login page), so "Open Admin Panel"
/// lands signed in without the user typing the API key.
public struct LoginLink: Sendable, Equatable {
    /// The path and query on the manager, e.g. `/admin/login?code=…`.
    public let path: String
    public let expiresIn: Double?

    public init(path: String, expiresIn: Double? = nil) {
        self.path = path
        self.expiresIn = expiresIn
    }

    /// Only a path on the manager itself is accepted: never an absolute URL, `//host`, or a
    /// path outside `/admin/`, so a confused response cannot send the browser elsewhere.
    public init?(json: JSONValue) {
        guard let url = json["url"]?.string, Self.isManagerPath(url) else { return nil }
        self.init(path: url, expiresIn: json["expires_in"]?.double)
    }

    static func isManagerPath(_ s: String) -> Bool {
        s.hasPrefix("/admin/") && !s.hasPrefix("//") && !s.contains("\\") && !s.contains("://")
            && !s.contains(where: { $0.isWhitespace || $0.isNewline })
    }

    /// `http://<host>:<port>/admin/login?code=…&next=<admin path>[&theme=light|dark]`. `next` is
    /// the page to land on; the login page keeps only same-app paths (web `safeNext`). A
    /// `theme` in `next` (the Welcome window's appearance) is repeated on the login URL itself,
    /// because index.html applies `?theme=` before first paint and the login page paints first.
    public func url(base: URL, next: String?) -> URL? {
        var s = base.absoluteString
        while s.hasSuffix("/") { s.removeLast() }
        s += path
        if let next, !next.isEmpty {
            s += (path.contains("?") ? "&" : "?") + "next=" + Self.encodeQueryValue(next)
            if let theme = Self.theme(in: next) { s += "&theme=" + theme }
        }
        return URL(string: s)
    }

    /// `light` or `dark` from a path's `theme` query item; anything else is ignored.
    static func theme(in path: String) -> String? {
        let value = URLComponents(string: path)?.queryItems?.first { $0.name == "theme" }?.value
        return value == "light" || value == "dark" ? value : nil
    }

    static func encodeQueryValue(_ v: String) -> String {
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return v.addingPercentEncoding(withAllowedCharacters: allowed) ?? v
    }
}

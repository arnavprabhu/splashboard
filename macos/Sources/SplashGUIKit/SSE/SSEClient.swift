import Foundation

/// What the SSE client reports to its consumer.
public enum SSEClientEvent: Sendable, Equatable {
    case connected
    case event(SSEEvent)
    /// The stream ended or failed; `notImplemented` is a 501 from a stub route (the manager
    /// is still being built), in which case the client retries slowly and the app polls.
    case disconnected(reason: String, notImplemented: Bool)
}

/// Reconnect delays: start at the server's `retry` (3 s for the manager), double per failed
/// attempt, cap at 30 s, ±20 % jitter. A stub route (501) waits the cap.
public struct Backoff: Sendable {
    public var cap: Double

    public init(cap: Double = 30) {
        self.cap = cap
    }

    /// `attempt` counts consecutive failures, starting at 0.
    public func delay(attempt: Int, retryMs: Int?, notImplemented: Bool = false, jitter: Double = 0) -> Double {
        if notImplemented { return cap }
        let base = max(0.25, Double(retryMs ?? 3000) / 1000)
        let raw = base * pow(2, Double(min(attempt, 10)))
        let capped = min(cap, raw)
        return max(0.1, capped * (1 + max(-0.2, min(0.2, jitter))))
    }
}

/// A reconnecting `text/event-stream` client on URLSession. Parsing is done by `SSEParser` on
/// raw bytes (not `AsyncBytes.lines`, which drops the blank lines that delimit events).
public final class SSEClient: Sendable {
    private let makeRequest: @Sendable () -> URLRequest
    private let session: URLSession
    private let backoff: Backoff

    /// `makeRequest` is called for every (re)connection, so the URL and the token can change.
    public init(session: URLSession? = nil, backoff: Backoff = Backoff(), makeRequest: @escaping @Sendable () -> URLRequest) {
        self.makeRequest = makeRequest
        self.backoff = backoff
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            // Idle timeout between bytes; the manager pings every 15 s.
            config.timeoutIntervalForRequest = 45
            config.timeoutIntervalForResource = .infinity
            config.requestCachePolicy = .reloadIgnoringLocalCacheData
            config.connectionProxyDictionary = [:]
            self.session = URLSession(configuration: config)
        }
    }

    /// Convenience: a GET with the given headers.
    public static func request(url: URL, headers: [String: String]) -> URLRequest {
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        for (k, v) in headers { req.setValue(v, forHTTPHeaderField: k) }
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        return req
    }

    /// Streams events until the consuming task is cancelled, reconnecting with backoff.
    public func events() -> AsyncStream<SSEClientEvent> {
        let session = self.session
        let backoff = self.backoff
        let makeRequest = self.makeRequest
        return AsyncStream { continuation in
            let task = Task {
                var attempt = 0
                var retryMs: Int?
                while !Task.isCancelled {
                    var parser = SSEParser()
                    var reason = "stream ended"
                    var notImplemented = false
                    var receivedSomething = false
                    do {
                        let (bytes, response) = try await session.bytes(for: makeRequest())
                        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
                        if http.statusCode != 200 {
                            var body = Data()
                            for try await b in bytes {
                                body.append(b)
                                if body.count > 16_384 { break }
                            }
                            throw APIError.from(status: http.statusCode, data: body)
                        }
                        continuation.yield(.connected)
                        var chunk: [UInt8] = []
                        chunk.reserveCapacity(4096)
                        for try await byte in bytes {
                            chunk.append(byte)
                            if byte == 0x0A || byte == 0x0D || chunk.count >= 4096 {
                                for ev in parser.feed(chunk) {
                                    receivedSomething = true
                                    continuation.yield(.event(ev))
                                }
                                chunk.removeAll(keepingCapacity: true)
                            }
                        }
                        if !chunk.isEmpty {
                            for ev in parser.feed(chunk) { continuation.yield(.event(ev)) }
                        }
                        parser.finish()
                    } catch let e as APIError {
                        reason = e.description
                        notImplemented = e.isNotImplemented
                    } catch is CancellationError {
                        break
                    } catch {
                        reason = error.localizedDescription
                    }
                    if Task.isCancelled { break }
                    if let r = parser.retry { retryMs = r }
                    if receivedSomething { attempt = 0 }
                    continuation.yield(.disconnected(reason: reason, notImplemented: notImplemented))
                    let delay = backoff.delay(
                        attempt: attempt, retryMs: retryMs, notImplemented: notImplemented,
                        jitter: Double.random(in: -0.2...0.2))
                    attempt += 1
                    try? await Task.sleep(for: .seconds(delay))
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

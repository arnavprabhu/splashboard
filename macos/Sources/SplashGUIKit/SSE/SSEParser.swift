import Foundation

/// One dispatched server-sent event.
public struct SSEEvent: Sendable, Equatable {
    /// `event:` field; `message` when absent (WHATWG default).
    public var event: String
    public var data: String
    /// The last event ID seen on the stream (persists across events, as in the spec).
    public var id: String?

    public init(event: String = "message", data: String, id: String? = nil) {
        self.event = event
        self.data = data
        self.id = id
    }

    /// `data` parsed as JSON (always one line of JSON).
    public var json: JSONValue? { try? JSONValue.parse(data) }
}

/// Incremental `text/event-stream` parser following the WHATWG "event stream interpretation":
/// - lines end in CRLF, LF or CR (a CR at the end of a chunk waits for a possible LF);
/// - a leading UTF-8 BOM is dropped;
/// - `:` lines are comments (the manager's `: ping` keep-alive);
/// - `field: value` strips one space after the colon; a line without a colon is a field with
///   an empty value;
/// - `data` lines accumulate, joined with `\n`; a blank line dispatches; an event with an
///   empty data buffer is not dispatched;
/// - `id` without NUL sets the last event ID; `retry` with only digits sets `retry`;
/// - unknown fields are ignored; an unterminated event at EOF is discarded.
public struct SSEParser: Sendable {
    public private(set) var retry: Int?
    public private(set) var lastEventId: String?

    private var line: [UInt8] = []
    private var pendingCR = false
    private var atStreamStart = true
    private var dataBuffer: String = ""
    private var hasData = false
    private var eventType: String = ""

    public init() {}

    public mutating func feed(_ text: String) -> [SSEEvent] {
        feed(Array(text.utf8))
    }

    public mutating func feed<S: Sequence>(_ bytes: S) -> [SSEEvent] where S.Element == UInt8 {
        var out: [SSEEvent] = []
        for byte in bytes {
            if pendingCR {
                pendingCR = false
                if byte == 0x0A { continue }  // CRLF: the CR already ended the line
            }
            switch byte {
            case 0x0D:
                pendingCR = true
                endLine(into: &out)
            case 0x0A:
                endLine(into: &out)
            default:
                line.append(byte)
            }
        }
        return out
    }

    /// End of stream: a partial line is processed but an unterminated event is discarded.
    public mutating func finish() {
        line.removeAll()
        pendingCR = false
        dataBuffer = ""
        hasData = false
        eventType = ""
        atStreamStart = true
    }

    private mutating func endLine(into out: inout [SSEEvent]) {
        var bytes = line
        line.removeAll(keepingCapacity: true)
        if atStreamStart {
            atStreamStart = false
            if bytes.starts(with: [0xEF, 0xBB, 0xBF]) { bytes.removeFirst(3) }
        }
        if bytes.isEmpty {
            dispatch(into: &out)
            return
        }
        if bytes[0] == UInt8(ascii: ":") { return }  // comment
        let text = String(decoding: bytes, as: UTF8.self)
        let field: Substring
        var value: Substring
        if let colon = text.firstIndex(of: ":") {
            field = text[..<colon]
            value = text[text.index(after: colon)...]
            if value.first == " " { value = value.dropFirst() }
        } else {
            field = Substring(text)
            value = ""
        }
        switch field {
        case "event":
            eventType = String(value)
        case "data":
            if hasData { dataBuffer += "\n" }
            dataBuffer += value
            hasData = true
        case "id":
            if !value.contains("\u{0}") { lastEventId = String(value) }
        case "retry":
            if !value.isEmpty, value.allSatisfy({ $0.isASCII && $0.isNumber }), let ms = Int(value) {
                retry = ms
            }
        default:
            break
        }
    }

    private mutating func dispatch(into out: inout [SSEEvent]) {
        defer {
            dataBuffer = ""
            hasData = false
            eventType = ""
        }
        guard hasData else { return }
        out.append(SSEEvent(event: eventType.isEmpty ? "message" : eventType, data: dataBuffer, id: lastEventId))
    }
}

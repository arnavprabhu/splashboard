import Foundation
import Testing
@testable import SplashGUIKit

@Suite("SSE framing (docs/api.md §1.4, WHATWG event stream)")
struct SSEParserTests {
    @Test func managerFraming() {
        var p = SSEParser()
        let events = p.feed("retry: 3000\n\nid: 1\nevent: hello\ndata: {\"engine\":{\"state\":\"stopped\"}}\n\n: ping\n\nid: 2\nevent: engine.state\ndata: {\"state\":\"ready\"}\n\n")
        #expect(p.retry == 3000)
        #expect(events.count == 2)
        #expect(events[0] == SSEEvent(event: "hello", data: "{\"engine\":{\"state\":\"stopped\"}}", id: "1"))
        #expect(events[1].event == "engine.state")
        #expect(events[1].json?["state"]?.string == "ready")
        #expect(events[1].id == "2")
    }

    @Test func multiLineDataJoinedWithNewline() {
        var p = SSEParser()
        #expect(p.feed("data: a\ndata: b\ndata:c\n\n") == [SSEEvent(data: "a\nb\nc")])
    }

    @Test func defaultEventNameIsMessage() {
        var p = SSEParser()
        #expect(p.feed("data: x\n\n").first?.event == "message")
    }

    @Test func crlfAndCrLineEndings() {
        var p = SSEParser()
        #expect(p.feed("event: a\r\ndata: 1\r\n\r\nevent: b\rdata: 2\r\r") == [
            SSEEvent(event: "a", data: "1"), SSEEvent(event: "b", data: "2"),
        ])
    }

    @Test func crlfSplitAcrossChunks() {
        var p = SSEParser()
        var out = p.feed("data: x\r")
        out += p.feed("\n\r")
        out += p.feed("\n")
        #expect(out == [SSEEvent(data: "x")])
    }

    @Test func byteByByte() {
        var p = SSEParser()
        var out: [SSEEvent] = []
        for b in Array("id: 7\nevent: notification\ndata: {\"title\":\"é\"}\n\n".utf8) { out += p.feed([b]) }
        #expect(out == [SSEEvent(event: "notification", data: "{\"title\":\"é\"}", id: "7")])
    }

    @Test func bomStrippedAtStart() {
        var p = SSEParser()
        #expect(p.feed([0xEF, 0xBB, 0xBF] + Array("data: x\n\n".utf8)) == [SSEEvent(data: "x")])
    }

    @Test func commentsAndUnknownFieldsIgnored() {
        var p = SSEParser()
        #expect(p.feed(": ping\nfoo: bar\ndata: x\n\n") == [SSEEvent(data: "x")])
    }

    @Test func eventWithoutDataNotDispatched() {
        var p = SSEParser()
        #expect(p.feed("event: hello\n\n").isEmpty)
        // …and the event type does not leak into the next event.
        #expect(p.feed("data: y\n\n").first?.event == "message")
    }

    @Test func emptyDataLineDispatchesEmptyString() {
        var p = SSEParser()
        #expect(p.feed("data\n\n") == [SSEEvent(data: "")])
    }

    @Test func onlyOneLeadingSpaceStripped() {
        var p = SSEParser()
        #expect(p.feed("data:  two\n\n").first?.data == " two")
    }

    @Test func idPersistsAndRetryNeedsDigits() {
        var p = SSEParser()
        let out = p.feed("id: 5\ndata: a\n\nretry: soon\ndata: b\n\n")
        #expect(out.map(\.id) == ["5", "5"])
        #expect(p.retry == nil)
        _ = p.feed("id: bad\u{0}id\ndata: c\n\n")
        #expect(p.lastEventId == "5")
    }

    @Test func unterminatedEventDiscardedAtEOF() {
        var p = SSEParser()
        #expect(p.feed("data: partial\n").isEmpty)
        p.finish()
        #expect(p.feed("\n").isEmpty)
    }

    @Test func backoff() {
        let b = Backoff()
        #expect(b.delay(attempt: 0, retryMs: 3000) == 3)
        #expect(b.delay(attempt: 1, retryMs: 3000) == 6)
        #expect(b.delay(attempt: 5, retryMs: 3000) == 30)
        #expect(b.delay(attempt: 0, retryMs: nil) == 3)
        #expect(b.delay(attempt: 0, retryMs: 3000, notImplemented: true) == 30)
        #expect(abs(b.delay(attempt: 0, retryMs: 3000, jitter: 0.2) - 3.6) < 1e-9)
    }
}

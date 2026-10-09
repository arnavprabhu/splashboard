import Foundation

/// A JSON tree. Every API payload is decoded into this first and the typed models read
/// it with lenient accessors, so a missing field or an unexpected type never fails a whole
/// object ("Readers must tolerate missing fields").
///
/// Deliberately not decoded with `JSONDecoder.keyDecodingStrategy`: keys stay exactly as the
/// manager sent them (settings keys, request bodies such as `{"confirm_restart": true}`).
public enum JSONValue: Sendable, Equatable, Hashable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    // MARK: Parsing / serialising

    public static func parse(_ data: Data) throws -> JSONValue {
        let raw = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        return JSONValue(any: raw)
    }

    public static func parse(_ string: String) throws -> JSONValue {
        try parse(Data(string.utf8))
    }

    /// Builds a value from a Foundation object (`JSONSerialization` output or a
    /// `WKScriptMessage.body`). Unknown types become `.null`.
    public init(any value: Any?) {
        switch value {
        case nil:
            self = .null
        case let n as NSNumber:
            // NSNumber bridges Bool too; CFBoolean has its own type id.
            if CFGetTypeID(n) == CFBooleanGetTypeID() {
                self = .bool(n.boolValue)
            } else {
                self = .number(n.doubleValue)
            }
        case let s as String:
            self = .string(s)
        case let a as [Any]:
            self = .array(a.map { JSONValue(any: $0) })
        case let d as [String: Any]:
            self = .object(d.mapValues { JSONValue(any: $0) })
        case is NSNull:
            self = .null
        default:
            self = .null
        }
    }

    /// The Foundation representation, for `JSONSerialization` and `userInfo` dictionaries.
    public var foundationValue: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let b): return b
        case .number(let n):
            if n.rounded() == n, abs(n) < 9.0e15 { return Int(n) }
            return n
        case .string(let s): return s
        case .array(let a): return a.map(\.foundationValue)
        case .object(let o): return o.mapValues(\.foundationValue)
        }
    }

    public func serialized(sortedKeys: Bool = true) -> Data {
        var options: JSONSerialization.WritingOptions = [.fragmentsAllowed, .withoutEscapingSlashes]
        if sortedKeys { options.insert(.sortedKeys) }
        return (try? JSONSerialization.data(withJSONObject: foundationValue, options: options)) ?? Data("null".utf8)
    }

    public func serializedString(sortedKeys: Bool = true) -> String {
        String(decoding: serialized(sortedKeys: sortedKeys), as: UTF8.self)
    }

    // MARK: Lenient accessors

    public subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    public subscript(index: Int) -> JSONValue? {
        if case .array(let a) = self, a.indices.contains(index) { return a[index] }
        return nil
    }

    /// Follows a dotted path: `json[path: "global.menubar.show_tokps"]`.
    public subscript(path path: String) -> JSONValue? {
        var current: JSONValue? = self
        for part in path.split(separator: ".") {
            current = current?[String(part)]
        }
        return current
    }

    public var isNull: Bool {
        if case .null = self { return true }
        return false
    }

    public var string: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    public var double: Double? {
        switch self {
        case .number(let n): return n
        case .string(let s): return Double(s)
        default: return nil
        }
    }

    public var int: Int? {
        guard let d = double, d.isFinite, abs(d) < 9.0e18 else { return nil }
        return Int(d)
    }

    public var bool: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }

    public var array: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    public var object: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByBooleanLiteral, ExpressibleByIntegerLiteral,
    ExpressibleByFloatLiteral, ExpressibleByNilLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral
{
    public init(stringLiteral value: String) { self = .string(value) }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(integerLiteral value: Int) { self = .number(Double(value)) }
    public init(floatLiteral value: Double) { self = .number(value) }
    public init(nilLiteral: ()) { self = .null }
    public init(arrayLiteral elements: JSONValue...) { self = .array(elements) }
    public init(dictionaryLiteral elements: (String, JSONValue)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { _, last in last }))
    }
}

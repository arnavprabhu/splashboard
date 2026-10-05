import Foundation

/// Number formatting shared by the menu, the title and the About window.
/// Rules follow docs/ui/00-foundations.md §8 (formatBytes base 1024, one decimal below 100;
/// tok/s with one decimal below 100) and use `—` for unknown values, never 0.
public enum Format {
    public static let unknown = "—"

    /// `21.3 GB`, `512 MB`, `0 B` (base 1024; one decimal below 100, none above). Hub download
    /// sizes pass `base: 1000`, as the web's downloads panel does.
    public static func bytes(_ value: Int?, base: Double = 1024) -> String {
        guard let value, value >= 0 else { return unknown }
        let units = ["B", "KB", "MB", "GB", "TB", "PB"]
        var v = Double(value)
        var i = 0
        while v >= base, i < units.count - 1 {
            v /= base
            i += 1
        }
        if i == 0 { return "\(value) B" }
        return v < 100 ? String(format: "%.1f %@", v, units[i]) : String(format: "%.0f %@", v, units[i])
    }

    /// `74.3 tok/s`, `210 tok/s`.
    public static func tokPerSec(_ value: Double?) -> String {
        guard let value, value.isFinite, value >= 0 else { return unknown }
        return value < 100 ? String(format: "%.1f tok/s", value) : String(format: "%.0f tok/s", value)
    }

    /// The menu bar title form (D-10-9): integer and the short unit, `74 t/s`.
    public static func menuBarTokPerSec(_ value: Double) -> String {
        "\(Int(value.rounded())) t/s"
    }

    /// Integer tok/s for the header line: `74 tok/s`.
    public static func integerTokPerSec(_ value: Double) -> String {
        "\(Int(value.rounded())) tok/s"
    }

    /// Milliseconds rendered as seconds with two decimals: `0.31 s`.
    public static func seconds(fromMs ms: Double?) -> String {
        guard let ms, ms.isFinite, ms >= 0 else { return unknown }
        return String(format: "%.2f s", ms / 1000)
    }

    /// `87 %` for a 0–1 ratio.
    public static func percent(_ ratio: Double?) -> String {
        guard let ratio, ratio.isFinite else { return unknown }
        return "\(Int((ratio * 100).rounded())) %"
    }

    /// `1,204,311` with grouping (POSIX-stable so tests do not depend on the locale).
    public static func integer(_ value: Int?) -> String {
        guard let value else { return unknown }
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.groupingSeparator = ","
        formatter.usesGroupingSeparator = true
        formatter.groupingSize = 3
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }

    /// `up 2 h 05 m`, `up 4 m`, `up 12 s`.
    public static func uptime(_ seconds: Double?) -> String? {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return nil }
        let s = Int(seconds)
        if s < 60 { return "up \(s) s" }
        let m = s / 60
        if m < 60 { return "up \(m) m" }
        let h = m / 60
        if h < 48 { return String(format: "up %d h %02d m", h, m % 60) }
        return "up \(h / 24) d \(h % 24) h"
    }

    /// Seconds for an ETA: `45 s`, `2 m 15 s`, `1 h 05 m` (as the web's formatDuration).
    public static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite, seconds >= 0 else { return unknown }
        let s = Int(seconds)
        if s < 60 { return "\(s) s" }
        let m = s / 60
        if m < 60 { return String(format: "%d m %02d s", m, s % 60) }
        let h = m / 60
        if h < 24 { return String(format: "%d h %02d m", h, m % 60) }
        return "\(h / 24) d \(h % 24) h"
    }

    /// Cuts a single line to `limit` characters with an ellipsis.
    public static func truncate(_ text: String, to limit: Int) -> String {
        let oneLine = text.replacingOccurrences(of: "\n", with: " ").trimmingCharacters(in: .whitespaces)
        guard oneLine.count > limit else { return oneLine }
        return String(oneLine.prefix(max(0, limit - 1))) + "…"
    }

    /// Short model name for the header (10-menubar §3.1): the repo part after `/`, keeping
    /// `:VARIANT` for GGUF. `unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M` → `Qwen3.8-27B-GGUF:UD-Q4_K_M`.
    public static func shortModelName(_ id: String?) -> String? {
        guard let id, !id.isEmpty else { return nil }
        if let slash = id.firstIndex(of: "/") {
            return String(id[id.index(after: slash)...])
        }
        return id
    }
}

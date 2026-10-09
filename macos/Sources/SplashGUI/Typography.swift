import AppKit
import SplashGUIKit
import SwiftUI

/// Design tokens for the app's own surfaces (About window, welcome placeholder).
/// Native menus and alerts are not styled.
enum Tokens {
    static func bg(dark: Bool) -> Color { dark ? Color(hex: 0x0E0E0E) : Color(hex: 0xECEBE7) }
    static func ink(dark: Bool) -> Color { dark ? Color(hex: 0xECEBE7) : Color(hex: 0x0E0E0E) }
    static func mute(dark: Bool) -> Color { dark ? Color(hex: 0x8D8D88) : Color(hex: 0x5F5F5B) }
    static func rule(dark: Bool) -> Color { ink(dark: dark) }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255, opacity: 1)
    }
}

/// Archivo is self-hosted for the web; scripts/bundle.sh copies the same file into
/// `Contents/Resources/Fonts`. Without it (e.g. `swift run`) the system font stands in.
enum FontLoader {
    static func registerBundledFonts() {
        guard let dir = Bundle.main.resourceURL?.appendingPathComponent("Fonts"),
              let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
        else { return }
        ArchivoFont.register(files)
    }

    /// Archivo at a weight/width (variable axes `wght`, `wdth`), else the system font.
    static func archivo(size: CGFloat, weight: CGFloat, width: CGFloat = 100) -> Font {
        if let font = ArchivoFont.registeredFont(size: size, weight: weight, width: width) {
            return Font(font)
        }
        let systemWeight: NSFont.Weight = weight >= 800 ? .black : (weight >= 600 ? .semibold : .regular)
        let systemWidth: NSFont.Width = width < 90 ? .condensed : .standard
        return Font(NSFont.systemFont(ofSize: size, weight: systemWeight, width: systemWidth))
    }

    /// Display type: 900 weight, 72 % width, uppercase.
    static func display(_ size: CGFloat) -> Font { archivo(size: size, weight: 900, width: 72) }
    static func body(_ size: CGFloat = 13) -> Font { archivo(size: size, weight: 400) }
    static func label(_ size: CGFloat = 12) -> Font { archivo(size: size, weight: 600) }
}

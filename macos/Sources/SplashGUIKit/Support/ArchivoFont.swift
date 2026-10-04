import AppKit
import CoreText

/// Archivo for the app's own windows (DESIGN.md: one family only).
///
/// The bundled file is the web admin's subset (`web/src/assets/fonts/archivo-latin.woff2`).
/// Its family name is **"Archivo SemiBold"** (the subset kept the default instance's name), so a
/// lookup by the family "Archivo" finds nothing. The family is therefore read from the file's
/// own descriptors when it is registered, and the weight/width come from the variable axes.
public enum ArchivoFont {
    static let wght = 0x7767_6874  // 'wght'
    static let wdth = 0x7764_7468  // 'wdth'

    private static let registeredBox = LockedBox<String?>(nil)

    /// The Archivo family registered by `register(_:)`, or nil (e.g. `swift run` without the bundle).
    public static var registered: String? { registeredBox.value }

    /// Registers each font file for this process and returns the first Archivo family found.
    @discardableResult
    public static func register(_ urls: [URL]) -> String? {
        var family: String?
        for url in urls where ["ttf", "otf", "woff2", "woff"].contains(url.pathExtension.lowercased()) {
            CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
            if family == nil { family = familyName(in: url) }
        }
        if let family { registeredBox.value = family }
        return family
    }

    /// Archivo from the registered family, or nil when none is registered.
    public static func registeredFont(size: CGFloat, weight: CGFloat, width: CGFloat = 100) -> NSFont? {
        registered.flatMap { font(family: $0, size: size, weight: weight, width: width) }
    }

    /// The family name stored in a font file, when it is an Archivo face.
    public static func familyName(in url: URL) -> String? {
        let descriptors = CTFontManagerCreateFontDescriptorsFromURL(url as CFURL) as? [CTFontDescriptor] ?? []
        for d in descriptors {
            if let name = CTFontDescriptorCopyAttribute(d, kCTFontFamilyNameAttribute) as? String, isArchivo(name) {
                return name
            }
        }
        return nil
    }

    public static func isArchivo(_ family: String?) -> Bool {
        family?.hasPrefix("Archivo") == true
    }

    /// Archivo at a weight (400–900) and width (62–100 %), or nil when the family is not registered.
    public static func font(family: String, size: CGFloat, weight: CGFloat, width: CGFloat = 100) -> NSFont? {
        let descriptor = NSFontDescriptor(fontAttributes: [
            .family: family,
            .variation: [NSNumber(value: wght): weight, NSNumber(value: wdth): width],
        ])
        guard let font = NSFont(descriptor: descriptor, size: size), isArchivo(font.familyName) else { return nil }
        return font
    }
}

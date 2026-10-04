import AppKit
import CoreText
import Foundation
import Testing
@testable import SplashGUIKit

/// The About window is set in the bundled Archivo (DESIGN.md). The subset's family is
/// "Archivo SemiBold", so the old lookup by the family "Archivo" always fell back to the system font.
@Suite("Bundled Archivo font")
struct ArchivoFontTests {
    /// The same file scripts/bundle.sh copies into Contents/Resources/Fonts.
    static let fontURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("web/src/assets/fonts/archivo-latin.woff2")

    @Test func registersAndResolvesTheVariableFace() throws {
        try #require(FileManager.default.fileExists(atPath: Self.fontURL.path))
        let family = try #require(ArchivoFont.register([Self.fontURL]))
        #expect(ArchivoFont.isArchivo(family))

        let display = try #require(ArchivoFont.font(family: family, size: 40, weight: 900, width: 72))
        #expect(ArchivoFont.isArchivo(display.familyName))
        let axes = CTFontCopyVariation(display) as? [NSNumber: NSNumber] ?? [:]
        #expect(axes[NSNumber(value: ArchivoFont.wght)]?.doubleValue == 900)
        #expect(axes[NSNumber(value: ArchivoFont.wdth)]?.doubleValue == 72)
    }

    @Test func plainFamilyLookupDoesNotFindTheSubset() throws {
        try #require(FileManager.default.fileExists(atPath: Self.fontURL.path))
        ArchivoFont.register([Self.fontURL])
        // Documents why the family is read from the file: "Archivo" alone is not a registered family.
        #expect(ArchivoFont.font(family: "Archivo", size: 12, weight: 400) == nil)
    }

    @Test func statusItemMarkUsesArchivoOnceRegistered() throws {
        try #require(FileManager.default.fileExists(atPath: Self.fontURL.path))
        ArchivoFont.register([Self.fontURL])
        #expect(ArchivoFont.isArchivo(ArchivoFont.registered))
        let glyph = StatusItemRenderer.glyphFont()
        #expect(ArchivoFont.isArchivo(glyph.familyName))
        let axes = CTFontCopyVariation(glyph) as? [NSNumber: NSNumber] ?? [:]
        #expect(axes[NSNumber(value: ArchivoFont.wdth)]?.doubleValue == 72)
    }

    @Test func ignoresNonFontFiles() {
        #expect(ArchivoFont.register([URL(fileURLWithPath: "/tmp/readme.txt")]) == nil)
    }
}

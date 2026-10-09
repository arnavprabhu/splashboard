import Foundation
import Testing
@testable import SplashGUIKit

/// PKG-5: the About window's Licenses button opens `Contents/Resources/licenses/THIRD_PARTY.txt`
/// when the packaged app carries it, and falls back to the web admin otherwise.
@Suite("Bundled third-party licenses (PKG-5)")
struct BundledLicensesTests {
    private static func makeResources() throws -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("splash-licenses-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    @Test func findsTheFileWhereBuildAppPutsIt() throws {
        let resources = try Self.makeResources()
        defer { try? FileManager.default.removeItem(at: resources) }
        let folder = resources.appendingPathComponent("licenses", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent("THIRD_PARTY.txt")
        try "Splashboard (Apache-2.0)\n".write(to: file, atomically: true, encoding: .utf8)

        let found = try #require(BundledLicenses.thirdPartyFile(in: resources))
        #expect(found.path == file.path)
    }

    @Test func returnsNilWhenTheAppHasNoLicenseFile() throws {
        let resources = try Self.makeResources()
        defer { try? FileManager.default.removeItem(at: resources) }
        #expect(BundledLicenses.thirdPartyFile(in: resources) == nil)
    }

    @Test func returnsNilWithoutAResourceFolder() {
        #expect(BundledLicenses.thirdPartyFile(in: nil) == nil)
    }

    @Test func aFileOutsideTheLicensesFolderDoesNotCount() throws {
        let resources = try Self.makeResources()
        defer { try? FileManager.default.removeItem(at: resources) }
        try "stray\n".write(to: resources.appendingPathComponent("THIRD_PARTY.txt"), atomically: true, encoding: .utf8)
        #expect(BundledLicenses.thirdPartyFile(in: resources) == nil)
    }
}

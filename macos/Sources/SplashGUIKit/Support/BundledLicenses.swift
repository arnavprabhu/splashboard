import Foundation

/// The third-party license file of the packaged app (docs/plans/packaging.md, PKG-5).
/// `packaging/scripts/build-app.sh` writes `Contents/Resources/licenses/THIRD_PARTY.txt` through
/// `packaging/scripts/collect-licenses.py`. The development bundle and `swift run` have no such file.
public enum BundledLicenses {
    public static let folderName = "licenses"
    public static let fileName = "THIRD_PARTY.txt"

    /// The license file under an app's resource folder (`Bundle.main.resourceURL`), or nil when the
    /// app does not carry one.
    public static func thirdPartyFile(in resourceDirectory: URL?) -> URL? {
        guard let resourceDirectory else { return nil }
        let file = resourceDirectory
            .appendingPathComponent(folderName, isDirectory: true)
            .appendingPathComponent(fileName)
        return FileManager.default.fileExists(atPath: file.path) ? file : nil
    }
}

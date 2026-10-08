import Foundation
import Sparkle
import SplashGUIKit

/// Sparkle 2 behind `Updater` (SPEC §19, PKG-9). Created only when the bundle carries a feed and a public key,
/// which packaging/scripts/build-app.sh writes from packaging/identity.env (`SUFeedURL`, `SUPublicEDKey`); the
/// dev bundle and `swift run` have neither, so they get no updater and keep the About fallback.
@MainActor
final class SparkleUpdater: Updater {
    private let controller: SPUStandardUpdaterController

    init?(bundle: Bundle = .main) {
        let feed = bundle.object(forInfoDictionaryKey: "SUFeedURL") as? String ?? ""
        let key = bundle.object(forInfoDictionaryKey: "SUPublicEDKey") as? String ?? ""
        guard !feed.isEmpty, !key.isEmpty else { return nil }
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
    }

    var canCheckForUpdates: Bool { controller.updater.canCheckForUpdates }

    func checkForUpdates() { controller.checkForUpdates(nil) }
}

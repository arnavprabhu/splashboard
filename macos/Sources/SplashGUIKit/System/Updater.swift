import Foundation

/// The app's own update check. A packaged build backs it with Sparkle 2
/// (`SparkleUpdater` in the app target); tests use a fake. A build without a feed (`swift run`, the dev bundle)
/// has no updater, and the menu falls back to the About section of Settings.
@MainActor
public protocol Updater: AnyObject {
    /// False while a check is already showing, or when Sparkle cannot check.
    var canCheckForUpdates: Bool { get }
    /// Sparkle's user-initiated check: its window says the app is up to date or offers the update.
    func checkForUpdates()
}

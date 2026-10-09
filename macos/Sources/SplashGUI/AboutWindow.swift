import AppKit
import SplashGUIKit
import SwiftUI

/// About window (docs/ui/10-menubar.md §8): native SwiftUI in the DESIGN.md tokens, following
/// the system appearance (D-10-6). No icons, no rounded corners; structure from 1px/2px rules.
@MainActor
final class AboutWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let model: MenuBarViewModel
    private weak var host: AppDelegate?

    init(model: MenuBarViewModel, host: AppDelegate) {
        self.model = model
        self.host = host
    }

    func show() {
        if window == nil {
            let view = AboutView(model: model, rerunWizard: { [weak self] in self?.host?.showWelcome() })
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 560),
                             styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "About Splashboard"
            w.contentView = NSHostingView(rootView: view)
            w.isReleasedWhenClosed = false
            w.center()
            w.delegate = self
            window = w
        }
        Task { await model.refreshVersions() }
        NSApp.activate()
        window?.makeKeyAndOrderFront(nil)
    }
}

struct AboutView: View {
    let model: MenuBarViewModel
    let rerunWizard: () -> Void
    @Environment(\.colorScheme) private var scheme

    private var dark: Bool { scheme == .dark }
    private var appVersion: String {
        let info = Bundle.main.infoDictionary
        let v = info?["CFBundleShortVersionString"] as? String ?? "0.1.0"
        if let b = info?["CFBundleVersion"] as? String, b != v { return "\(v) (build \(b))" }
        return v
    }

    var body: some View {
        let ink = Tokens.ink(dark: dark)
        let mute = Tokens.mute(dark: dark)
        let running = model.manager == .running
        let v = model.versions
        VStack(alignment: .leading, spacing: 0) {
            Text("SPLASH\nGUI.")
                .font(FontLoader.display(64))
                .lineSpacing(-12)
                .foregroundStyle(ink)
                .padding(.bottom, 28)

            Text("VERSIONS").font(FontLoader.label(12)).foregroundStyle(mute).padding(.bottom, 6)
            Rule(width: 2, color: ink)
            row("App", appVersion)
            row("Manager", running ? "\(v?.manager ?? Format.unknown) · running" : "not running")
            row("Engine", engineText(v, running: running))
            row("Status", v?.statusSchemaVersion.map { "schema \($0)" } ?? Format.unknown)

            HStack(spacing: 12) {
                SquareButton("Check for Updates") { Task { await model.checkForUpdates() } }
                SquareButton("Upgrade Engine…") { model.openAdmin("/admin/settings/about") }
                    .disabled(!running)
            }
            .padding(.top, 20)
            HStack(spacing: 12) {
                SquareButton("Re-run Welcome Wizard", action: rerunWizard).disabled(!running)
                SquareButton("Remove Splashboard Data…") { Task { await model.removeData() } }.disabled(!running)
                SquareButton("Licenses", action: showLicenses)
            }
            .padding(.top, 10)

            Spacer(minLength: 16)
            Rule(width: 1, color: ink)
            HStack(spacing: 16) {
                LinkText("Repository ↗") { NSWorkspace.shared.open(ProjectLinks.repository) }
                LinkText("Issues ↗") { NSWorkspace.shared.open(ProjectLinks.issues) }
                LinkText("Splash engine ↗") { NSWorkspace.shared.open(ProjectLinks.engineRepository) }
            }
            .foregroundStyle(ink)
            .padding(.top, 10)
            Text("APACHE-2.0 · SPLASH GUI IS NOT AN INCO.AI PRODUCT.")
                .font(FontLoader.label(11))
                .foregroundStyle(mute)
                .padding(.top, 8)
        }
        .padding(32)
        .frame(width: 480, height: 560, alignment: .topLeading)
        .background(Tokens.bg(dark: dark))
    }

    /// PKG-5: the bundled THIRD_PARTY.txt in the default text viewer. The development bundle has none,
    /// so it opens the web admin's Licenses section instead.
    private func showLicenses() {
        if let file = BundledLicenses.thirdPartyFile(in: Bundle.main.resourceURL) {
            NSWorkspace.shared.open(file)
        } else {
            model.openAdmin("/admin/settings/about#licenses")
        }
    }

    private func engineText(_ v: VersionsInfo?, running: Bool) -> String {
        guard running, let v else { return "not running" }
        guard v.engineFound, let version = v.engineVersion else { return "Splash not found" }
        let source = v.engineSource == "brew" ? " · Homebrew" : ""
        return "Splash \(version)\(source)"
    }

    private func row(_ key: String, _ value: String) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text(key).font(FontLoader.body()).frame(width: 90, alignment: .leading)
                Text(value).font(FontLoader.body()).monospacedDigit()
                Spacer()
            }
            .foregroundStyle(Tokens.ink(dark: dark))
            .padding(.vertical, 7)
            Rule(width: 1, color: Tokens.rule(dark: dark))
        }
    }
}

struct Rule: View {
    let width: CGFloat
    let color: Color
    var body: some View { Rectangle().fill(color).frame(height: width) }
}

/// Text button with a square 1px rule (DESIGN.md: no radius, no shadow).
struct SquareButton: View {
    let title: String
    let action: () -> Void
    @Environment(\.colorScheme) private var scheme
    @Environment(\.isEnabled) private var enabled

    init(_ title: String, action: @escaping () -> Void) {
        self.title = title
        self.action = action
    }

    var body: some View {
        let ink = Tokens.ink(dark: scheme == .dark)
        Button(action: action) {
            Text(title.uppercased())
                .font(FontLoader.label(12))
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .foregroundStyle(ink)
                .overlay(Rectangle().stroke(ink, lineWidth: 1))
                .opacity(enabled ? 1 : 0.4)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

struct LinkText: View {
    let title: String
    let action: () -> Void

    init(_ title: String, action: @escaping () -> Void) {
        self.title = title
        self.action = action
    }

    var body: some View {
        Button(action: action) {
            Text(title).font(FontLoader.label(12)).underline()
        }
        .buttonStyle(.plain)
    }
}

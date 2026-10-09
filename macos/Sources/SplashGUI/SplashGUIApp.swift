import AppKit
import SplashGUIKit
import SwiftUI

/// Splashboard menu bar app.
@main
struct SplashGUIApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            MenuContentView(model: delegate.model)
        } label: {
            StatusLabel(model: delegate.model)
        }
        .menuBarExtraStyle(.menu)
    }
}

/// The status item: one template image with the glyph and the optional title items.
struct StatusLabel: View {
    let model: MenuBarViewModel

    var body: some View {
        Image(nsImage: StatusItemRenderer.image(icon: model.icon, title: model.title))
            .renderingMode(.template)
            .accessibilityLabel(model.accessibilityLabel)
    }
}

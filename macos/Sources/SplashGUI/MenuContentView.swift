import SplashGUIKit
import SwiftUI

/// Renders `MenuModel.build(_:)` as native menu items (menus stay native).
struct MenuContentView: View {
    let model: MenuBarViewModel

    var body: some View {
        MenuEntriesView(entries: model.menu, model: model)
    }
}

struct MenuEntriesView: View {
    let entries: [MenuEntry]
    let model: MenuBarViewModel

    var body: some View {
        ForEach(Array(entries.enumerated()), id: \.offset) { _, entry in
            switch entry {
            case .text(let text):
                Button(text) {}.disabled(true)
            case .separator:
                Divider()
            case .submenu(let title, let enabled, let children):
                Menu(title) {
                    MenuEntriesView(entries: children, model: model)
                }
                .disabled(!enabled)
            case .button(let button):
                MenuItemView(button: button, model: model)
            }
        }
    }
}

struct MenuItemView: View {
    let button: MenuItem
    let model: MenuBarViewModel

    var body: some View {
        content
            .disabled(!button.enabled)
            .help(button.help ?? "")
            .modifier(ShortcutModifier(shortcut: button.shortcut))
            .modifier(AlternateModifier(alternate: button.alternate, model: model))
    }

    @ViewBuilder private var content: some View {
        if button.checked {
            Toggle(isOn: .constant(true)) { label }
        } else {
            Button {
                run(button.command)
            } label: {
                label
            }
        }
    }

    @ViewBuilder private var label: some View {
        Text(button.title)
        if let subtitle = button.subtitle { Text(subtitle) }
    }

    private func run(_ command: MenuCommand) {
        Task { @MainActor in await model.perform(command) }
    }
}

private struct ShortcutModifier: ViewModifier {
    let shortcut: Character?

    func body(content: Content) -> some View {
        if let shortcut {
            content.keyboardShortcut(KeyEquivalent(shortcut), modifiers: .command)
        } else {
            content
        }
    }
}

/// ⌥ flips the quit behaviour for one quit.
private struct AlternateModifier: ViewModifier {
    let alternate: MenuAlternate?
    let model: MenuBarViewModel

    func body(content: Content) -> some View {
        if let alternate {
            content.modifierKeyAlternate(.option) {
                Button(alternate.title) {
                    Task { @MainActor in await model.perform(alternate.command) }
                }
            }
        } else {
            content
        }
    }
}

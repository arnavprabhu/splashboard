import AppKit
import SplashGUIKit
import SwiftUI
import WebKit

/// The first-launch Welcome window (docs/ui/10-menubar.md §5, 04-welcome-wizard.md §8): a native
/// window hosting the web wizard in a WKWebView, with the native bridge described in
/// SplashGUIKit/Bridge/WelcomeBridge.swift (handler `splashGUI`).
@MainActor
final class WelcomeWindowController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    private let model: MenuBarViewModel
    private let onClose: () -> Void
    private var window: NSWindow?
    private var webView: WKWebView?
    private var placeholder = PlaceholderState()
    private var completed = false
    private var waitTask: Task<Void, Never>?

    init(model: MenuBarViewModel, onClose: @escaping () -> Void) {
        self.model = model
        self.onClose = onClose
    }

    func show() {
        if window == nil { buildWindow() }
        NSApp.activate()
        window?.makeKeyAndOrderFront(nil)
        if webView?.url == nil { waitForManagerThenLoad() }
    }

    private func buildWindow() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 920, height: 680),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Welcome to Splash GUI"
        w.minSize = NSSize(width: 720, height: 560)
        w.isReleasedWhenClosed = false
        w.center()
        w.delegate = self

        let config = WKWebViewConfiguration()
        config.userContentController.add(WeakMessageHandler(self), name: WelcomeBridge.handlerName)
        config.websiteDataStore = .nonPersistent()
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isHidden = true
        webView = web

        let container = NSView()
        let hosting = NSHostingView(rootView: PlaceholderView(
            state: placeholder,
            showLog: { [weak self] in self?.model.host?.openFile(self?.model.paths.managerLaunchLog ?? URL(fileURLWithPath: "/")) },
            tryAgain: { [weak self] in
                guard let self else { return }
                Task {
                    await self.model.startManager()
                    self.waitForManagerThenLoad()
                }
            }))
        for v in [hosting, web] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(v)
            NSLayoutConstraint.activate([
                v.leadingAnchor.constraint(equalTo: container.leadingAnchor),
                v.trailingAnchor.constraint(equalTo: container.trailingAnchor),
                v.topAnchor.constraint(equalTo: container.topAnchor),
                v.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            ])
        }
        w.contentView = container
        window = w
    }

    /// Polls `/health` every 500 ms for up to 20 s, then loads the wizard.
    private func waitForManagerThenLoad() {
        waitTask?.cancel()
        placeholder.failed = false
        waitTask = Task { [weak self] in
            guard let self else { return }
            for _ in 0..<40 {
                if Task.isCancelled { return }
                if await self.model.api.health() {
                    self.load()
                    return
                }
                try? await Task.sleep(for: .milliseconds(500))
            }
            self.placeholder.failed = true
        }
    }

    private func load() {
        let dark = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        let theme = WelcomeBridge.themeParameter(preference: model.settings.theme, systemIsDark: dark)
        let url = WelcomeBridge.welcomeURL(manager: model.managerBaseURL, theme: theme)
        webView?.load(URLRequest(url: url))
        webView?.isHidden = false
    }

    // MARK: Bridge

    fileprivate func didReceive(_ message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame,
              WelcomeBridge.isAllowedOrigin(scheme: origin.protocol, host: origin.host, port: origin.port,
                                            manager: model.managerBaseURL)
        else { return }
        let (requestId, parsed) = WelcomeBridge.parse(JSONValue(any: message.body))
        switch parsed {
        case .failure(let error):
            reply(requestId, error.reply)
        case .success(let request):
            Task { await handle(request) }
        }
    }

    private func handle(_ request: BridgeRequest) async {
        switch request.command {
        case .hello:
            let version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.1.0"
            reply(request.requestId, WelcomeBridge.helloReply(appVersion: version))
        case .pickFolder(let target, let current):
            reply(request.requestId, await pickFolder(target: target, current: current))
        case .openHomebrewInstaller:
            reply(request.requestId, TerminalRunner.run(TerminalCommandPolicy.homebrewInstall))
        case .openTerminal(let command):
            guard TerminalCommandPolicy.isAllowed(command) else {
                reply(request.requestId, WelcomeBridge.errorReply("command not allowed"))
                return
            }
            reply(request.requestId, TerminalRunner.run(command))
        case .openURL(let url):
            let ok = NSWorkspace.shared.open(url)
            reply(request.requestId, ok ? WelcomeBridge.okReply : WelcomeBridge.errorReply("could not open URL"))
        case .closeWelcome(let done):
            reply(request.requestId, WelcomeBridge.okReply)
            completed = done
            window?.close()
        }
    }

    private func pickFolder(target: String?, current: String?) async -> JSONValue {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        switch target {
        case "models": panel.message = "Choose where Splash GUI keeps models."
        case "cache": panel.message = "Choose where Splash GUI keeps the SSD cache."
        default: panel.message = "Choose a folder."
        }
        if let current, !current.isEmpty {
            panel.directoryURL = URL(fileURLWithPath: (current as NSString).expandingTildeInPath, isDirectory: true)
        }
        let response: NSApplication.ModalResponse
        if let window {
            response = await panel.beginSheetModal(for: window)
        } else {
            response = panel.runModal()
        }
        guard response == .OK, let url = panel.url else { return WelcomeBridge.cancelledReply }
        return WelcomeBridge.pathReply(url.path)
    }

    private func reply(_ requestId: String?, _ result: JSONValue) {
        guard let requestId, let webView else { return }
        webView.evaluateJavaScript(WelcomeBridge.replyScript(requestId: requestId, result: result), completionHandler: nil)
    }

    // MARK: Navigation: links off the manager's origin open in the default browser

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction) async
        -> WKNavigationActionPolicy
    {
        guard let url = navigationAction.request.url else { return .cancel }
        if url.scheme == "about" { return .allow }
        if WelcomeBridge.isAllowedOrigin(scheme: url.scheme, host: url.host(percentEncoded: false), port: url.port,
                                         manager: model.managerBaseURL),
           navigationAction.targetFrame != nil
        {
            return .allow
        }
        NSWorkspace.shared.open(url)
        return .cancel
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView?
    {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: NSWindowDelegate

    func windowWillClose(_ notification: Notification) {
        waitTask?.cancel()
        model.welcomeClosed(completed: completed)
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: WelcomeBridge.handlerName)
        onClose()
    }
}

/// Breaks the WKUserContentController → handler retain cycle.
private final class WeakMessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: WelcomeWindowController?

    init(_ target: WelcomeWindowController) {
        self.target = target
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        MainActor.assumeIsolated { target?.didReceive(message) }
    }
}

/// Opens Terminal with a command: AppleScript first (needs Automation permission for
/// Terminal), else a temporary `.command` file opened with Terminal.
@MainActor
enum TerminalRunner {
    static func run(_ command: String) -> JSONValue {
        var error: NSDictionary?
        if let script = NSAppleScript(source: TerminalCommandPolicy.appleScript(for: command)) {
            script.executeAndReturnError(&error)
            if error == nil { return WelcomeBridge.okReply }
        }
        do {
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("splash-gui-\(UUID().uuidString.prefix(8)).command")
            try TerminalCommandPolicy.commandFile(for: command).write(to: url, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
            let terminal = URL(fileURLWithPath: "/System/Applications/Utilities/Terminal.app")
            NSWorkspace.shared.open([url], withApplicationAt: terminal, configuration: NSWorkspace.OpenConfiguration())
            return WelcomeBridge.okReply
        } catch {
            let why = (error as NSError).localizedDescription
            return WelcomeBridge.errorReply("could not open Terminal: \(why)")
        }
    }
}

// MARK: Native placeholder (D-10-8)

@MainActor
@Observable
final class PlaceholderState {
    var failed = false
}

struct PlaceholderView: View {
    let state: PlaceholderState
    let showLog: () -> Void
    let tryAgain: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        let dark = scheme == .dark
        VStack(alignment: .leading, spacing: 20) {
            Spacer()
            Text(state.failed ? "COULDN'T START\nTHE MANAGER." : "STARTING\nSPLASH GUI.")
                .font(FontLoader.display(56))
                .foregroundStyle(Tokens.ink(dark: dark))
            if state.failed {
                HStack(spacing: 12) {
                    SquareButton("Show Log", action: showLog)
                    SquareButton("Try Again", action: tryAgain)
                }
            } else {
                ProgressView().controlSize(.small)
            }
            Spacer()
        }
        .padding(48)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background(Tokens.bg(dark: dark))
    }
}

import AppKit
import SplashGUIKit
import SwiftUI
import UserNotifications

/// Composition root and the `MenuBarHost` (alerts, windows, opening URLs).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, MenuBarHost {
    let model: MenuBarViewModel
    let api: HTTPAdminAPI
    let bundled: Bool

    private var welcome: WelcomeWindowController?
    private var about: AboutWindowController?
    private var responder: NotificationResponder?
    private var terminationApproved = false
    private var powerOffTask: Task<Void, Never>?
    private var observers: [NSObjectProtocol] = []

    override init() {
        let paths = HomePaths.fromEnvironment()
        let settings = paths.readLocalSettings()
        let api = HTTPAdminAPI(baseURL: settings.managerBaseURL, tokens: CLITokenReader(paths: paths))
        self.api = api
        bundled = Bundle.main.bundlePath.hasSuffix(".app")

        let streams: @Sendable (String) -> AsyncStream<SSEClientEvent> = { path in
            SSEClient { SSEClient.request(url: api.url(for: path), headers: api.authHeaders()) }.events()
        }
        let agent: AppService? = ManagerAgent.plistInBundle() ? SMAppServiceWrapper.agent() : nil
        let manager = ManagerController(api: api, paths: paths, repo: RepoLocator(), agent: agent)
        let loginItems = bundled ? LoginItemController(mainApp: SMAppServiceWrapper.mainApp) : nil
        model = MenuBarViewModel(
            api: api, settings: settings, paths: paths,
            setBaseURL: { api.baseURL = $0 },
            streams: streams,
            notifier: UserNotificationPoster(),
            managerController: manager,
            loginItems: loginItems)
        super.init()
        model.host = self
        model.updater = SparkleUpdater()
    }

    // MARK: NSApplicationDelegate

    func applicationWillFinishLaunching(_ notification: Notification) {
        // A second copy (another build path) activates the running one and leaves. `exit` skips
        // the quit flow, which could stop the shared server; nothing has started yet (start() runs later).
        if bundled, SingleInstance.deferToRunningInstance() { exit(0) }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        FontLoader.registerBundledFonts()

        // PKG-16: a packaged app that replaced the development bundle moves the manager agent to the bundled
        // interpreter before the manager starts. A development bundle (no bundled runtime) does nothing here.
        let migration = DevAgentMigration.plan(
            currentLabel: ManagerAgent.label, legacyLabels: DevAgentMigration.legacyLabels,
            packaged: BundledRuntime().python != nil, printer: DevAgentMigration.launchctlPrint)
        DevAgentMigration.apply(migration, agent: ManagerAgent.plistInBundle() ? SMAppServiceWrapper.agent() : nil)

        if bundled {
            let model = self.model
            let responder = NotificationResponder { action, info in
                await model.handleNotificationResponse(actionIdentifier: action, userInfo: info)
            }
            self.responder = responder
            UNUserNotificationCenter.current().delegate = responder
        }

        model.reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let ws = NSWorkspace.shared.notificationCenter
        observers.append(ws.addObserver(forName: NSWorkspace.willPowerOffNotification, object: nil, queue: .main) {
            [weak self] _ in
            MainActor.assumeIsolated { self?.willPowerOff() }
        })
        observers.append(ws.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.model.reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            }
        })
        // The only menu this accessory app shows is the status menu: use tracking as "menu open".
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main) {
            [weak self] _ in
            MainActor.assumeIsolated { self?.model.menuOpen = true }
        })
        observers.append(nc.addObserver(forName: NSMenu.didEndTrackingNotification, object: nil, queue: .main) {
            [weak self] _ in
            MainActor.assumeIsolated { self?.model.menuOpen = false }
        })

        Task { await model.start() }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationApproved { return .terminateNow }
        if let powerOffTask {
            // Logout / shutdown: restore desktop integrations first (≤ 10 s, SPEC §13.3).
            Task {
                await powerOffTask.value
                NSApp.reply(toApplicationShouldTerminate: true)
            }
            return .terminateLater
        }
        // Quit from outside the menu (e.g. `osascript -e 'quit app "Splash GUI"'`): same flow as ⌘Q.
        Task {
            let outcome = await model.quitCoordinator.quit(
                stopServer: model.settings.stopOnQuit, requestsInFlight: model.engine?.requestsInFlight ?? 0)
            NSApp.reply(toApplicationShouldTerminate: outcome != .cancelled)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.stop()
    }

    private func willPowerOff() {
        guard powerOffTask == nil else { return }
        let model = self.model
        powerOffTask = Task { await model.restoreIntegrationsForPowerOff(timeout: 10) }
    }

    // MARK: MenuBarHost

    func open(_ url: URL) {
        NSWorkspace.shared.open(url)
    }

    func openFile(_ url: URL) {
        let console = URL(fileURLWithPath: "/System/Applications/Utilities/Console.app")
        if FileManager.default.fileExists(atPath: url.path), FileManager.default.fileExists(atPath: console.path) {
            NSWorkspace.shared.open([url], withApplicationAt: console, configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.activateFileViewerSelecting([url.deletingLastPathComponent()])
        }
    }

    func confirm(message: String, info: String, confirmTitle: String) async -> Bool {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = info
        alert.alertStyle = .warning
        alert.addButton(withTitle: confirmTitle)
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }

    func showError(title: String, message: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    /// The Remove Splash GUI Data… sheet (SPEC §19, docs/ui/10 §8; PKG-12): the steps, two unticked
    /// boxes with sizes, and a typed DELETE when either is ticked. Nil when the user cancels.
    func chooseRemoval(_ plan: UninstallSummary) async -> RemovalChoice? {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = "Remove Splash GUI data?"
        var info = plan.steps.map { "· \($0)" }.joined(separator: "\n")
        info += "\n\nData folder \(plan.home): \(Format.bytes(plan.dataBytes))."
        for path in plan.kept { info += "\nKept: \(path) is outside the data folder." }
        alert.informativeText = info
        alert.alertStyle = .critical
        let models = NSButton(checkboxWithTitle: "Delete models (\(Format.bytes(plan.modelsBytes)))", target: nil, action: nil)
        let cache = NSButton(checkboxWithTitle: "Delete cache (\(Format.bytes(plan.cacheBytes)))", target: nil, action: nil)
        let typed = NSTextField(string: "")
        typed.placeholderString = "Type DELETE to delete models or the cache"
        let stack = NSStackView(views: [models, cache, typed])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.frame = NSRect(x: 0, y: 0, width: 340, height: 78)
        alert.accessoryView = stack
        alert.addButton(withTitle: "Remove")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return nil }
        let choice = RemovalChoice(deleteModels: models.state == .on, deleteCache: cache.state == .on)
        if (choice.deleteModels || choice.deleteCache) && typed.stringValue != "DELETE" {
            showError(title: "Nothing was removed", message: "Type DELETE to delete models or the cache.")
            return nil
        }
        return choice
    }

    func showAbout() {
        if about == nil { about = AboutWindowController(model: model, host: self) }
        about?.show()
    }

    func showWelcome() {
        if welcome == nil {
            welcome = WelcomeWindowController(model: model) { [weak self] in self?.welcome = nil }
        }
        welcome?.show()
    }

    func terminate() {
        terminationApproved = true
        NSApp.terminate(nil)
    }
}

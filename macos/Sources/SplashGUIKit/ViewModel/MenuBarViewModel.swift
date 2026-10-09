import Foundation
import Observation

/// UI services the view model needs from the app target (alerts, windows, opening things).
@MainActor
public protocol MenuBarHost: AnyObject {
    func open(_ url: URL)
    func openFile(_ url: URL)
    /// A native alert with a default button and Cancel; true when the default was chosen.
    func confirm(message: String, info: String, confirmTitle: String) async -> Bool
    func showError(title: String, message: String)
    func showAbout()
    func showWelcome()
    func terminate()
    /// The Remove Splashboard Data… sheet (PKG-12): nil when the user cancels.
    func chooseRemoval(_ plan: UninstallSummary) async -> RemovalChoice?
}

/// The menu bar app's state and behaviour. Everything UI-free lives here so it can be tested.
@MainActor
@Observable
public final class MenuBarViewModel {
    // MARK: Observed state
    public private(set) var manager: ManagerPhase = .unknown
    public private(set) var engine: EngineView?
    public private(set) var settings: AppSettings
    public private(set) var models: [InstalledModel]?
    public private(set) var integrations: IntegrationsView?
    public private(set) var metrics: LiveMetrics?
    public private(set) var usageToday: UsageToday?
    public private(set) var versions: VersionsInfo?
    public private(set) var downloads: [String: DownloadItem] = [:]
    public private(set) var gpuUtilization: Int?
    public private(set) var notificationsDenied = false
    public private(set) var eventsConnected = false
    /// Last known decode tok/s while busy (kept for the 2 s linger).
    public private(set) var lastTps: Double?
    public private(set) var busyEndedAt: Date?
    public private(set) var tick = 0
    public private(set) var now = Date()
    public var reduceMotion = false {
        didSet { reconcileAnimation() }
    }
    public var menuOpen = false {
        didSet {
            guard menuOpen != oldValue else { return }
            if menuOpen { Task { await refreshInventory() } }
            reconcileMetrics()
        }
    }
    /// The welcome window was closed before the wizard finished.
    public var welcomeDismissedUnfinished = false
    public private(set) var wizardCompletedLocally = false

    // MARK: Derived

    public var iconKind: IconKind { Presentation.iconKind(engine: engine?.state, manager: manager) }
    public var icon: StatusIcon { Presentation.icon(kind: iconKind, tick: tick, reduceMotion: reduceMotion) }
    public var title: StatusTitle {
        StatusTitle.make(settings: settings, engine: engine, manager: manager, metrics: metrics, lastTps: lastTps,
                         busyEndedAt: busyEndedAt, now: now, gpu: gpuUtilization, reduceMotion: reduceMotion)
    }
    public var accessibilityLabel: String { Presentation.accessibilityLabel(engine, manager: manager) }
    public var setupUnfinished: Bool {
        welcomeDismissedUnfinished && !settings.wizardCompleted && !wizardCompletedLocally
    }
    public var menuInput: MenuInput {
        MenuInput(manager: manager, engine: engine, settings: settings, models: models, integrations: integrations,
                  metrics: metrics, usageToday: usageToday, downloads: Array(downloads.values),
                  notificationsDenied: notificationsDenied, setupUnfinished: setupUnfinished)
    }
    public var menu: [MenuEntry] { MenuModel.build(menuInput) }
    public var managerBaseURL: URL { settings.managerBaseURL }

    // MARK: Dependencies
    @ObservationIgnored public let api: AdminAPI
    @ObservationIgnored private let setBaseURL: @Sendable (URL) -> Void
    @ObservationIgnored private let streams: @Sendable (String) -> AsyncStream<SSEClientEvent>
    @ObservationIgnored private let notifier: NotificationPosting
    @ObservationIgnored private let managerController: ManagerController?
    @ObservationIgnored private let loginItems: LoginItemController?
    @ObservationIgnored private let gpu: GPUSampling
    @ObservationIgnored public let paths: HomePaths
    @ObservationIgnored public weak var host: MenuBarHost?
    /// Sparkle in a packaged build (PKG-9); nil when the bundle has no feed.
    @ObservationIgnored public var updater: Updater?
    @ObservationIgnored public private(set) var quitCoordinator: QuitCoordinator!

    @ObservationIgnored private var eventsTask: Task<Void, Never>?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var metricsTask: Task<Void, Never>?
    @ObservationIgnored private var animationTask: Task<Void, Never>?
    @ObservationIgnored private var gpuTask: Task<Void, Never>?
    @ObservationIgnored private var lingerTask: Task<Void, Never>?
    @ObservationIgnored private var lastContact: Date?
    @ObservationIgnored private var appliedLaunchAtLogin: Bool?
    @ObservationIgnored private var welcomeShownThisSession = false
    @ObservationIgnored private var startedAt = Date()

    public init(
        api: AdminAPI, settings: AppSettings, paths: HomePaths,
        setBaseURL: @escaping @Sendable (URL) -> Void = { _ in },
        streams: @escaping @Sendable (String) -> AsyncStream<SSEClientEvent> = { _ in AsyncStream { $0.finish() } },
        notifier: NotificationPosting, managerController: ManagerController? = nil,
        loginItems: LoginItemController? = nil, gpu: GPUSampling = IOKitGPUSampler()
    ) {
        self.api = api
        self.settings = settings
        self.paths = paths
        self.setBaseURL = setBaseURL
        self.streams = streams
        self.notifier = notifier
        self.managerController = managerController
        self.loginItems = loginItems
        self.gpu = gpu
        self.quitCoordinator = QuitCoordinator(api: api, manager: managerController) { [weak self] n in
            let text = QuitCoordinator.confirmationText(inFlight: n)
            return await self?.host?.confirm(message: text.message, info: text.info, confirmTitle: "Quit and Stop") ?? false
        }
    }

    public static let eventsPath = "/api/admin/events?client=menubar"

    // MARK: Lifecycle

    /// Finds or starts the manager, then starts the live loops.
    public func start() async {
        startedAt = Date()
        await startManager()
        startPolling()
        startEvents()
        reconcileGPU()
    }

    public func stop() {
        for t in [eventsTask, pollTask, metricsTask, animationTask, gpuTask, lingerTask] { t?.cancel() }
    }

    public func startManager() async {
        guard let managerController else {
            await refreshEngine()
            return
        }
        setManager(.starting)
        switch await managerController.ensureRunning() {
        case .success:
            await refreshEngine()
            if engine == nil { setManager(.starting) }
        case .failure(let error):
            setManager(.failedToStart(error.description))
        }
    }

    // MARK: Polling (fallback while /events is unavailable, heartbeat otherwise)

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            var n = 0
            while !Task.isCancelled {
                guard let self else { return }
                await self.refreshEngine()
                if !self.eventsConnected, n % 3 == 0, self.manager == .running { await self.refreshSettings() }
                if n % 15 == 0, self.manager == .running { await self.refreshInventory() }
                n += 1
                let interval: Double = self.eventsConnected ? 15 : 2
                try? await Task.sleep(for: .seconds(interval))
            }
        }
    }

    /// `GET /engine`; drives the manager phase (down after 5 s without an answer).
    public func refreshEngine() async {
        do {
            let view = try await api.engine()
            lastContact = Date()
            let wasRunning = manager == .running
            applyEngine(view)
            setManager(.running)
            if !wasRunning { await managerBecameReachable() }
        } catch {
            let silent = Date().timeIntervalSince(lastContact ?? startedAt)
            if case .failedToStart = manager { return }
            if manager == .starting, silent < 25 { return }
            if silent > 5 || manager == .unknown {
                setManager(.down)
                engine = nil
            }
        }
    }

    private func managerBecameReachable() async {
        await refreshSettings()
        await refreshInventory()
        versions = try? await api.versions()
        if !settings.wizardCompleted, !welcomeShownThisSession {
            welcomeShownThisSession = true
            host?.showWelcome()
        } else if settings.wizardCompleted {
            await requestNotificationPermission()
        }
    }

    public func refreshSettings() async {
        guard let response = try? await api.settingsResponse(),
              response[path: "settings.global"]?.object != nil || response["global"]?.object != nil
        else { return }
        applySettings(AppSettings(response: response))
    }

    public func refreshInventory() async {
        guard manager == .running else { return }
        async let m = try? api.models()
        async let i = try? api.integrations()
        async let u = try? api.usageToday()
        let (models, integrations, usage) = await (m, i, u)
        if let models { self.models = models }
        if let integrations { self.integrations = integrations }
        self.usageToday = usage
    }

    public func refreshVersions() async {
        versions = try? await api.versions()
    }

    // MARK: Applying state

    public func applySettings(_ new: AppSettings) {
        let old = settings
        settings = new
        if old.managerBaseURL != new.managerBaseURL { setBaseURL(new.managerBaseURL) }
        if appliedLaunchAtLogin != new.launchAtLogin {
            appliedLaunchAtLogin = new.launchAtLogin
            loginItems?.apply(launchAtLogin: new.launchAtLogin)
        }
        if old.showGPU != new.showGPU { reconcileGPU() }
        reconcileMetrics()
    }

    public func applyEngine(_ view: EngineView) {
        let previous = engine?.state
        engine = view
        now = Date()
        if previous == .busy, view.state != .busy {
            busyEndedAt = now
            scheduleLingerRefresh()
        } else if view.state == .busy {
            busyEndedAt = nil
        }
        if !view.state.hasProcess { metrics = nil }
        reconcileAnimation()
        reconcileMetrics()
    }

    public func applyMetrics(_ m: LiveMetrics) {
        metrics = m
        if let tps = m.decodeTps, (engine?.state == .busy || m.engineState == .busy) { lastTps = tps }
        now = Date()
    }

    private func setManager(_ phase: ManagerPhase) {
        guard manager != phase else { return }
        manager = phase
        reconcileAnimation()
        reconcileMetrics()
    }

    private func scheduleLingerRefresh() {
        lingerTask?.cancel()
        lingerTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(StatusTitle.tokpsLinger + 0.1))
            guard !Task.isCancelled else { return }
            self?.now = Date()
        }
    }

    // MARK: Events (GET /api/admin/events)

    private func startEvents() {
        eventsTask?.cancel()
        // `client=menubar` tells the manager to stop its osascript fallback (docs/api.md changelog).
        let stream = streams(Self.eventsPath)
        eventsTask = Task { [weak self] in
            for await item in stream {
                guard let self else { return }
                switch item {
                case .connected:
                    self.eventsConnected = true
                case .disconnected:
                    self.eventsConnected = false
                case .event(let ev):
                    await self.handle(ev)
                }
            }
        }
    }

    /// Handles one event from `/events`. Unknown event names are ignored (docs/api.md §1.4).
    public func handle(_ event: SSEEvent) async {
        guard let data = event.json else { return }
        switch event.event {
        case "hello":
            lastContact = Date()
            if let e = data["engine"], e.object != nil { applyEngine(EngineView(json: e)) }
            downloads = [:]
            for d in (data["downloads"]?.array ?? []).compactMap(DownloadItem.init(json:)) { downloads[d.id] = d }
            if manager != .running {
                setManager(.running)
                await managerBecameReachable()
            }
        case "engine.state":
            lastContact = Date()
            applyEngine(EngineView(json: data))
        case "notification":
            if let n = NotificationPayload(json: data), let mapped = NotificationMapper.map(n, settings: settings) {
                await notifier.post(mapped)
            }
        case "download.progress", "download.state":
            if let d = DownloadItem(json: data) {
                downloads[d.id] = d
                if !d.isActive {
                    downloads[d.id] = nil
                    if event.event == "download.state" { await refreshInventory() }
                }
            }
        case "models.changed":
            if let m = try? await api.models() { models = m }
        case "settings.changed":
            await refreshSettings()
        case "app.check_updates":
            // The web About page's Check for updates (`POST /app/check-updates`, PKG-9).
            await checkForUpdates()
        case "integration.state":
            if let d = DesktopIntegration(json: data) {
                var view = integrations ?? IntegrationsView()
                if let i = view.desktop.firstIndex(where: { $0.name == d.name }) {
                    view.desktop[i] = d
                } else {
                    view.desktop.append(d)
                }
                integrations = view
            }
        default:
            break  // alert, alert.cleared, job, engine.upgrade, benchmark.progress, future events
        }
    }

    // MARK: Live metrics (only while a gauge/t/s item is on or the menu is open)

    public var needsLiveMetrics: Bool {
        guard manager == .running, let state = engine?.state, state.hasProcess else { return false }
        return settings.showTokps || settings.showMemory || menuOpen
    }

    private func reconcileMetrics() {
        if needsLiveMetrics {
            guard metricsTask == nil else { return }
            let stream = streams("/api/admin/metrics/live")
            metricsTask = Task { [weak self] in
                for await item in stream {
                    guard let self else { return }
                    if case .event(let ev) = item, ev.event == "snapshot", let json = ev.json {
                        self.applyMetrics(LiveMetrics(json: json))
                    }
                }
            }
        } else {
            metricsTask?.cancel()
            metricsTask = nil
        }
    }

    // MARK: Animation and GPU

    private func reconcileAnimation() {
        let animate = Presentation.animates(iconKind) && !reduceMotion
        if animate {
            guard animationTask == nil else { return }
            animationTask = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(StatusIcon.loadingFrameInterval))
                    guard let self else { return }
                    self.tick &+= 1
                }
            }
        } else {
            animationTask?.cancel()
            animationTask = nil
        }
    }

    private func reconcileGPU() {
        gpuTask?.cancel()
        gpuTask = nil
        guard settings.showGPU else {
            gpuUtilization = nil
            return
        }
        let sampler = gpu
        gpuTask = Task { [weak self] in
            while !Task.isCancelled {
                let value = await Task.detached { sampler.utilization() }.value
                guard let self else { return }
                self.gpuUtilization = value
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    // MARK: Notifications

    public func requestNotificationPermission() async {
        _ = await notifier.requestAuthorization()
        notificationsDenied = await notifier.isDenied()
    }

    /// A tap on a delivered notification (body or action button).
    public func handleNotificationResponse(actionIdentifier: String, userInfo: [String: String]) async {
        switch NotificationMapper.resolve(actionIdentifier: actionIdentifier, userInfo: userInfo) {
        case .none:
            break
        case .openPage(let path):
            await openAdminSignedIn(path)
        case .call(let action):
            do {
                try await api.perform(action)
            } catch let e as APIError {
                await notifier.post(NotificationMapper.failureNotification(for: action, error: e))
            } catch {
                await notifier.post(NotificationMapper.failureNotification(
                    for: action, error: .unreachable(error.localizedDescription)))
            }
        }
    }

    // MARK: Shutdown (NSWorkspace.willPowerOffNotification, SPEC §13.3)

    /// `POST /api/admin/integrations/restore-all`, bounded so logout is never held > 10 s.
    public func restoreIntegrationsForPowerOff(timeout: Double = 10) async {
        let api = self.api
        await withTaskGroup(of: Void.self) { group in
            group.addTask { _ = try? await api.restoreAllIntegrations() }
            group.addTask { try? await Task.sleep(for: .seconds(timeout)) }
            await group.next()
            group.cancelAll()
        }
    }

    // MARK: Welcome

    public func welcomeClosed(completed: Bool) {
        if completed {
            wizardCompletedLocally = true
            welcomeDismissedUnfinished = false
        } else {
            welcomeDismissedUnfinished = true
        }
        Task {
            await refreshSettings()
            await requestNotificationPermission()
        }
    }

    // MARK: Commands

    public func url(forAdminPath path: String) -> URL {
        var base = managerBaseURL.absoluteString
        if base.hasSuffix("/") { base.removeLast() }
        return URL(string: base + (path.hasPrefix("/") ? path : "/" + path)) ?? managerBaseURL
    }

    /// Opens an admin page in the browser, signed in (D58): mints a one-time link with the CLI
    /// token and opens `/admin/login?code=…&next=<path>`. If minting fails (an older manager,
    /// no token yet), the plain page opens and the browser asks for the key if it needs to.
    public func openAdmin(_ path: String) {
        Task { await openAdminSignedIn(path) }
    }

    public func openAdminSignedIn(_ path: String) async {
        let url = await signedInURL(forAdminPath: path)
        host?.open(url)
    }

    /// The one-time link URL for `path`, or the plain admin URL when no link can be minted.
    public func signedInURL(forAdminPath path: String) async -> URL {
        let plain = url(forAdminPath: path)
        guard let link = try? await api.mintLoginLink(),
              let url = link.url(base: managerBaseURL, next: path)
        else { return plain }
        return url
    }

    /// Sparkle's check when this build has an updater (PKG-9). Without one (no feed: `swift run`, the dev
    /// bundle) the About section of Settings shows the versions instead, as before Sparkle.
    public func checkForUpdates() async {
        if let updater {
            if updater.canCheckForUpdates { updater.checkForUpdates() }
            return
        }
        if manager == .running { await openAdminSignedIn("/admin/settings/about") } else { host?.showAbout() }
    }

    /// About → Remove Splashboard Data… (SPEC §19, PKG-12). The manager restores the integrations and
    /// removes the PATH block, the shim and the chosen folders (`stop: false`); a failed restore stops
    /// here with nothing removed (D36). Then the app unregisters its login item and the manager's
    /// LaunchAgent, which only it can do (unregistering the agent stops a running manager), stops a
    /// manager it did not start through `POST /shutdown`, and quits.
    public func removeData() async {
        let plan: UninstallSummary
        do {
            plan = UninstallSummary(json: try await api.post("/api/admin/uninstall/plan"))
        } catch {
            host?.showError(title: "Couldn’t read what would be removed", message: error.localizedDescription)
            return
        }
        guard let choice = await host?.chooseRemoval(plan) else { return }
        let body: JSONValue = [
            "delete_data": true, "delete_models": .bool(choice.deleteModels),
            "delete_cache": .bool(choice.deleteCache), "stop": false,
        ]
        do {
            _ = try await api.post("/api/admin/uninstall", body: body)
        } catch {
            host?.showError(title: "Couldn’t remove Splashboard data", message: error.localizedDescription)
            return
        }
        var problems: [String] = []
        if let error = loginItems?.unregisterForRemoval() {
            problems.append("The login item could not be removed (\(error)). Remove Splashboard in System Settings → General → Login Items.")
        }
        switch managerController?.unregisterAgentForRemoval() ?? .notRegistered {
        case .unregistered:
            break
        case .failed(let error):
            problems.append("The manager's background item could not be removed (\(error)). Remove it in System Settings → General → Login Items.")
            // The agent stays registered, so stop the manager explicitly. It exits 0, which launchd does not restart.
            _ = try? await api.post("/api/admin/shutdown")
        case .notRegistered:
            if managerController?.ownership == .child {
                await managerController?.stopOwned()
            } else {
                _ = try? await api.post("/api/admin/shutdown")
            }
        }
        // A login item or agent that is still registered could start the app or manager again and recreate the data:
        // keep the app open and say what is left instead of quitting as if the removal were complete.
        guard problems.isEmpty else {
            host?.showError(title: "Splashboard data was removed, but not everything", message: problems.joined(separator: "\n\n"))
            return
        }
        host?.terminate()
    }

    public func perform(_ command: MenuCommand) async {
        switch command {
        case .startManager:
            await startManager()
        case .showManagerLog:
            let fm = FileManager.default
            host?.openFile(fm.fileExists(atPath: paths.managerLog.path) ? paths.managerLog : paths.managerLaunchLog)
        case .stopServer, .unloadModel:
            await stopEngine(confirmTitle: "Stop")
        case .cancelLoading:
            await call { try await $0.stopEngine() }
        case .loadModel(let id):
            await load(id)
        case .restartEngine:
            let inFlight = engine?.requestsInFlight ?? 0
            if inFlight > 0 {
                let noun = inFlight == 1 ? "request is" : "requests are"
                guard await host?.confirm(message: "\(inFlight) \(noun) running.",
                                          info: "Restart the engine anyway? Clients will get an error.",
                                          confirmTitle: "Restart") == true
                else { return }
            }
            await interruptingInstall(model: nil) { api, force in try await api.restartEngine(force: force || inFlight > 0) }
        case .openAdmin(let path):
            await openAdminSignedIn(path)
        case .connectIntegration(let name):
            await openAdminSignedIn("/admin/integrations?connect=\(name)")
        case .disconnectIntegration(let name):
            let label = integrations?.desktop.first { $0.name == name }?.label ?? name
            let app = label.split(separator: " ").first.map(String.init) ?? label
            guard await host?.confirm(
                message: "Disconnect and restore \(app)'s configuration?",
                info: "\(app) will restart if it is running.", confirmTitle: "Disconnect") == true
            else { return }
            await call { try await $0.disconnectIntegration(name) }
        case .restoreIntegration(let name):
            await call { try await $0.disconnectIntegration(name) }
        case .openTerminal(let client):
            await call { try await $0.openTerminal(client: client) }
        case .checkForUpdates:
            await checkForUpdates()
        case .about:
            host?.showAbout()
        case .continueSetup:
            host?.showWelcome()
        case .openNotificationSettings:
            if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
                host?.open(url)
            }
        case .quit(let stopServer):
            let outcome = await quitCoordinator.quit(stopServer: stopServer,
                                                     requestsInFlight: engine?.requestsInFlight ?? 0)
            if case .terminate = outcome { host?.terminate() }
        }
    }

    private func stopEngine(confirmTitle: String) async {
        let inFlight = engine?.requestsInFlight ?? 0
        if inFlight > 0 {
            let noun = inFlight == 1 ? "request is" : "requests are"
            guard await host?.confirm(message: "\(inFlight) \(noun) running.",
                                      info: "Stop the engine anyway? Clients will get an error.",
                                      confirmTitle: confirmTitle) == true
            else { return }
        }
        await call { try await $0.stopEngine() }
    }

    /// Asks before a Load or Restart stops an install (Splash can't resume the file in progress,
    /// SPEC Q24): up front while the engine shows `starting.installing`, and again on a 409
    /// `install_in_progress`; Yes reruns the call with `force`. Loading the model that is being
    /// installed doesn't ask: the manager answers 202 and keeps the install (an older manager
    /// answered 409 with `details.same_model`, which is treated the same way, since `force` would
    /// change nothing).
    private func confirmInterruptInstall() async -> Bool {
        let q = Presentation.interruptInstall(engine)
        return await host?.confirm(message: q.message, info: q.info, confirmTitle: q.confirmTitle) == true
    }

    private func interruptingInstall(model: String?, _ body: (AdminAPI, Bool) async throws -> Any) async {
        var force = false
        if let e = engine, e.isInstalling, model == nil || model != e.model {
            guard await confirmInterruptInstall() else { return }
            force = true
        }
        do {
            _ = try await body(api, force)
        } catch let e as APIError where e.isSameModelInstall {
            // Nothing to interrupt: the model asked for is the one installing.
        } catch let e as APIError where e.code == "install_in_progress" && !force {
            guard await confirmInterruptInstall() else { return }
            await call { try await body($0, true) }
            return
        } catch let e as APIError {
            host?.showError(title: "Splashboard", message: e.userMessage)
        } catch {
            host?.showError(title: "Splashboard", message: error.localizedDescription)
        }
        await refreshEngine()
    }

    private func load(_ id: String) async {
        var force = false
        if let e = engine, e.state.isServing, e.requestsInFlight > 0 {
            let noun = e.requestsInFlight == 1 ? "request is" : "requests are"
            guard await host?.confirm(message: "\(e.requestsInFlight) \(noun) running.",
                                      info: "Switch models anyway? Clients will get an error.",
                                      confirmTitle: "Switch") == true
            else { return }
            force = true
        }
        if let e = engine, e.isInstalling, e.model != id {
            guard await confirmInterruptInstall() else { return }
            force = true
        }
        do {
            applyEngine(try await api.loadModel(id, force: force))
        } catch let e as APIError where e.isSameModelInstall {
            await refreshEngine()
        } catch let e as APIError where e.code == "install_in_progress" && !force {
            guard await confirmInterruptInstall() else { return }
            await call { try await $0.loadModel(id, force: true) }
        } catch let e as APIError where e.code == "model_switch_busy" {
            guard await host?.confirm(message: "Requests are running.",
                                      info: "Switch models anyway? Clients will get an error.",
                                      confirmTitle: "Switch") == true
            else { return }
            await call { try await $0.loadModel(id, force: true) }
        } catch let e as APIError {
            host?.showError(title: "Couldn't load \(id)", message: e.userMessage)
        } catch {
            host?.showError(title: "Couldn't load \(id)", message: error.localizedDescription)
        }
    }

    /// Runs an API call; shows an alert on failure and refreshes the engine afterwards.
    private func call(_ body: (AdminAPI) async throws -> Any) async {
        do {
            _ = try await body(api)
        } catch let e as APIError {
            host?.showError(title: "Splashboard", message: e.userMessage)
        } catch {
            host?.showError(title: "Splashboard", message: error.localizedDescription)
        }
        await refreshEngine()
    }
}

import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Development agent migration (PKG-16)")
struct MigrationTests {
    static let devPrint = """
    gui/501/ai.splashgui.manager = {
    \tactive count = 1
    \tpath = /Applications/Splashboard.app/Contents/Library/LaunchAgents/ai.splashgui.manager.plist
    \tprogram = /opt/homebrew/bin/uv
    \targuments = {
    \t\t/opt/homebrew/bin/uv
    \t\trun
    \t\t--project
    \t\t/Users/x/Desktop/Projects/Splash-GUI/manager
    \t\tsplash-gui-manager
    \t}
    \tpid = 87576
    }
    """
    static let packagedPrint = """
    gui/501/ai.splashgui.manager = {
    \tprogram = Contents/Resources/manager/python/bin/python3
    \targuments = {
    \t\tContents/Resources/manager/python/bin/python3
    \t\t-I
    \t\t-B
    \t\t-m
    \t\tsplash_gui.manager
    \t}
    }
    """

    @Test func readsTheArgumentsBlock() {
        #expect(DevAgentMigration.arguments(fromLaunchctlPrint: Self.devPrint)
            == ["/opt/homebrew/bin/uv", "run", "--project", "/Users/x/Desktop/Projects/Splash-GUI/manager", "splash-gui-manager"])
        #expect(DevAgentMigration.arguments(fromLaunchctlPrint: "no such job").isEmpty)
    }

    @Test func recognisesOnlyTheDevelopmentProgram() {
        #expect(DevAgentMigration.isDevelopmentProgram(DevAgentMigration.arguments(fromLaunchctlPrint: Self.devPrint)))
        #expect(!DevAgentMigration.isDevelopmentProgram(DevAgentMigration.arguments(fromLaunchctlPrint: Self.packagedPrint)))
        #expect(!DevAgentMigration.isDevelopmentProgram(["/usr/bin/python3", "--project", "x"]))
        #expect(!DevAgentMigration.isDevelopmentProgram([]))
    }

    @Test func aPackagedAppReregistersADevelopmentAgent() {
        let actions = DevAgentMigration.plan(currentLabel: "ai.splashgui.manager", legacyLabels: [], packaged: true) { _ in
            Self.devPrint
        }
        #expect(actions == [.reregister])
    }

    @Test func aPackagedAgentIsLeftAlone() {
        let actions = DevAgentMigration.plan(currentLabel: "ai.splashgui.manager", legacyLabels: [], packaged: true) { _ in
            Self.packagedPrint
        }
        #expect(actions.isEmpty)
    }

    @Test func theDevelopmentBundleNeverMigratesOrAsksLaunchd() {
        var asked: [String] = []
        let actions = DevAgentMigration.plan(currentLabel: "ai.splashgui.manager",
                                             legacyLabels: ["old.label"], packaged: false) { label in
            asked.append(label)
            return Self.devPrint
        }
        #expect(actions.isEmpty && asked.isEmpty)
    }

    @Test func loadedLegacyLabelsAreBootedOut() {
        let new = "io.github.arnavprabhu.splashboard.manager"
        let actions = DevAgentMigration.plan(currentLabel: new, legacyLabels: ["ai.splashgui.manager", "gone.label"],
                                             packaged: true) { label in
            label == "ai.splashgui.manager" ? Self.devPrint : nil
        }
        #expect(actions == [.bootout("ai.splashgui.manager")])
    }

    @MainActor @Test func reregisterUnregistersThenRegisters() {
        let agent = MockService(.enabled)
        DevAgentMigration.apply([.reregister], agent: agent)
        #expect(agent.unregisterCount.value == 1 && agent.registerCount.value == 1)
        #expect(agent.status == .enabled)
    }
}

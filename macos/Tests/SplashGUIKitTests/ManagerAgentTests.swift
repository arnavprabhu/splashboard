import Testing
@testable import SplashGUIKit

@Suite("Manager agent label comes from the bundle (packaging PKG-1)")
struct ManagerAgentTests {
    @Test func readsTheLabelFromInfoPlist() {
        let info: [String: Any] = [ManagerAgent.labelInfoKey: "ai.splashgui.app.verify.manager"]
        #expect(ManagerAgent.resolveLabel(info: info) == "ai.splashgui.app.verify.manager")
    }

    @Test func fallsBackWithoutTheKey() {
        #expect(ManagerAgent.resolveLabel(info: nil) == "io.github.arnavprabhu.splashboard.manager")
        #expect(ManagerAgent.resolveLabel(info: ["CFBundleName": "Splashboard"]) == "io.github.arnavprabhu.splashboard.manager")
    }

    @Test func fallsBackOnAnEmptyOrWrongTypedValue() {
        #expect(ManagerAgent.resolveLabel(info: [ManagerAgent.labelInfoKey: ""]) == "io.github.arnavprabhu.splashboard.manager")
        #expect(ManagerAgent.resolveLabel(info: [ManagerAgent.labelInfoKey: 42]) == "io.github.arnavprabhu.splashboard.manager")
    }

    @Test func plistNameIsTheLabelWithPlistSuffix() {
        #expect(ManagerAgent.plistName(forLabel: "ai.splashgui.app.verify.manager") == "ai.splashgui.app.verify.manager.plist")
        #expect(ManagerAgent.plistName(forLabel: ManagerAgent.fallbackLabel) == "io.github.arnavprabhu.splashboard.manager.plist")
    }

    @Test func staticLabelMatchesTheTestBundle() {
        // `swift test` runs from the xctest bundle, which has no SplashGUIAgentLabel key.
        #expect(ManagerAgent.label == ManagerAgent.fallbackLabel)
        #expect(ManagerAgent.plistName == "io.github.arnavprabhu.splashboard.manager.plist")
    }
}

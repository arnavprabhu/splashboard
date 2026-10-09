import Testing
@testable import SplashGUIKit

@Suite("Manager agent label comes from the bundle (packaging PKG-1)")
struct ManagerAgentTests {
    @Test func readsTheLabelFromInfoPlist() {
        let info: [String: Any] = [ManagerAgent.labelInfoKey: "ai.splashgui.app.verify.manager"]
        #expect(ManagerAgent.resolveLabel(info: info) == "ai.splashgui.app.verify.manager")
    }

    @Test func fallsBackWithoutTheKey() {
        #expect(ManagerAgent.resolveLabel(info: nil) == "ai.splashgui.manager")
        #expect(ManagerAgent.resolveLabel(info: ["CFBundleName": "Splash GUI"]) == "ai.splashgui.manager")
    }

    @Test func fallsBackOnAnEmptyOrWrongTypedValue() {
        #expect(ManagerAgent.resolveLabel(info: [ManagerAgent.labelInfoKey: ""]) == "ai.splashgui.manager")
        #expect(ManagerAgent.resolveLabel(info: [ManagerAgent.labelInfoKey: 42]) == "ai.splashgui.manager")
    }

    @Test func plistNameIsTheLabelWithPlistSuffix() {
        #expect(ManagerAgent.plistName(forLabel: "ai.splashgui.app.verify.manager") == "ai.splashgui.app.verify.manager.plist")
        #expect(ManagerAgent.plistName(forLabel: ManagerAgent.fallbackLabel) == "ai.splashgui.manager.plist")
    }

    @Test func staticLabelMatchesTheTestBundle() {
        // `swift test` runs from the xctest bundle, which has no SplashGUIAgentLabel key.
        #expect(ManagerAgent.label == ManagerAgent.fallbackLabel)
        #expect(ManagerAgent.plistName == "ai.splashgui.manager.plist")
    }
}

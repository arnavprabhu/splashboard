import Foundation
import Testing
@testable import SplashGUIKit

@Suite("Single instance (QA row 24)")
struct SingleInstanceTests {
    let id = "ai.splash.gui"
    let t0 = Date(timeIntervalSince1970: 1_000)

    func c(_ pid: Int32, _ at: TimeInterval?, bundle: String? = "ai.splash.gui", terminated: Bool = false) -> SingleInstance.Candidate {
        .init(pid: pid, bundleID: bundle, launchDate: at.map { t0.addingTimeInterval($0) }, terminated: terminated)
    }

    @Test func secondCopyDefersToTheRunningOne() {
        let me = c(200, 60)
        #expect(SingleInstance.instanceToDeferTo(bundleID: id, me: me, running: [c(100, 0), me])?.pid == 100)
    }

    @Test func aloneOrFirstKeepsRunning() {
        let me = c(100, 0)
        #expect(SingleInstance.instanceToDeferTo(bundleID: id, me: me, running: [me]) == nil)
        #expect(SingleInstance.instanceToDeferTo(bundleID: id, me: me, running: [me, c(200, 60)]) == nil)
    }

    @Test func simultaneousLaunchesLeaveExactlyOne() {
        let a = c(100, 0), b = c(200, 0)
        let all = [a, b]
        let aDefers = SingleInstance.instanceToDeferTo(bundleID: id, me: a, running: all) != nil
        let bDefers = SingleInstance.instanceToDeferTo(bundleID: id, me: b, running: all) != nil
        #expect(aDefers != bDefers)
        #expect(!aDefers)
    }

    @Test func ignoresTerminatedOtherBundlesAndUnbundledRuns() {
        let me = c(200, 60)
        #expect(SingleInstance.instanceToDeferTo(bundleID: id, me: me, running: [c(100, 0, terminated: true), me]) == nil)
        #expect(SingleInstance.instanceToDeferTo(bundleID: id, me: me, running: [c(100, 0, bundle: "other"), me]) == nil)
        #expect(SingleInstance.instanceToDeferTo(bundleID: nil, me: me, running: [c(100, 0), me]) == nil)
    }
}

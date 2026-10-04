// swift-tools-version: 6.2
// Splash GUI menu bar app (SPEC §13, docs/ui/10-menubar.md).
// SwiftPM package + scripts/bundle.sh instead of an .xcodeproj (docs/spec-drift.md #1).
import PackageDescription

let package = Package(
    name: "SplashGUI",
    platforms: [.macOS("26.4")],
    products: [
        .executable(name: "SplashGUI", targets: ["SplashGUI"]),
        .library(name: "SplashGUIKit", targets: ["SplashGUIKit"]),
    ],
    targets: [
        .target(
            name: "SplashGUIKit",
            swiftSettings: [.swiftLanguageMode(.v6)],
            linkerSettings: [
                .linkedFramework("IOKit"),
                .linkedFramework("ServiceManagement"),
                .linkedFramework("UserNotifications"),
            ]
        ),
        .executableTarget(
            name: "SplashGUI",
            dependencies: ["SplashGUIKit"],
            swiftSettings: [.swiftLanguageMode(.v6)],
            linkerSettings: [.linkedFramework("WebKit")]
        ),
        .testTarget(
            name: "SplashGUIKitTests",
            dependencies: ["SplashGUIKit"],
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
    ]
)

// swift-tools-version: 6.2
// Splashboard menu bar app.
// SwiftPM package + scripts/bundle.sh instead of an .xcodeproj.
import PackageDescription

let package = Package(
    name: "SplashGUI",
    platforms: [.macOS("26.4")],
    products: [
        .executable(name: "SplashGUI", targets: ["SplashGUI"]),
        .library(name: "SplashGUIKit", targets: ["SplashGUIKit"]),
    ],
    // Sparkle 2 for the app's own updates. Exact pin; 2.10.0 was released 2026-09-13.
    dependencies: [
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.0"),
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
            dependencies: ["SplashGUIKit", .product(name: "Sparkle", package: "Sparkle")],
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

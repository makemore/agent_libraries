// swift-tools-version: 5.9
import PackageDescription

// scripts/test_ios_headless.py supplies source symlinks in a temporary directory.
// Test the real headless client without resolving the UI's WhisperKit dependency.
let package = Package(
    name: "AgentClientValidation",
    platforms: [.macOS(.v13)],
    products: [.library(name: "AgentClient", targets: ["AgentClient"])],
    targets: [
        .target(name: "AgentClient", path: "Sources/AgentClient"),
        .testTarget(name: "AgentClientTests", dependencies: ["AgentClient"], path: "Tests/AgentClientTests")
    ]
)
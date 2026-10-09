# Splashboard — menu bar app

SwiftUI `MenuBarExtra` app for macOS 26.4+ (SPEC §13, `docs/ui/10-menubar.md`). A SwiftPM package plus a bundle script, with no `.xcodeproj` (`docs/spec-drift.md` #1). Swift 6 language mode.

| Target | What it holds |
|---|---|
| `SplashGUIKit` (library) | Everything testable: admin API client (`API/`), SSE parser and reconnecting client (`SSE/`), settings, paths and token (`Config/`), notification mapping and posting (`Notifications/`), the Welcome bridge protocol (`Bridge/`), SMAppService wrappers, manager start/stop and the GPU sampler (`System/`), the view model, menu model and quit flow (`ViewModel/`), and the status item renderer (`Icon/`). |
| `SplashGUI` (executable) | `@main` app, menu rendering, AppDelegate (composition root, alerts, power-off), Welcome window (WKWebView), About window. |
| `SplashGUIKitTests` | Swift Testing suites. |

## Build, test, run

```sh
cd macos
swift build
swift test
swift run SplashGUI          # dev: no bundle, so notifications use osascript and there are no login items
scripts/bundle.sh [--variant verify]  # → build/Splashboard.app (release, ad-hoc signed; identity from packaging/identity.env)
open "build/Splashboard.app"
```

## Finding the manager

Packaging is deferred (D30), so the app runs the manager from this checkout: `uv run --project <repo>/manager splash-gui-manager`.

- **Address.** The app reads `server.host` and `server.port` from `$SPLASH_GUI_HOME/settings.json` (default `~/.splash`, 127.0.0.1:8000), then follows `GET /api/admin/settings`.
- **Auth.** Every admin call sends `Authorization: Bearer $(cat ~/.splash/run/cli.token)`.
- **Startup order.** If `GET /health` already answers, the app uses that manager and never stops it. Inside a bundle, it registers the LaunchAgent named by Info.plist `SplashGUIAgentLabel` (`ai.splashgui.manager` by default) through `SMAppService.agent`; the plist is in `Contents/Library/LaunchAgents/` with the repo path baked in. If neither works, it spawns a child process, logging to `~/.splash/logs/manager.launch.log`.
- **Repo path.** In priority order: `SPLASH_GUI_REPO`, `defaults write <bundle id> SplashGUIRepoPath <path>` (the bundle id from `packaging/identity.env`), Info.plist `SplashGUIRepoPath`, the directories above the executable, then `~/Desktop/Projects/Splash-GUI`.
- **Finding uv.** PATH, then `/opt/homebrew/bin`, then `~/.local/bin`.

## Live data

- **`GET /api/admin/events?client=menubar` (SSE).** Carries `hello`, `engine.state`, `notification`, `download.*`, `models.changed`, `settings.changed` and `integration.state`; unknown events are ignored. It reconnects with backoff: the server's `retry` (3 s), doubled up to 30 s; a `501` stub waits 30 s.
- **Polling fallback.** `GET /engine` runs every 2 s while the stream is down, every 15 s otherwise. The manager counts as down after 5 s without an answer.
- **`GET /api/admin/metrics/live` (`snapshot`).** Streams only while `menubar.show_tokps` or `menubar.show_memory` is on, or while the menu is open.
- **GPU gauge.** Sampled every 2 s from IOKit `IOAccelerator` → `PerformanceStatistics` → `Device Utilization %`. It is hidden when that value is unavailable.

## Welcome bridge (WKWebView ⇄ web wizard)

The full contract is documented at the top of `Sources/SplashGUIKit/Bridge/WelcomeBridge.swift`.

- **URL.** `http://<manager>/admin/welcome?host=app`, plus `&theme=dark|light` only when `ui.theme` is `system`.
- **Sending.** JS sends `window.webkit.messageHandlers.splashGUI.postMessage({type, requestId, ...payload})`.
- **Replying.** Native replies with `window.splashGUIHost && window.splashGUIHost.resolve(<requestId>, <result>)`.

| type | payload | result |
|---|---|---|
| `hello` | `{}` | `{ok: true, version, features: [...]}` |
| `pickFolder` | `{target: "models"\|"cache", current?}` | `{path}` or `{cancelled: true}` |
| `openHomebrewInstaller` | `{}` | `{ok: true}` or `{error}` |
| `openTerminal` | `{command}` (allow-listed) | `{ok: true}` or `{error}` |
| `openURL` | `{url}` | `{ok: true}` or `{error}` |
| `closeWelcome` | `{completed}` | `{ok: true}` (may not arrive) |
| other | | `{error: "unknown type"}` |

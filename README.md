# Splashboard

[![Latest release](https://img.shields.io/github/v/release/arnavprabhu/splashboard?label=download&color=E8432E)](https://github.com/arnavprabhu/splashboard/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/arnavprabhu/splashboard/total?color=111111)](https://github.com/arnavprabhu/splashboard/releases)
[![CI](https://github.com/arnavprabhu/splashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/arnavprabhu/splashboard/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%2026.4%2B%20·%20Apple%20silicon-black.svg)](#requirements)
[![Built on Splash](https://img.shields.io/badge/built%20on-Splash-E8432E.svg)](https://github.com/incoai/splash)

**The menu bar app for [Splash](https://github.com/incoai/splash), the local inference engine for Apple silicon.**
Install Splash and a model in a few clicks, run it in the background, watch it live, chat with it, and point
Claude Code, Codex and other coding agents at it, all on your own Mac.

**[⬇ Download Splashboard for macOS](https://github.com/arnavprabhu/splashboard/releases/latest)** · or `brew install --cask arnavprabhu/tap/splashboard`

## Why Splashboard
Splash is fast, but it is a command-line engine that serves one model per process. Splashboard wraps it in an app:

- **Set up in minutes.** A welcome wizard installs Homebrew and Splash, picks where models live, recommends a model that fits your Mac's memory, and downloads it.
- **One local endpoint for everything.** OpenAI- and Anthropic-compatible `/v1` on `http://127.0.0.1:8000`, whatever model is loaded.
- **Switch models fast.** Load Qwen3.8-27B or Qwen3.6-35B-A3B (MLX or GGUF) from the menu bar; the same URL serves whichever model is loaded.
- **See what the engine is doing.** Live tokens per second, memory, cache and request history, from the menu bar or a full web dashboard.
- **Coding agents, session-only.** `splash launch claude`, `codex`, `opencode`, `pi` or `hermes` runs the agent on your local model for that session and leaves its normal setup untouched. Claude Desktop and the Codex app connect with one click and are always restored.
- **Chat and tools built in.** Chat with tools and MCP servers, a playground, a tokenizer and benchmarks.
- **Private by default.** Everything runs on your Mac; secrets live in the macOS Keychain.
- **Updates itself.** New versions arrive through **Check for Updates…** in the menu.

> Repository: <https://github.com/arnavprabhu/splashboard> · [issues](https://github.com/arnavprabhu/splashboard/issues) · [releases](https://github.com/arnavprabhu/splashboard/releases).

## Install
Download `Splashboard-<version>.dmg` from the [latest release](https://github.com/arnavprabhu/splashboard/releases/latest) and drag Splashboard to Applications, or install it with Homebrew:

```bash
brew install --cask arnavprabhu/tap/splashboard
```

Splashboard is signed ad hoc and **not notarized**, so macOS blocks the first open. Open System Settings → Privacy & Security and click **Open Anyway** next to "Splashboard" (or run `xattr -dr com.apple.quarantine /Applications/Splashboard.app`). You only do this once.

## Requirements
- An Apple-silicon Mac with an **M3 or newer** and **macOS 26.4+** (Splash's own requirements).
- [Homebrew](https://brew.sh) and the engine: `brew install incoai/tap/splash` (the welcome wizard can do this for you).
- For running from source: [`uv`](https://docs.astral.sh/uv/) (Python 3.12+), Node 22+ with [`pnpm`](https://pnpm.io) 11, and Xcode 27 / Swift 6 for the menu bar app.

## Quick start (from source)
```sh
git clone https://github.com/arnavprabhu/splashboard.git && cd splashboard
make web          # build the web admin into web/dist (checks the bundle budget)
make manager      # start the manager on http://127.0.0.1:8000 → open /admin
```
The first visit opens the welcome wizard. Data lives in `~/.splash` (models, cache, settings, chats, usage, logs); secrets go in the macOS Keychain.

Menu bar app:
```sh
make bundle       # builds "macos/build/Splashboard.app" (ad-hoc signed)
open "macos/build/Splashboard.app"
```
The app starts the manager from this checkout with `uv run --project manager splash-gui-manager` (the repository path is configurable; see `macos/README.md`).

### Developing without a model
`make dev FAKE=1` runs the manager against the fake engine in `scripts/fake_splash` with a throwaway home in `build/dev-home/`, plus the Vite dev server with hot reload on `http://127.0.0.1:5173/admin/`. No model, Metal or network is needed.

| Task | Command |
|---|---|
| All tests (manager, fake engine, web unit + Playwright, macOS) | `make test` |
| Lint and type checks | `make lint` |
| Web only | `cd web && pnpm typecheck && pnpm test && pnpm build && pnpm size && pnpm exec playwright test` |
| Regenerate the web API types from the manager's OpenAPI | `cd web && pnpm gen:api` |
| Contract tests against the installed Splash and the live Hub (reads configs and GGUF headers, never weights) | `make test-real` |

## Architecture
```
menu bar app (SwiftUI) ──┐
browser ── /admin ───────┼──> manager (Python · FastAPI · :8000) ──> splash serve (internal port)
agents ── /v1/* ─────────┘        settings · downloads · usage.db · chats · logs · MCP
```
- `manager/` — the manager service, admin API (`/api/admin`) and the `splash` CLI shim.
- `web/` — the admin SPA: Preact + Vite + TypeScript, hand-written CSS, uPlot, fully offline.
- `macos/` — the menu bar app (SwiftPM package + bundle script).
- `scripts/fake_splash/` — a stdlib stand-in for the Splash engine used by tests and development.

## Releases

`make app` and `make dmg` build the self-contained app and its DMG; `.github/workflows/release.yml` builds and publishes a release from a `vX.Y.Z` tag, with Sparkle updates and a Homebrew cask.

## Built on Splash
Splashboard is a front end for [**Splash**](https://github.com/incoai/splash), the local inference engine for Apple silicon by [inco.ai](https://inco.ai/blog/splash/). Splash does the inference, including its [DFlash 2](https://inco.ai/blog/dflash2/) speculative decoding, the model packaging and the agent launchers; Splashboard installs, configures, supervises and monitors it. Splash is Apache-2.0 and is installed separately from its own Homebrew tap (`incoai/tap/splash`). The test stand-in in `scripts/fake_splash/` contains code copied from Splash 1.3.0; [its NOTICE](scripts/fake_splash/NOTICE) lists the files.

Splashboard is not an official inco.ai product and is not endorsed by inco.ai. Report engine problems to [Splash's issues](https://github.com/incoai/splash/issues), and problems with this app [here](https://github.com/arnavprabhu/splashboard/issues).

## Citing
[`CITATION.cff`](CITATION.cff) cites Splashboard and, as a reference, Splash (inco.ai, version 1.3.0, <https://github.com/incoai/splash>). If you publish results measured with Splashboard, please cite Splash as the engine that produced them.

## License
[Apache-2.0](LICENSE). See [NOTICE](NOTICE). The Archivo font is under the SIL Open Font License (`web/src/assets/fonts/OFL.txt`). Splash is a separate project by its authors and is installed from its own Homebrew tap.

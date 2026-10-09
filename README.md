# Splashboard

A macOS app for serving, monitoring and chatting with models on the [Splash](https://github.com/incoai/splash) local inference engine: a SwiftUI menu bar app plus a web admin served by a local manager, laid out like oMLX and styled in a Swiss poster design.

- **One URL.** The manager listens on `http://127.0.0.1:8000`, proxies the OpenAI- and Anthropic-compatible API (`/v1/*`) to one Splash process, and serves the admin at `/admin`.
- **One active model, quick switching.** Install supported Qwen3.8-27B / Qwen3.6-35B-A3B builds (MLX 4-bit group 64, or GGUF), load one, switch from the menu bar or the admin.
- **Everything in the GUI.** Welcome wizard (Homebrew, Splash, storage, presets, first model), live status and usage history, model downloads with compatibility checks, settings for every `splash serve` option with Splash's own validation, chat with tools and MCP, a playground, tokenizer, judgments and benchmarks, session-only agent launchers (`splash launch claude`) and desktop-app integrations.

> Repository: <https://github.com/arnavprabhu/splashboard> · [issues](https://github.com/arnavprabhu/splashboard/issues) · [releases](https://github.com/arnavprabhu/splashboard/releases).

## Install
Download `Splashboard-<version>.dmg` from the [latest release](https://github.com/arnavprabhu/splashboard/releases/latest) and drag Splashboard to Applications. Updates arrive through **Check for Updates…** in the menu.

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

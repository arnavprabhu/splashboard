# Splashboard web admin

Preact + Vite + TypeScript SPA served by the manager at `/admin`.

| Script | What it does |
|---|---|
| `pnpm dev` | Vite dev server; proxies `/api /v1 /status /metrics /health /ready` to `SPLASH_GUI_MANAGER` (default `http://127.0.0.1:8000`) |
| `pnpm build` | Production build to `dist/` with hashed filenames and `.vite/manifest.json` |
| `pnpm typecheck` | `tsc --noEmit` (strict) |
| `pnpm test` | vitest (jsdom) |
| `pnpm size` | Enforces the §18.6 budgets against `dist/` |
| `pnpm e2e` | Playwright smoke tests against `vite preview` |
| `pnpm gen:api` | Regenerates `src/api/schema.d.ts` from the manager's `/openapi.json` (`SPLASH_GUI_OPENAPI` overrides the source) |

- Design: the tokens live in `src/styles/tokens.css`; components in `src/components/`. `/admin/_design` (hidden) shows every component in both themes.
- Font: `scripts/build-font.sh` rebuilds the Archivo subset (OFL, `src/assets/fonts/OFL.txt`).
- API shapes: until `pnpm gen:api` can run, `src/api/types.ts` mirrors `manager/splash_gui/schemas.py` (`EngineView`, `Alert`, `AuthState`, `SettingsResponse`). The shell listens on `GET /api/admin/events` for the named events `engine`, `alert`, `alert_cleared`, `alerts`, `settings` and `auth`.
- Validation parity: `tests/parity.test.ts` runs Splash's own parsers (`../splash`, else the Homebrew install) under its bundled Python and compares them with `src/lib/size.ts` and `src/lib/model-id.ts`. It is skipped when Splash is not installed; `SPLASH_PYTHON` and `SPLASH_PKG` override the paths.

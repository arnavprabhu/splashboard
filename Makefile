# Splashboard developer tasks. Everything runs from the source tree (packaging is deferred).
#
#   make dev        manager + Vite dev server (FAKE=1 uses the fake engine and a throwaway home)
#   make manager    run the manager on 127.0.0.1:$(PORT) serving web/dist
#   make web        build the web admin (web/dist)
#   make macos      build the menu bar app (swift build)
#   make bundle     build "macos/build/Splashboard.app" (ad-hoc signed)
#   make runtime    build the bundled Python runtime, build/package/manager/python
#   make app        build the self-contained build/package/Splashboard.app (web, runtime, menu bar app)
#   make dmg        build build/package/<Name>-<version>.dmg from that app
#   make notarize   TARGET=<app or dmg> ARGS="--keychain-profile NAME": notarize and staple (needs Developer ID)
#   make test       unit and integration tests: manager, fake engine, packaging scripts, web (vitest + Playwright), macOS
#   make test-real  contract tests against the installed Splash and the live Hub
#   make lint       ruff, mypy, tsc

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PORT ?= 8000
VITE_PORT ?= 5173
FAKE ?= 0
FAKE_HOME ?= $(ROOT)/build/dev-home

ifeq ($(FAKE),1)
DEV_ENV := SPLASH_GUI_HOME=$(FAKE_HOME) SPLASH_GUI_SECRETS=file \
	SPLASH_GUI_REAL_SPLASH=$(ROOT)/scripts/fake_splash/pkg/bin/splash \
	SPLASH_GUI_FAKE_DATA=$(FAKE_HOME)/fake-data HF_HUB_CACHE=$(FAKE_HOME)/models
else
DEV_ENV :=
endif

.PHONY: help dev manager web web-deps manager-deps macos bundle runtime app dmg notarize test test-manager test-fake test-packaging test-web test-e2e test-macos test-real lint

help:
	@sed -n 's/^#   //p' $(MAKEFILE_LIST)

manager-deps:
	cd manager && uv sync

web-deps:
	cd web && pnpm install --frozen-lockfile

## Run the manager and the Vite dev server together; Ctrl-C stops both.
dev: manager-deps web-deps
	@echo "manager: http://127.0.0.1:$(PORT)   web (hot reload): http://127.0.0.1:$(VITE_PORT)/admin/"
	@trap 'kill 0' INT TERM EXIT; \
	( cd manager && $(DEV_ENV) uv run splash-gui-manager --port $(PORT) --console ) & \
	( cd web && SPLASH_GUI_MANAGER=http://127.0.0.1:$(PORT) pnpm dev --port $(VITE_PORT) ) & \
	wait

manager: manager-deps
	cd manager && $(DEV_ENV) uv run splash-gui-manager --port $(PORT) --web-dist $(ROOT)/web/dist --console

web: web-deps
	cd web && pnpm build && pnpm size

macos:
	cd macos && swift build

bundle:
	macos/scripts/bundle.sh

## Bundled Python runtime for the packaged app: python-build-standalone (pinned in packaging/runtime.lock) plus splash_gui.
runtime:
	packaging/scripts/build-runtime.sh

## The self-contained app: make web, make runtime, swift build -c release, then one bundle with the
## bundled runtime (Contents/Resources/manager), the web admin (Contents/Resources/web) and the agent plist.
app:
	packaging/scripts/build-app.sh

dmg:
	packaging/scripts/make-dmg.sh

notarize:
	@test -n "$(TARGET)" || { echo 'usage: make notarize TARGET=<app or dmg> ARGS="--keychain-profile NAME"'; exit 1; }
	packaging/scripts/notarize.sh $(ARGS) "$(TARGET)"

test: test-manager test-fake test-packaging test-web test-e2e test-macos

test-manager: manager-deps
	cd manager && SPLASH_GUI_SECRETS=memory uv run pytest -n auto --timeout=300

test-fake:
	uv run --no-project --with pytest pytest scripts/fake_splash/tests

## Packaging scripts: the sign.sh shell tests and the pytest suite under packaging/. They need no network
## and no signing identity: sign.sh and build-app.sh run on fixtures in temp folders, never on a real app.
test-packaging:
	bash packaging/tests/test_sign.sh
	bash packaging/tests/test_dmg.sh
	bash packaging/tests/test_notarize.sh
	cd macos && swift package resolve >/dev/null  # Sparkle's generate_appcast and sign_update
	bash packaging/tests/test_appcast.sh
	bash packaging/tests/test_cask.sh
	uv run --no-project --with pytest pytest packaging/tests

test-web: web-deps
	cd web && pnpm typecheck && pnpm test

## Builds the SPA, checks the bundle-size budgets, then runs Playwright (stubbed shell + real manager with the fake engine).
test-e2e: web-deps manager-deps
	cd web && pnpm build && pnpm size && pnpm exec playwright test

test-macos:
	cd macos && swift test

## Contract tests against the installed Homebrew Splash and the live Hub.
## These need the engine and network, and never download weights; run them manually.
test-real: manager-deps
	cd manager && uv run pytest -m real -rs

lint: manager-deps web-deps
	cd manager && uv run ruff check . && uv run ruff format --check . && uv run mypy
	uv run --project manager ruff check scripts/fake_splash && uv run --project manager ruff format --check scripts/fake_splash
	uv run --project manager mypy --config-file scripts/fake_splash/mypy.ini
	cd web && pnpm typecheck

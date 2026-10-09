#!/usr/bin/env bash
# Tests for packaging/scripts/render-cask.sh (docs/plans/packaging.md, PKG-14).
#
# Builds an ad hoc signed fixture app (with SplashGUIAgentLabel, as build-app.sh writes it) and its DMG in a mktemp
# folder, renders the cask and checks: every value comes from the app, the URL interpolates the version, the sha256
# is the DMG's, zap keeps models and cache (D72), and the refusals (a .verify build, --pull-request outside CI).
# `brew style` and `brew audit --cask --strict` run on the real release DMG's cask in a local tap (PKG-14 evidence),
# not here, so this test needs no Homebrew. Nothing outside the mktemp folder is written.
#
#   bash packaging/tests/test_cask.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RENDER="$REPO/packaging/scripts/render-cask.sh"
MAKE_DMG="$REPO/packaging/scripts/make-dmg.sh"
TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/cask-test.XXXXXX")" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0

check() { # check DESCRIPTION COMMAND...
  local desc="$1"
  shift
  if "$@"; then
    PASS=$((PASS + 1))
    echo "ok   $desc"
  else
    FAIL=$((FAIL + 1))
    echo "FAIL $desc"
  fi
}
run() { OUT="$("$@" 2>&1)" && RC=0 || RC=$?; }

# fixture NAME BUNDLE_ID: an app and its DMG under $TMP/<bundle id>/.
fixture() {
  local dir="$TMP/$2" app
  app="$dir/apps/$1.app"
  mkdir -p "$app/Contents/MacOS"
  cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>Fixture</string>
  <key>CFBundleIdentifier</key><string>$2</string>
  <key>CFBundleName</key><string>$1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.3.4</string>
  <key>CFBundleVersion</key><string>7</string>
  <key>SplashGUIAgentLabel</key><string>$2.manager</string>
</dict>
</plist>
PLIST
  cp /usr/bin/true "$app/Contents/MacOS/Fixture"
  codesign --force --sign - "$app" >/dev/null 2>&1
  "$MAKE_DMG" --app "$app" --out "$dir" >/dev/null
}

fixture "Fixture Board" io.example.fixtureboard
DMG="$TMP/io.example.fixtureboard/Fixture-Board-2.3.4.dmg"
run "$RENDER" --dmg "$DMG" --out "$TMP/cask.rb"
check "render-cask.sh exits 0" test "$RC" -eq 0
CASK="$(cat "$TMP/cask.rb")"
check "the token is the app name in lower case with hyphens" grep -q '^cask "fixture-board" do' <<<"$CASK"
check "the version comes from the app" grep -q '^  version "2.3.4"' <<<"$CASK"
check "the sha256 is the DMG's" grep -q "^  sha256 \"$(shasum -a 256 "$DMG" | cut -d' ' -f1)\"" <<<"$CASK"
check "the URL is the releases repo path with #{version}" \
  grep -q 'url "https://github.com/arnavprabhu/splashboard/releases/download/v#{version}/Fixture-Board-#{version}.dmg"' <<<"$CASK"
check "uninstall names the agent label, the bundle id and the login item" \
  bash -c 'grep -q "launchctl:  \"io.example.fixtureboard.manager\"" <<<"$1" && grep -q "quit:       \"io.example.fixtureboard\"" <<<"$1" && grep -q "login_item: \"Fixture Board\"" <<<"$1"' _ "$CASK"
check "depends on the Splash formula" grep -q 'depends_on formula: "incoai/tap/splash"' <<<"$CASK"
check "zap keeps models and cache (D72)" bash -c '! grep -Eq "\"~/.splash/(models|cache)" <<<"$1"' _ "$CASK"
check "zap removes settings, chats and the usage database" \
  bash -c 'grep -q "~/.splash/settings.json" <<<"$1" && grep -q "~/.splash/chats" <<<"$1" && grep -q "~/.splash/usage.db" <<<"$1"' _ "$CASK"
check "zap deletes the Keychain services from SecretName" grep -q '"io.github.arnavprabhu.splashboard.apikey"' <<<"$CASK"
check "no placeholder is left" bash -c '! grep -q "@[A-Z_0-9]*@" <<<"$1"' _ "$CASK"
check "the cask parses as Ruby" ruby -c "$TMP/cask.rb"

fixture "Fixture Board" io.example.fixtureboard.verify
run "$RENDER" --dmg "$TMP/io.example.fixtureboard.verify/Fixture-Board-2.3.4.dmg"
check "a .verify build is refused" bash -c '[[ $1 -ne 0 ]] && grep -q "refusing to render a cask for a .verify build" <<<"$2"' _ "$RC" "$OUT"
run env -u GITHUB_ACTIONS "$RENDER" --dmg "$DMG" --pull-request
check "--pull-request is refused outside CI" bash -c '[[ $1 -ne 0 ]] && grep -q "runs only on a CI runner" <<<"$2"' _ "$RC" "$OUT"

echo
echo "cask tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

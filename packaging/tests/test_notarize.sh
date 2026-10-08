#!/usr/bin/env bash
# Tests for packaging/scripts/notarize.sh (docs/plans/packaging.md, PKG-8).
#
# Builds an ad hoc signed fixture app and a DMG of it (make-dmg.sh) in a mktemp folder, then checks that
# `--dry-run` lists the commands and stops at the Developer ID check for both, and the refusals: a real run without
# credentials, half the API key values, a target that is not a .app or .dmg, two targets. Nothing is submitted:
# every case stops before notarytool. Nothing outside the mktemp folder is written.
#
#   bash packaging/tests/test_notarize.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NOTARIZE="$REPO/packaging/scripts/notarize.sh"
MAKE_DMG="$REPO/packaging/scripts/make-dmg.sh"
TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/notarize-test.XXXXXX")" && pwd -P)"
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

# outputs RC PATTERN... COMMAND is awkward in bash; run once, then test the saved output and code.
run() { OUT="$("$@" 2>&1)" && RC=0 || RC=$?; }

APP="$TMP/Fixture.app"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>Fixture</string>
  <key>CFBundleIdentifier</key><string>io.splashgui.notarizetest.fixture</string>
  <key>CFBundleName</key><string>Fixture</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
</dict>
</plist>
PLIST
cp /usr/bin/true "$APP/Contents/MacOS/Fixture"
codesign --force --sign - --options runtime "$APP" >/dev/null 2>&1
"$MAKE_DMG" --app "$APP" --out "$TMP" --identity - >/dev/null
DMG="$TMP/Fixture-1.0.0.dmg"

run "$NOTARIZE" --dry-run "$DMG"
check "dry-run on an ad hoc DMG exits 1" test "$RC" -eq 1
check "dry-run lists notarytool submit" grep -q "xcrun notarytool submit" <<<"$OUT"
check "dry-run lists stapler staple and validate" bash -c 'grep -q "stapler staple" <<<"$1" && grep -q "stapler validate" <<<"$1"' _ "$OUT"
check "dry-run lists spctl -t install for a DMG" grep -q "spctl -a -vv -t install" <<<"$OUT"
check "dry-run stops at the Developer ID check" grep -q "not signed with a Developer ID Application identity (ad hoc)" <<<"$OUT"
check "the DMG is never mounted after the identity check (no volume left)" bash -c '! mount | grep -q "$1"' _ "$TMP"

run "$NOTARIZE" --dry-run "$APP"
check "dry-run on an ad hoc app exits 1 at the identity check" \
  bash -c '[[ $1 -eq 1 ]] && grep -q "the app is not signed with a Developer ID" <<<"$2"' _ "$RC" "$OUT"
check "dry-run on an app lists the ditto zip and spctl -t exec" \
  bash -c 'grep -q "ditto -c -k --keepParent" <<<"$1" && grep -q "spctl -a -vv -t exec" <<<"$1"' _ "$OUT"

run "$NOTARIZE" "$DMG"
check "a real run without credentials is refused" \
  bash -c '[[ $1 -ne 0 ]] && grep -q "credentials are required" <<<"$2"' _ "$RC" "$OUT"
run "$NOTARIZE" --key "$TMP/k.p8" "$DMG"
check "--key without --key-id and --issuer is refused" \
  bash -c '[[ $1 -ne 0 ]] && grep -q "go together" <<<"$2"' _ "$RC" "$OUT"
run "$NOTARIZE" --dry-run "$TMP/Fixture.zip"
check "a target that is not a .app or .dmg is refused" \
  bash -c '[[ $1 -ne 0 ]] && grep -q "must be a .app or a .dmg" <<<"$2"' _ "$RC" "$OUT"
run "$NOTARIZE" --dry-run "$DMG" "$APP"
check "two targets are refused" bash -c '[[ $1 -ne 0 ]] && grep -q "one target only" <<<"$2"' _ "$RC" "$OUT"

echo
echo "notarize tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

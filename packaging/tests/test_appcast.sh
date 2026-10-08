#!/usr/bin/env bash
# Tests for packaging/scripts/appcast.sh (docs/plans/packaging.md, PKG-10).
#
# Makes a throwaway Ed25519 key with openssl (Sparkle's file format: the base64 32-byte seed), two fixture apps
# (1.0.0 build 1 and 1.0.1 build 2) and their DMGs (make-dmg.sh), then builds the appcast and checks it with xmllint
# and Sparkle's `sign_update --verify`: one item per DMG, each enclosure's edSignature valid for its file, no deltas,
# no channel, embedded release notes, the URL prefix, the phased rollout interval, and the refusals. Needs Sparkle's
# tools from `cd macos && swift package resolve`. Nothing outside the mktemp folder is written except Sparkle's
# own cache of extracted archives (~/Library/Caches/Sparkle_generate_appcast).
#
#   bash packaging/tests/test_appcast.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APPCAST_SH="$REPO/packaging/scripts/appcast.sh"
MAKE_DMG="$REPO/packaging/scripts/make-dmg.sh"
BIN="${SPARKLE_BIN:-$REPO/macos/.build/artifacts/sparkle/Sparkle/bin}"
TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/appcast-test.XXXXXX")" && pwd -P)"
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

[[ -x "$BIN/sign_update" ]] || { echo "skip: no Sparkle tools at $BIN (cd macos && swift package resolve)"; exit 1; }

openssl genpkey -algorithm ed25519 -out "$TMP/key.pem" 2>/dev/null
openssl pkey -in "$TMP/key.pem" -outform DER | tail -c 32 | base64 > "$TMP/key.b64"
# generate_appcast signs only archives whose app declares the matching SUPublicEDKey.
PUBLIC="$(openssl pkey -in "$TMP/key.pem" -pubout -outform DER | tail -c 32 | base64)"

# app VERSION BUILD: an ad hoc signed fixture app, then its DMG in $TMP/release.
app() {
  local dir="$TMP/apps/$1" a
  a="$dir/Fixture.app"
  mkdir -p "$a/Contents/MacOS"
  cat > "$a/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>Fixture</string>
  <key>CFBundleIdentifier</key><string>io.splashgui.appcasttest.fixture</string>
  <key>CFBundleName</key><string>Fixture</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$1</string>
  <key>CFBundleVersion</key><string>$2</string>
  <key>LSMinimumSystemVersion</key><string>26.4</string>
  <key>SUPublicEDKey</key><string>$PUBLIC</string>
</dict>
</plist>
PLIST
  cp /usr/bin/true "$a/Contents/MacOS/Fixture"
  codesign --force --sign - "$a" >/dev/null 2>&1
  "$MAKE_DMG" --app "$a" --out "$TMP/release" >/dev/null
}
app 1.0.0 1
app 1.0.1 2
mkdir -p "$TMP/notes"
printf '<h2>Fixture 1.0.1</h2>\n<ul><li>A fixture change.</li></ul>\n' > "$TMP/notes/1.0.1.html"

run "$APPCAST_SH" --ed-key-file "$TMP/key.b64" --notes "$TMP/notes" \
  --download-url-prefix https://example.invalid/releases/ "$TMP/release"
check "appcast.sh exits 0" test "$RC" -eq 0
XML="$TMP/release/appcast.xml"
check "appcast.xml is well-formed" xmllint --noout "$XML"
check "one item per DMG" test "$(xmllint --xpath 'count(//item)' "$XML")" -eq 2
check "no delta updates" test "$(xmllint --xpath 'count(//*[local-name()="deltas"])' "$XML")" -eq 0
check "no sparkle:channel" test "$(xmllint --xpath 'count(//*[local-name()="channel" and namespace-uri()!=""])' "$XML")" -eq 0
check "enclosures use the URL prefix" \
  test "$(xmllint --xpath 'count(//enclosure[starts-with(@url,"https://example.invalid/releases/Fixture-1.0.")])' "$XML")" -eq 2
check "the 1.0.1 notes are embedded" bash -c 'grep -q "A fixture change." "$1"' _ "$XML"
for v in 1.0.0 1.0.1; do
  sig="$(xmllint --xpath "string(//enclosure[contains(@url,'Fixture-$v.dmg')]/@*[local-name()='edSignature'])" "$XML")"
  len="$(xmllint --xpath "string(//enclosure[contains(@url,'Fixture-$v.dmg')]/@length)" "$XML")"
  check "$v: the edSignature verifies against the DMG (sign_update --verify)" \
    "$BIN/sign_update" --verify --ed-key-file "$TMP/key.b64" "$TMP/release/Fixture-$v.dmg" "$sig"
  check "$v: length is the DMG's size" test "$len" -eq "$(stat -f %z "$TMP/release/Fixture-$v.dmg")"
done
run "$BIN/sign_update" --verify --ed-key-file "$TMP/key.b64" "$TMP/release/Fixture-1.0.1.dmg" \
  "$(xmllint --xpath "string(//enclosure[contains(@url,'Fixture-1.0.0.dmg')]/@*[local-name()='edSignature'])" "$XML")"
check "a signature does not verify another DMG" test "$RC" -ne 0

mkdir -p "$TMP/phased"
cp "$TMP/release/Fixture-1.0.1.dmg" "$TMP/phased/"
run "$APPCAST_SH" --ed-key-file "$TMP/key.b64" --notes "$TMP/notes" --phased-rollout-interval 86400 "$TMP/phased"
check "--phased-rollout-interval is written to the item" \
  bash -c 'grep -q "<sparkle:phasedRolloutInterval>86400</sparkle:phasedRolloutInterval>" "$1"' _ "$TMP/phased/appcast.xml"

run "$APPCAST_SH" "$TMP/release"
check "no key source is refused" bash -c '[[ $1 -ne 0 ]] && grep -q "there is no default" <<<"$2"' _ "$RC" "$OUT"
run "$APPCAST_SH" --ed-key-file "$TMP/key.b64" --account x "$TMP/release"
check "--ed-key-file with --account is refused" bash -c '[[ $1 -ne 0 ]] && grep -q "exclusive" <<<"$2"' _ "$RC" "$OUT"
mkdir -p "$TMP/empty"
run "$APPCAST_SH" --ed-key-file "$TMP/key.b64" "$TMP/empty"
check "a folder without DMGs is refused" bash -c '[[ $1 -ne 0 ]] && grep -q "no .dmg files" <<<"$2"' _ "$RC" "$OUT"

echo
echo "appcast tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

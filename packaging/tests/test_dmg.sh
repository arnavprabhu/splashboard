#!/usr/bin/env bash
# Tests for packaging/scripts/make-dmg.sh (docs/plans/packaging.md, PKG-7).
#
# Builds a tiny ad hoc signed fixture app in a mktemp folder, makes a DMG from it, mounts the DMG with
# `hdiutil attach -nobrowse -readonly` at a mount point inside the same folder, and checks that the volume holds
# exactly the app and an `Applications` symlink to /Applications. Also checks the file name, the volume name,
# `hdiutil verify`, a signed DMG, and the refusals (an unsigned app, a bad version, /Applications as output).
# Nothing outside the mktemp folder is written.
#
#   bash packaging/tests/test_dmg.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MAKE_DMG="$REPO/packaging/scripts/make-dmg.sh"
TMP="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/dmg-test.XXXXXX")" && pwd -P)"
MNT="$TMP/mnt"
ATTACHED=0
detach() {
  [[ $ATTACHED -eq 1 ]] || return 0
  hdiutil detach -quiet "$MNT" || hdiutil detach -quiet -force "$MNT"
  ATTACHED=0
}
# Never rm a mounted volume: if the detach fails, leave the folder for the user rather than fail on a read-only tree.
trap 'detach && rm -rf "$TMP"' EXIT
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

refuses() { # refuses PATTERN COMMAND...: non-zero exit and PATTERN in the output
  local pattern="$1" out rc=0
  shift
  out="$("$@" 2>&1)" || rc=$?
  [[ $rc -ne 0 && "$out" == *"$pattern"* ]]
}

# make_app DIR NAME VERSION SIGN(yes|no): prints the path of a minimal app bundle.
make_app() {
  local dir="$1" name="$2" version="$3" sign="$4" app
  app="$dir/$name.app"
  mkdir -p "$app/Contents/MacOS"
  cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>Fixture</string>
  <key>CFBundleIdentifier</key><string>io.splashgui.dmgtest.fixture</string>
  <key>CFBundleName</key><string>$name</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>1</string>
</dict>
</plist>
PLIST
  cp /usr/bin/true "$app/Contents/MacOS/Fixture"
  if [[ "$sign" == yes ]]; then codesign --force --sign - "$app" >/dev/null 2>&1; fi
  printf '%s' "$app"
}

# --- a normal build ---
mkdir -p "$TMP/apps" "$TMP/out"
APP="$(make_app "$TMP/apps" "Fixture App" 1.2.3 yes)"
OUT="$("$MAKE_DMG" --app "$APP" --out "$TMP/out" 2>&1)" || { echo "$OUT"; exit 1; }
DMG="$TMP/out/Fixture-App-1.2.3.dmg"
check "the DMG is named <CFBundleName with hyphens>-<version>.dmg" test -f "$DMG"
check "no partial file or stage folder is left behind" \
  bash -c '[[ -z "$(find "$1" -name "*.partial.dmg" -o -name ".dmg-stage.*" | head -1)" ]]' _ "$TMP/out"
check "hdiutil verify passes" hdiutil verify -quiet "$DMG"
check "the image is compressed (UDZO)" bash -c 'hdiutil imageinfo "$1" | grep -q "Format: UDZO"' _ "$DMG"

mkdir -p "$MNT"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MNT" "$DMG"
ATTACHED=1
ENTRIES="$(find "$MNT" -mindepth 1 -maxdepth 1 ! -name '.*' -exec basename {} \; | sort | tr '\n' '|')"
check "the volume shows exactly the app and Applications (got: $ENTRIES)" test "$ENTRIES" = "Applications|Fixture App.app|"
check "Applications is a symlink to /Applications" test "$(readlink "$MNT/Applications")" = "/Applications"
check "the copied app keeps its signature" codesign --verify --strict "$MNT/Fixture App.app"
check "the volume is named after CFBundleName" bash -c 'diskutil info "$1" | grep -q "Volume Name: *Fixture App$"' _ "$MNT"
detach

# --- a signed DMG ---
mkdir -p "$TMP/out-signed"
"$MAKE_DMG" --app "$APP" --out "$TMP/out-signed" --identity - >/dev/null
check "--identity signs the DMG itself" codesign --verify --strict "$TMP/out-signed/Fixture-App-1.2.3.dmg"

# --- refusals ---
UNSIGNED="$(make_app "$TMP/apps" Unsigned 1.0.0 no)"
check "an app that fails codesign --verify is refused" \
  refuses "does not pass codesign" "$MAKE_DMG" --app "$UNSIGNED" --out "$TMP/out-unsigned"
check "the refused build writes no DMG" bash -c '[[ ! -e "$1/Unsigned-1.0.0.dmg" ]]' _ "$TMP/out-unsigned"
BADVER="$(make_app "$TMP/apps" Badver 1.0 yes)"
check "a version that is not X.Y.Z is refused" refuses "not X.Y.Z" "$MAKE_DMG" --app "$BADVER" --out "$TMP/out"
check "/Applications as the output folder is refused" refuses "refusing to write" "$MAKE_DMG" --app "$APP" --out /Applications
check "a missing app is refused" refuses "no app bundle" "$MAKE_DMG" --app "$TMP/nope.app" --out "$TMP/out"

echo
echo "dmg tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

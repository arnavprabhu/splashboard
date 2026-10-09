#!/usr/bin/env bash
# Tests for packaging/scripts/sign.sh (docs/plans/packaging.md, PKG-6; D86).
#
# Builds a tiny fixture bundle in a mktemp folder: an app with its main executable, a bundled interpreter under
# Contents/Resources/manager/python/bin, a nested .dylib, and a nested framework with its own dylib. Then checks
# the order from `sign.sh --dry-run`, the entitlements, the --timestamp rule, an ad hoc signing run with the
# hardened runtime, the refusals (in any letter case), that a failing codesign stops the run, and that a file or
# folder that cannot be listed or read stops the run before anything is signed. Nothing outside the mktemp folder is
# written. A path under /Applications is only ever passed as a refused argument, never read or signed.
#
#   bash packaging/tests/test_sign.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIGN="$REPO/packaging/scripts/sign.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sign-test.XXXXXX")"
# A folder made read-only or unreadable for a failure case must be accessible again before it can be removed.
trap 'chmod -R u+rwx "$TMP" 2>/dev/null || true; rm -rf "$TMP"' EXIT
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

# refuses PATTERN COMMAND...: the command exits non-zero and its output contains PATTERN.
refuses() {
  local pattern="$1" out rc=0
  shift
  out="$("$@" 2>&1)" || rc=$?
  [[ $rc -ne 0 && "$out" == *"$pattern"* ]]
}

# make_fixture ROOT: prints the path of an app with one nested dylib, one interpreter and one framework.
make_fixture() {
  local root="$1" app fw
  app="$root/FixtureApp.app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/manager/python/bin" \
    "$app/Contents/Resources/manager/python/lib" \
    "$app/Contents/Frameworks/Fixture.framework/Versions/A/Resources"
  cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>FixtureApp</string>
  <key>CFBundleIdentifier</key><string>io.splashgui.signtest.fixture</string>
  <key>CFBundleName</key><string>FixtureApp</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
</dict>
</plist>
PLIST
  printf 'int main(void) { return 0; }\n' > "$root/main.c"
  cc -o "$app/Contents/MacOS/FixtureApp" "$root/main.c"
  cc -o "$app/Contents/Resources/manager/python/bin/python3.13" "$root/main.c"
  printf 'int fixture_value(void) { return 1; }\n' > "$root/lib.c"
  cc -dynamiclib -o "$app/Contents/Resources/manager/python/lib/libfixture.dylib" "$root/lib.c"
  fw="$app/Contents/Frameworks/Fixture.framework"
  cc -dynamiclib -o "$fw/Versions/A/Fixture" "$root/lib.c"
  cat > "$fw/Versions/A/Resources/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>Fixture</string>
  <key>CFBundleIdentifier</key><string>io.splashgui.signtest.fixture.framework</string>
  <key>CFBundleName</key><string>Fixture</string>
  <key>CFBundlePackageType</key><string>FMWK</string>
  <key>CFBundleVersion</key><string>1</string>
</dict>
</plist>
PLIST
  (cd "$fw/Versions" && ln -s A Current)
  (cd "$fw" && ln -s Versions/Current/Fixture Fixture && ln -s Versions/Current/Resources Resources)
  echo "$app"
}

# line_of PATTERN FILE: the line number of the first "sign[" line containing PATTERN, or 0 when there is none.
line_of() {
  local n
  n="$(grep -n '^sign\[' "$2" | grep -F -- "$1" | head -1 | cut -d: -f1)" || true
  echo "${n:-0}"
}

# before A B: true when line A is set (not 0) and comes before line B.
before() { [[ "$1" -gt 0 && "$2" -gt 0 && "$1" -lt "$2" ]]; }

APP="$(make_fixture "$TMP/one")"
DRY="$TMP/dry.txt"

echo "== dry run"
"$SIGN" --identity - --app "$APP" --dry-run >"$DRY"
check "dry run lists 5 items (3 Mach-O files, 1 nested bundle, the app)" \
  test "$(grep -c '^sign\[' "$DRY")" -eq 5
check "the nested dylib is signed before the app" \
  before "$(line_of 'libfixture.dylib' "$DRY")" "$(line_of ' app . ' "$DRY")"
check "the framework's binary is signed before the framework bundle" \
  before "$(line_of 'Fixture.framework/Versions/A/Fixture' "$DRY")" \
  "$(line_of 'bundle Contents/Frameworks/Fixture.framework' "$DRY")"
check "the framework bundle is signed before the app" \
  before "$(line_of 'bundle Contents/Frameworks/Fixture.framework' "$DRY")" "$(line_of ' app . ' "$DRY")"
check "the app is the last item" \
  bash -c "grep '^sign\[' \"$DRY\" | tail -1 | grep -q ' app \. '"
check "under an ad hoc identity the interpreter gets python-adhoc.plist (D100)" \
  grep -q 'bin/python3.13 entitlements=python-adhoc.plist' "$DRY"
check "under an ad hoc identity the app gets app-adhoc.plist (D101: it must load the ad hoc Sparkle.framework)" \
  grep -q 'app \. entitlements=app-adhoc.plist' "$DRY"
check "the nested dylib gets no entitlements" \
  grep -q 'libfixture.dylib entitlements=none' "$DRY"
check "no codesign line uses --deep (signing never does)" \
  test "$(grep -c -- '--deep' "$DRY")" -eq 0
check "every codesign line passes --options runtime" \
  test "$(grep -c 'codesign ' "$DRY")" -eq "$(grep -c -- '--options runtime' "$DRY")"
check "an ad hoc run adds no --timestamp" \
  test "$(grep -c -- '--timestamp' "$DRY")" -eq 0

REAL="$TMP/real-identity.txt"
"$SIGN" --identity 'Apple Development: Test (ABCDE12345)' --app "$APP" --dry-run >"$REAL"
check "a real identity adds --timestamp to every item" \
  test "$(grep -c -- '--timestamp' "$REAL")" -eq 5
check "a real identity is passed to codesign by name" \
  grep -q -- "--sign Apple Development: Test (ABCDE12345)" "$REAL"
check "a real identity gives the interpreter python.plist, which keeps library validation on" \
  grep -q 'bin/python3.13 entitlements=python.plist' "$REAL"
check "a real identity gives the app app.plist, which keeps library validation on" \
  grep -q 'app \. entitlements=app.plist' "$REAL"
check "app-adhoc.plist turns library validation off and nothing else" \
  bash -c "[[ \"\$(plutil -convert json -o - '$REPO/packaging/entitlements/app-adhoc.plist')\" == '{\"com.apple.security.cs.disable-library-validation\":true}' ]]"
check "python.plist stays empty (identity builds)" \
  bash -c "[[ \"\$(plutil -convert json -o - '$REPO/packaging/entitlements/python.plist')\" == '{}' ]]"
check "python-adhoc.plist turns library validation off and nothing else" \
  bash -c "[[ \"\$(plutil -convert json -o - '$REPO/packaging/entitlements/python-adhoc.plist')\" == '{\"com.apple.security.cs.disable-library-validation\":true}' ]]"

echo "== refusals (nothing is signed)"
check "no --identity is refused" \
  refuses "--identity is required" "$SIGN" --app "$APP"
check "an unknown option is refused" \
  refuses "unknown option" "$SIGN" --identity - --bogus
check "a path under /Applications is refused, as written and not read" \
  refuses "refusing to sign" "$SIGN" --identity - --app "/Applications/Splashboard.app"
check "a missing path under /Applications is refused too" \
  refuses "refusing to sign" "$SIGN" --identity - --app "/Applications/Nope-sign-test.app"
check "a path under ~/Applications is refused" \
  refuses "refusing to sign" "$SIGN" --identity - --app "$HOME/Applications/Nope.app"
# --dry-run on these two: if the refusal ever regressed, the test could not sign the owner's installed copy.
check "a lowercase /applications path is refused too (APFS ignores letter case)" \
  refuses "refusing to sign" "$SIGN" --identity - --dry-run --app "/applications/Splashboard.app"
check "a lowercase ~/applications path is refused" \
  refuses "refusing to sign" "$SIGN" --identity - --dry-run --app "$HOME/applications/Nope.app"
check "a missing bundle is refused" \
  refuses "no app bundle" "$SIGN" --identity - --app "$TMP/missing.app"

echo "== ad hoc signing with the hardened runtime"
check "sign.sh --identity - signs the fixture and passes its own verify" \
  "$SIGN" --identity - --app "$APP"
check "the nested dylib reports the runtime flag" \
  bash -c "codesign -dvvv '$APP/Contents/Resources/manager/python/lib/libfixture.dylib' 2>&1 | grep -q 'flags=.*runtime'"
check "the framework binary reports the runtime flag" \
  bash -c "codesign -dvvv '$APP/Contents/Frameworks/Fixture.framework/Versions/A/Fixture' 2>&1 | grep -q 'flags=.*runtime'"
check "the interpreter reports the runtime flag" \
  bash -c "codesign -dvvv '$APP/Contents/Resources/manager/python/bin/python3.13' 2>&1 | grep -q 'flags=.*runtime'"
check "the app passes codesign --verify --strict --deep" \
  codesign --verify --strict --deep "$APP"

echo "== a failing codesign stops the run"
BROKEN="$(make_fixture "$TMP/two")"
# A read-only folder makes codesign fail ("Write permissions error"); codesign replaces files by rename, so a
# read-only file alone would still be signed.
chmod 555 "$BROKEN/Contents/Resources/manager/python/lib"
check "a codesign failure makes sign.sh exit non-zero, with codesign's own error" \
  refuses "Write permissions error" "$SIGN" --identity - --app "$BROKEN"

echo "== a file or folder that cannot be listed or read stops the run before anything is signed"
UNREAD="$(make_fixture "$TMP/three")"
chmod 000 "$UNREAD/Contents/Resources/manager/python/lib/libfixture.dylib"
check "an unreadable Mach-O file makes --dry-run exit non-zero, and names the file" \
  refuses "cannot read" "$SIGN" --identity - --app "$UNREAD" --dry-run
check "an unreadable Mach-O file makes a real run exit non-zero, and names the file" \
  refuses "cannot read" "$SIGN" --identity - --app "$UNREAD"
check "the real run signed nothing: the interpreter still has no runtime flag" \
  bash -c "! codesign -dvvv '$UNREAD/Contents/Resources/manager/python/bin/python3.13' 2>&1 | grep -q 'flags=.*runtime'"
UNLIST="$(make_fixture "$TMP/four")"
chmod 000 "$UNLIST/Contents/Resources/manager/python/bin"
check "a folder that cannot be listed makes sign.sh exit non-zero, and names the listing" \
  refuses "cannot list" "$SIGN" --identity - --app "$UNLIST" --dry-run

echo
echo "sign tests: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]

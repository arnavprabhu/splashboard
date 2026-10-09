#!/usr/bin/env bash
# Assembles the self-contained Splashboard.app.
#
#   packaging/scripts/build-app.sh                              # default identity → build/package/Splashboard.app, ad hoc signed
#   packaging/scripts/build-app.sh --out build/x/Splash\ GUI.app # a .app under build/ (absolute or repo-relative)
#   packaging/scripts/build-app.sh --identity "Apple Development: <name> (<id>)"  # signed with a certificate
#   SPLASH_GUI_VERIFY_HOME=/abs/build/verify/<run>/home packaging/scripts/build-app.sh --variant verify \
#       --out /abs/build/verify/<run>/Splash\ GUI.app           # .verify ids, throwaway home baked in
#   make app                                                    # the default build, through the Makefile
#
# Steps: make web (web/dist), make runtime (build/package/manager), swift build -c release, then one
# bundle with:
#   Contents/MacOS/SplashGUI                          the menu bar app
#   Contents/Resources/manager/                       the bundled CPython 3.13 runtime and splash_gui
#   Contents/Resources/web/                           the built web admin
#   Contents/Resources/LICENSE, NOTICE                the app's license and notice
#   Contents/Resources/licenses/THIRD_PARTY.txt       CPython and every runtime package, from collect-licenses.py
#   Contents/Library/LaunchAgents/<label>.plist       from packaging/launchagent.plist.in
#   Contents/Info.plist                               as macos/scripts/bundle.sh writes it, minus SplashGUIRepoPath
#
# The app runs with no repo, uv or Xcode: the Swift app starts Contents/Resources/manager/python/bin/python3
# (BundledRuntime), and the manager finds its web admin in Contents/Resources/web. macos/scripts/
# bundle.sh stays the development bundle, which runs the manager from the source tree with uv.
#
# Identity (bundle id, agent label) comes from packaging/identity.env. The verify variant appends ".verify"
# and bakes SPLASH_GUI_HOME (it must lie under build/verify/) and SPLASH_GUI_SECRETS=file into LSEnvironment
# and the agent plist. The bundle is signed by sign.sh: ad hoc by default, and --identity (or
# SPLASH_GUI_SIGN_IDENTITY) names a certificate (Apple Development, or a Developer ID); notarize.sh notarizes.
#
# The output is staged next to its final path and moved into place only after every check passes, so a
# failed build leaves no bundle behind. Nothing downloaded is executed (the runtime script checks its pin).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MACOS_DIR="$REPO/macos"
PKG_DIR="$REPO/build/package"
RUNTIME="$PKG_DIR/manager"
EXECUTABLE="SplashGUI"
TEMPLATE="$REPO/packaging/launchagent.plist.in"
IDENTITY="$REPO/packaging/identity.env"
SIGN="$REPO/packaging/scripts/sign.sh"
SIGN_IDENTITY="${SPLASH_GUI_SIGN_IDENTITY:--}"
OUT="$PKG_DIR/Splashboard.app"
VARIANT="default"
STAGE=""
SUCCESS=0

die() { echo "error: $*" >&2; exit 1; }
note() { echo "· $*" >&2; }

# physical PATH: the physical path of PATH's longest existing ancestor, then the rest as written. The --out
# and verify home checks use it, so a symlink cannot lead out of build/ even when the folder does not exist yet.
physical() {
  local dir="$1" rest=""
  while [[ ! -d "$dir" ]]; do
    rest="/$(basename "$dir")$rest"
    dir="$(dirname "$dir")"
  done
  printf '%s%s\n' "$(cd -P "$dir" && pwd -P)" "$rest"
}

cleanup() {
  local rc=$?
  if [[ $SUCCESS -ne 1 && -n "$STAGE" ]]; then
    rm -rf "$STAGE"
    echo "build-app: failed (exit $rc), no bundle left behind" >&2
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="${2:?--variant needs default or verify}"; shift 2 ;;
    --out) OUT="${2:?--out needs a path}"; shift 2 ;;
    --identity) SIGN_IDENTITY="${2:?--identity needs - (ad hoc) or a certificate name}"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown option $1 (build-app.sh --help)" ;;
  esac
done
case "$VARIANT" in
  default|verify) ;;
  *) die "--variant must be default or verify (got $VARIANT)" ;;
esac
[[ "$OUT" == /* ]] || OUT="$PWD/$OUT"
[[ "$OUT" == *.app ]] || die "--out must end in .app (got $OUT)"
# The script deletes $OUT before it moves the new bundle in, so $OUT must be a .app under build/, with no ..
# and no symlink that leads out. The user's /Applications/Splashboard.app is never a target (package.sh guards
# the same way, SKILL.md).
case "$OUT" in
  *"/../"*|*/..) die "--out must not contain .. (got $OUT)" ;;
esac
REPO_PHYSICAL="$(physical "$REPO")"
OUT_PHYSICAL="$(physical "$(dirname "$OUT")")/$(basename "$OUT")"
case "$OUT_PHYSICAL" in
  "$REPO_PHYSICAL/build/"*.app) ;;
  *) die "--out must be a .app under $REPO/build/ (got $OUT)" ;;
esac
# The default variant carries the user's bundle id and agent label, so it never goes under build/verify/, where every
# bundle is a throwaway .verify copy.
if [[ "$VARIANT" == default ]]; then
  case "$OUT_PHYSICAL" in
    "$REPO_PHYSICAL/build/verify/"*) die "--variant default may not write under $REPO/build/verify/ (it carries the user's bundle id and agent label); use --variant verify" ;;
    *) ;;
  esac
fi

[[ -f "$IDENTITY" ]] || die "missing $IDENTITY"
set -a
# shellcheck source=/dev/null
source "$IDENTITY"
set +a
BASE_BUNDLE_ID="$BUNDLE_ID"
: "${BASE_BUNDLE_ID:?identity.env sets no BUNDLE_ID}"
: "${AGENT_LABEL:?identity.env sets no AGENT_LABEL}"

# Inputs the bundle reads or copies, checked before the first build step, so a missing one stops the build
# at once and never ships without its license. build/ holds the step logs, so it must exist before the first redirect.
FONT="$REPO/web/src/assets/fonts/archivo-latin.woff2"
ARCHIVO_LICENSE="$REPO/web/public/licenses/Archivo-OFL.txt"
COLLECTOR="$REPO/packaging/scripts/collect-licenses.py"
for input in "$TEMPLATE" "$FONT" "$ARCHIVO_LICENSE" "$COLLECTOR" "$REPO/LICENSE" "$REPO/NOTICE" \
  "$SIGN" "$REPO/packaging/entitlements/app.plist" "$REPO/packaging/entitlements/python.plist" \
  "$REPO/packaging/entitlements/python-adhoc.plist" "$REPO/packaging/entitlements/app-adhoc.plist" \
  "$REPO/packaging/scripts/lib-sparkle.sh" "$REPO/packaging/AppIcon.icns"; do
  [[ -f "$input" ]] || die "missing $input"
done
mkdir -p "$REPO/build"

VERIFY_HOME=""
if [[ "$VARIANT" == verify ]]; then
  VERIFY_HOME="${SPLASH_GUI_VERIFY_HOME:?the verify variant needs SPLASH_GUI_VERIFY_HOME (absolute); package.sh build --packaged --variant verify sets it}"
  [[ "$VERIFY_HOME" == /* ]] || die "SPLASH_GUI_VERIFY_HOME must be absolute (got $VERIFY_HOME)"
  # The home is baked into Info.plist and the agent plist, so the .verify app reads and writes it. It must be a
  # throwaway folder under build/verify/ (package.sh requires the same), with no .. and no symlink on the way.
  case "$VERIFY_HOME" in
    *"/../"*|*/..) die "SPLASH_GUI_VERIFY_HOME must not contain .. (got $VERIFY_HOME)" ;;
  esac
  # physical() resolves only the folders that exist, so a dangling symlink on the path is refused here.
  probe="$VERIFY_HOME"
  while [[ ! -d "$probe" ]]; do
    [[ ! -L "$probe" ]] || die "SPLASH_GUI_VERIFY_HOME must not pass through a symlink (got $VERIFY_HOME)"
    probe="$(dirname "$probe")"
  done
  case "$(physical "$VERIFY_HOME")" in
    "$REPO_PHYSICAL/build/verify/"?*) ;;
    *) die "SPLASH_GUI_VERIFY_HOME must be under $REPO/build/verify/ (got $VERIFY_HOME)" ;;
  esac
  BUNDLE_ID="$BASE_BUNDLE_ID.verify"
  AGENT_LABEL="$BASE_BUNDLE_ID.verify.manager"
fi
PLIST_NAME="$AGENT_LABEL.plist"
PYTHON_REL="Contents/Resources/manager/python/bin/python3"

# Version from git (a vX.Y.Z tag), else 0.1.0; build number = commit count (as bundle.sh).
VERSION="0.1.0"
BUILD="1"
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  TAG="$(git -C "$REPO" describe --tags --abbrev=0 2>/dev/null || true)"
  [[ "$TAG" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)$ ]] && VERSION="${BASH_REMATCH[1]}"
  BUILD="$(git -C "$REPO" rev-list --count HEAD 2>/dev/null || echo 1)"
fi

note "make web"
make -C "$REPO" web >"$REPO/build/package-web.log" 2>&1 \
  || { tail -20 "$REPO/build/package-web.log" >&2; die "make web failed (log: build/package-web.log)"; }
[[ -f "$REPO/web/dist/index.html" ]] || die "web/dist/index.html is missing after make web"

note "make runtime"
make -C "$REPO" runtime >"$REPO/build/package-runtime.log" 2>&1 \
  || { tail -20 "$REPO/build/package-runtime.log" >&2; die "make runtime failed (log: build/package-runtime.log)"; }
[[ -x "$RUNTIME/python/bin/python3" ]] || die "no bundled interpreter at $RUNTIME/python/bin/python3"

note "swift build -c release --product $EXECUTABLE"
(cd "$MACOS_DIR" && swift build -c release --product "$EXECUTABLE") >"$REPO/build/package-swift.log" 2>&1 \
  || { tail -20 "$REPO/build/package-swift.log" >&2; die "swift build failed (log: build/package-swift.log)"; }
BIN_DIR="$(cd "$MACOS_DIR" && swift build -c release --show-bin-path)"
[[ -x "$BIN_DIR/$EXECUTABLE" ]] || die "no $EXECUTABLE in $BIN_DIR"

note "assembling $OUT"
mkdir -p "$(dirname "$OUT")"
STAGE="${OUT%.app}.partial.app"   # still a .app, so sign.sh accepts it
rm -rf "$STAGE"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources/Fonts" "$STAGE/Contents/Library/LaunchAgents"
cp "$BIN_DIR/$EXECUTABLE" "$STAGE/Contents/MacOS/$EXECUTABLE"
chmod 755 "$STAGE/Contents/MacOS/$EXECUTABLE"
# Sparkle 2 in Contents/Frameworks, without its XPC services, and the rpath to it.
# shellcheck source=lib-sparkle.sh
source "$REPO/packaging/scripts/lib-sparkle.sh"
embed_sparkle "$STAGE" "$BIN_DIR" "$EXECUTABLE"

# ditto keeps symlinks (python3 → python3.13), modes and extended attributes.
ditto "$RUNTIME" "$STAGE/Contents/Resources/manager"
ditto "$REPO/web/dist" "$STAGE/Contents/Resources/web"

# Archivo (OFL), the same file the web admin self-hosts, for the About window, and its OFL text. Both were
# checked above, so a missing one cannot be skipped.
cp "$FONT" "$STAGE/Contents/Resources/Fonts/"
cp "$ARCHIVO_LICENSE" "$STAGE/Contents/Resources/"

# The app's license and notice, as the Apache-2.0 terms require them with a binary (checked above).
cp "$REPO/LICENSE" "$REPO/NOTICE" "$STAGE/Contents/Resources/"

# The app icon (packaging/scripts/make-icon.py draws it).
cp "$REPO/packaging/AppIcon.icns" "$STAGE/Contents/Resources/AppIcon.icns"

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

# Sparkle's feed and public key: identity.env, or SPLASH_GUI_SU_FEED_URL / SPLASH_GUI_SU_PUBLIC_ED_KEY for a
# local test feed and a throwaway key. Without both, no keys are written and the app creates no updater.
SU_FEED="${SPLASH_GUI_SU_FEED_URL:-${SU_FEED_URL:-}}"
SU_KEY="${SPLASH_GUI_SU_PUBLIC_ED_KEY:-${SU_PUBLIC_ED_KEY:-}}"
SU_BLOCK=""
if [[ -n "$SU_FEED" && -n "$SU_KEY" ]]; then
  SU_BLOCK="  <key>SUFeedURL</key><string>$(xml_escape "$SU_FEED")</string>
  <key>SUPublicEDKey</key><string>$(xml_escape "$SU_KEY")</string>
  <key>SUEnableAutomaticChecks</key><true/>"
elif [[ -n "$SU_FEED$SU_KEY" ]]; then
  die "a Sparkle feed needs both SU_FEED_URL and SU_PUBLIC_ED_KEY (identity.env or SPLASH_GUI_SU_*)"
fi

LS_ENV_BLOCK=""
if [[ "$VARIANT" == verify ]]; then
  VERIFY_HOME_XML="$(xml_escape "$VERIFY_HOME")"
  LS_ENV_BLOCK="  <key>LSEnvironment</key>
  <dict>
    <key>SPLASH_GUI_HOME</key><string>$VERIFY_HOME_XML</string>
    <key>SPLASH_GUI_SECRETS</key><string>file</string>
  </dict>"
fi

cat > "$STAGE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Splashboard</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleDisplayName</key><string>Splashboard</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$BUILD</string>
  <key>LSMinimumSystemVersion</key><string>26.4</string>
  <key>LSUIElement</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHumanReadableCopyright</key><string>Copyright © 2026 Splashboard contributors. Apache-2.0. Splashboard is not an inco.ai product.</string>
  <key>NSAppleEventsUsageDescription</key><string>Splashboard opens Terminal to run the Homebrew installer you asked for.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>SplashGUIAgentLabel</key><string>$AGENT_LABEL</string>
$SU_BLOCK
$LS_ENV_BLOCK
</dict>
</plist>
PLIST

# The LaunchAgent: placeholders filled from the template, no absolute paths.
sed -e "s|@AGENT_LABEL@|$AGENT_LABEL|g" -e "s|@BUNDLE_ID@|$BUNDLE_ID|g" -e "s|@PYTHON_REL@|$PYTHON_REL|g" \
  "$TEMPLATE" > "$STAGE/Contents/Library/LaunchAgents/$PLIST_NAME"
if [[ "$VARIANT" == verify ]]; then
  plutil -insert EnvironmentVariables.SPLASH_GUI_HOME -string "$VERIFY_HOME" \
    "$STAGE/Contents/Library/LaunchAgents/$PLIST_NAME"
  plutil -insert EnvironmentVariables.SPLASH_GUI_SECRETS -string file \
    "$STAGE/Contents/Library/LaunchAgents/$PLIST_NAME"
fi
if grep -q '@[A-Z_]*@' "$STAGE/Contents/Library/LaunchAgents/$PLIST_NAME"; then
  die "unfilled placeholder in $PLIST_NAME"
fi

plutil -lint "$STAGE/Contents/Info.plist" "$STAGE/Contents/Library/LaunchAgents/$PLIST_NAME" >/dev/null

# The bundled runtime must import the manager in isolated mode before it is signed.
PY="$STAGE/$PYTHON_REL"
[[ -x "$PY" ]] || die "the staged bundle has no interpreter at $PYTHON_REL"
"$PY" -I -B -c 'import splash_gui.manager, sys; print(sys.version.split()[0])' >/dev/null \
  || die "the bundled interpreter cannot import splash_gui.manager"

# CPython's license and one section per installed package, read from the staged runtime. A package
# with no license text stops the build here, so no bundle ships without its notices.
"$PY" -I -B "$COLLECTOR" \
  --runtime "$STAGE/Contents/Resources/manager/python" \
  --app-license "$REPO/LICENSE" \
  --out "$STAGE/Contents/Resources/licenses/THIRD_PARTY.txt" \
  || die "collect-licenses.py stopped the build (its reasons are above)"

# Sign inside out with the hardened runtime (sign.sh). Ad hoc unless --identity or SPLASH_GUI_SIGN_IDENTITY
# names a certificate. sign.sh ends with codesign --verify --strict --deep, so a failed signature stops the build.
note "sign.sh --identity $SIGN_IDENTITY (inside out, hardened runtime)"
"$SIGN" --identity "$SIGN_IDENTITY" --app "$STAGE" >"$REPO/build/package-sign.log" 2>&1 \
  || { tail -20 "$REPO/build/package-sign.log" >&2; die "sign.sh failed (log: build/package-sign.log)"; }

rm -rf "$OUT"
mv "$STAGE" "$OUT"
STAGE=""
SUCCESS=1

echo "built $OUT ($VERSION, build $BUILD)"
echo "variant $VARIANT · bundle id $BUNDLE_ID · agent $AGENT_LABEL"
echo "runtime $RUNTIME → Contents/Resources/manager (python $("$RUNTIME/python/bin/python3" -I -B -c 'import sys; print(sys.version.split()[0])'))"
echo "web     $REPO/web/dist → Contents/Resources/web"
echo "license LICENSE, NOTICE, licenses/THIRD_PARTY.txt → Contents/Resources"
echo "agent   Contents/Library/LaunchAgents/$PLIST_NAME (BundleProgram $PYTHON_REL)"
echo "sign    $SIGN_IDENTITY (sign.sh, inside out, hardened runtime; log build/package-sign.log)"
if [[ -n "$VERIFY_HOME" ]]; then echo "home    $VERIFY_HOME (SPLASH_GUI_SECRETS=file)"; fi

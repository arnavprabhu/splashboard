#!/usr/bin/env bash
# Builds `macos/build/Splash GUI.app` for development (packaging proper is deferred, D30).
#
#   macos/scripts/bundle.sh                              # default variant, release build, ad-hoc signed
#   macos/scripts/bundle.sh --variant verify             # .verify ids, baked to a throwaway home
#   SPLASH_GUI_REPO=/path macos/scripts/bundle.sh
#   SPLASH_GUI_VERIFY_HOME=/abs/build/verify/<run>/home macos/scripts/bundle.sh --variant verify
#
# The bundle runs the manager from this source checkout with uv: the repo path is baked into
# Info.plist (SplashGUIRepoPath) and into the LaunchAgent plist that SMAppService.agent needs at
# Contents/Library/LaunchAgents/<agent label>.plist.
#
# Identity (bundle id, agent label, Sparkle and team placeholders) comes from packaging/identity.env.
# The label is also written to Info.plist as SplashGUIAgentLabel, which the Swift app reads (PKG-1).
#
# The verify variant (PKG-1, docs/plans/packaging.md) appends ".verify" to the bundle id and makes the
# label "<bundle id>.verify.manager". Both apps then run with SPLASH_GUI_HOME set to the run's home and
# SPLASH_GUI_SECRETS=file, so they never reach the owner's ~/.splash, port 8123 or the login Keychain.
set -euo pipefail

MACOS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${SPLASH_GUI_REPO:-$(cd "$MACOS_DIR/.." && pwd)}"
BUILD_DIR="$MACOS_DIR/build"
APP="$BUILD_DIR/Splash GUI.app"
EXECUTABLE="SplashGUI"

VARIANT="default"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="${2:?--variant needs default or verify}"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "error: unknown option $1" >&2; exit 2 ;;
  esac
done
case "$VARIANT" in
  default|verify) ;;
  *) echo "error: --variant must be default or verify (got $VARIANT)" >&2; exit 2 ;;
esac

if [[ ! -f "$REPO/manager/pyproject.toml" ]]; then
  echo "error: $REPO does not look like the Splash GUI repo (no manager/pyproject.toml)" >&2
  exit 1
fi

# The identity: packaging/identity.env next to this script's repo, not the SPLASH_GUI_REPO override.
IDENTITY="$MACOS_DIR/../packaging/identity.env"
if [[ ! -f "$IDENTITY" ]]; then
  echo "error: missing $IDENTITY" >&2
  exit 1
fi
set -a
# shellcheck source=../../packaging/identity.env
source "$IDENTITY"
set +a
BASE_BUNDLE_ID="$BUNDLE_ID"
: "${BASE_BUNDLE_ID:?identity.env sets no BUNDLE_ID}"
: "${AGENT_LABEL:?identity.env sets no AGENT_LABEL}"

# Verify variant: the home is a throwaway run folder, passed by package.sh (absolute path).
VERIFY_HOME=""
if [[ "$VARIANT" == verify ]]; then
  VERIFY_HOME="${SPLASH_GUI_VERIFY_HOME:?the verify variant needs SPLASH_GUI_VERIFY_HOME (absolute); package.sh build --variant verify sets it}"
  [[ "$VERIFY_HOME" == /* ]] || { echo "error: SPLASH_GUI_VERIFY_HOME must be absolute (got $VERIFY_HOME)" >&2; exit 1; }
  BUNDLE_ID="$BASE_BUNDLE_ID.verify"
  AGENT_LABEL="$BASE_BUNDLE_ID.verify.manager"
fi

# uv: PATH, Homebrew, then the standalone installer location.
UV="$(command -v uv || true)"
for candidate in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
  [[ -z "$UV" && -x "$candidate" ]] && UV="$candidate"
done
if [[ -z "$UV" ]]; then
  echo "warning: uv not found; the LaunchAgent will point at $HOME/.local/bin/uv" >&2
  UV="$HOME/.local/bin/uv"
fi

# Version from git (a vX.Y.Z tag), else 0.1.0; build number = commit count.
VERSION="0.1.0"
BUILD="1"
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  TAG="$(git -C "$REPO" describe --tags --abbrev=0 2>/dev/null || true)"
  [[ "$TAG" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)$ ]] && VERSION="${BASH_REMATCH[1]}"
  BUILD="$(git -C "$REPO" rev-list --count HEAD 2>/dev/null || echo 1)"
fi

echo "==> swift build -c release"
cd "$MACOS_DIR"
swift build -c release --product "$EXECUTABLE"
BIN_DIR="$(swift build -c release --show-bin-path)"

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/Fonts" "$APP/Contents/Library/LaunchAgents"
cp "$BIN_DIR/$EXECUTABLE" "$APP/Contents/MacOS/$EXECUTABLE"
chmod 755 "$APP/Contents/MacOS/$EXECUTABLE"

# Archivo (OFL), the same file the web admin self-hosts, for the About window.
FONT="$REPO/web/src/assets/fonts/archivo-latin.woff2"
[[ -f "$FONT" ]] && cp "$FONT" "$APP/Contents/Resources/Fonts/"
[[ -f "$REPO/web/public/licenses/Archivo-OFL.txt" ]] && cp "$REPO/web/public/licenses/Archivo-OFL.txt" "$APP/Contents/Resources/"

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
REPO_XML="$(xml_escape "$REPO")"
UV_XML="$(xml_escape "$UV")"
HOME_XML="$(xml_escape "$HOME")"

# Verify variant only: the app (LSEnvironment, for launches through LaunchServices) and the manager
# agent (EnvironmentVariables, for launchd) both get the throwaway home and the file secrets backend.
LS_ENV_BLOCK=""
AGENT_ENV_BLOCK=""
if [[ "$VARIANT" == verify ]]; then
  VERIFY_HOME_XML="$(xml_escape "$VERIFY_HOME")"
  LS_ENV_BLOCK="  <key>LSEnvironment</key>
  <dict>
    <key>SPLASH_GUI_HOME</key><string>$VERIFY_HOME_XML</string>
    <key>SPLASH_GUI_SECRETS</key><string>file</string>
  </dict>"
  AGENT_ENV_BLOCK="    <key>SPLASH_GUI_HOME</key><string>$VERIFY_HOME_XML</string>
    <key>SPLASH_GUI_SECRETS</key><string>file</string>"
fi
PLIST_NAME="$AGENT_LABEL.plist"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleExecutable</key><string>$EXECUTABLE</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Splash GUI</string>
  <key>CFBundleDisplayName</key><string>Splash GUI</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$BUILD</string>
  <key>LSMinimumSystemVersion</key><string>26.4</string>
  <key>LSUIElement</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHumanReadableCopyright</key><string>Copyright © 2026 Splash GUI contributors. Apache-2.0. Splash GUI is not an inco.ai product.</string>
  <key>NSAppleEventsUsageDescription</key><string>Splash GUI opens Terminal to run the Homebrew installer you asked for.</string>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>SplashGUIRepoPath</key><string>$REPO_XML</string>
  <key>SplashGUIAgentLabel</key><string>$AGENT_LABEL</string>
$LS_ENV_BLOCK
</dict>
</plist>
PLIST

# LaunchAgent for SMAppService.agent(plistName:) (SPEC §4.2). ProcessType Interactive so
# launchd does not apply background throttling to the manager or the engine it spawns.
cat > "$APP/Contents/Library/LaunchAgents/$PLIST_NAME" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$AGENT_LABEL</string>
  <key>AssociatedBundleIdentifiers</key><array><string>$BUNDLE_ID</string></array>
  <key>ProgramArguments</key>
  <array>
    <string>$UV_XML</string>
    <string>run</string>
    <string>--project</string>
    <string>$REPO_XML/manager</string>
    <string>splash-gui-manager</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_XML/manager</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME_XML/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
$AGENT_ENV_BLOCK
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME_XML/Library/Logs/$AGENT_LABEL.log</string>
  <key>StandardErrorPath</key><string>$HOME_XML/Library/Logs/$AGENT_LABEL.log</string>
</dict>
</plist>
PLIST

plutil -lint "$APP/Contents/Info.plist" "$APP/Contents/Library/LaunchAgents/$PLIST_NAME" >/dev/null

echo "==> codesign (ad-hoc)"
codesign --force --sign - --deep "$APP"
codesign --verify --deep --strict "$APP"

echo "==> built $APP ($VERSION, build $BUILD)"
echo "    variant: $VARIANT · bundle id $BUNDLE_ID · agent $AGENT_LABEL"
if [[ -n "$VERIFY_HOME" ]]; then echo "    SPLASH_GUI_HOME: $VERIFY_HOME (SPLASH_GUI_SECRETS=file)"; fi
echo "    repo: $REPO"
echo "    uv:   $UV"

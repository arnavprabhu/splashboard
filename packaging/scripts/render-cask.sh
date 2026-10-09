#!/usr/bin/env bash
# Renders the Homebrew cask for a release DMG.
#
#   packaging/scripts/render-cask.sh --dmg PATH [--url-prefix URL] [--out FILE]
#   packaging/scripts/render-cask.sh --dmg PATH --pull-request         # CI only: a PR to the tap repo
#
# Everything the cask names comes from the app inside the DMG (mounted read-only, -nobrowse): the app name, the
# bundle id, the version and the agent label (SplashGUIAgentLabel), so the identity switch needs no edit
# here. The token is the app name in lower case with hyphens (`splash-gui`, later `splashboard`). The URL is
# <prefix><DMG file name>; the default prefix is the source repo's release download path for this version.
# The Keychain services for zap come from the manager's SecretName.
#
# --pull-request clones the tap (TAP_REPO, default arnavprabhu/homebrew-tap) with gh, writes Casks/<token>.rb on a
# branch and opens a PR. It needs GH_TOKEN and runs only on a CI runner (GITHUB_ACTIONS=true), so a person's Mac
# never pushes anywhere from it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="$REPO/packaging/cask/cask.rb.in"
RELEASES_REPO="${RELEASES_REPO:-arnavprabhu/splashboard}"
TAP_REPO="${TAP_REPO:-arnavprabhu/homebrew-tap}"
DMG=""
PREFIX=""
OUT=""
PR=0

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dmg) DMG="${2:?--dmg needs a path}"; shift 2 ;;
    --url-prefix) PREFIX="${2:?--url-prefix needs a URL}"; shift 2 ;;
    --out) OUT="${2:?--out needs a file}"; shift 2 ;;
    --pull-request) PR=1; shift ;;
    --version) shift 2 ;;  # accepted for older callers; the version comes from the app
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (render-cask.sh --help)" ;;
  esac
done
[[ -n "$DMG" && -f "$DMG" ]] || die "--dmg needs an existing DMG (got ${DMG:-nothing})"
[[ -f "$TEMPLATE" ]] || die "missing $TEMPLATE"
if [[ $PR -eq 1 ]]; then
  [[ "${GITHUB_ACTIONS:-}" == true ]] || die "--pull-request runs only on a CI runner (GITHUB_ACTIONS=true)"
  : "${GH_TOKEN:?--pull-request needs GH_TOKEN (the tap token)}"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/render-cask.XXXXXX")"
MNT="$TMP/mnt"
ATTACHED=0
cleanup() {
  if [[ $ATTACHED -eq 1 ]] && { hdiutil detach -quiet "$MNT" || hdiutil detach -quiet -force "$MNT"; }; then ATTACHED=0; fi
  if [[ $ATTACHED -eq 0 ]]; then rm -rf "$TMP"; fi
}
trap cleanup EXIT

mkdir -p "$MNT"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MNT" "$DMG" </dev/null
ATTACHED=1
APP="$(find "$MNT" -mindepth 1 -maxdepth 1 -name '*.app' -print -quit)"
[[ -n "$APP" ]] || die "no .app inside $DMG"
plist() { plutil -extract "$1" raw -o - "$APP/Contents/Info.plist" 2>/dev/null; }
APP_NAME="$(basename "$APP" .app)"
BUNDLE_ID="$(plist CFBundleIdentifier)" || die "no CFBundleIdentifier"
VERSION="$(plist CFBundleShortVersionString)" || die "no CFBundleShortVersionString"
AGENT_LABEL="$(plist SplashGUIAgentLabel)" || die "no SplashGUIAgentLabel (build-app.sh writes it)"
hdiutil detach -quiet "$MNT"
ATTACHED=0
case "$BUNDLE_ID" in *.verify) die "refusing to render a cask for a .verify build ($BUNDLE_ID)" ;; *) ;; esac

TOKEN="$(printf '%s' "$APP_NAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
SHA256="$(shasum -a 256 "$DMG" | cut -d' ' -f1)"
[[ -n "$PREFIX" ]] || PREFIX="https://github.com/$RELEASES_REPO/releases/download/v$VERSION/"
# The URL interpolates the version, as brew audit asks for a versioned URL.
URL="${PREFIX%/}/$(basename "$DMG")"
URL="${URL//$VERSION/#\{version\}}"
SECRETS="$(cd "$REPO/manager" && uv run --quiet python -c \
  'from splash_gui.secrets import SecretName; print(", ".join(f"\"{s}\"" for s in SecretName))')" \
  || die "could not read the Keychain service names from splash_gui.secrets"

render() {
  sed -e "s|@TOKEN@|$TOKEN|g" -e "s|@VERSION@|$VERSION|g" -e "s|@SHA256@|$SHA256|g" -e "s|@URL@|$URL|g" \
    -e "s|@APP_NAME@|$APP_NAME|g" -e "s|@BUNDLE_ID@|$BUNDLE_ID|g" -e "s|@AGENT_LABEL@|$AGENT_LABEL|g" \
    -e "s|@HOMEPAGE@|https://github.com/$RELEASES_REPO|g" -e "s|@SECRET_SERVICES@|$SECRETS|g" "$TEMPLATE"
}
RENDERED="$(render)"
if grep -q '@[A-Z_0-9]*@' <<<"$RENDERED"; then die "unfilled placeholder in the cask"; fi

if [[ $PR -eq 0 ]]; then
  if [[ -n "$OUT" ]]; then
    mkdir -p "$(dirname "$OUT")"
    printf '%s\n' "$RENDERED" > "$OUT"
    echo "wrote $OUT (cask $TOKEN $VERSION)"
  else
    printf '%s\n' "$RENDERED"
  fi
  exit 0
fi

gh repo clone "$TAP_REPO" "$TMP/tap" -- --depth 1
branch="$TOKEN-$VERSION"
git -C "$TMP/tap" switch -c "$branch"
mkdir -p "$TMP/tap/Casks"
printf '%s\n' "$RENDERED" > "$TMP/tap/Casks/$TOKEN.rb"
git -C "$TMP/tap" add "Casks/$TOKEN.rb"
git -C "$TMP/tap" -c user.name="splash-gui release" -c user.email="releases@users.noreply.github.com" \
  commit -m "$TOKEN $VERSION"
git -C "$TMP/tap" push origin "$branch"
gh pr create --repo "$TAP_REPO" --head "$branch" --title "$TOKEN $VERSION" \
  --body "Release $VERSION from the release workflow."

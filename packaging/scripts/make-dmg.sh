#!/usr/bin/env bash
# Builds the distributable DMG from a signed app (docs/plans/packaging.md, PKG-7).
#
#   packaging/scripts/make-dmg.sh                                  # build/package/Splashboard.app → build/package/<Name>-<version>.dmg
#   packaging/scripts/make-dmg.sh --app PATH --out DIR             # another app, another output folder
#   packaging/scripts/make-dmg.sh --identity "Apple Development: <name> (<id>)"  # also sign the DMG
#   make dmg                                                       # the default build, through the Makefile
#
# The DMG holds exactly two entries: the app and an `Applications` symlink. It has no background image, icon
# layout or custom volume icon, because DESIGN.md rules out decoration. The volume name is the app's
# CFBundleName and the file is `<CFBundleName with spaces as hyphens>-<CFBundleShortVersionString>.dmg`, so the
# identity switch (PKG-16) changes both without editing this script.
#
# The app is copied with `ditto`, which keeps its signature, extended attributes and symlinks. The image is
# APFS, compressed (UDZO), written next to its final path and moved into place only after `hdiutil verify`
# passes, so a failed build leaves no DMG behind. With --identity the DMG itself is signed too (--timestamp only
# for a real identity, as sign.sh does). Notarization is PKG-8.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$REPO/build/package/Splashboard.app"
OUT_DIR="$REPO/build/package"
IDENTITY=""

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="${2:?--app needs a path}"; shift 2 ;;
    --out) OUT_DIR="${2:?--out needs a folder}"; shift 2 ;;
    --identity) IDENTITY="${2:?--identity needs - (ad hoc) or a certificate name}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (make-dmg.sh --help)" ;;
  esac
done

[[ "$APP" == /* ]] || APP="$PWD/$APP"
[[ "$OUT_DIR" == /* ]] || OUT_DIR="$PWD/$OUT_DIR"
[[ -d "$APP" && "$APP" == *.app ]] || die "no app bundle at $APP (build it with make app)"
[[ -f "$APP/Contents/Info.plist" ]] || die "no Contents/Info.plist in $APP"
case "$(printf '%s' "$OUT_DIR" | tr '[:upper:]' '[:lower:]')" in
  /applications|/applications/*) die "refusing to write a DMG into $OUT_DIR" ;;
  *) ;;
esac

plist() { plutil -extract "$1" raw -o - "$APP/Contents/Info.plist" 2>/dev/null; }
NAME="$(plist CFBundleName)" || die "no CFBundleName in $APP/Contents/Info.plist"
VERSION="$(plist CFBundleShortVersionString)" || die "no CFBundleShortVersionString in $APP/Contents/Info.plist"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "CFBundleShortVersionString is not X.Y.Z: $VERSION"
DMG="$OUT_DIR/${NAME// /-}-$VERSION.dmg"

# An unsigned or broken app would be shipped as is: refuse it here, before anything is written.
codesign --verify --strict --deep "$APP" </dev/null 2>/dev/null \
  || die "$APP does not pass codesign --verify --strict --deep (sign it first: packaging/scripts/sign.sh)"

mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d "$OUT_DIR/.dmg-stage.XXXXXX")"
PARTIAL="$DMG.partial.dmg"
cleanup() { rm -rf "$STAGE"; rm -f "$PARTIAL"; }
trap cleanup EXIT

ditto "$APP" "$STAGE/$(basename "$APP")"
ln -s /Applications "$STAGE/Applications"

rm -f "$PARTIAL"
hdiutil create -quiet -fs APFS -format UDZO -volname "$NAME" -srcfolder "$STAGE" "$PARTIAL" </dev/null
if [[ -n "$IDENTITY" ]]; then
  args=(--force --sign "$IDENTITY")
  if [[ "$IDENTITY" != - ]]; then args+=(--timestamp); fi
  codesign "${args[@]}" "$PARTIAL" </dev/null
  codesign --verify --strict "$PARTIAL" </dev/null
fi
hdiutil verify -quiet "$PARTIAL" </dev/null
mv -f "$PARTIAL" "$DMG"
echo "built $DMG ($(du -h "$DMG" | cut -f1 | tr -d ' '), volume \"$NAME\"${IDENTITY:+, signed with $IDENTITY})"

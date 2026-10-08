#!/usr/bin/env bash
# Writes the Sparkle appcast for a folder of release DMGs (docs/plans/packaging.md, PKG-10; D71).
#
#   packaging/scripts/appcast.sh --ed-key-file KEY FOLDER                 # CI, or a local test key
#   packaging/scripts/appcast.sh --account NAME FOLDER                    # the owner's Mac (key in the login Keychain)
#   packaging/scripts/appcast.sh --ed-key-file KEY --download-url-prefix URL FOLDER   # enclosures on the releases repo
#   packaging/scripts/appcast.sh ... --phased-rollout-interval SECONDS FOLDER          # phased rollout for new items
#
# FOLDER holds `<Name>-<version>.dmg` files (make-dmg.sh). Each DMG is its own update archive, so one file serves the
# first install, Sparkle and the cask (D71). Release notes come from packaging/release-notes/<version>.html, copied
# next to the DMG under the DMG's name (that is how generate_appcast pairs them) and embedded in the item. One
# channel, the default one (no <sparkle:channel>), and no delta updates (--maximum-deltas 0): D71.
#
# generate_appcast signs an archive only when the app inside declares SUPublicEDKey (build-app.sh writes it from
# identity.env), and that key must belong to the private key given here; an unsigned enclosure stops this script.
# The key source must be named: there is no default, so the script never signs with whatever key the Keychain
# holds by accident. Sparkle's generate_appcast comes from the SwiftPM artifact (macos/.build/artifacts/sparkle,
# `swift package resolve` in macos/) or SPARKLE_BIN. The result is checked: well-formed XML, one item per DMG, an
# edSignature and length on every enclosure, no sparkle:deltas, no sparkle:channel.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NOTES_DIR="$REPO/packaging/release-notes"
SPARKLE_BIN="${SPARKLE_BIN:-$REPO/macos/.build/artifacts/sparkle/Sparkle/bin}"
KEY_FILE=""
ACCOUNT=""
URL_PREFIX=""
PHASED=""
FOLDER=""

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ed-key-file) KEY_FILE="${2:?--ed-key-file needs a path}"; shift 2 ;;
    --account) ACCOUNT="${2:?--account needs a Keychain account name}"; shift 2 ;;
    --download-url-prefix) URL_PREFIX="${2:?--download-url-prefix needs a URL}"; shift 2 ;;
    --phased-rollout-interval) PHASED="${2:?--phased-rollout-interval needs seconds}"; shift 2 ;;
    --notes) NOTES_DIR="${2:?--notes needs a folder}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option $1 (appcast.sh --help)" ;;
    *) [[ -z "$FOLDER" ]] || die "one folder only"; FOLDER="$1"; shift ;;
  esac
done
[[ -n "$FOLDER" ]] || die "a folder of DMGs is required (appcast.sh --help)"
[[ -d "$FOLDER" ]] || die "no folder at $FOLDER"
FOLDER="$(cd "$FOLDER" && pwd -P)"
if [[ -n "$KEY_FILE" && -n "$ACCOUNT" ]]; then die "--ed-key-file and --account are exclusive"; fi
[[ -n "$KEY_FILE$ACCOUNT" ]] || die "name the signing key: --ed-key-file KEY or --account NAME (there is no default)"
if [[ -n "$KEY_FILE" ]]; then [[ -f "$KEY_FILE" ]] || die "no key file at $KEY_FILE"; fi
if [[ -n "$PHASED" ]]; then [[ "$PHASED" =~ ^[0-9]+$ ]] || die "--phased-rollout-interval takes whole seconds"; fi
if [[ -n "$URL_PREFIX" ]]; then [[ "$URL_PREFIX" == http://* || "$URL_PREFIX" == https://* ]] || die "--download-url-prefix must be a URL"; fi
GEN="$SPARKLE_BIN/generate_appcast"
[[ -x "$GEN" ]] || die "no generate_appcast at $GEN (run: cd macos && swift package resolve)"

shopt -s nullglob
DMGS=("$FOLDER"/*.dmg)
shopt -u nullglob
[[ ${#DMGS[@]} -gt 0 ]] || die "no .dmg files in $FOLDER"

# Release notes: <version>.html next to each DMG under the DMG's own name, unless one is already there.
for dmg in "${DMGS[@]}"; do
  base="$(basename "$dmg" .dmg)"
  version="${base##*-}"
  notes="$NOTES_DIR/$version.html"
  if [[ -f "$notes" && ! -e "$FOLDER/$base.html" ]]; then cp "$notes" "$FOLDER/$base.html"; fi
done

args=(--maximum-deltas 0 --embed-release-notes -o "$FOLDER/appcast.xml")
if [[ -n "$KEY_FILE" ]]; then args+=(--ed-key-file "$KEY_FILE"); else args+=(--account "$ACCOUNT"); fi
if [[ -n "$URL_PREFIX" ]]; then args+=(--download-url-prefix "${URL_PREFIX%/}/"); fi
if [[ -n "$PHASED" ]]; then args+=(--phased-rollout-interval "$PHASED"); fi
"$GEN" "${args[@]}" "$FOLDER" </dev/null

APPCAST="$FOLDER/appcast.xml"
xmllint --noout "$APPCAST" || die "appcast.xml is not well-formed"
items="$(xmllint --xpath 'count(//item)' "$APPCAST")"
signed="$(xmllint --xpath 'count(//item/enclosure[@*[local-name()="edSignature"] and @length])' "$APPCAST")"
[[ "$items" -eq ${#DMGS[@]} ]] || die "appcast.xml has $items items for ${#DMGS[@]} DMGs"
[[ "$signed" -eq "$items" ]] || die "only $signed of $items enclosures carry an edSignature and a length"
[[ "$(xmllint --xpath 'count(//*[local-name()="deltas"])' "$APPCAST")" -eq 0 ]] || die "appcast.xml has delta updates"
[[ "$(xmllint --xpath 'count(//*[local-name()="channel" and namespace-uri()!=""])' "$APPCAST")" -eq 0 ]] \
  || die "appcast.xml names a sparkle:channel"
echo "wrote $APPCAST: $items items, every enclosure signed, no deltas, the default channel"

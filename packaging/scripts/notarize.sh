#!/usr/bin/env bash
# Notarizes and staples a Developer ID signed app or DMG.
#
#   packaging/scripts/notarize.sh --keychain-profile NAME TARGET     # the user's Mac (xcrun notarytool store-credentials)
#   packaging/scripts/notarize.sh --key K.p8 --key-id ID --issuer UUID TARGET   # CI (App Store Connect API key)
#   packaging/scripts/notarize.sh --dry-run TARGET                   # print the commands, check the signature, submit nothing
#   make notarize TARGET=build/package/Splash-GUI-0.1.0.dmg ARGS="--keychain-profile splash-notary"
#
# TARGET is a .app or a .dmg. A stapled ticket can only be added to an item that is not changed afterwards, so a
# release notarizes twice, in this order:
#   1. notarize.sh <app>   zips the app with ditto, submits the zip, staples the app;
#   2. make-dmg.sh         builds and signs the DMG from the stapled app;
#   3. notarize.sh <dmg>   submits the DMG, staples it.
# Then the app opens offline from the DMG on first launch, and the DMG itself passes Gatekeeper.
#
# Before anything is submitted the target's signature is checked: a `Developer ID Application` authority, a secure
# timestamp, and the hardened runtime flag on the app (for a DMG, on the app inside it, mounted read-only and
# -nobrowse in a temp folder). Any other identity (ad hoc, Apple Development) stops here with exit 1, in --dry-run
# too: that is the expected result until the Developer ID certificate exists.
#
# On `Accepted`: xcrun stapler staple, xcrun stapler validate, then spctl (`-t exec` for an app, `-t install` with
# --context context:primary-signature for a DMG). On any other status the notary log is saved to
# build/package/notary-<submission id>.json and the script exits 1. Nothing is uploaded except the target.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="$REPO/build/package"
DRY_RUN=0
PROFILE=""
KEY=""
KEY_ID=""
ISSUER=""
TARGET=""

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --keychain-profile) PROFILE="${2:?--keychain-profile needs a name}"; shift 2 ;;
    --key) KEY="${2:?--key needs a .p8 path}"; shift 2 ;;
    --key-id) KEY_ID="${2:?--key-id needs an id}"; shift 2 ;;
    --issuer) ISSUER="${2:?--issuer needs a UUID}"; shift 2 ;;
    --log-dir) LOG_DIR="${2:?--log-dir needs a folder}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option $1 (notarize.sh --help)" ;;
    *) [[ -z "$TARGET" ]] || die "one target only (got $TARGET and $1)"; TARGET="$1"; shift ;;
  esac
done
[[ -n "$TARGET" ]] || die "a target .app or .dmg is required (notarize.sh --help)"
[[ "$TARGET" == /* ]] || TARGET="$PWD/$TARGET"
TARGET="${TARGET%/}"
case "$TARGET" in
  *.app) KIND=app; [[ -d "$TARGET" ]] || die "no app at $TARGET" ;;
  *.dmg) KIND=dmg; [[ -f "$TARGET" ]] || die "no DMG at $TARGET" ;;
  *) die "the target must be a .app or a .dmg (got $TARGET)" ;;
esac

# Credentials: a keychain profile, or all three API key values. Not needed for --dry-run.
CREDS=()
if [[ -n "$PROFILE" ]]; then
  CREDS=(--keychain-profile "$PROFILE")
elif [[ -n "$KEY$KEY_ID$ISSUER" ]]; then
  [[ -n "$KEY" && -n "$KEY_ID" && -n "$ISSUER" ]] || die "--key, --key-id and --issuer go together"
  [[ -f "$KEY" ]] || die "no API key file at $KEY"
  CREDS=(--key "$KEY" --key-id "$KEY_ID" --issuer "$ISSUER")
elif [[ $DRY_RUN -eq 0 ]]; then
  die "credentials are required: --keychain-profile NAME, or --key/--key-id/--issuer"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/notarize.XXXXXX")"
MNT="$TMP/mnt"
ATTACHED=0
# A volume that will not detach is left mounted with its folder, never removed while mounted.
cleanup() {
  if [[ $ATTACHED -eq 1 ]] && { hdiutil detach -quiet "$MNT" || hdiutil detach -quiet -force "$MNT"; }; then
    ATTACHED=0
  fi
  if [[ $ATTACHED -eq 0 ]]; then rm -rf "$TMP"; fi
}
trap cleanup EXIT

# check_signed ITEM LABEL NEED_RUNTIME: Developer ID authority, secure timestamp, and (for code) the runtime flag.
check_signed() {
  local item="$1" label="$2" need_runtime="$3" info flag=""
  info="$(codesign -dvv "$item" 2>&1 </dev/null)" || die "$label is not signed: $info"
  grep -q '^Authority=Developer ID Application: ' <<<"$info" \
    || die "$label is not signed with a Developer ID Application identity ($(grep -m1 '^Authority=' <<<"$info" || echo 'ad hoc'))"
  grep -q '^Timestamp=' <<<"$info" || die "$label has no secure timestamp (sign with --timestamp)"
  if [[ "$need_runtime" == yes ]]; then
    codesign -dvvv "$item" 2>&1 </dev/null | grep -q 'flags=.*runtime' || die "$label has no hardened runtime flag"
    flag=" and the runtime flag"
  fi
  codesign --verify --strict --deep "$item" </dev/null || die "$label fails codesign --verify --strict --deep"
  echo "ok: $label is Developer ID signed with a secure timestamp$flag"
}

if [[ $KIND == app ]]; then SUBMIT="$TMP/$(basename "$TARGET" .app).zip"; else SUBMIT="$TARGET"; fi

# --dry-run lists the commands first, then runs the same signature checks a real run does (and fails the same way).
if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry-run: the commands are"
  if [[ $KIND == app ]]; then echo "  ditto -c -k --keepParent \"$TARGET\" \"$SUBMIT\""; fi
  echo "  xcrun notarytool submit \"$SUBMIT\" --wait --output-format json <credentials>"
  echo "  xcrun stapler staple \"$TARGET\""
  echo "  xcrun stapler validate \"$TARGET\""
  if [[ $KIND == app ]]; then echo "  spctl -a -vv -t exec \"$TARGET\""
  else echo "  spctl -a -vv -t install --context context:primary-signature \"$TARGET\""; fi
  echo "dry-run: checking the signature"
fi

if [[ $KIND == app ]]; then
  check_signed "$TARGET" "the app" yes
else
  check_signed "$TARGET" "the DMG" no
  mkdir -p "$MNT"
  hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MNT" "$TARGET" </dev/null
  ATTACHED=1
  APP_IN="$(find "$MNT" -mindepth 1 -maxdepth 1 -name '*.app' -print -quit)"
  [[ -n "$APP_IN" ]] || die "no .app inside $TARGET"
  check_signed "$APP_IN" "the app inside the DMG" yes
  xcrun stapler validate "$APP_IN" >/dev/null 2>&1 \
    || echo "note: the app inside the DMG has no stapled ticket; notarize the app first (step 1 above)"
  hdiutil detach -quiet "$MNT"
  ATTACHED=0
fi
[[ $DRY_RUN -eq 0 ]] || { echo "dry-run: the target passes the checks; nothing was submitted"; exit 0; }

if [[ $KIND == app ]]; then ditto -c -k --keepParent "$TARGET" "$SUBMIT"; fi
START=$SECONDS
RESULT="$(xcrun notarytool submit "$SUBMIT" --wait --output-format json "${CREDS[@]}" </dev/null)" \
  || die "notarytool submit failed: $RESULT"
STATUS="$(plutil -extract status raw -o - - <<<"$RESULT" 2>/dev/null || echo unknown)"
ID="$(plutil -extract id raw -o - - <<<"$RESULT" 2>/dev/null || echo unknown)"
echo "notarytool: submission $ID status $STATUS after $((SECONDS - START)) s"
if [[ "$STATUS" != Accepted ]]; then
  mkdir -p "$LOG_DIR"
  xcrun notarytool log "$ID" "${CREDS[@]}" "$LOG_DIR/notary-$ID.json" </dev/null || true
  die "notarization was not accepted ($STATUS); log: $LOG_DIR/notary-$ID.json"
fi
xcrun stapler staple "$TARGET" </dev/null
xcrun stapler validate "$TARGET" </dev/null
if [[ $KIND == app ]]; then spctl -a -vv -t exec "$TARGET" </dev/null
else spctl -a -vv -t install --context context:primary-signature "$TARGET" </dev/null; fi
echo "notarized and stapled $TARGET"

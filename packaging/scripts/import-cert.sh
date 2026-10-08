#!/usr/bin/env bash
# Imports the Developer ID certificate into a temporary keychain for a CI release (docs/plans/packaging.md, PKG-13).
#
#   packaging/scripts/import-cert.sh import   # prints the signing identity's name; needs the three secrets below
#   packaging/scripts/import-cert.sh delete   # removes the temporary keychain (release.yml runs it in always())
#
# Environment: DEVELOPER_ID_P12_BASE64 (the exported .p12, base64), DEVELOPER_ID_P12_PASSWORD, KEYCHAIN_PASSWORD
# (any random string; it only locks the temporary keychain), RUNNER_TEMP (where the keychain lives).
#
# It runs only on a CI runner (GITHUB_ACTIONS=true): on a person's Mac it would change their keychain search
# list, so it refuses. The keychain is added to the user search list so codesign finds the identity, unlocked
# for six hours, and its key partition list allows apple-tool: and apple: (codesign) without a prompt. The
# decoded .p12 is deleted as soon as it is imported. Secret values are never printed.
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }
[[ "${GITHUB_ACTIONS:-}" == true ]] || die "import-cert.sh runs only on a CI runner (GITHUB_ACTIONS=true); it edits the keychain search list"
: "${RUNNER_TEMP:?RUNNER_TEMP is not set}"
KEYCHAIN="$RUNNER_TEMP/splash-gui-signing.keychain-db"

case "${1:-}" in
  import)
    : "${DEVELOPER_ID_P12_BASE64:?the secret DEVELOPER_ID_P12_BASE64 is not set}"
    : "${DEVELOPER_ID_P12_PASSWORD:?the secret DEVELOPER_ID_P12_PASSWORD is not set}"
    : "${KEYCHAIN_PASSWORD:?the secret KEYCHAIN_PASSWORD is not set}"
    p12="$(mktemp "$RUNNER_TEMP/cert.XXXXXX")"
    trap 'rm -f "$p12"' EXIT
    printf '%s' "$DEVELOPER_ID_P12_BASE64" | base64 --decode > "$p12"
    security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
    security set-keychain-settings -lut 21600 "$KEYCHAIN"
    security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
    security import "$p12" -k "$KEYCHAIN" -P "$DEVELOPER_ID_P12_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security >/dev/null
    security set-key-partition-list -S apple-tool:,apple: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
    existing=()
    while IFS= read -r line; do
      line="${line//\"/}"
      line="${line#"${line%%[![:space:]]*}"}"
      [[ -n "$line" ]] && existing+=("$line")
    done < <(security list-keychains -d user)
    security list-keychains -d user -s "$KEYCHAIN" "${existing[@]}"
    identity="$(security find-identity -v -p codesigning "$KEYCHAIN" \
      | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)"
    [[ -n "$identity" ]] || die "the .p12 holds no Developer ID Application identity"
    printf '%s\n' "$identity"
    ;;
  delete)
    if [[ -e "$KEYCHAIN" ]]; then security delete-keychain "$KEYCHAIN"; fi
    echo "deleted the temporary keychain"
    ;;
  *) die "usage: import-cert.sh import|delete" ;;
esac

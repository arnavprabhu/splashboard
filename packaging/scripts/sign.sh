#!/usr/bin/env bash
# Signs a Splashboard.app inside out with the hardened runtime (docs/plans/packaging.md, PKG-6; D86).
#
#   packaging/scripts/sign.sh --identity - --app PATH                                  # ad hoc (CI, quick builds)
#   packaging/scripts/sign.sh --identity "Apple Development: <name> (<id>)" --app PATH  # local lanes
#   packaging/scripts/sign.sh --identity - --app PATH --dry-run                        # print the order, sign nothing
#   packaging/scripts/sign.sh --help
#
# Each item is signed on its own, so nothing is sealed before what it contains:
#   1. every Mach-O file in the bundle except the main executable, deepest path first: python3.13, the dylibs
#      and .so modules under Contents/Resources/manager, and any Mach-O file inside a nested bundle. The
#      interpreter binaries (…/python/bin/*) get packaging/entitlements/python.plist, or python-adhoc.plist when the
#      identity is - (ad hoc; D100); the rest get none.
#   2. every nested bundle (*.framework, *.xpc, *.appex, *.app), deepest first.
#   3. the app bundle. Signing it signs Contents/MacOS/<executable> with packaging/entitlements/app.plist, or
#      app-adhoc.plist when the identity is - (ad hoc; D101), and seals everything else.
# Every codesign call passes --options runtime. --timestamp is added only for a real identity, because an
# ad hoc signature has no secure timestamp. The script never signs with --deep (docs/plans/packaging.md,
# Appendix B). It ends with codesign --verify --strict --deep on the app and a check that every Mach-O file
# reports the runtime flag. Any failure exits 1. The installed copies in /Applications and ~/Applications are
# never signed here. Refusals ignore letter case, and a file or folder that cannot be listed or read stops the run
# before anything is signed (PKG-6 review).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENT_DIR="$REPO/packaging/entitlements"
APP_ENT="$ENT_DIR/app.plist"
PY_ENT="$ENT_DIR/python.plist"
PY_ADHOC_ENT="$ENT_DIR/python-adhoc.plist"
APP_ADHOC_ENT="$ENT_DIR/app-adhoc.plist"
IDENTITY=""
APP_ARG=""
DRY_RUN=0
N=0

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --identity) IDENTITY="${2:?--identity needs - (ad hoc) or a certificate name}"; shift 2 ;;
    --app) APP_ARG="${2:?--app needs a path}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (sign.sh --help)" ;;
  esac
done
[[ -n "$IDENTITY" ]] || die "--identity is required: - for ad hoc, or a certificate name (there is no default)"
[[ -n "$APP_ARG" ]] || die "--app is required (the .app bundle to sign)"

# The installed copies are refused twice: as written, and after symlinks are resolved. APFS is case-insensitive by
# default, so /applications/... names the same folder as /Applications/..., and the match ignores letter case.
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
refuse_installed() {
  local p home_apps
  p="$(lower "$1")"
  home_apps="$(lower "$HOME")/applications"
  case "$p" in
    /applications/*|"$home_apps"/*) die "refusing to sign $1: the installed copies are never signed here (PKG-6)" ;;
    *) ;;
  esac
}

APP="$APP_ARG"
[[ "$APP" == /* ]] || APP="$PWD/$APP"
refuse_installed "$APP"
[[ -d "$APP" ]] || die "no app bundle at $APP"
APP="$(cd "$APP" && pwd -P)"
refuse_installed "$APP"
[[ "$APP" == *.app ]] || die "--app must end in .app (got $APP)"
[[ -f "$APP/Contents/Info.plist" ]] || die "no Contents/Info.plist in $APP"
[[ -f "$APP_ENT" && -f "$APP_ADHOC_ENT" && -f "$PY_ENT" && -f "$PY_ADHOC_ENT" ]] \
  || die "missing $APP_ENT, $APP_ADHOC_ENT, $PY_ENT or $PY_ADHOC_ENT"

MAIN="$(plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist")" \
  || die "no CFBundleExecutable in $APP/Contents/Info.plist"
MAIN_REL="Contents/MacOS/$MAIN"
[[ -f "$APP/$MAIN_REL" ]] || die "missing main executable $MAIN_REL"

# Mach-O files other than the main executable, one per line, as paths relative to the app. A folder or file that
# cannot be listed or read stops the run here, before anything is signed: a shorter list would seal unsigned code
# into the bundle with no error.
macho_items() {
  local rel kind files
  files="$(cd "$APP" && find . -type f -print)" || die "cannot list the files in $APP"
  while IFS= read -r rel; do
    rel="${rel#./}"
    [[ -n "$rel" && "$rel" != "$MAIN_REL" ]] || continue
    [[ -r "$APP/$rel" ]] || die "cannot read $rel: it would be left unsigned"
    kind="$(file -b "$APP/$rel")" || die "cannot identify $rel"
    case "$kind" in
      *"cannot open"*) die "cannot read $rel: $kind" ;;
      *Mach-O*) printf '%s\n' "$rel" ;;
      *) ;;
    esac
  done <<<"$files"
}

# Nested bundles under Contents, as paths relative to the app. A failed listing stops the run, as in macho_items.
nested_bundles() {
  local found
  found="$(cd "$APP" && find Contents -mindepth 1 -type d \( -name '*.framework' -o -name '*.xpc' -o -name '*.appex' -o -name '*.app' \) -print)" \
    || die "cannot list the nested bundles in $APP"
  printf '%s\n' "$found"
}

# Reads paths on stdin and prints them deepest first (more "/" first), ties sorted by name.
deepest_first() {
  awk -F/ '{ printf "%d\t%s\n", NF, $0 }' | sort -t $'\t' -k1,1nr -k2,2 | cut -f2-
}

entitlements_for() {
  case "$1" in
    Contents/Resources/manager/python/bin/*)
      # An ad hoc signature has no team identifier, so library validation needs the exception (D100).
      if [[ "$IDENTITY" == - ]]; then printf '%s' "$PY_ADHOC_ENT"; else printf '%s' "$PY_ENT"; fi ;;
    *) ;;
  esac
}

# sign_item KIND ABSOLUTE-PATH LABEL ENTITLEMENTS-OR-EMPTY
sign_item() {
  local kind="$1" target="$2" label="$3" ent="$4"
  local args=(--force --sign "$IDENTITY" --options runtime)
  N=$((N + 1))
  if [[ "$IDENTITY" != - ]]; then args+=(--timestamp); fi
  if [[ -n "$ent" ]]; then args+=(--entitlements "$ent"); fi
  echo "sign[$N] $kind $label entitlements=$(basename "${ent:-none}")"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "    codesign ${args[*]} $target"
  else
    codesign "${args[@]}" "$target" </dev/null
  fi
}

# verify: the strict deep check on the app, and the runtime flag on every Mach-O file.
verify() {
  local rel info app_info
  codesign --verify --strict --deep --verbose=2 "$APP" </dev/null \
    || die "codesign --verify --strict --deep failed for $APP"
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    info="$(codesign -dvvv "$APP/$rel" 2>&1 </dev/null)" || die "codesign cannot read $rel"
    grep -q 'flags=.*runtime' <<<"$info" || die "$rel has no hardened runtime flag"
  done <<<"$(printf '%s\n' "$MAIN_REL"; printf '%s\n' "$MACHO")"
  app_info="$(codesign -dvv "$APP" 2>&1 </dev/null || true)"
  grep -E '^(Identifier|TeamIdentifier)=' <<<"$app_info" || true
  echo "verify: codesign --verify --strict --deep ok; every Mach-O file reports the runtime flag"
}

# Every list is read before the first codesign call, so a listing that fails signs nothing.
MACHO="$(macho_items)" || exit 1
NESTED="$(nested_bundles)" || exit 1
MACHO_ORDERED="$(printf '%s\n' "$MACHO" | deepest_first)" || exit 1
NESTED_ORDERED="$(printf '%s\n' "$NESTED" | deepest_first)" || exit 1

# Step 1: the Mach-O files, deepest first.
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  sign_item code "$APP/$rel" "$rel" "$(entitlements_for "$rel")"
done <<<"$MACHO_ORDERED"

# Step 2: the nested bundles, deepest first.
while IFS= read -r rel; do
  [[ -n "$rel" ]] || continue
  sign_item bundle "$APP/$rel" "$rel" ""
done <<<"$NESTED_ORDERED"

# Step 3: the app, last.
# An ad hoc app cannot load the ad hoc Sparkle.framework under library validation (no team identifier; D101).
if [[ "$IDENTITY" == - ]]; then sign_item app "$APP" "." "$APP_ADHOC_ENT"; else sign_item app "$APP" "." "$APP_ENT"; fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry-run: $N items in this order, nothing signed ($APP)"
  exit 0
fi
verify
echo "signed $APP: $N items, inside out, hardened runtime, identity $IDENTITY"

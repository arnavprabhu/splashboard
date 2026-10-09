#!/usr/bin/env bash
# Builds the bundled Python runtime for the packaged Splashboard.app (docs/plans/packaging.md, PKG-2).
#
#   packaging/scripts/build-runtime.sh          # → build/package/manager/python plus splash_gui and its locked deps
#   make runtime                                 # the same, through the Makefile
#   RUNTIME_LOCK=/abs/other.lock packaging/scripts/build-runtime.sh   # test hook (PKG-2 lane 3)
#
# Steps: read packaging/runtime.lock; download the pinned python-build-standalone archive from its
# official GitHub release into an empty folder under build/downloads; refuse when its SHA-256 differs
# from the lock; extract it into a staging folder; build the splash_gui wheel with uv; export the locked
# runtime dependencies (hash-pinned); install both into the runtime's own site-packages with the bundled
# interpreter; delete pip and the test suites; precompile with unchecked-hash .pyc files; smoke-import
# splash_gui; then move the result to build/package/manager. Any failure removes the staging folder and
# the runtime, so none is left behind.
#
# Nothing downloaded is ever executed: the archive is only checked and extracted. The runtime is run
# with -I -B (no site customisation, no bytecode writes), so the signed bundle is never written at run
# time (docs/plans/packaging.md, Appendix C).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCK="${RUNTIME_LOCK:-$REPO/packaging/runtime.lock}"
PKG_DIR="$REPO/build/package"
OUT="$PKG_DIR/manager"                 # the runtime: $OUT/python/bin/python3
STAGE="$PKG_DIR/manager.staging"
WORK="$PKG_DIR/work"                   # wheel, requirements, logs; outside the runtime
MANIFEST="$PKG_DIR/runtime-manifest.txt"
TRUSTED_PREFIX="https://github.com/astral-sh/python-build-standalone/releases/download/"
SUCCESS=0

die() { echo "error: $*" >&2; exit 1; }
note() { echo "· $*" >&2; }

cleanup() {
  local rc=$?
  if [[ $SUCCESS -ne 1 ]]; then
    rm -rf "$STAGE" "$OUT"
    echo "build-runtime: failed (exit $rc), no runtime left behind" >&2
  fi
}
trap cleanup EXIT

# read_lock: loads PBS_* keys from the lock. Only KEY=VALUE lines of plain characters are accepted,
# so the file is parsed, never sourced.
read_lock() {
  [[ -f "$LOCK" ]] || die "missing lock file $LOCK"
  local line key value pattern='^(PBS_[A-Z0-9_]+)=([^[:space:]"'"'"'`$;&|<>()]+)$'
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" =~ $pattern ]] || die "unreadable line in $LOCK: $line"
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    printf -v "$key" '%s' "$value"
  done <"$LOCK"
  local var
  for var in PBS_RELEASE PBS_PYTHON PBS_TARGET PBS_ARCHIVE PBS_URL PBS_SHA256; do
    [[ -n "${!var:-}" ]] || die "$LOCK does not set $var"
  done
  [[ "$PBS_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "PBS_SHA256 in $LOCK is not a SHA-256 hex digest"
  [[ "$PBS_URL" == "$TRUSTED_PREFIX"* ]] || die "PBS_URL is not under $TRUSTED_PREFIX"
  [[ "$PBS_URL" == *"/$PBS_ARCHIVE" || "$PBS_URL" == *"/${PBS_ARCHIVE//+/%2B}" ]] \
    || die "PBS_URL does not end with PBS_ARCHIVE"
  [[ "$PBS_PYTHON" =~ ^3\.13\.[0-9]+$ ]] || die "PBS_PYTHON must be 3.13.x (SPEC §19 and §4.1)"
  [[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 && "$PBS_TARGET" == aarch64-apple-darwin ]] \
    || die "this runtime is built on Apple silicon for aarch64-apple-darwin only (host: $(uname -sm), lock: $PBS_TARGET)"
}

# download_archive: a fresh, empty folder for the archive. Only the pinned HTTPS URL is fetched.
download_archive() {
  DL_DIR="$REPO/build/downloads/pbs-$PBS_RELEASE-$PBS_TARGET"
  rm -rf "$DL_DIR"
  mkdir -p "$DL_DIR"
  note "download $PBS_ARCHIVE"
  curl --fail --location --silent --show-error --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --output "$DL_DIR/$PBS_ARCHIVE" "$PBS_URL"
  local actual
  actual="$(shasum -a 256 "$DL_DIR/$PBS_ARCHIVE" | awk '{print $1}')"
  if [[ "$actual" != "$PBS_SHA256" ]]; then
    die "SHA-256 of $DL_DIR/$PBS_ARCHIVE is $actual but $LOCK pins $PBS_SHA256; refusing to continue"
  fi
  note "sha256 ok ($PBS_SHA256)"
}

# extract_runtime: unpacks the verified archive into the staging folder and checks the version.
extract_runtime() {
  rm -rf "$STAGE"
  mkdir -p "$STAGE"
  tar -xzf "$DL_DIR/$PBS_ARCHIVE" -C "$STAGE"
  PY="$STAGE/python/bin/python3"
  [[ -x "$PY" ]] || die "the archive has no python/bin/python3"
  local got
  got="$("$PY" -I -B -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  [[ "$got" == "$PBS_PYTHON" ]] || die "the runtime is $got, the lock pins $PBS_PYTHON"
  PYMM="${PBS_PYTHON%.*}"
  LIBDIR="$STAGE/python/lib/python$PYMM"
  SITE="$LIBDIR/site-packages"
  # The entry scripts that pip-style installs add to bin/ carry this staging path as their shebang,
  # which breaks after the move. Remember what the archive shipped so trim_runtime can drop the rest.
  BIN_SHIPPED=()
  local entry
  while IFS= read -r entry; do BIN_SHIPPED+=("$entry"); done < <(ls -A "$STAGE/python/bin")
}

# build_wheel: the splash_gui wheel from manager/ (the working tree, recorded in the manifest).
build_wheel() {
  rm -rf "$WORK"
  mkdir -p "$WORK/wheels"
  note "uv build --wheel manager"
  uv build --quiet --wheel --out-dir "$WORK/wheels" "$REPO/manager" >"$WORK/uv-build.log" 2>&1 \
    || { tail -20 "$WORK/uv-build.log" >&2; die "uv build failed (log: $WORK/uv-build.log)"; }
  local wheels=("$WORK"/wheels/splash_gui-*.whl)
  [[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]] || die "expected one splash_gui wheel, found ${#wheels[@]}"
  WHEEL="${wheels[0]}"
}

# export_requirements: every locked runtime dependency, hash-pinned, without the dev group or the project.
export_requirements() {
  note "uv export --frozen --no-dev"
  (cd "$REPO/manager" && uv export --frozen --no-dev --no-emit-project --no-header --no-annotate \
    --output-file "$WORK/requirements.txt" >"$WORK/uv-export.log" 2>&1) \
    || { tail -20 "$WORK/uv-export.log" >&2; die "uv export failed (log: $WORK/uv-export.log)"; }
  grep -q -- '--hash=sha256:' "$WORK/requirements.txt" || die "exported requirements carry no hashes"
}

# install_into_runtime: dependencies, then the wheel, into the runtime's own site-packages.
install_into_runtime() {
  note "install locked dependencies into the runtime"
  uv pip install --quiet --python "$PY" --link-mode copy --require-hashes --no-deps \
    --requirement "$WORK/requirements.txt" >"$WORK/uv-install.log" 2>&1 \
    || { tail -20 "$WORK/uv-install.log" >&2; die "installing the locked dependencies failed (log: $WORK/uv-install.log)"; }
  note "install $(basename "$WHEEL")"
  uv pip install --quiet --python "$PY" --link-mode copy --no-deps "$WHEEL" >>"$WORK/uv-install.log" 2>&1 \
    || { tail -20 "$WORK/uv-install.log" >&2; die "installing the wheel failed (log: $WORK/uv-install.log)"; }
}

# trim_runtime: no pip, no ensurepip, no test suites, and no console scripts from the installs (the shim
# comes from PKG-3). The manager never creates a venv or installs packages.
trim_runtime() {
  note "delete pip, ensurepip and test suites"
  rm -rf "$SITE"/pip "$SITE"/pip-*.dist-info "$LIBDIR/ensurepip" "$LIBDIR/test"
  rm -f "$STAGE"/python/bin/pip "$STAGE"/python/bin/pip3 "$STAGE"/python/bin/pip"$PYMM"
  find "$SITE" -depth -type d \( -name tests -o -name test \) -exec rm -rf {} + 2>/dev/null || true
  local entry shipped known
  while IFS= read -r entry; do
    known=0
    for shipped in "${BIN_SHIPPED[@]}"; do
      [[ "$shipped" == "$entry" ]] && known=1 && break
    done
    [[ $known -eq 1 ]] || rm -f "$STAGE/python/bin/$entry"
  done < <(ls -A "$STAGE/python/bin")
}

# precompile: every .py under the runtime gets an unchecked-hash .pyc, so the first run writes nothing.
precompile() {
  note "compileall (unchecked-hash .pyc)"
  "$PY" -I -B -m compileall -q -j 0 --invalidation-mode unchecked-hash "$LIBDIR" >"$WORK/compileall.log" 2>&1 \
    || { tail -20 "$WORK/compileall.log" >&2; die "compileall failed (log: $WORK/compileall.log)"; }
}

# smoke: the bundled interpreter imports the manager package in isolated mode and reports 3.13.
smoke() {
  local out
  out="$("$PY" -I -B -c 'import splash_gui, sys; print(sys.version)')"
  case "$out" in
    3.13.*) note "smoke: $(echo "$out" | head -1)" ;;
    *) die "smoke import printed '$out', expected 3.13.x" ;;
  esac
}

write_manifest() {
  local git_rev dirty size
  git_rev="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  dirty="$(git -C "$REPO" status --porcelain -- manager 2>/dev/null | wc -l | tr -d ' ')"
  size="$(du -sk "$OUT" | awk '{print $1}')"
  {
    echo "built        $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "python       $PBS_PYTHON ($PBS_TARGET)"
    echo "pbs release  $PBS_RELEASE"
    echo "archive      $PBS_ARCHIVE"
    echo "archive sha  $PBS_SHA256"
    echo "wheel        $(basename "$WHEEL") sha256 $(shasum -a 256 "$WHEEL" | awk '{print $1}')"
    echo "requirements $(grep -c -- '--hash=sha256:' "$WORK/requirements.txt" || true) pinned packages"
    echo "repo HEAD    $git_rev, manager/ entries with uncommitted changes: $dirty"
    echo "runtime KiB  $size"
  } >"$MANIFEST"
}

main() {
  read_lock
  rm -rf "$OUT" "$STAGE"
  mkdir -p "$PKG_DIR"
  download_archive
  extract_runtime
  build_wheel
  export_requirements
  install_into_runtime
  trim_runtime
  precompile
  smoke
  rm -rf "$OUT"
  mv "$STAGE" "$OUT"
  write_manifest
  SUCCESS=1
  echo "runtime   $OUT/python/bin/python3 ($PBS_PYTHON)"
  echo "size      $(du -sh "$OUT" | awk '{print $1}')"
  echo "manifest  $MANIFEST"
}

main "$@"

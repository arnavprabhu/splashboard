# shellcheck shell=bash
# Sourced by packaging/scripts/build-app.sh and macos/scripts/bundle.sh (PKG-9): embeds Sparkle 2 in an app.
#
#   embed_sparkle APP BIN_DIR EXECUTABLE   # BIN_DIR is `swift build -c release --show-bin-path`
#
# Copies BIN_DIR/Sparkle.framework into APP/Contents/Frameworks with ditto (its Versions/Current symlinks must
# survive), removes the framework's XPC services, and adds the @executable_path/../Frameworks rpath to the main
# executable (SwiftPM links it with @loader_path only). The XPC services exist for sandboxed apps; Splash GUI is not
# sandboxed, and Sparkle's documentation allows removing them ([docs] https://sparkle-project.org/documentation/sandboxing/).
# Must run before signing: install_name_tool invalidates a signature, and sign.sh then signs the framework's
# helpers (Autoupdate, Updater.app) inside out.

embed_sparkle() {
  local app="$1" bin_dir="$2" exe="$1/Contents/MacOS/$3" framework="$2/Sparkle.framework"
  [[ -d "$framework" ]] || { echo "error: no Sparkle.framework in $bin_dir (swift build -c release first)" >&2; return 1; }
  [[ -f "$exe" ]] || { echo "error: no main executable in $app" >&2; return 1; }
  mkdir -p "$app/Contents/Frameworks"
  rm -rf "$app/Contents/Frameworks/Sparkle.framework"
  ditto "$framework" "$app/Contents/Frameworks/Sparkle.framework"
  rm -rf "$app/Contents/Frameworks/Sparkle.framework/Versions/B/XPCServices" \
    "$app/Contents/Frameworks/Sparkle.framework/XPCServices"
  if ! otool -l "$exe" | grep -q "path @executable_path/../Frameworks "; then
    install_name_tool -add_rpath "@executable_path/../Frameworks" "$exe"
  fi
}

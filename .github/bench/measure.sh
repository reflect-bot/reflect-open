#!/usr/bin/env bash
# TEMPORARY (release size bench): records the files users download as JSON.
set -euo pipefail

json=$(jq -n --arg target "$BENCH_TARGET" --arg config "$BENCH_CONFIG" \
  --argjson attempt "$BENCH_ATTEMPT" --argjson cache_hit "$BENCH_CACHE_HIT" \
  '{target: $target, config: $config, attempt: $attempt, cache_hit: $cache_hit}')
add() { json=$(jq --arg key "$1" --argjson value "$2" '. + {($key): $value}' <<<"$json"); }
bytes() { wc -c <"$1" | tr -d ' '; }
# The one path that matches a glob.
one() {
  local matches=($1)
  if [ "${#matches[@]}" -ne 1 ] || [ ! -e "${matches[0]}" ]; then
    echo "expected one match for $1" >&2
    exit 1
  fi
  echo "${matches[0]}"
}

case "$BENCH_TARGET" in
  macos-*)
    app=$(one "target/$BENCH_TRIPLE/release/bundle/macos/*.app")
    # The same commands as createUpdaterArchive and createDmg in the build script.
    tar -czf "$RUNNER_TEMP/app.tar.gz" -C "$(dirname "$app")" "$(basename "$app")"
    add updater_archive_bytes "$(bytes "$RUNNER_TEMP/app.tar.gz")"
    stage="$RUNNER_TEMP/dmg"
    mkdir -p "$stage"
    ditto "$app" "$stage/$(basename "$app")"
    ln -s /Applications "$stage/Applications"
    size_mb=$(( $(du -sk "$stage" | cut -f1) / 1024 * 2 + 32 ))
    hdiutil create -volname Reflect -srcfolder "$stage" -format UDRW -size "${size_mb}m" "$RUNNER_TEMP/writable.dmg"
    hdiutil convert "$RUNNER_TEMP/writable.dmg" -format UDZO -imagekey zlib-level=9 -o "$RUNNER_TEMP/app.dmg"
    add dmg_bytes "$(bytes "$RUNNER_TEMP/app.dmg")"
    add symbol_count "$(nm -a "$app/Contents/MacOS/reflect-open" | wc -l | tr -d ' ')"
    shopt -s nullglob
    dsyms=(target/*-apple-darwin/release/*.dSYM)
    if [ "${#dsyms[@]}" -gt 0 ]; then
      symbols="$RUNNER_TEMP/dsyms"
      for dsym in "${dsyms[@]}"; do
        triple=$(basename "$(dirname "$(dirname "$dsym")")")
        ditto "$(realpath "$dsym")" "$symbols/$triple/$(basename "$dsym")"
      done
      ditto -c -k "$symbols" "$RUNNER_TEMP/dsyms.zip"
      add dsym_zip_bytes "$(bytes "$RUNNER_TEMP/dsyms.zip")"
      # The dSYM must turn an address back into a file and line.
      main="$symbols/aarch64-apple-darwin/reflect-open.dSYM"
      dwarf=$(one "$main/Contents/Resources/DWARF/*")
      address=$(nm "$dwarf" | awk '/ [Tt] .*reflect_open_lib/ && !found { print $1; found = 1 }')
      line=$(atos -o "$main" -arch arm64 -l 0x100000000 "0x$address")
      echo "atos: $line"
      if grep -qE '\.rs:[0-9]+' <<<"$line"; then add line_tables true; else add line_tables false; fi
    fi
    ;;
  ios)
    app=apps/desktop/src-tauri/gen/apple/build/reflect-open_iOS.xcarchive/Products/Applications/Reflect.app
    ditto -c -k --keepParent "$app" "$RUNNER_TEMP/app.zip"
    add app_zip_bytes "$(bytes "$RUNNER_TEMP/app.zip")"
    ;;
  windows)
    add installer_bytes "$(bytes "$(one 'target/*/release/bundle/nsis/*-setup.exe')")"
    ;;
  linux-*)
    add appimage_bytes "$(bytes "$(one 'target/*/release/bundle/appimage/*.AppImage')")"
    add deb_bytes "$(bytes "$(one 'target/*/release/bundle/deb/*.deb')")"
    ;;
esac

mkdir -p "$RUNNER_TEMP/bench"
tee "$RUNNER_TEMP/bench/$BENCH_ATTEMPT-$BENCH_CONFIG-$BENCH_TARGET.json" <<<"$json"

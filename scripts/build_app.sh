#!/bin/bash
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="$repo_dir/build"
app_dir="$build_dir/작업 보드.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources" "$build_dir/AppIcon.iconset" "$build_dir/swift-cache"
export CLANG_MODULE_CACHE_PATH="$build_dir/swift-cache"
xcrun swiftc -O -module-cache-path "$build_dir/swift-cache" "$repo_dir/app/main.swift" -o "$app_dir/Contents/MacOS/WorkBoard"
xcrun swiftc -O -module-cache-path "$build_dir/swift-cache" "$repo_dir/app/icon.swift" -o "$build_dir/draw-icon"
"$build_dir/draw-icon" "$build_dir/AppIcon.iconset"
iconutil -c icns "$build_dir/AppIcon.iconset" -o "$app_dir/Contents/Resources/AppIcon.icns"
cp "$repo_dir/app/Info.plist" "$app_dir/Contents/Info.plist"
plutil -lint "$app_dir/Contents/Info.plist"
codesign --force --sign - "$app_dir"
codesign --verify --deep --strict "$app_dir"
printf '앱 생성: %s\n' "$app_dir"

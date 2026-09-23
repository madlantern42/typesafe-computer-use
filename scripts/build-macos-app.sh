#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "The menu app can only be built on macOS." >&2
    exit 1
fi

identity="Developer ID Application: Arbutus Investments LLC (GRZLKR2P3F)"
config_path="${TYPESAFE_CONFIG_PATH:-$root/.env}"
if [[ ! -f "$config_path" ]]; then
    echo "Missing configuration: $config_path" >&2
    exit 1
fi

uv sync --frozen
uv run pyinstaller \
    --noconfirm --clean --log-level=WARN --onedir --windowed \
    --name "TypeSafe Computer Use" \
    --target-architecture arm64 \
    --osx-bundle-identifier me.madlantern.typesafe-computer-use \
    --distpath dist --workpath build --specpath build \
    macos/app.py

app="dist/TypeSafe Computer Use.app"
version="$(uv run python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
plutil -replace CFBundleShortVersionString -string "$version" "$app/Contents/Info.plist"
plutil -insert CFBundleVersion -string "$version" "$app/Contents/Info.plist"
plutil -insert NSScreenCaptureUsageDescription -string \
    "Reads the visible screen to choose actions toward your goal." "$app/Contents/Info.plist"
plutil -insert NSAppleEventsUsageDescription -string \
    "Opens and activates the apps and webpages needed for your goal." "$app/Contents/Info.plist"
plutil -insert NSMicrophoneUsageDescription -string \
    "Dictates a goal by streaming microphone audio to Soniox while you use Dictate." "$app/Contents/Info.plist"
plutil -insert TypeSafeConfigPath -string "$config_path" "$app/Contents/Info.plist"

# PyInstaller signs its collected code ad hoc. Sign every nested binary and the
# outer bundle with one Developer ID identity after Info.plist is complete.
# A timestamp is unnecessary for this locally built app and Apple's timestamp
# service is intermittently unavailable; a distributed build can add it later.
codesign --deep --force --options runtime --timestamp=none \
    --entitlements macos/app.entitlements --sign "$identity" "$app"
codesign --verify --deep --strict --verbose=2 "$app"
echo "Built and signed: $root/$app"

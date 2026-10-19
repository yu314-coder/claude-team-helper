#!/bin/zsh
set -e
cd "${0:A:h}"
APP="build/Claude Team Helper.app"
rm -rf build && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/icon.iconset
swift icon.swift build/icon1024.png
for s in 16 32 128 256 512; do
  sips -z $s $s build/icon1024.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) build/icon1024.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/icon.iconset -o "$APP/Contents/Resources/AppIcon.icns"
swiftc -Osize -swift-version 5 -parse-as-library -target arm64-apple-macos14.0 main.swift ComputerControl.swift SyncEngine.swift -o "$APP/Contents/MacOS/ClaudeTeamHelper"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Claude Team Helper</string>
  <key>CFBundleDisplayName</key><string>Claude Team Helper</string>
  <key>CFBundleIdentifier</key><string>io.github.yu314-coder.claude-team-helper</string>
  <key>CFBundleExecutable</key><string>ClaudeTeamHelper</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST
cp mcp/computer_control.py mcp/requirements-mac.txt "$APP/Contents/Resources/"
strip -x "$APP/Contents/MacOS/ClaudeTeamHelper"
codesign --force --deep -s - "$APP"
echo "Built $APP"

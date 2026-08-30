#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DELIVERY="$ROOT/delivery"
RAW_APP="$DELIVERY/app"
BUNDLE="$DELIVERY/Discount Parser.app"
CONTENTS="$BUNDLE/Contents"
RESOURCES="$CONTENTS/Resources"
MACOS="$CONTENTS/MacOS"
STAGE="$DELIVERY/dmg-stage"
DMG="$DELIVERY/DiscountParser.dmg"

if [ ! -x "$RAW_APP/DiscountParser" ]; then
  echo "[ERROR] Frozen app is missing: $RAW_APP/DiscountParser" >&2
  exit 1
fi

rm -rf "$BUNDLE" "$STAGE" "$DMG"
mkdir -p "$MACOS" "$RESOURCES/app"
cp -R "$RAW_APP/." "$RESOURCES/app/"

cat > "$MACOS/DiscountParserLauncher" <<'EOF'
#!/bin/bash
set -euo pipefail
APP_RESOURCES="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
SUPPORT_DIR="$HOME/Library/Application Support/DiscountParser"
LEGACY_DIR="$HOME/Applications/DiscountParser"
mkdir -p "$SUPPORT_DIR"

# One-time migration from the pre-DMG installation layout.
if [ ! -f "$SUPPORT_DIR/.env" ] && [ -f "$LEGACY_DIR/.env" ]; then
  cp "$LEGACY_DIR/.env" "$SUPPORT_DIR/.env"
fi
if [ ! -f "$SUPPORT_DIR/discount_parser.db" ] && [ -f "$LEGACY_DIR/discount_parser.db" ]; then
  cp "$LEGACY_DIR/discount_parser.db" "$SUPPORT_DIR/discount_parser.db"
fi
if [ ! -f "$SUPPORT_DIR/.env.example" ] && [ -f "$APP_RESOURCES/.env.example" ]; then
  cp "$APP_RESOURCES/.env.example" "$SUPPORT_DIR/.env.example"
fi

export DP_RUNTIME_ROOT="$SUPPORT_DIR"
export DP_DATABASE_URL="sqlite:///$SUPPORT_DIR/discount_parser.db"
cd "$APP_RESOURCES"
./DiscountParser migrate >/dev/null 2>&1
exec ./DiscountParser web
EOF
chmod +x "$MACOS/DiscountParserLauncher"

cat > "$CONTENTS/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Discount Parser</string>
  <key>CFBundleDisplayName</key><string>Discount Parser</string>
  <key>CFBundleIdentifier</key><string>com.discountparser.desktop</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>0.1.16</string>
  <key>CFBundleExecutable</key><string>DiscountParserLauncher</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF

# Ad-hoc signing verifies bundle integrity in CI. A production release can
# replace this with Developer ID signing + Apple notarization without changing
# the bundle layout.
codesign --force --deep --sign - "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE"

mkdir -p "$STAGE"
cp -R "$BUNDLE" "$STAGE/Discount Parser.app"
ln -s /Applications "$STAGE/Applications"

hdiutil create -volname "Discount Parser" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
hdiutil verify "$DMG" >/dev/null
rm -rf "$STAGE"

echo "DMG BUILD: PASSED"
echo "$DMG"

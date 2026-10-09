#!/usr/bin/env bash
# ==============================================================================
# CarDex iOS Build & Verification Pipeline
# Synchronizes Capacitor iOS assets, updates App Store icons, validates
# Info.plist permissions, and verifies Xcode workspace integrity.
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "================================================================================"
echo "🏎️  CarDex iOS Build & Verification Pipeline"
echo "================================================================================"

# 1. Verify Node/NPM dependencies
echo "==> 1. Verifying Node/NPM and Capacitor dependencies..."
if ! command -v node >/dev/null 2>&1; then
  echo "Error: Node.js is not installed."
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Error: npm is not installed."
  exit 1
fi
if ! command -v npx >/dev/null 2>&1; then
  echo "Error: npx is not installed."
  exit 1
fi
echo "    Node $(node -v), npm $(npm -v)"

# 2. Synchronize web assets to iOS workspace
echo "==> 2. Synchronizing web assets with Capacitor iOS..."
npx cap copy ios
npx cap sync ios

# 3. Generate App Store assets and dark LaunchScreen
echo "==> 3. Generating iOS App Store icons and launch screen assets..."
python3 scripts/generate_ios_assets.py

# 4. Verify mandatory Apple Info.plist usage descriptions
echo "==> 4. Verifying Info.plist privacy usage strings..."
INFO_PLIST="ios/App/App/Info.plist"
if [ ! -f "$INFO_PLIST" ]; then
  echo "Error: Info.plist not found at $INFO_PLIST"
  exit 1
fi

REQUIRED_KEYS=(
  "NSCameraUsageDescription"
  "NSMicrophoneUsageDescription"
  "NSLocationWhenInUseUsageDescription"
)

for key in "${REQUIRED_KEYS[@]}"; do
  if ! grep -q "<key>$key</key>" "$INFO_PLIST"; then
    echo "Error: Required permission key $key is missing from $INFO_PLIST"
    exit 1
  fi
  echo "    ✔ $key verified"
done

# 5. Verify Xcode workspace
echo "==> 5. Verifying Xcode workspace integrity..."
WORKSPACE="ios/App/App.xcworkspace"
if [ ! -d "$WORKSPACE" ]; then
  echo "Error: Xcode workspace not found at $WORKSPACE"
  exit 1
fi

if command -v xcodebuild >/dev/null 2>&1; then
  xcodebuild -workspace ios/App/App.xcworkspace -scheme App -showBuildSettings > /dev/null && echo "iOS Project Verified Successfully"
else
  echo "    Notice: xcodebuild is macOS-only. Verifying workspace files for CI/Linux..."
  test -f "ios/App/App.xcworkspace/contents.xcworkspacedata"
  test -f "ios/App/App.xcodeproj/project.pbxproj"
  echo "iOS Project Verified Successfully"
fi

echo "================================================================================"
echo "🎉 iOS Project Build & Verification Completed Successfully!"
echo "================================================================================"

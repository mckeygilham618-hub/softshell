#!/bin/bash
# 编译原生窗口 → 项目根目录下的 SoftshellWindow.app。需要 Xcode 命令行工具（swiftc）。
set -e
cd "$(dirname "$0")"
OUT="../SoftshellWindow.app"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
swiftc -O -framework Cocoa -framework WebKit -framework Speech -framework AVFoundation \
  -o "$OUT/Contents/MacOS/SoftshellWindow" SoftshellWindow.swift
cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>SoftshellWindow</string>
  <key>CFBundleIdentifier</key><string>local.softshell.window</string>
  <key>CFBundleName</key><string>Softshell</string>
  <key>CFBundleDisplayName</key><string>Softshell 软壳</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>语音对讲需要用麦克风听你说话。</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>语音对讲用系统自带的本机听写把你的话转成文字，全程在本机完成、不上传。</string>
  <key>NSCameraUsageDescription</key><string>网页请求摄像头时使用。</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
# 图标：复用项目里的 softshell.png
if [ -f ../softshell.png ] && command -v iconutil >/dev/null; then
  T=$(mktemp -d)/AppIcon.iconset; mkdir -p "$T"
  for s in 16 32 128 256 512; do
    sips -z $s $s ../softshell.png --out "$T/icon_${s}x${s}.png" >/dev/null 2>&1
    sips -z $((s*2)) $((s*2)) ../softshell.png --out "$T/icon_${s}x${s}@2x.png" >/dev/null 2>&1
  done
  iconutil -c icns "$T" -o "$OUT/Contents/Resources/AppIcon.icns" 2>/dev/null || true
fi
# 临时签名：让系统把麦克风授权稳定记在这个 App 名下
codesign --force --sign - --identifier local.softshell.window "$OUT" >/dev/null
echo "已生成 $OUT"

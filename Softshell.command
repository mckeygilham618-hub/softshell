#!/bin/bash
# Softshell 软壳 · macOS 启动器。双击即可（首次若被系统拦下：右键 → 打开）。
# 服务在后台运行，聊天窗口会自动弹出；再次双击只会重新打开窗口。
cd "$(dirname "$0")" || exit 1
# 双击启动时 PATH 很短，把 claude 常见的安装位置补进来
export PATH="$HOME/.local/bin:$HOME/.claude/local:$HOME/.claude/local/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.npm-global/bin:$HOME/.volta/bin:$HOME/.bun/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "没找到 python3" message "请先安装 Python 3.8+（brew install python 或 python.org），再双击本文件。"'
  exit 1
fi
nohup python3 bridge.py >/dev/null 2>&1 &
disown
osascript -e 'tell application "Terminal" to close (every window whose name contains "Softshell.command")' >/dev/null 2>&1 &
exit 0

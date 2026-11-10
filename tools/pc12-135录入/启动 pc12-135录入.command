#!/bin/zsh
# 双击打开 pc12-135录入 图形界面
cd "$(dirname "$0")"
export PATH="$HOME/Downloads/platform-tools:/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"
PY="$(command -v python3.12 || command -v python3)"
if [ -z "$PY" ]; then
  osascript -e 'display alert "pc12-135录入" message "没有找到 Python，请先安装 Python 3.12（python.org）"'
  exit 1
fi
"$PY" gui.py

#!/bin/bash
# 双击运行：下载新视频 + 批量处理
cd "$(dirname "$0")" || exit 1

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "❌ 没有安装 ffmpeg，请先在终端运行：brew install ffmpeg"
  read -r -p "按回车关闭窗口"; exit 1
fi
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "已生成 config.yaml，请先用文本编辑打开它，填写 api_id、api_hash 和频道，然后再双击运行。"
  open -e config.yaml
  read -r -p "按回车关闭窗口"; exit 0
fi
if [ ! -x .venv/bin/python ]; then
  echo "第一次运行，正在安装依赖…"
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || { read -r -p "安装失败，按回车关闭"; exit 1; }
fi

.venv/bin/python run.py "$@"
echo
open output 2>/dev/null
read -r -p "完成，按回车关闭窗口"

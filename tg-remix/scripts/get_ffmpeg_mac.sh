#!/bin/bash
# 在 GitHub 的 M 芯片 Mac 上获取 arm64 版 ffmpeg/ffprobe，并检查需要的功能是否齐全
# 用法：bash scripts/get_ffmpeg_mac.sh 输出目录
set -uo pipefail
OUT="$1"
mkdir -p "$OUT"
cd "$OUT" || exit 1

NEED_FILTERS="drawtext gblur boxblur colorbalance curves eq hue unsharp vignette overlay atempo amix anullsrc concat pad crop scale"

check() {
  [ -x ./ffmpeg ] && [ -x ./ffprobe ] || { echo "缺少可执行文件"; return 1; }
  file ./ffmpeg | grep -q arm64 || { echo "ffmpeg 不是 arm64 版"; return 1; }
  ./ffmpeg -hide_banner -version | head -1 || return 1
  local filters; filters=$(./ffmpeg -hide_banner -filters 2>/dev/null)
  for f in $NEED_FILTERS; do
    echo "$filters" | grep -qE "^ \S+ $f " || { echo "缺少滤镜 $f"; return 1; }
  done
  ./ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264 || { echo "缺少 libx264"; return 1; }
  ./ffmpeg -hide_banner -encoders 2>/dev/null | grep -q aac || { echo "缺少 aac"; return 1; }
  echo "videotoolbox: $(./ffmpeg -hide_banner -encoders 2>/dev/null | grep -c h264_videotoolbox)"
  return 0
}

clean() { rm -rf ./ffmpeg ./ffprobe ./libs ./*.zip; }

from_riedl() {
  echo "== 方案 1：martin-riedl.de 静态版 =="
  curl -fsSL --retry 3 -o ffmpeg.zip https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip &&
  curl -fsSL --retry 3 -o ffprobe.zip https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffprobe.zip &&
  unzip -o -q ffmpeg.zip && unzip -o -q ffprobe.zip && chmod +x ffmpeg ffprobe && rm -f ./*.zip
}

from_brew() {
  echo "== 方案 2：Homebrew 版 + 打包依赖库 =="
  brew install ffmpeg dylibbundler >/dev/null &&
  cp "$(brew --prefix)/bin/ffmpeg" "$(brew --prefix)/bin/ffprobe" . && chmod u+w ffmpeg ffprobe &&
  dylibbundler -cd -of -b -x ./ffmpeg -x ./ffprobe -d ./libs -p @executable_path/libs/ >/dev/null
}

for source in from_riedl from_brew; do
  clean
  if $source && check; then
    echo "✅ ffmpeg 准备好了（$source）"
    ls -la
    exit 0
  fi
  echo "⚠️ $source 不可用，换下一个方案"
done
echo "❌ 拿不到可用的 ffmpeg"
exit 1

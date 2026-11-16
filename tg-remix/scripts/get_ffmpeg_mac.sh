#!/bin/bash
# 在 GitHub 的 M 芯片 Mac 上获取 arm64 版 ffmpeg/ffprobe，并检查需要的功能是否齐全
# 用法：bash scripts/get_ffmpeg_mac.sh 输出目录
set -uo pipefail
OUT="$1"
mkdir -p "$OUT"
cd "$OUT" || exit 1

NEED_FILTERS="drawtext gblur boxblur colorbalance curves eq hue unsharp vignette overlay atempo amix anullsrc concat pad crop scale xfade acrossfade select showinfo sidechaincompress asplit adelay apad fade colorchannelmixer"

check() {
  [ -x ./ffmpeg ] && [ -x ./ffprobe ] || { echo "缺少可执行文件"; return 1; }
  file ./ffmpeg | grep -q arm64 || { echo "ffmpeg 不是 arm64 版"; return 1; }
  ./ffmpeg -hide_banner -version | head -1 || return 1
  local filters; filters=$(./ffmpeg -hide_banner -filters 2>/dev/null)
  for f in $NEED_FILTERS; do
    # 注意：不能用 echo | grep -q，grep 提前退出会让 echo 报 Broken pipe，在 pipefail 下被误判为失败
    grep -E "^ \S+ $f " <<<"$filters" >/dev/null || { echo "缺少滤镜 ${f}"; return 1; }
  done
  local encoders; encoders=$(./ffmpeg -hide_banner -encoders 2>/dev/null)
  grep libx264 <<<"$encoders" >/dev/null || { echo "缺少 libx264"; return 1; }
  grep aac <<<"$encoders" >/dev/null || { echo "缺少 aac"; return 1; }
  echo "videotoolbox: $(grep -c h264_videotoolbox <<<"$encoders")"
  return 0
}

clean() { rm -rf ./ffmpeg ./ffprobe ./libs ./*.zip; }

# 自己仓库里保存的一份（检查通过过的），优先用它：不受第三方网站变化影响，每次打包都一样
CACHE_TAG="tg-remix-ffmpeg"
CACHE_FILE="ffbin-arm64.zip"
from_cache() {
  echo "== 方案 0：仓库里保存的 ffmpeg =="
  [ -n "${GH_TOKEN:-}" ] || { echo "没有 GH_TOKEN"; return 1; }
  gh release download "$CACHE_TAG" -p "$CACHE_FILE" -D . --clobber >/dev/null 2>&1 &&
  unzip -o -q "$CACHE_FILE" && rm -f "$CACHE_FILE" && chmod +x ffmpeg ffprobe
}

save_cache() {
  [ -n "${GH_TOKEN:-}" ] || return 0
  rm -f "/tmp/$CACHE_FILE"
  zip -q -r -y "/tmp/$CACHE_FILE" ffmpeg ffprobe $( [ -d libs ] && echo libs ) &&
  { gh release view "$CACHE_TAG" >/dev/null 2>&1 ||
    gh release create "$CACHE_TAG" --title "TG二创 打包用的 ffmpeg（自动保存，勿删）" \
      --notes "打包流程自动保存的 arm64 ffmpeg/ffprobe，已通过功能检查。删除后下次打包会重新获取。" >/dev/null; } &&
  gh release upload "$CACHE_TAG" "/tmp/$CACHE_FILE" --clobber >/dev/null &&
  echo "💾 已把这份 ffmpeg 保存到仓库 Releases（${CACHE_TAG}）"
}

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

for source in from_cache from_riedl from_brew; do
  clean
  if $source && check; then
    echo "✅ ffmpeg 准备好了（${source}）"
    [ "$source" != "from_cache" ] && save_cache
    ls -la
    exit 0
  fi
  echo "⚠️ ${source} 不可用，换下一个方案"
done
echo "❌ 拿不到可用的 ffmpeg"
exit 1

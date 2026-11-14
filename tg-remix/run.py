#!/usr/bin/env python3
"""
TG 视频批量二创

用法：
  python run.py                 下载新视频 + 处理（最常用）
  python run.py download        只下载
  python run.py process         只处理已下载、还没处理过的视频
  python run.py process --redo  全部重新处理（改了参数后用）
  python run.py test 视频.mp4    用一个视频试效果（只处理前 15 秒，不记录）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更新版本")

from tgremix.config import load_config, resolve
from tgremix.state import State


def main() -> None:
    ap = argparse.ArgumentParser(description="TG 视频批量二创")
    ap.add_argument("command", nargs="?", default="all", choices=["all", "download", "process", "test"],
                    help="all=下载+处理（默认）；download=只下载；process=只处理；test=试效果")
    ap.add_argument("file", nargs="?", help="test 模式下要试的视频文件")
    ap.add_argument("-c", "--config", default="config.yaml", help="配置文件（默认 config.yaml）")
    ap.add_argument("--redo", action="store_true", help="已经处理过的也重新处理")
    ap.add_argument("--limit", type=int, help="本次最多处理多少个")
    ap.add_argument("--seconds", type=float, help="只处理每个视频的前 N 秒（试效果用）")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = Path(__file__).resolve().parent / cfg_path
    cfg = load_config(cfg_path)
    state = State(cfg["_root"] / "state.json")

    try:
        if args.command in ("all", "download"):
            from tgremix.downloader import download_all
            download_all(cfg, state)
        if args.command in ("all", "process"):
            from tgremix.processor import process_all
            process_all(cfg, state, redo=args.redo, limit=args.limit, seconds=args.seconds)
        if args.command == "test":
            if not args.file:
                sys.exit("用法：python run.py test 视频文件.mp4")
            from tgremix.processor import process_all
            f = Path(args.file).expanduser().resolve()
            cfg["paths"]["output"] = str(resolve(cfg, cfg["paths"]["output"]) / "试效果")
            process_all(cfg, state, only=f, seconds=args.seconds or 15)
    except KeyboardInterrupt:
        state.save()
        print("\n已中断，进度已保存，下次运行会接着来")


if __name__ == "__main__":
    main()

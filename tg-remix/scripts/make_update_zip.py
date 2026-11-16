#!/usr/bin/env python3
"""
生成小更新包 TGRemix-update.zip（只含功能代码和界面，几百 KB）

用法：
  python3 scripts/make_update_zip.py 输出.zip [--notes "更新说明"]
测试用：
  --version 9.9.9   覆盖版本号
  --break           故意放一个坏掉的 api.py（测试自动回退）
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--notes", default="")
    ap.add_argument("--version")
    ap.add_argument("--break", dest="broken", action="store_true")
    ap.add_argument("--vendor", help="把这个目录里的第三方包（如 cryptg）一起放进更新包")
    a = ap.parse_args()

    init = (ROOT / "tgremix" / "__init__.py").read_text(encoding="utf-8")
    version = a.version or re.search(r'__version__ = "(.+?)"', init).group(1)
    base = int(re.search(r"^BASE_BUILD = (\d+)", (ROOT / "app" / "main.py").read_text(encoding="utf-8"), re.M).group(1))

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version.json", json.dumps({"version": version, "min_base": base, "notes": a.notes},
                                              ensure_ascii=False, indent=1))
        for f in sorted((ROOT / "tgremix").glob("*.py")):
            text = f.read_text(encoding="utf-8")
            if f.name == "__init__.py":
                text = re.sub(r'__version__ = ".+?"', f'__version__ = "{version}"', text)
            z.writestr(f"code/tgremix/{f.name}", text)
        api = (ROOT / "app" / "api.py").read_text(encoding="utf-8")
        if a.broken:
            api += "\nthis is a broken line (\n"
        z.writestr("code/api.py", api)
        for f in sorted((ROOT / "app" / "ui").rglob("*")):
            if f.is_file():
                z.write(f, f"code/ui/{f.relative_to(ROOT / 'app' / 'ui')}")
        if a.vendor:
            vd = Path(a.vendor)
            for f in sorted(vd.rglob("*")):
                rel = f.relative_to(vd)
                if f.is_file() and ".dist-info" not in str(rel) and "__pycache__" not in str(rel):
                    z.write(f, f"code/{rel}")
    print(f"{out}  版本 {version}  需要大包 ≥ {base}  {out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
把「飞行任务书 + 飞行报告」识别出的数据，按固定规则生成云执照录入用的 Excel（规则见 任务书转换规则.md）。

用法：
    python report_to_excel.py legs.json -o flights_2026-06-19.xlsx

legs.json 示例：
{
  "date": "2026-06-19",              # 任务书日期
  "registration": "B-12R6",          # 任务书机号
  "aircraft_type": "PC-12",          # 任务书机型
  "position": "机长",                 # 本人职位（不知道时用 机长，并向用户确认）
  "legs": [
    {"flight_no": "WUA2760", "origin": "ZBAL", "dest": "ZBEN",
     "startup": "08:40", "taxi_out": "08:46", "takeoff": "08:51", "landing": "10:40",
     "block_in": "10:44", "engine_off": "10:44",
     "approach": "VOR",
     "night_time": "",                # 报告夜航时间，没有留空
     "day_landings": 1, "night_landings": "",
     "reported": {"ground": "0:15", "flight": "1:49", "experience": "1:58", "total": "2:04"},
     "remark": ""}
  ]
}
"reported" 为报告上的手写时长，可省略；给出时会核对，不一致的会打印出来（不写进 Excel）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

COLUMNS = ["日期", "航班号", "航空器型号", "航空器登记号", "起飞站", "降落站", "出发时刻", "到达时刻", "职位", "进近方式",
           "夜间飞行经历时间", "真实仪表时间", "转场时间", "昼间着陆次数", "夜间着陆次数", "备注"]
WIDTHS = [13, 11, 12, 13, 9, 9, 10, 10, 10, 13, 16, 13, 10, 13, 13, 20]


def _t(s: str) -> dt.time:
    h, m = str(s).strip().replace("：", ":").split(":")[:2]
    return dt.time(int(h), int(m))


def _m(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _span(a: str, b: str) -> int:
    """a→b 的分钟数（跨午夜时自动加 24 小时）"""
    d = _m(_t(b)) - _m(_t(a))
    return d + 1440 if d < 0 else d


def _dur(minutes: int) -> dt.time:
    return dt.time(minutes // 60, minutes % 60)


def _hm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def build_rows(data: dict) -> tuple[list[list], list[str]]:
    date = dt.date.fromisoformat(data["date"])
    rows, problems = [], []
    for i, g in enumerate(data["legs"], 1):
        air = _span(g["takeoff"], g["landing"])          # 空中时间 = 起飞→降落
        exp = _span(g["taxi_out"], g["block_in"])        # 经历时间 = 滑出→滑行终止
        cross = exp if g["origin"].upper() != g["dest"].upper() else None
        night = _t(g["night_time"]) if g.get("night_time") else None
        rows.append([
            date, g.get("flight_no", ""), data.get("aircraft_type", "PC-12"), data.get("registration", ""),
            g["origin"].upper(), g["dest"].upper(),
            _t(g["taxi_out"]), _t(g["block_in"]),           # 出发时刻 = 滑出；到达时刻 = 滑行终止
            g.get("position") or data.get("position") or "机长",
            g.get("approach", ""),
            night,
            _dur(air),                                       # 真实仪表时间 = 空中时间
            _dur(cross) if cross is not None else None,      # 转场时间 = 起降站不同时的经历时间
            g.get("day_landings", 1) if g.get("day_landings", 1) != "" else None,
            g.get("night_landings") or None,
            g.get("remark") or None,
        ])
        # 与报告手写值核对
        rep = g.get("reported") or {}
        calc = {"flight": air, "experience": exp}
        if "startup" in g and "engine_off" in g:
            total = _span(g["startup"], g["engine_off"])
            calc.update(total=total, ground=total - air)
        for k, v in rep.items():
            if k in calc and v and _m(_t(v)) != calc[k]:
                problems.append(f"第 {i} 段 {g.get('flight_no', '')}：{k} 报告 {v}，按时刻计算 {_hm(calc[k])}")
    return rows, problems


def write_excel(rows: list[list], out: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "飞行记录"
    thin = Side(style="thin", color="BFBFBF")
    for i, (name, w) in enumerate(zip(COLUMNS, WIDTHS), 1):
        c = ws.cell(row=1, column=i, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="C00000" if name in ("日期", "航空器登记号") else "1F4E79")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = w
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "B2"
    for r, row in enumerate(rows, 2):
        for i, (name, v) in enumerate(zip(COLUMNS, row), 1):
            c = ws.cell(row=r, column=i, value=v)
            c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            c.alignment = Alignment(horizontal="center")
            if name == "日期":
                c.number_format = "yyyy-mm-dd"
            elif name in ("出发时刻", "到达时刻"):
                c.number_format = "hh:mm"
            elif "时间" in name:
                c.number_format = "[h]:mm"
    wb.save(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="任务书/飞行报告 → 云执照录入 Excel")
    ap.add_argument("json", help="识别结果 JSON")
    ap.add_argument("-o", "--output", help="输出 xlsx，默认 flights_<日期>.xlsx")
    args = ap.parse_args(argv)
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    rows, problems = build_rows(data)
    out = Path(args.output or f"flights_{data['date']}.xlsx")
    write_excel(rows, out)
    print(f"已生成 {out}（{len(rows)} 段）")
    if problems:
        print("⚠ 与报告手写值不一致：")
        for p in problems:
            print("  " + p)
    else:
        print("✓ 时长与报告手写值一致" if any(g.get("reported") for g in data["legs"]) else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())

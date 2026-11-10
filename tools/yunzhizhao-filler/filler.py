#!/usr/bin/env python3
"""
云执照 App 飞行记录自动录入工具（安卓）

原理：电脑通过 USB(ADB) 连接安卓手机，用 uiautomator2 读取 App 界面上的控件，
按 config.yaml 里写好的步骤，把 Excel/CSV 中的每一行数据自动填到 App 里。

常用命令：
    python filler.py dump                          # 打印当前手机屏幕上的文字和输入框，用来写配置
    python filler.py run  -d flights.xlsx          # 开始录入（默认每条提交前让你确认）
    python filler.py run  -d flights.xlsx --auto   # 全自动，不逐条确认
    python filler.py run  -d flights.xlsx --dry-run  # 不连手机，只预览每条会执行什么
    python filler.py run  -d flights.xlsx --start 5  # 从第 5 条数据开始（断点续录）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 pyyaml，请先运行：pip install -r requirements.txt")

HERE = Path(__file__).resolve().parent
PROGRESS_FILE = HERE / ".progress.json"


# =============================================================================
# 读取数据
# =============================================================================
def _cell_to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, dt.timedelta):  # Excel 时长格式 [h]:mm 读出来是 timedelta
        m = round(v.total_seconds() / 60)
        return f"{m // 60:02d}:{m % 60:02d}"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def load_rows(path: str | Path, sheet: str | None = None) -> list[dict[str, str]]:
    """读取 .xlsx / .csv / .tsv / .txt，第一行为表头，返回 [{列名: 值}]"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"找不到数据文件：{path}")

    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            sys.exit("读取 Excel 需要 openpyxl，请先运行：pip install -r requirements.txt")
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        table = [[_cell_to_str(c) for c in r] for r in ws.iter_rows(values_only=True)]
    elif path.suffix.lower() == ".xls":
        sys.exit("不支持旧版 .xls，请在 Excel 里「另存为」.xlsx 或 .csv")
    else:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "gbk", "utf-16"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法识别文件编码，请另存为 UTF-8 的 CSV")
        first = text.splitlines()[0] if text else ""
        delim = "\t" if "\t" in first else ","
        table = [[c.strip() for c in r] for r in csv.reader(text.splitlines(), delimiter=delim)]

    table = [r for r in table if any(c for c in r)]
    if not table:
        return []
    headers = [h or f"列{i + 1}" for i, h in enumerate(table[0])]
    rows = []
    for r in table[1:]:
        r = list(r) + [""] * (len(headers) - len(r))
        rows.append(dict(zip(headers, r)))
    return rows


def render_value(template, row: dict[str, str]) -> str:
    """把 "{日期} {起飞时间}" 这样的模板替换成行里的值；未知列名会报错提醒。"""
    template = "" if template is None else str(template)

    def rep(m):
        key = m.group(1)
        if key not in row:
            raise KeyError(f"数据中没有列「{key}」，现有列：{', '.join(row)}")
        return row[key]

    return re.sub(r"\{([^{}]+)\}", rep, template)


def step_value(step: dict, row: dict[str, str]) -> str:
    if "column" in step:
        col = step["column"]
        if col not in row and "default" not in step:
            raise KeyError(f"数据中没有列「{col}」，现有列：{', '.join(row)}")
        value = row.get(col, "")
    else:
        value = render_value(step.get("value", ""), row)
    if value == "" and "default" in step:  # 表格里这一格为空（或没有这一列）时用默认值
        value = render_value(step["default"], row)
    mapping = step.get("map")  # 值转换，例如 {"是": "训练飞行"}
    if mapping and value in mapping:
        value = str(mapping[value])
    return value


# =============================================================================
# 控件定位（纯函数部分，便于测试）
# =============================================================================
def parse_bounds(b) -> tuple[int, int, int, int]:
    """接受 uiautomator2 的 dict 或 "[x1,y1][x2,y2]" 字符串"""
    if isinstance(b, dict):
        return b["left"], b["top"], b["right"], b["bottom"]
    nums = list(map(int, re.findall(r"-?\d+", b)))
    return nums[0], nums[1], nums[2], nums[3]


def nearest_input(label_bounds, inputs: list[dict]) -> dict | None:
    """
    给定标签的位置，从输入框列表中找最可能属于它的那个：
    优先同一行、在标签右边的；其次是标签正下方最近的。
    inputs: [{"bounds": (l,t,r,b), ...}]
    """
    ll, lt, lr, lb = label_bounds
    lcy = (lt + lb) / 2
    best, best_score = None, None
    for item in inputs:
        l, t, r, b = item["bounds"]
        cy = (t + b) / 2
        same_row = t - 10 <= lcy <= b + 10
        if same_row and r > lr - 5:
            score = abs(l - lr) * 0.5
        elif t >= lt - 5:  # 在下方
            dx = max(0, ll - r, l - lr)
            score = 1000 + (cy - lcy) + dx * 2
        else:
            continue
        if best_score is None or score < best_score:
            best, best_score = item, score
    return best


# =============================================================================
# 设备操作
# =============================================================================
class Device:
    INPUT_CLASSES = ("android.widget.EditText", "android.widget.AutoCompleteTextView")

    def __init__(self, serial: str | None, settings: dict):
        try:
            import uiautomator2 as u2
        except ImportError:
            sys.exit("缺少依赖 uiautomator2，请先运行：pip install -r requirements.txt")
        self.u2 = u2
        self.d = u2.connect(serial) if serial else u2.connect()
        self.timeout = float(settings.get("find_timeout", 5))
        self.max_scrolls = int(settings.get("max_scrolls", 6))
        self.step_delay = float(settings.get("step_delay", 0.4))
        self.fast = bool(settings.get("fast", True))  # 用 adb 点击/输入，绕开 uiautomator 的等待
        self._orig_ime = None
        try:
            cfg = self.d.jsonrpc.getConfigurator()
            self.d.jsonrpc.setConfigurator(dict(cfg, waitForIdleTimeout=100))
            self.idle_off = True
        except Exception as e:  # noqa: BLE001
            self.idle_off = False
            print(f"（提示：无法关闭「等待界面静止」：{str(e)[:80]}）")
        info = self.d.info
        print(f"已连接手机：{info.get('productName', '')}，屏幕 {info.get('displayWidth')}x{info.get('displayHeight')}")

    # ---------- 查找 ----------
    def _selector(self, loc: dict):
        kw = {}
        if "text" in loc:
            kw["text"] = loc["text"]
        if "text_contains" in loc:
            kw["textContains"] = loc["text_contains"]
        if "id" in loc:
            kw["resourceId"] = loc["id"]
        if "desc" in loc:
            kw["description"] = loc["desc"]
        if "desc_contains" in loc:
            kw["descriptionContains"] = loc["desc_contains"]
        if "hint" in loc:  # 输入框里的灰色提示文字，未填写时就是它的 text
            kw["text"] = loc["hint"]
        if "class" in loc:
            kw["className"] = loc["class"]
        if not kw:
            return None
        sel = self.d(**kw)
        if "nth" in loc:
            sel = sel[int(loc["nth"])]
        return sel

    def _inputs_on_screen(self) -> list[dict]:
        items = []
        for cls in self.INPUT_CLASSES:
            for el in self.d(className=cls):
                try:
                    items.append({"el": el, "bounds": parse_bounds(el.info["bounds"])})
                except Exception:
                    pass
        return items

    def _find_once(self, loc: dict, want_input: bool):
        if "label" in loc:
            lab = self.d(text=loc["label"])
            if not lab.exists:
                lab = self.d(textContains=loc["label"])
            if not lab.exists:
                return None
            lb = parse_bounds(lab.info["bounds"])
            if not want_input:
                # 点击类：点标签所在整行（标签右侧通常是「请选择 >」）
                return lab
            hit = nearest_input(lb, self._inputs_on_screen())
            return hit["el"] if hit else None
        if "input_index" in loc:  # 当前屏幕上第几个输入框（从 0 开始）
            ins = sorted(self._inputs_on_screen(), key=lambda x: (x["bounds"][1], x["bounds"][0]))
            i = int(loc["input_index"])
            return ins[i]["el"] if i < len(ins) else None
        sel = self._selector(loc)
        if sel is not None and sel.exists:
            return sel
        return None

    def find(self, loc: dict, want_input: bool = False, scroll: bool = True):
        deadline = time.time() + self.timeout
        while True:
            el = self._find_once(loc, want_input)
            if el is not None:
                return el
            if time.time() > deadline:
                break
            time.sleep(0.3)
        if scroll and loc.get("scroll", True):
            for direction in ("forward", "backward"):
                for _ in range(self.max_scrolls):
                    if not self._scroll(direction):
                        break
                    el = self._find_once(loc, want_input)
                    if el is not None:
                        return el
        return None

    def _scroll(self, direction: str) -> bool:
        sc = self.d(scrollable=True)
        try:
            if sc.exists:
                moved = sc.scroll.forward() if direction == "forward" else sc.scroll.backward()
                time.sleep(0.4)
                return bool(moved)
        except Exception:
            pass
        self.d.swipe_ext("up" if direction == "forward" else "down", scale=0.6)
        time.sleep(0.5)
        return True

    # ---------- 动作 ----------
    # ---------- 快速模式（adb 点击 / 输入法广播输入）----------
    FAST_KEYS = {"id", "text", "text_contains", "hint", "desc"}

    def _can_fast(self, loc: dict) -> bool:
        return self.fast and bool(set(loc) & (self.FAST_KEYS | {"label", "ocr"})) and not (set(loc) & {"input_index", "class", "nth"})

    @staticmethod
    def _norm_label(t: str) -> str:
        return re.sub(r"[\s\xa0*＊:：]", "", t or "")

    def _find_fast(self, loc: dict):
        """先按 id/text 找；找不到（例如输入中时 App 网页会隐藏相邻输入框的 id）再按标签文字找，
        返回标签所在行右侧输入区域的位置。返回 (bounds, 当前文字或 None)"""
        nodes = self._hier_nodes()
        if set(loc) & self.FAST_KEYS:
            r = self._find_node(loc, nodes)
            if r:
                return r
        if "label" in loc:
            want = self._norm_label(loc["label"])
            w, _ = self.d.window_size()
            for n in nodes:
                if self._norm_label(n.get("text")) == want:
                    l, t, r_, b = parse_bounds(n.get("bounds"))
                    if b - t < 20:
                        continue
                    # 标签节点可能很宽（延伸到中间），取它右边缘和屏幕右边的中点作为输入区域
                    x = loc["tap_x"] * w if loc.get("tap_x") else min((r_ + w) / 2, w - 60)
                    return (int(x - 30), t, int(x + 30), b), None
            # 输入状态下 App 网页有时把整行（连标签）都从界面数据里藏起来：改用 OCR 在截图里找标签文字
            if self._ocr_engine():
                _, h = self.d.window_size()
                cands = []
                for t, l, tp, r_, b in self._ocr_tokens(self.d.screenshot()):
                    if self._norm_label(t) == want and l < w * 0.5 and tp > h * 0.1:
                        cands.append((l, tp, r_, b))
                if cands:
                    l, tp, r_, b = min(cands, key=lambda c: c[0])
                    # OCR 只框住标签文字本身，输入框在行的右侧：默认点屏幕宽度 85% 处（可用 tap_x 指定）
                    x = float(loc.get("tap_x", 0.85)) * w
                    print(f"      （界面数据里没有「{loc['label']}」，按截图文字定位）")
                    return (int(x - 30), int(tp), int(x + 30), int(b)), None
        if "ocr" in loc:  # 按截图里的文字直接点（如按钮「保存」）
            b = self._ocr_find(loc["ocr"])
            if b:
                print(f"      （按截图文字「{loc['ocr']}」定位）")
                return b, None
        return None

    def _ocr_find(self, text: str):
        """在截图里找文字完全相同的位置（忽略空格），返回 bounds"""
        if not self._ocr_engine():
            return None
        want = self._norm_label(text)
        _, h = self.d.window_size()
        for t, l, tp, r_, b in self._ocr_tokens(self.d.screenshot()):
            if self._norm_label(t) == want and tp > h * 0.05:
                return int(l), int(tp), int(r_), int(b)
        return None

    def _scroll_page_by(self, dy: int):
        """页面内容上移 dy 像素（dy<0 下移），无惯性"""
        w, h = self.d.window_size()
        y0 = int(h * 0.75) if dy > 0 else int(h * 0.3)
        y1 = max(int(h * 0.12), min(int(h * 0.95), y0 - dy))
        self._drag_no_fling(int(w * 0.08), y0, y1)
        time.sleep(0.3)

    def _locate_fast(self, loc: dict):
        """找到控件并保证它完整显示在屏幕中部附近；找不到时上下滚动页面查找。返回 (bounds, 文字)"""
        w, h = self.d.window_size()
        moves = [0] + [1] * self.max_scrolls + [-1] * (self.max_scrolls * 2)
        for mv in moves:
            if mv:
                self._scroll_page_by(mv * int(h * 0.4))
            r = self._find_fast(loc)
            if not r:
                continue
            b = r[0]
            if b[1] >= h * 0.13 and b[3] <= h * 0.92 and b[3] - b[1] >= 40:
                return r
            # 被标题栏/屏幕边缘遮住一部分：滚到中间再找
            self._scroll_page_by(int((b[1] + b[3]) / 2 - h * 0.5))
            r = self._find_fast(loc)
            if r:
                return r
        return None

    def _ensure_ime(self):
        """切换到 uiautomator2 的输入法（可输入中文、不弹键盘）；结束时 restore() 切回原输入法"""
        if self._orig_ime is None:
            self._orig_ime = self._adb("settings get secure default_input_method").strip()
            self.d.set_input_ime(True)

    def restore(self):
        if self._orig_ime and "AdbKeyboard" not in self._orig_ime:
            self._adb(f"ime set {self._orig_ime}")
            print(f"已切回原输入法：{self._orig_ime}")
        self._orig_ime = None

    def _type(self, value: str):
        self._ensure_ime()
        self.d.send_keys(value)

    def click(self, loc: dict):
        if self._can_fast(loc):
            r = self._locate_fast(loc)
            if r is None:
                raise LookupError(f"找不到要点击的控件：{loc}")
            b = r[0]
            self._tap((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            time.sleep(self.step_delay)
            return
        el = self.find(loc)
        if el is None:
            raise LookupError(f"找不到要点击的控件：{loc}")
        el.click()
        time.sleep(self.step_delay)

    def fill(self, loc: dict, value: str):
        if self._can_fast(loc):
            r = self._locate_fast(loc)
            if r is None:
                raise LookupError(f"找不到输入框：{loc}")
            (l, t, rr, b), cur = r
            self._ensure_ime()
            self._tap((l + rr) / 2, (t + b) / 2)
            time.sleep(0.35)
            # 清空：光标移到末尾后逐个删除
            n = len(cur) + 3 if cur is not None else 40  # 按标签定位时不知道原有内容，多删几个
            self._adb("input keyevent KEYCODE_MOVE_END " + " ".join(["KEYCODE_DEL"] * n))
            self._type(value)
            time.sleep(self.step_delay)
            return
        el = self.find(loc, want_input=True)
        if el is None:
            raise LookupError(f"找不到输入框：{loc}")
        el.click()
        time.sleep(0.2)
        try:
            el.clear_text()
        except Exception:
            pass
        ok = False
        try:
            el.set_text(value)
            time.sleep(0.2)
            ok = value in (el.get_text() or "")
        except Exception:
            ok = False
        if not ok:  # 有些输入框不支持 set_text，改用输入法逐字输入（支持中文）
            try:
                el.clear_text()
            except Exception:
                pass
            self.d.send_keys(value, clear=True)
        time.sleep(self.step_delay)

    def select(self, loc: dict, value: str, option_loc: dict | None = None,
               deselect_current: bool = False, assume_current: str | None = None):
        """点开选择框，再点与 value 文字相同的选项（会自动滚动查找）"""
        if self._can_fast(loc):
            # 页面上已经显示为目标值就不用再选（有的选择框再点一次已选项会取消选中）
            from wheel_picker import contains
            cur_text = None
            if set(loc) & self.FAST_KEYS:
                cur = self._find_node(loc)
                if cur and cur[1]:
                    cur_text = cur[1]
            if cur_text is None and assume_current:  # 读不到当前值时按默认值处理（新页面的默认选项）
                cur_text = assume_current
            if cur_text and contains(cur_text, value):
                print(f"      当前已经是「{cur_text}」，跳过")
                return
            self.click(loc)
            time.sleep(0.6)
            for attempt in range(10):
                nodes = self._hier_nodes()
                if attempt == 0 and self._find_node({"text": "确定"}, nodes):
                    # 弹出的是自绘选择框（滚轮或点选列表，选项不在界面数据里；页面上显示的当前值不能当选项点）
                    print("      弹出的是自绘选择框，用 OCR 定位选项")
                    self.wheel({"confirm": "确定", "pattern": r"^(.*)$", "match": "contains",
                                "read": "ocr", "count": 1, "offscreen": True, "tap_target": True,
                                "deselect": cur_text if deselect_current else None}, value)
                    return
                r = self._find_node({"text": value}, nodes) or self._find_node({"text_contains": value}, nodes)
                if r:
                    b = r[0]
                    self._tap((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                    time.sleep(self.step_delay)
                    return
                # 在弹出的列表里滚动查找（先往下 5 次，再往上）
                lists = [parse_bounds(n.get("bounds")) for n in nodes if n.get("scrollable") == "true"]
                lists = [x for x in lists if (x[2] - x[0]) * (x[3] - x[1]) < 0.7 * self.d.window_size()[0] * self.d.window_size()[1]]
                if not lists:
                    break
                lb = min(lists, key=lambda x: (x[2] - x[0]) * (x[3] - x[1]))
                cx, span = (lb[0] + lb[2]) // 2, int((lb[3] - lb[1]) * 0.6)
                d = 1 if attempt < 5 else -1
                y0 = lb[3] - 20 if d > 0 else lb[1] + 20
                self._drag_no_fling(cx, y0, y0 - d * span)
                time.sleep(0.3)
            raise LookupError(f"选项里没有「{value}」")
        self.click(loc)
        time.sleep(0.5)
        opt = dict(option_loc or {})
        opt.setdefault("text", value)
        el = self.find(opt)
        if el is None:
            el = self.find({"text_contains": value})
        if el is None:
            raise LookupError(f"选项里没有「{value}」")
        el.click()
        time.sleep(self.step_delay)

    # ---------- 滚轮 ----------
    def _hier_nodes(self):
        import xml.etree.ElementTree as ET
        return list(ET.fromstring(self.d.dump_hierarchy(compressed=False)).iter("node"))

    def _wheel_columns(self, spec: dict, need: int):
        from wheel_picker import Column, resolve_area, split_area

        w, h = self.d.window_size()
        if spec.get("area"):
            area = resolve_area(spec["area"], w, h)
            return split_area(area, int(spec.get("count", need)), spec.get("weights")), "area"

        nodes = self._hier_nodes()
        # 1) 原生 NumberPicker（系统 DatePicker / TimePicker 滚轮模式）
        nps = [n for n in nodes if n.get("class", "").endswith("NumberPicker")]
        if nps:
            cols = [Column(*parse_bounds(n.get("bounds"))) for n in nps]
            return sorted(cols, key=lambda c: c.left), "numberpicker"

        # 2) 可滚动的窄列（RecyclerView / ListView / ScrollView 实现的滚轮）
        sc = []
        for n in nodes:
            if n.get("scrollable") != "true":
                continue
            l, t, r, b = parse_bounds(n.get("bounds"))
            if (r - l) < 0.75 * w and (b - t) < 0.6 * h and r > l and b > t:
                sc.append(Column(l, t, r, b))
        def inside(a, b):  # a 在 b 内部
            return a is not b and b.left <= a.left and b.right >= a.right and b.top <= a.top and b.bottom >= a.bottom
        sc = [c for c in sc if not any(inside(o, c) for o in sc)]  # 只保留最内层
        if len(sc) >= need:
            return sorted(sc, key=lambda c: c.left), "scrollable"

        # 3) 整屏 OCR：竖向排成一列、间距均匀的数字就是滚轮
        if spec.get("read", "auto") in ("auto", "ocr") and self._ocr_engine():
            from wheel_picker import columns_from_tokens
            self._last_tokens = self._ocr_tokens(self.d.screenshot())
            cols = columns_from_tokens(self._last_tokens, need, w)
            if len(cols) >= need:
                return cols, "ocr-scan"

        # 4) 按「年 月 日 / 时 分」这类表头文字划分列：每列以表头为中心，区域从表头下方开始
        units = spec.get("labels") or ["年", "月", "日", "时", "分", "秒"]
        labs = []
        for n in nodes:
            t = (n.get("text") or "").strip()
            if t in units:
                labs.append((t, parse_bounds(n.get("bounds"))))
        if len(labs) >= need:
            labs.sort(key=lambda x: x[1][0])
            ys = [b[3] for _, b in labs]
            top = max(ys) + 5
            bottom = int(h * float(spec.get("area_bottom", 0.97)))
            btn = self.d(text=spec.get("confirm") or "确定")
            if btn.exists:
                _, bt, _, bb = parse_bounds(btn.info["bounds"])
                if bt > top + 100:  # 确定按钮在滚轮下方
                    bottom = bt - 5
            cxs = [(b[0] + b[2]) / 2 for _, b in labs]
            edges = [0] + [(a + b) / 2 for a, b in zip(cxs, cxs[1:])] + [w]
            cols = [Column(int(edges[i]), top, int(edges[i + 1]), bottom) for i in range(len(labs))]
            if bottom - top > 150:
                return cols, "labels"

        # 5) 猜测：「确定」按钮下方到屏幕底部为滚轮区域，按列数等分
        confirm = spec.get("confirm") or "确定"
        cb = self._find_node_bounds({"text": confirm}, nodes)
        if cb is None and not self.fast:
            btn = self.d(text=confirm)
            cb = parse_bounds(btn.info["bounds"]) if btn.exists else None
        if cb is not None:
            bb = cb[3]
            bottom = int(h * float(spec.get("area_bottom", 0.97)))
            if bottom - bb > 100:
                return split_area((0, bb + 10, w, bottom), int(spec.get("count", need)), spec.get("weights")), "guess"
        raise LookupError("找不到滚轮。请打开滚轮后运行 dump 查看，或在 wheel 中配置 area")

    def _read_hier(self, col):
        from wheel_picker import Item

        items = []
        for n in self._hier_nodes():
            t = n.get("text") or n.get("content-desc") or ""
            if not t.strip():
                continue
            l, tp, r, b = parse_bounds(n.get("bounds"))
            cx, cy = (l + r) / 2, (tp + b) / 2
            if col.left <= cx <= col.right and col.top <= cy <= col.bottom and (b - tp) < col.height * 0.8:
                items.append(Item(t, cy))
        return items

    def _ocr_engine(self):
        if getattr(self, "_ocr", None) is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                try:
                    from rapidocr import RapidOCR
                except ImportError:
                    self._ocr = False
                    return None
            self._ocr = RapidOCR()
        return self._ocr or None

    def _ocr_tokens(self, img, dx=0, dy=0):
        """OCR 一张图，返回 [(文字, left, top, right, bottom)]（加上偏移换算为屏幕坐标）"""
        import numpy as np

        eng = self._ocr_engine()
        if not eng:
            return []
        res = eng(np.array(img.convert("RGB")))
        if isinstance(res, tuple):  # rapidocr_onnxruntime: (result, elapse)
            boxes = [(r[0], r[1]) for r in (res[0] or [])]
        else:  # rapidocr >= 2
            boxes = list(zip(res.boxes if res.boxes is not None else [], res.txts or []))
        out = []
        for box, text in boxes:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            out.append((str(text), min(xs) + dx, min(ys) + dy, max(xs) + dx, max(ys) + dy))
        return out

    def _read_ocr(self, col):
        from wheel_picker import Item

        img = self.d.screenshot()  # PIL.Image
        crop = img.crop((col.left, col.top, col.right, col.bottom))
        return [Item(t, (tp + b) / 2) for t, l, tp, r, b in self._ocr_tokens(crop, col.left, col.top)]

    def _adb(self, cmd: str) -> str:
        """直接用 adb shell 执行（不经过 uiautomator，不会等待界面静止）"""
        r = self.d.shell(cmd)
        return getattr(r, "output", r if isinstance(r, str) else "") or ""

    def _tap(self, x, y):
        self._adb(f"input tap {int(x)} {int(y)}")

    def _drag_no_fling(self, x, y0, y1):
        """拖动滚轮：移动到终点后停住一会儿再抬手，抬手速度为 0，滚轮不会惯性甩动。
        drag_mode: adb(默认，用 input motionevent，最快) | swipe(uiautomator 一次下发) | touch(逐个事件，最慢)"""
        x, y0, y1 = int(x), int(y0), int(y1)
        mode = getattr(self, "wheel_drag_mode", "adb")
        hold = getattr(self, "wheel_hold", 0.2)
        if mode == "adb" and getattr(self, "_adb_motion_ok", True):
            ym = (y0 + y1) // 2
            cmd = (f"input motionevent DOWN {x} {y0}; input motionevent MOVE {x} {ym}; "
                   f"input motionevent MOVE {x} {y1}; sleep {hold}; input motionevent MOVE {x} {y1}; "
                   f"input motionevent UP {x} {y1}")
            out = self._adb(cmd)
            if any(w in out for w in ("Usage", "usage:", "Error", "Exception", "Unknown")):
                print("        （此手机不支持 input motionevent，改用 uiautomator 拖动）")
                self._adb_motion_ok = False
                self._adb(f"input motionevent UP {x} {y1}")
                mode = "swipe"
            else:
                self._need_settle = True
                return
        if mode in ("swipe", "adb") and hasattr(self.d, "swipe_points"):
            hold_pts = max(1, int(hold / 0.1))
            pts = [(x, y0), (x, (y0 + y1) // 2), (x, y1)] + [(x, y1)] * hold_pts
            self.d.swipe_points(pts, getattr(self, "wheel_seg", 0.1))
        else:
            steps = getattr(self, "wheel_steps", 6)
            t = self.d.touch
            t.down(x, y0)
            for k in range(1, steps + 1):
                t.move(x, y0 + (y1 - y0) * k / steps)
            t.move(x, y1)
            time.sleep(hold)
            t.up(x, y1)
        self._need_settle = True

    def _find_node_bounds(self, loc: dict, nodes=None):
        """在界面 XML 里按 id / text 找控件位置（只 dump 一次，比 UiObject 查找快）"""
        r = self._find_node(loc, nodes)
        return r[0] if r else None

    def _find_node(self, loc: dict, nodes=None):
        """返回 (bounds, 当前文字)"""
        for n in (nodes if nodes is not None else self._hier_nodes()):
            rid = n.get("resource-id") or ""
            t = (n.get("text") or "").replace("\xa0", " ").strip()
            ok = False
            if "id" in loc:
                ok = rid == loc["id"] or rid.endswith(":id/" + loc["id"])
            elif "text" in loc:
                ok = t == loc["text"]
            elif "hint" in loc:
                ok = t == loc["hint"]
            elif "text_contains" in loc:
                ok = loc["text_contains"] in t
            elif "desc" in loc:
                ok = (n.get("content-desc") or "") == loc["desc"]
            if ok:
                b = parse_bounds(n.get("bounds"))
                if b[2] - b[0] >= 20 and b[3] - b[1] >= 20:  # 过小的是隐藏控件（如 <select>），点不准
                    return b, t
        return None

    def _fast_click(self, loc: dict) -> bool:
        b = self._find_node_bounds(loc)
        if not b:
            return False
        self._tap((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        return True

    def wheel(self, spec: dict, value: str):
        """拨滚轮期间临时把 uiautomator 的「等待界面静止」超时设为 0（滚轮动画会让每个操作都等很久）"""
        saved = None
        try:
            saved = self.d.jsonrpc.getConfigurator()
            self.d.jsonrpc.setConfigurator(dict(saved, waitForIdleTimeout=0))
        except Exception:
            saved = None
        try:
            return self._wheel(spec, value)
        finally:
            if saved is not None:
                try:
                    self.d.jsonrpc.setConfigurator(saved)
                except Exception:
                    pass

    def _wheel(self, spec: dict, value: str):
        from wheel_picker import WheelColumnDriver, WheelError, blind_set, split_value, to_int

        if spec.get("open") and getattr(self, "fast", False) and self._find_node({"text": "取消"}) and self._find_node({"text": "确定"}):
            print("      发现上一个选择框还开着，先点「取消」关掉")
            self._fast_click({"text": "取消"})
            time.sleep(0.6)
        if spec.get("open"):  # 先点开滚轮
            t0 = time.time()
            confirm_name = spec.get("confirm") or "确定"
            opened = False
            for attempt in range(3):
                # 上一个输入框还在输入状态时，第一下点击往往只是让它失去焦点，滚轮不会弹出，需要再点一次
                self.click(locator_of(spec["open"]))
                time.sleep(0.8)
                if not self.fast or self._find_node({"text": confirm_name}):
                    opened = True
                    break
                print(f"      滚轮没有弹出，再点一次（第 {attempt + 2} 次）")
            if not opened:
                raise LookupError(f"点了 3 次 {spec['open']} 都没有弹出滚轮（没看到「{confirm_name}」按钮）")
            print(f"      打开滚轮用时 {time.time() - t0:.1f}s")
        self.wheel_settle = float(spec.get("settle", 0.3))
        self.wheel_steps = int(spec.get("drag_steps", 6))
        self.wheel_hold = float(spec.get("hold", 0.2))
        self.wheel_seg = float(spec.get("segment", 0.1))      # swipe_points 每段耗时（秒）
        self.wheel_drag_mode = spec.get("drag_mode", "adb")  # adb(最快) | swipe | touch(最慢)
        self._last_tokens = []
        _, scr_h = self.d.window_size()
        parts = split_value(value, spec.get("pattern"))
        targets = spec.get("wheels") or list(range(len(parts)))
        if len(targets) != len(parts):
            raise ValueError(f"「{value}」拆成了 {len(parts)} 段 {parts}，但 wheels 配了 {len(targets)} 列")
        cols, how = self._wheel_columns(spec, max(targets) + 1)
        if max(targets) >= len(cols):
            raise LookupError(f"只识别到 {len(cols)} 列滚轮（方式：{how}），但需要第 {max(targets) + 1} 列，请配置 area/count")
        print(f"      识别到 {len(cols)} 列滚轮（{how}），设置为 {parts}")

        mode = str(spec.get("read", "auto"))  # auto | text | ocr | blind
        initial = None
        if spec.get("initial") is not None:
            init = str(spec["initial"])
            if init.startswith("now"):
                fmt = spec.get("initial_format") or {2: "%H:%M", 3: "%Y-%m-%d"}.get(len(parts), "%Y-%m-%d %H:%M")
                phone_now = self.d.shell(["date", "+%Y-%m-%d %H:%M:%S"]).output.strip()
                init = dt.datetime.strptime(phone_now, "%Y-%m-%d %H:%M:%S").strftime(fmt)
            initial = split_value(init, spec.get("pattern"))

        if spec.get("deselect"):
            # 点选式列表：当前已选中的项要先点一下取消，才能选别的（如进近方式默认的 ILS CAT1）
            from wheel_picker import contains
            col = cols[targets[0]]
            hits = [it for it in self._read_ocr(col) if contains(it.text, spec["deselect"])]
            if hits:
                h = min(hits, key=lambda it: abs(it.cy - col.cy))
                self._tap(col.cx, h.cy)
                time.sleep(0.5)
                print(f"      先取消当前选中的「{h.text}」")
            else:
                print(f"      没在列表里看到当前选中的「{spec['deselect']}」，直接选目标")
        for k, (part, ci) in enumerate(zip(parts, targets)):
            col = cols[ci]
            if spec.get("center_y") is not None:
                cy = float(spec["center_y"])
                col.center_y = cy * self.d.window_size()[1] if cy <= 1 else cy

            def reader(c, _m=mode, _how=how):
                if _m == "blind":
                    return []
                drawn = _how in ("ocr-scan", "labels", "guess", "area")  # 自绘滚轮：界面树里多半是被盖住的页面文字
                if _m == "ocr" or (_m == "auto" and drawn):
                    its = self._read_ocr(c)
                    if its or _m == "ocr":
                        return its
                return self._read_hier(c)

            stat = {"reads": 0, "rt": 0.0, "drags": 0, "dt": 0.0}

            def timed_reader(c, _r=reader, _s=stat):
                t0 = time.time()
                if getattr(self, "_need_settle", False):  # 只在读数前等滚轮停稳，连续拖动之间不等
                    time.sleep(self.wheel_settle)
                    self._need_settle = False
                try:
                    return _r(c)
                finally:
                    _s["reads"] += 1
                    _s["rt"] += time.time() - t0

            def timed_drag(x, y0, y1, _s=stat):
                t0 = time.time()
                self._drag_no_fling(x, y0, y1)
                _s["drags"] += 1
                _s["dt"] += time.time() - t0
                print(f"        拖动 {int(y0)}→{int(y1)}（{int(y0 - y1):+d}px）用时 {time.time() - t0:.1f}s")

            cyc = spec.get("cyclic")
            modulus = (cyc[k] if isinstance(cyc, list) else cyc) if cyc else None
            t_col = time.time()
            drv = WheelColumnDriver(col, timed_reader, timed_drag, modulus=modulus,
                                    item_height=spec.get("item_height"),
                                    visible_items=int(spec.get("visible_items", 3 if how == "numberpicker" else 5)),
                                    log=print, match=spec.get("match", "exact"),
                                    limits=None if spec.get("short_drag") else
                                    (scr_h * 0.08, scr_h * (1.8 if spec.get("offscreen") else 0.96)))
            try:
                drv.set(part)
                print(f"      第 {ci} 列完成：共 {time.time() - t_col:.1f}s ｜ 读数 {stat['reads']} 次 {stat['rt']:.1f}s ｜ "
                      f"拖动 {stat['drags']} 次 {stat['dt']:.1f}s ｜ 格高约 {drv.item_height or 0:.0f}px ｜ 列区域 y={col.top}~{col.bottom}")
            except WheelError as e:
                if initial is None or "读不到" not in str(e):
                    raise
                ih = spec.get("item_height") or col.height / int(spec.get("visible_items", 5))
                cur = to_int(initial[k])
                print(f"      读不到内容，按初始值 {initial[k]} 盲拨到 {part}（格高 {ih:.0f}px）")
                blind_set(col, self._drag_no_fling, cur, to_int(part), ih, int(spec.get("direction", 1)))

        if getattr(self, "_need_settle", False):
            time.sleep(self.wheel_settle)
            self._need_settle = False
        if spec.get("tap_target"):
            # 点选式列表需要点一下选项才算选中；滚轮型点一下已居中的选项也无妨
            from wheel_picker import contains
            col = cols[targets[0]]
            hits = [it for it in self._read_ocr(col) if contains(it.text, parts[0])]
            if hits:
                h = min(hits, key=lambda it: abs(it.cy - col.cy))
                self._tap(col.cx, h.cy)
                time.sleep(0.5)
                print(f"      点选「{h.text}」")
        if spec.get("confirm", "确定"):
            t0 = time.time()
            name = spec.get("confirm", "确定")
            if getattr(self, "fast", False) and not self._find_node({"text": name}):
                print(f"      选择框已关闭，不用再点「{name}」")
                return
            tok = [t for t in getattr(self, "_last_tokens", []) if t[0].strip() == name]
            if tok:  # 整屏 OCR 时已经看到「确定」的位置
                _, l, tp, r, b = tok[0]
                self._tap((l + r) / 2, (tp + b) / 2)
            elif not self._fast_click({"text": name}):
                btn = self.d(text=name)
                if btn.exists:
                    btn.click()
            time.sleep(self.step_delay)
            print(f"      点「{name}」用时 {time.time() - t0:.1f}s")

    # ---------- 系统日历（DatePickerDialog）----------
    _TITLE_RE = re.compile(r"^\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*$")

    def _date_screen(self):
        """读取日历当前状态：返回 {"title": (年, 月, bounds), "days": {日: bounds}, "prev": b, "next": b, "ok": b}
        先读界面数据（系统控件一般都能读到），读不到的部分用整屏 OCR 补"""
        info = {"days": {}}
        nodes = self._hier_nodes()
        for n in nodes:
            t = (n.get("text") or "").strip()
            desc = (n.get("content-desc") or "").strip()
            rid = n.get("resource-id") or ""
            b = parse_bounds(n.get("bounds"))
            m = self._TITLE_RE.match(t) or self._TITLE_RE.match(desc)
            if m and "title" not in info:
                info["title"] = (int(m.group(1)), int(m.group(2)), b)
            if rid.endswith(":id/prev") or desc in ("上个月", "Previous month"):
                info["prev"] = b
            if rid.endswith(":id/next") or desc in ("下个月", "Next month"):
                info["next"] = b
            if t == self._date_ok_name:
                info["ok"] = b
            if re.fullmatch(r"\d{1,2}", t) and b[2] - b[0] >= 20:
                info["days"].setdefault(int(t), []).append(b)
        def only_in_dialog(days):
            """只保留标题下方、「设置」上方的日期格子（排除页面背景里的数字）"""
            if "title" not in info:
                return {}
            ty, okb = info["title"][2][3], info.get("ok")
            out = {dd: [bb for bb in bs if bb[1] > ty + 40 and (not okb or bb[3] < okb[1])] for dd, bs in days.items()}
            return {dd: bs for dd, bs in out.items() if bs}

        info["days"] = only_in_dialog(info["days"])
        if "title" not in info or not info["days"] or "ok" not in info:
            ocr_days = {}
            for t, l, tp, r, bt in self._ocr_tokens(self.d.screenshot()):
                t2 = t.replace(" ", "")
                bb = (int(l), int(tp), int(r), int(bt))
                m = self._TITLE_RE.match(t2)
                if m and "title" not in info:
                    info["title"] = (int(m.group(1)), int(m.group(2)), bb)
                elif t2 == self._date_ok_name and "ok" not in info:
                    info["ok"] = bb
                elif re.fullmatch(r"\d{1,2}", t2):
                    ocr_days.setdefault(int(t2), []).append(bb)
            if not info["days"]:
                info["days"] = only_in_dialog(ocr_days)
        return info

    def date(self, spec: dict, value: str):
        """在系统日历里选日期：翻到目标年月 → 点日 → 点「设置」"""
        from wheel_picker import split_value

        parts = [int(x) for x in split_value(value, spec.get("pattern"))]
        if len(parts) < 3:
            raise ValueError(f"日期「{value}」格式不对，应为 2026-11-01")
        y, m, d = parts[:3]
        self._date_ok_name = spec.get("confirm", "设置")
        w, h = self.d.window_size()
        t0 = time.time()
        if spec.get("open"):
            for attempt in range(3):
                self.click(locator_of(spec["open"]))
                time.sleep(0.8)
                if "title" in self._date_screen():
                    break
                print(f"      日历没有弹出，再点一次（第 {attempt + 2} 次）")
            else:
                raise LookupError("点了 3 次都没有弹出日历")
        for _ in range(int(spec.get("max_months", 60))):
            info = self._date_screen()
            if "title" not in info:
                raise LookupError("读不到日历顶部的年月")
            cy, cm, tb = info["title"]
            diff = (y - cy) * 12 + (m - cm)
            if diff == 0:
                break
            key = "next" if diff > 0 else "prev"
            if key in info:
                b = info[key]
                x, yy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            else:  # 箭头在标题左右两侧
                x = (tb[0] + tb[2]) / 2 + (0.30 if diff > 0 else -0.30) * w
                yy = (tb[1] + tb[3]) / 2
            print(f"      当前 {cy}年{cm}月 → 目标 {y}年{m}月，点{'下' if diff > 0 else '上'}个月")
            self._tap(x, yy)
            time.sleep(0.4)
        else:
            raise LookupError("翻月次数过多")
        info = self._date_screen()
        if d in info["days"]:
            b = info["days"][d][0]
            x, yy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        else:
            # 个别日期没读到（OCR 对单个数字容易漏）：用读到的日期拟合网格，推算目标格子的位置
            pos = calendar_grid_position(y, m, d, info["days"])
            if pos is None:
                raise LookupError(f"日历里找不到 {d} 日（读到：{sorted(info['days'])}）")
            x, yy = pos
            print(f"      {d} 日未直接读到，按网格推算位置 ({int(x)},{int(yy)})")
        self._tap(x, yy)
        time.sleep(0.4)
        ok = info.get("ok") or self._date_screen().get("ok")
        if ok:
            self._tap((ok[0] + ok[2]) / 2, (ok[1] + ok[3]) / 2)
        elif not self._fast_click({"text": self._date_ok_name}):
            raise LookupError(f"找不到「{self._date_ok_name}」按钮")
        time.sleep(self.step_delay)
        print(f"      已选 {y}-{m:02d}-{d:02d}（用时 {time.time() - t0:.1f}s）")

    # ---------- 页面跳转 ----------
    def tap_spec(self, spec: dict):
        """依次尝试 try 里的定位方式，都找不到时点 xy（屏幕比例坐标，如 [0.93, 0.086]）"""
        for loc in spec.get("try", []):
            loc = locator_of(loc)
            if self.exists(loc):
                self.click(loc)
                print(f"      点了 {loc}")
                return
        if "xy" in spec:
            w, h = self.d.window_size()
            x, y = spec["xy"]
            x, y = (x * w if x <= 1 else x), (y * h if y <= 1 else y)
            self._tap(x, y)
            print(f"      按位置点 ({int(x)},{int(y)})")
            time.sleep(self.step_delay)
            return
        raise LookupError(f"找不到要点的控件：{spec.get('try')}")

    def wait_for(self, spec: dict, gone: bool = False):
        """等待控件出现（gone=True 时等待消失），超时报错"""
        loc = locator_of(spec)
        timeout = float(spec.get("timeout", 10))
        end = time.time() + timeout
        while time.time() < end:
            if not gone:
                found = self._find_fast(loc) if self._can_fast(loc) else self._find_once(loc, False)
            else:
                # 判断「消失」要严格：界面数据里没有，且（配置了 ocr 时）截图里也看不到，才算消失
                found = (self._find_node_bounds(loc) if set(loc) & self.FAST_KEYS else None)
                if not found and "ocr" in loc:
                    found = self._ocr_find(loc["ocr"])
            if bool(found) != gone:
                return
            time.sleep(0.5)
        what = "消失" if gone else "出现"
        raise TimeoutError(spec.get("message") or f"等了 {timeout:.0f} 秒，{loc} 没有{what}")

    def wheel_debug(self, spec: dict, need: int, out: Path):
        """不拖动，只把识别到的列区域和读到的数字画在截图上"""
        from PIL import ImageDraw

        img = self.d.screenshot().convert("RGB")
        dr = ImageDraw.Draw(img)
        try:
            cols, how = self._wheel_columns(spec, need)
        except LookupError as e:
            print(f"✗ {e}")
            print("整屏 OCR 读到的文字（文字, 位置）：")
            for t, l, tp, r, b in self._ocr_tokens(img):
                print(f"   {t!r}  [{int(l)},{int(tp)}][{int(r)},{int(b)}]")
                dr.rectangle((l, tp, r, b), outline=(0, 200, 0), width=4)
            print("界面里的文字：")
            for n in self._hier_nodes():
                t = (n.get("text") or "").strip()
                if t and not n.get("resource-id", "").startswith("com.android.systemui"):
                    print(f"   {t!r}  {n.get('bounds')}")
            img.thumbnail((900, 2000))
            img.save(out)
            print(f"调试截图已保存：{out}")
            return
        print(f"识别方式：{how}，共 {len(cols)} 列")
        for i, c in enumerate(cols):
            dr.rectangle((c.left, c.top, c.right, c.bottom), outline=(255, 0, 0), width=6)
            dr.line((c.left, c.cy, c.right, c.cy), fill=(0, 160, 255), width=4)
            ocr = self._read_ocr(c)
            hier = self._read_hier(c)
            print(f"  第 {i} 列 位置=[{c.left},{c.top}][{c.right},{c.bottom}]")
            print(f"     OCR 读到：{[(it.text, int(it.cy)) for it in ocr]}")
            print(f"     界面文字：{[(it.text, int(it.cy)) for it in hier]}")
            for it in ocr:
                dr.ellipse((c.left + 10, it.cy - 12, c.left + 34, it.cy + 12), fill=(0, 200, 0))
        img.thumbnail((900, 2000))
        img.save(out)
        print(f"调试截图已保存：{out}")

    def back(self):
        if self.fast:
            self._adb("input keyevent KEYCODE_BACK")
        else:
            self.d.press("back")
        time.sleep(self.step_delay)

    def hide_keyboard(self):
        if self.fast:
            if self._orig_ime is not None:  # 使用 uiautomator2 输入法时，用广播收起
                try:
                    self.d._broadcast("ADB_KEYBOARD_HIDE")
                except Exception:
                    pass
            time.sleep(0.2)
            return
        try:
            self.d.hide_keyboard()
        except Exception:
            self.d.press("back")
        time.sleep(0.2)

    def screenshot(self, path: Path):
        self.d.screenshot(str(path))

    def exists(self, loc: dict) -> bool:
        if self._can_fast(loc):
            return self._find_node_bounds(loc) is not None
        return self._find_once(loc, want_input=False) is not None

    # ---------- 屏幕信息 ----------
    def dump(self):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(self.d.dump_hierarchy(compressed=False))
        print(f"当前 App：{self.d.app_current().get('package')}  页面：{self.d.app_current().get('activity')}\n")
        print("== 屏幕上的文字（可用于 text / label）==")
        input_idx = 0
        inputs = []
        for n in root.iter("node"):
            cls = n.get("class", "")
            text = n.get("text", "")
            desc = n.get("content-desc", "")
            rid = n.get("resource-id", "")
            b = n.get("bounds", "")
            if cls in self.INPUT_CLASSES:
                inputs.append((b, text, rid))
                continue
            if text or desc:
                flag = " [可点]" if n.get("clickable") == "true" else ""
                extra = f"  desc={desc!r}" if desc else ""
                extra += f"  id={rid}" if rid else ""
                print(f"  {text!r}{extra}{flag}  位置={b}")
        wheels = [n for n in root.iter("node") if n.get("class", "").endswith("NumberPicker") or n.get("scrollable") == "true"]
        if wheels:
            print("\n== 可滚动区域 / 滚轮（wheel 会自动识别；识别不对时把位置填到 area）==")
            for n in wheels:
                print(f"  {n.get('class', '').split('.')[-1]}  位置={n.get('bounds')}  id={n.get('resource-id') or '-'}")
        print("\n== 输入框（按从上到下排序，可用于 input_index / id / hint）==")
        inputs.sort(key=lambda x: (parse_bounds(x[0])[1], parse_bounds(x[0])[0]))
        for b, text, rid in inputs:
            print(f"  input_index={input_idx}  当前文字={text!r}  id={rid or '-'}  位置={b}")
            input_idx += 1


def calendar_grid_position(year: int, month: int, day: int, seen: dict):
    """日历按 7 列网格排布。用已读到的日期 {日: [bounds]} 拟合列宽/行高，推算 day 的格子中心。
    周日开头、周一开头两种排法都试，取误差小的。至少需要 2 个不同行、不同列的日期。"""
    import calendar as _cal
    import statistics as st

    pts = [(dd, (bs[0][0] + bs[0][2]) / 2, (bs[0][1] + bs[0][3]) / 2) for dd, bs in seen.items()]
    if len(pts) < 3:
        return None
    best = None
    for sunday_first in (True, False):
        first = _cal.monthrange(year, month)[0]  # 周一=0
        col0 = (first + 1) % 7 if sunday_first else first

        def rc(dd):
            i = col0 + dd - 1
            return i // 7, i % 7

        cws, chs = [], []
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                (ra, ca), (rb, cb) = rc(pts[a][0]), rc(pts[b][0])
                if ca != cb:
                    cws.append((pts[b][1] - pts[a][1]) / (cb - ca))
                if ra != rb:
                    chs.append((pts[b][2] - pts[a][2]) / (rb - ra))
        if not cws or not chs:
            continue
        cw, ch = st.median(cws), st.median(chs)
        x0 = st.median(px - rc(dd)[1] * cw for dd, px, _ in pts)
        y0 = st.median(py - rc(dd)[0] * ch for dd, _, py in pts)
        err = sum(abs(px - (x0 + rc(dd)[1] * cw)) + abs(py - (y0 + rc(dd)[0] * ch)) for dd, px, py in pts)
        r, c = rc(day)
        if best is None or err < best[0]:
            best = (err, (x0 + c * cw, y0 + r * ch))
    return best[1] if best else None


class DryRunDevice:
    """不连手机，只打印动作，用来检查配置和数据是否对得上"""

    def click(self, loc): print(f"    点击 {loc}")
    def fill(self, loc, value): print(f"    填写 {loc} ← {value!r}")
    def select(self, loc, value, option_loc=None, **kw): print(f"    选择 {loc} → {value!r}")
    def tap_spec(self, spec): print(f"    点 {spec.get('try')}，找不到就按位置 {spec.get('xy')}")
    def wait_for(self, spec, gone=False): print(f"    等待 {locator_of(spec)} {'消失' if gone else '出现'}")

    def date(self, spec, value):
        print(f"    日历 (先点 {spec.get('open')}) → {value}，然后点「{spec.get('confirm', '设置')}」")

    def wheel(self, spec, value):
        from wheel_picker import split_value
        print(f"    滚轮{' (先点 ' + str(spec['open']) + ')' if spec.get('open') else ''} → {split_value(value, spec.get('pattern'))}，然后点「{spec.get('confirm', '确定')}」")
    def back(self): print("    返回键")
    def hide_keyboard(self): print("    收起键盘")
    def screenshot(self, path): print(f"    截图 → {path}")
    def exists(self, loc): return False


# =============================================================================
# 执行流程
# =============================================================================
LOCATOR_KEYS = {"text", "text_contains", "id", "desc", "desc_contains", "hint", "class", "nth",
                "label", "input_index", "scroll", "ocr", "tap_x"}


def locator_of(spec) -> dict:
    """click: "提交"  或  click: {text: "提交"}  或  fill: {label: "起飞地点", column: ...}"""
    if isinstance(spec, str):
        return {"text": spec}
    return {k: v for k, v in spec.items() if k in LOCATOR_KEYS}


class Paused(Exception):
    pass


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def run_steps(dev, steps: list, row: dict[str, str], interactive: bool):
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or len(step) == 0:
            raise ValueError(f"第 {i} 步格式不对：{step}")
        action, spec = next(iter(step.items()))
        if isinstance(spec, dict) and spec.get("skip_if_empty", True) and action in ("fill", "select", "wheel", "date"):
            if step_value(spec, row) == "":
                print(f"    （「{spec.get('column', spec.get('value'))}」为空，跳过）")
                continue
        if isinstance(spec, dict) and "only_if" in spec:  # 只有当某列有值时才执行
            if not row.get(spec["only_if"], ""):
                continue
        if isinstance(spec, dict) and "unless_exists" in spec:  # 某控件已存在时跳过（如已在新增页面就不点「+」）
            if dev.exists(locator_of(spec["unless_exists"])):
                print(f"    （已存在 {spec['unless_exists']}，跳过这一步）")
                continue

        t_step = time.time()
        if not isinstance(dev, DryRunDevice) and action in ("click", "fill", "select", "wheel", "date"):
            what = spec if isinstance(spec, str) else (spec.get("column") or locator_of(spec))
            val = step_value(spec, row) if isinstance(spec, dict) and action != "click" else ""
            print(f"    → {action} {what}{' = ' + val if val else ''}", flush=True)
        if action == "click":
            dev.click(locator_of(spec))
        elif action == "click_if_exists":
            loc = locator_of(spec)
            if dev.exists(loc):
                dev.click(loc)
        elif action == "fill":
            dev.fill(locator_of(spec), step_value(spec, row))
        elif action == "select":
            dev.select(locator_of(spec), step_value(spec, row), spec.get("option"),
                       deselect_current=bool(spec.get("deselect_current")), assume_current=spec.get("assume_current"))
        elif action == "wheel":
            dev.wheel(spec, step_value(spec, row))
        elif action == "date":
            dev.date(spec, step_value(spec, row))
        elif action == "tap":
            dev.tap_spec(spec)
        elif action == "wait_for":
            dev.wait_for(spec)
        elif action == "wait_gone":
            dev.wait_for(spec, gone=True)
        elif action == "wait":
            time.sleep(float(spec))
        elif action == "back":
            dev.back()
        elif action == "hide_keyboard":
            dev.hide_keyboard()
        elif action == "manual":
            # 遇到日期滚轮、地图选点等难以自动化的控件：暂停，请你在手机上手动操作
            msg = render_value(spec if isinstance(spec, str) else spec.get("message", "请手动操作"), row)
            if not interactive:
                print(f"    [需要手动] {msg}")
                continue
            if ask(f"    ✋ {msg}，完成后按回车（q 退出）：") == "q":
                raise Paused()
        else:
            raise ValueError(f"不认识的动作「{action}」，可用：click / click_if_exists / tap / wait_for / wait_gone / fill / select / wheel / date / wait / back / hide_keyboard / manual")


def save_progress(data_file: str, index: int):
    PROGRESS_FILE.write_text(json.dumps({"data": str(Path(data_file).resolve()), "next": index}, ensure_ascii=False))


def load_progress(data_file: str) -> int | None:
    try:
        p = json.loads(PROGRESS_FILE.read_text())
        if p.get("data") == str(Path(data_file).resolve()):
            return int(p["next"])
    except Exception:
        pass
    return None


def cmd_run(args):
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    settings = cfg.get("settings", {}) or {}
    rows = load_rows(args.data, args.sheet)
    if not rows:
        sys.exit("数据文件里没有数据（第一行应为表头）")

    for key in ("record_steps",):
        if not cfg.get(key):
            sys.exit(f"配置文件缺少 {key}")

    # 先检查配置用到的列在数据里都有
    cols = set(rows[0])
    for step in cfg["record_steps"] + cfg.get("before_each", []) + cfg.get("after_each", []):
        if isinstance(step, dict):
            spec = next(iter(step.values()))
            if isinstance(spec, dict):
                if "default" in spec:
                    continue
                need = [spec["column"]] if "column" in spec else re.findall(r"\{([^{}]+)\}", str(spec.get("value", "")))
                miss = [c for c in need if c not in cols]
                if miss:
                    sys.exit(f"配置用到的列 {miss} 在数据文件中不存在。数据中的列：{sorted(cols)}")

    start = args.start - 1 if args.start else None
    if start is None:
        saved = None if args.dry_run else load_progress(args.data)
        if saved and 0 < saved < len(rows):
            if ask(f"上次录到第 {saved} 条，是否从第 {saved + 1} 条继续？[Y/n] ") in ("", "y", "yes"):
                start = saved
        start = start or 0
    end = min(len(rows), start + args.limit) if args.limit else len(rows)

    dev = DryRunDevice() if args.dry_run else Device(args.serial, settings)
    if args.only:
        # 只执行某一段步骤，用于单独测试（如手机上已填好表单，只测保存）
        groups = {"before": ["before_each"], "record": ["record_steps"],
                  "submit": ["submit_steps", "after_each"], "enter": ["before_each"]}[args.only]
        row = rows[(args.start or 1) - 1]
        try:
            for g in groups:
                print(f"== 执行 {g} ==")
                run_steps(dev, cfg.get(g, []) or [], row, interactive=not args.auto)
            print("✓ 完成")
        finally:
            if hasattr(dev, "restore"):
                dev.restore()
        return
    if hasattr(dev, "restore"):
        import atexit
        atexit.register(dev.restore)  # 无论正常结束、出错还是 Ctrl+C，都切回原输入法
    interactive = not args.auto and not args.dry_run
    shots = HERE / "screenshots"

    print(f"\n共 {len(rows)} 条数据，本次处理第 {start + 1} ~ {end} 条，模式：{'预览' if args.dry_run else ('全自动' if args.auto else '逐条确认')}\n")
    done = failed = 0
    i = start
    while i < end:
        row = rows[i]
        summary = "  ".join(f"{k}={v}" for k, v in list(row.items())[:4])
        t_row = time.time()
        print(f"[{i + 1}/{len(rows)}] {summary}")
        try:
            run_steps(dev, cfg.get("before_each", []), row, interactive)
            run_steps(dev, cfg["record_steps"], row, interactive)
            if interactive:
                a = ask("    已填好，请在手机上检查。回车=提交  s=跳过此条  r=重填  q=退出：")
                if a == "q":
                    break
                if a == "r":
                    continue
                if a == "s":
                    print("    已跳过")
                    run_steps(dev, cfg.get("on_skip", [{"back": None}]), row, interactive)
                    i += 1
                    continue
            run_steps(dev, cfg.get("submit_steps", []), row, interactive)
            run_steps(dev, cfg.get("after_each", []), row, interactive)
            done += 1
            print(f"    ✓ 完成（本条用时 {time.time() - t_row:.0f}s）")
            if not args.dry_run:
                save_progress(args.data, i + 1)
            i += 1
        except Paused:
            break
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"    ✗ 出错：{e}")
            if not args.dry_run:
                shots.mkdir(exist_ok=True)
                try:
                    p = shots / f"error_row{i + 1}_{int(time.time())}.jpg"
                    dev.screenshot(p)
                    print(f"    已截图：{p}")
                except Exception:
                    pass
            if args.auto or args.dry_run:
                if args.stop_on_error:
                    break
                i += 1
                continue
            a = ask("    r=重试  s=跳过  m=我已手动完成，继续下一条  q=退出：")
            if a == "q":
                break
            if a in ("s", "m"):
                if a == "m":
                    save_progress(args.data, i + 1)
                i += 1
    print(f"\n结束：成功 {done} 条，出错 {failed} 条。")
    if not args.dry_run and i < len(rows):
        print(f"下次运行会提示从第 {i + 1} 条继续（或用 --start {i + 1}）。")


def cmd_dump(args):
    cfg = {}
    if Path(args.config).exists():
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    Device(args.serial, cfg.get("settings", {}) or {}).dump()


def cmd_wheel(args):
    """手机上先打开滚轮，然后直接测试拨动，例如：python filler.py wheel "09:30" --wheels 3 4"""
    cfg = {}
    if Path(args.config).exists():
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    spec = {"confirm": args.confirm, "read": args.read}
    if args.wheels:
        spec["wheels"] = args.wheels
    if args.area:
        spec["area"] = args.area
    if args.cyclic:
        spec["cyclic"] = args.cyclic
    if args.offscreen:
        spec["offscreen"] = True
    if args.drag_mode:
        spec["drag_mode"] = args.drag_mode
    dev = Device(args.serial, cfg.get("settings", {}) or {})
    if args.debug:
        from wheel_picker import split_value
        need = max(args.wheels or [len(split_value(args.value)) - 1]) + 1
        dev.wheel_debug(spec, need, HERE / "wheel_debug.png")
        return
    dev.wheel(spec, args.value)
    print("完成")


def cmd_template(args):
    out = Path(args.output)
    if out.exists():
        sys.exit(f"{out} 已存在，不覆盖")
    src = HERE / "flights_template.csv"
    out.write_bytes(src.read_bytes())
    print(f"已生成数据模板：{out}（可用 Excel 打开编辑，另存为 .xlsx 或 .csv 均可）")


def main(argv=None):
    p = argparse.ArgumentParser(description="云执照 App 飞行记录自动录入（安卓）")
    p.add_argument("-c", "--config", default=str(HERE / "config.yaml"), help="配置文件，默认 config.yaml")
    p.add_argument("-s", "--serial", help="手机序列号（多台设备或无线连接时使用，如 192.168.1.5:5555）")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="开始录入")
    r.add_argument("-d", "--data", required=True, help="数据文件 .xlsx / .csv")
    r.add_argument("--sheet", help="Excel 工作表名称，默认第一个")
    r.add_argument("--start", type=int, help="从第几条数据开始（从 1 数起）")
    r.add_argument("--limit", type=int, help="本次最多录入几条")
    r.add_argument("--auto", action="store_true", help="全自动，不逐条确认")
    r.add_argument("--dry-run", action="store_true", help="不连手机，只预览")
    r.add_argument("--stop-on-error", action="store_true", help="全自动模式下出错即停止")
    r.add_argument("--only", choices=["before", "record", "submit"],
                   help="只执行一段：before=进入新增页，record=填写，submit=保存及之后（单独测试用）")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("dump", help="显示当前屏幕上的文字和输入框")
    d.set_defaults(func=cmd_dump)

    w = sub.add_parser("wheel", help="调试：把当前已打开的滚轮拨到指定值")
    w.add_argument("value", help='如 "2026-11-01" 或 "09:30"')
    w.add_argument("--wheels", type=int, nargs="+", help="每段对应第几列，如 3 4")
    w.add_argument("--area", type=float, nargs=4, help="滚轮区域 x1 y1 x2 y2（像素或 0~1 比例）")
    w.add_argument("--read", default="auto", choices=["auto", "text", "ocr", "blind"])
    w.add_argument("--confirm", default="", help='拨好后点的按钮，默认不点；如 "确定"')
    w.add_argument("--offscreen", action="store_true", help="往下拖时允许手指拖出屏幕（一次拖更多格，部分手机不支持）")
    w.add_argument("--drag-mode", choices=["adb", "swipe", "touch"], help="拖动方式，默认 adb")
    w.add_argument("--cyclic", type=int, nargs="+", help="循环滚轮每列的格数，如时分滚轮：--cyclic 24 60")
    w.add_argument("--debug", action="store_true", help="不拖动，只保存标注了识别区域的截图 wheel_debug.png")
    w.set_defaults(func=cmd_wheel)

    t = sub.add_parser("template", help="生成数据模板 CSV")
    t.add_argument("-o", "--output", default="flights.csv")
    t.set_defaults(func=cmd_template)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

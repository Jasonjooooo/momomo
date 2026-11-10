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
        if col not in row:
            raise KeyError(f"数据中没有列「{col}」，现有列：{', '.join(row)}")
        value = row[col]
    else:
        value = render_value(step.get("value", ""), row)
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
    def click(self, loc: dict):
        el = self.find(loc)
        if el is None:
            raise LookupError(f"找不到要点击的控件：{loc}")
        el.click()
        time.sleep(self.step_delay)

    def fill(self, loc: dict, value: str):
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

    def select(self, loc: dict, value: str, option_loc: dict | None = None):
        """点开选择框，再点与 value 文字相同的选项（会自动滚动查找）"""
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
        if self._ocr_engine():
            from wheel_picker import columns_from_tokens
            cols = columns_from_tokens(self._ocr_tokens(self.d.screenshot()), need, w)
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
        confirm = spec.get("confirm", "确定")
        btn = self.d(text=confirm)
        if btn.exists:
            _, _, _, bb = parse_bounds(btn.info["bounds"])
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

    def _drag_no_fling(self, x, y0, y1):
        """按下 → 缓慢移动 → 停顿 → 抬起：抬手速度为 0，滚轮不会惯性甩动"""
        steps = max(8, int(abs(y1 - y0) / 15))
        t = self.d.touch
        t.down(x, y0)
        time.sleep(0.05)
        for k in range(1, steps + 1):
            t.move(x, y0 + (y1 - y0) * k / steps)
            time.sleep(0.012)
        time.sleep(0.25)
        t.up(x, y1)
        time.sleep(self.wheel_settle)

    def wheel(self, spec: dict, value: str):
        from wheel_picker import WheelColumnDriver, WheelError, blind_set, split_value, to_int

        if spec.get("open"):  # 先点开滚轮
            self.click(locator_of(spec["open"]))
            time.sleep(0.6)
        self.wheel_settle = float(spec.get("settle", 0.35))
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

            drv = WheelColumnDriver(col, reader, self._drag_no_fling,
                                    item_height=spec.get("item_height"),
                                    visible_items=int(spec.get("visible_items", 3 if how == "numberpicker" else 5)),
                                    log=print)
            try:
                drv.set(part)
            except WheelError as e:
                if initial is None or "读不到" not in str(e):
                    raise
                ih = spec.get("item_height") or col.height / int(spec.get("visible_items", 5))
                cur = to_int(initial[k])
                print(f"      读不到内容，按初始值 {initial[k]} 盲拨到 {part}（格高 {ih:.0f}px）")
                blind_set(col, self._drag_no_fling, cur, to_int(part), ih, int(spec.get("direction", 1)))

        if spec.get("confirm", "确定"):
            btn = self.d(text=spec.get("confirm", "确定"))
            if btn.exists:
                btn.click()
                time.sleep(self.step_delay)

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
        self.d.press("back")
        time.sleep(self.step_delay)

    def hide_keyboard(self):
        try:
            self.d.hide_keyboard()
        except Exception:
            self.d.press("back")
        time.sleep(0.2)

    def screenshot(self, path: Path):
        self.d.screenshot(str(path))

    def exists(self, loc: dict) -> bool:
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


class DryRunDevice:
    """不连手机，只打印动作，用来检查配置和数据是否对得上"""

    def click(self, loc): print(f"    点击 {loc}")
    def fill(self, loc, value): print(f"    填写 {loc} ← {value!r}")
    def select(self, loc, value, option_loc=None): print(f"    选择 {loc} → {value!r}")
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
                "label", "input_index", "scroll"}


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
        if isinstance(spec, dict) and spec.get("skip_if_empty", True) and action in ("fill", "select", "wheel"):
            if step_value(spec, row) == "":
                print(f"    （「{spec.get('column', spec.get('value'))}」为空，跳过）")
                continue
        if isinstance(spec, dict) and "only_if" in spec:  # 只有当某列有值时才执行
            if not row.get(spec["only_if"], ""):
                continue

        if action == "click":
            dev.click(locator_of(spec))
        elif action == "click_if_exists":
            loc = locator_of(spec)
            if dev.exists(loc):
                dev.click(loc)
        elif action == "fill":
            dev.fill(locator_of(spec), step_value(spec, row))
        elif action == "select":
            dev.select(locator_of(spec), step_value(spec, row), spec.get("option"))
        elif action == "wheel":
            dev.wheel(spec, step_value(spec, row))
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
            raise ValueError(f"不认识的动作「{action}」，可用：click / click_if_exists / fill / select / wheel / wait / back / hide_keyboard / manual")


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
    interactive = not args.auto and not args.dry_run
    shots = HERE / "screenshots"

    print(f"\n共 {len(rows)} 条数据，本次处理第 {start + 1} ~ {end} 条，模式：{'预览' if args.dry_run else ('全自动' if args.auto else '逐条确认')}\n")
    done = failed = 0
    i = start
    while i < end:
        row = rows[i]
        summary = "  ".join(f"{k}={v}" for k, v in list(row.items())[:4])
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
            print("    ✓ 完成")
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
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("dump", help="显示当前屏幕上的文字和输入框")
    d.set_defaults(func=cmd_dump)

    w = sub.add_parser("wheel", help="调试：把当前已打开的滚轮拨到指定值")
    w.add_argument("value", help='如 "2026-11-01" 或 "09:30"')
    w.add_argument("--wheels", type=int, nargs="+", help="每段对应第几列，如 3 4")
    w.add_argument("--area", type=float, nargs=4, help="滚轮区域 x1 y1 x2 y2（像素或 0~1 比例）")
    w.add_argument("--read", default="auto", choices=["auto", "text", "ocr", "blind"])
    w.add_argument("--confirm", default="", help='拨好后点的按钮，默认不点；如 "确定"')
    w.add_argument("--debug", action="store_true", help="不拖动，只保存标注了识别区域的截图 wheel_debug.png")
    w.set_defaults(func=cmd_wheel)

    t = sub.add_parser("template", help="生成数据模板 CSV")
    t.add_argument("-o", "--output", default="flights.csv")
    t.set_defaults(func=cmd_template)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

"""
滚轮选择器（日期/时间滚轮）自动拨动

核心思路：
  1. 找出滚轮的每一列（原生 NumberPicker / 可滚动列表 / 配置的区域等分）
  2. 读出这一列当前可见的条目及其纵坐标（界面文字，读不到时用 OCR 识别截图）
  3. 目标值已在屏幕上 → 把它拖到中间选中线；不在 → 按差值朝正确方向拖若干格
  4. 拖动使用「按下-缓慢移动-停顿-抬起」，抬起时速度为 0，不会产生惯性甩动，
     滚轮会自动吸附到最近的一格
  5. 循环直到中间那一格就是目标值

本模块只包含与设备无关的逻辑，读取/拖动由调用方注入，便于离线测试。
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass
class Item:
    text: str
    cy: float  # 条目中心纵坐标（屏幕绝对坐标）


@dataclass
class Column:
    left: int
    top: int
    right: int
    bottom: int
    center_y: float | None = None  # 选中线位置，默认取列的竖直中心

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return self.center_y if self.center_y is not None else (self.top + self.bottom) / 2

    @property
    def height(self) -> int:
        return self.bottom - self.top


class WheelError(Exception):
    pass


# -----------------------------------------------------------------------------
# 值解析与比较
# -----------------------------------------------------------------------------
_NUM = re.compile(r"\d+")


def split_value(value: str, pattern: str | None = None) -> list[str]:
    """
    把 "2026-11-01" → ["2026","11","01"]，"09:30" → ["09","30"]，"2026年11月1日 9:05" → 5 段。
    可传自定义正则（每个分组为一段），如 r"(上午|下午)\\s*(\\d+):(\\d+)"。
    """
    value = str(value).strip()
    if pattern:
        m = re.search(pattern, value)
        if not m:
            raise WheelError(f"「{value}」不符合格式 {pattern}")
        return [g for g in m.groups() if g is not None]
    parts = _NUM.findall(value)
    if not parts:
        return [value]
    return parts


def to_int(text: str) -> int | None:
    m = _NUM.search(str(text))
    return int(m.group()) if m else None


_ITEM = re.compile(r"^\D{0,3}\d{1,4}\D{0,3}$")


def looks_like_item(text: str) -> bool:
    """像滚轮里的一项：09 / 30分 / 2026年 / 第3天；排除「ILS CAT1」这类页面文字"""
    return bool(_ITEM.match(re.sub(r"\s", "", str(text))))


def _norm_opt(s: str) -> str:
    return re.sub(r"[\s（）()/\\\-_.]", "", str(s)).upper()


def contains(text: str, target: str) -> bool:
    """文字选项匹配：OCR 结果包含目标即可（「机长」匹配「机长(PIC)」），忽略空格、括号、斜杠。
    另外容忍 OCR 把一个字母认错（如 RNP L/VNAV → RNP LNNAV），但要求长度相同、数字完全一致，
    避免把 ILS CAT1 和 ILS CAT2、RNP L/VNAV 和 RNP LNAV 混淆"""
    t, x = _norm_opt(target), _norm_opt(text)
    if not t:
        return False
    if t in x:
        return True
    if len(t) >= 6 and len(t) == len(x) and re.findall(r"\d", t) == re.findall(r"\d", x):
        return sum(a != b for a, b in zip(t, x)) <= 1
    return False


def same(a: str, b: str) -> bool:
    ia, ib = to_int(a), to_int(b)
    if ia is not None and ib is not None:
        return ia == ib
    norm = lambda s: re.sub(r"\s", "", str(s))  # noqa: E731
    return norm(a) == norm(b)


# -----------------------------------------------------------------------------
# 单列拨动
# -----------------------------------------------------------------------------
Reader = Callable[[Column], Sequence[Item]]
Dragger = Callable[[float, float, float], None]  # (x, y_from, y_to)


class WheelColumnDriver:
    def __init__(self, col: Column, read: Reader, drag: Dragger, *,
                 item_height: float | None = None, visible_items: int = 5,
                 max_chunk: int = 3, max_iter: int = 80, log: Callable[[str], None] | None = None,
                 limits: tuple[float, float] | None = None, modulus: int | None = None,
                 match: str = "exact"):
        self.col = col
        self.read = read
        self.drag = drag
        self.item_height = item_height
        self.visible_items = visible_items
        self.max_chunk = max_chunk
        self.max_iter = max_iter
        self.text_mode = match == "contains"  # 文字选项（如「ILS CAT1」）：不按数字处理
        self.match = (lambda text, t: contains(text, t)) if self.text_mode else (lambda text, t: same(text, t))
        self.modulus = modulus  # 循环滚轮的格数（如分钟 60），给出时走较短的方向
        self.limits = limits  # 允许手指移动到的屏幕纵向范围；给出时可一次拖很多格
        self.log = log or (lambda s: None)
        self._direction = None  # 数字随 y 增大而增大为 +1，减小为 -1
        self._numeric_target = False

    # --- 辅助 ---
    def _items(self) -> list[Item]:
        its = [i for i in self.read(self.col) if self.col.top - 5 <= i.cy <= self.col.bottom + 5 and str(i.text).strip()]
        if self._numeric_target:
            its = [i for i in its if looks_like_item(i.text)]
        return sorted(its, key=lambda i: i.cy)

    def _est_item_h(self, items: list[Item]) -> float:
        if len(items) >= 2:
            gaps = [b.cy - a.cy for a, b in zip(items, items[1:]) if b.cy - a.cy > 3]
            if gaps:
                h = statistics.median(gaps)
                self.item_height = self.item_height or h
                return h
        if self.item_height:
            return self.item_height
        return self.col.height / max(1, self.visible_items)

    def _update_direction(self, items: list[Item]):
        nums = [(to_int(i.text), i.cy) for i in items if to_int(i.text) is not None]
        if len(nums) < 2:
            return
        ups = sum(1 for (a, _), (b, _) in zip(nums, nums[1:]) if b > a)
        downs = sum(1 for (a, _), (b, _) in zip(nums, nums[1:]) if b < a)
        if ups != downs:
            self._direction = 1 if ups > downs else -1

    def _consensus(self, items: list[Item], item_h: float) -> int | None:
        """每一格按「数值 - 方向×离中线的格数」推算中线上的值，取多数票，单格 OCR 误读不影响结果"""
        from collections import Counter

        d = self._direction or 1
        st = self._step(items, item_h)
        votes = Counter()
        for it in items:
            v = to_int(it.text)
            if v is None:
                continue
            k = round((it.cy - self.col.cy) / item_h)
            votes[v - d * st * k] += 1
        if not votes:
            return None
        (best, n), *rest = votes.most_common()
        if rest and rest[0][1] == n and n == 1 and len(items) > 1:
            # 票数打平且都只有一票：取离中线最近那一格的推算值
            near = min((i for i in items if to_int(i.text) is not None), key=lambda i: abs(i.cy - self.col.cy))
            return to_int(near.text) - d * st * round((near.cy - self.col.cy) / item_h)
        return best

    def _step(self, items: list[Item], item_h: float) -> int:
        """相邻两格的数值差（分钟滚轮可能是 5 分钟一格），取多数"""
        from collections import Counter

        diffs = Counter()
        nums = [(to_int(i.text), round((i.cy - self.col.cy) / item_h)) for i in items if to_int(i.text) is not None]
        for (a, ka), (b, kb) in zip(nums, nums[1:]):
            if kb - ka == 1 and a != b:
                diffs[abs(b - a)] += 1
        if diffs:
            st, n = diffs.most_common(1)[0]
            if n >= 2 or len(diffs) == 1:
                self._step_cache = st
        return getattr(self, "_step_cache", 1)

    def _center_item(self, items: list[Item]) -> Item | None:
        if not items:
            return None
        return min(items, key=lambda i: abs(i.cy - self.col.cy))

    def _drag_cost(self, n_items: float, item_h: float) -> float:
        """估算需要拖几次（考虑上下可拖动空间不同）"""
        import math
        if not self.limits:
            return math.ceil(abs(n_items) / self.max_chunk)
        lo, hi = self.limits
        m = min(item_h * 0.5, self.col.height * 0.2)
        room = (self.col.bottom - m - lo) if n_items > 0 else (hi - self.col.top - m)
        cap = max(1, int(room // item_h))
        return math.ceil(abs(n_items) / cap) + abs(n_items) * 1e-3

    def _drag_items(self, n_items: float, item_h: float):
        """内容向上移动 n_items 格（n<0 则向下）。分段拖动，每段不超过 max_chunk 格。"""
        remaining = n_items
        if self.limits:
            # 手指从滚轮内侧边缘按下，可一直拖到滚轮外，单次可移动很多格
            lo, hi = self.limits
            m = min(item_h * 0.5, self.col.height * 0.2)
            while abs(remaining) > 1e-6:
                if remaining > 0:  # 内容上移：从列底部往上拖
                    y0 = self.col.bottom - m
                    cap = int((y0 - lo) // item_h)
                else:
                    y0 = self.col.top + m
                    cap = int((hi - y0) // item_h)
                cap = max(1, cap)
                step = max(-cap, min(cap, remaining))
                self.drag(self.col.cx, y0, y0 - step * item_h)
                remaining -= step
            return
        while abs(remaining) > 1e-6:
            step = max(-self.max_chunk, min(self.max_chunk, remaining))
            dist = step * item_h
            # 让起止点都落在列内
            half = self.col.height / 2 - 2
            dist = max(-2 * half, min(2 * half, dist))
            y0 = self.col.cy + dist / 2
            y1 = self.col.cy - dist / 2
            self.drag(self.col.cx, y0, y1)
            remaining -= step

    # --- 主流程 ---
    def set(self, target: str) -> str:
        self._numeric_target = to_int(target) is not None and not self.text_mode
        self._last_hit = None
        self.uncentered = False
        last_center = None
        stuck = 0
        search_dir = 1
        search_tries = 0
        for _ in range(self.max_iter):
            items = self._items()
            if not items:
                raise WheelError("读不到这一列的内容（可安装 OCR 或配置 initial 盲拨）")
            self._update_direction(items)
            item_h = self._est_item_h(items)
            cur = self._center_item(items)
            ti = None if self.text_mode else to_int(target)
            if ti is not None:
                cons = self._consensus(items, item_h)
                snapped = abs(cur.cy - self.col.cy) <= item_h / 2
                if cons == ti and snapped:
                    return target
                if cons is not None:
                    key = cons
                    stuck = stuck + 1 if key == last_center else 0
                    last_center = key
                    if stuck >= 3 and self.modulus:
                        self.log("      这一列似乎不循环，改走普通方向")
                        self.modulus, stuck = None, 0
                    if stuck >= 3:
                        raise WheelError(f"滚不到「{target}」，当前停在「{cons}」（可能超出可选范围）")
                    st = self._step(items, item_h)
                    if (ti - cons) % st:
                        raise WheelError(f"「{target}」不在可选项里（这一列每格相差 {st}）")
                    diff = (ti - cons) // st * (self._direction or 1)
                    if self.modulus:
                        n = self.modulus // st
                        alt = diff - n if diff > 0 else diff + n  # 绕另一圈
                        diff = min((diff, alt), key=lambda d: self._drag_cost(d, item_h))
                    if diff == 0:  # 值对了但没对齐中线：把最近的一格拖到中线
                        self.drag(self.col.cx, cur.cy, self.col.cy)
                        continue
                    self.log(f"      当前 {cons} → 目标 {target}，拖动 {diff:+d} 格")
                    self._drag_items(diff, item_h)
                    continue

            if abs(cur.cy - self.col.cy) <= item_h / 2 and self.match(cur.text, target):
                return cur.text

            # 目标已可见：直接把它拖到选中线
            hit = [i for i in items if self.match(i.text, target)]
            if hit:
                h = min(hit, key=lambda i: abs(i.cy - self.col.cy))
                # 拖了也不动（列表到头了，或者是点选式列表）：交给调用方直接点它
                if self._last_hit is not None and abs(h.cy - self._last_hit) < 15:
                    self.uncentered = True
                    return h.text
                self._last_hit = h.cy
                self.drag(self.col.cx, h.cy, self.col.cy)
                continue

            # 卡住检测（滚到头了）
            key = (cur.text, round(cur.cy))
            stuck = stuck + 1 if key == last_center else 0
            last_center = key
            if stuck >= 2:
                if to_int(target) is None and search_tries < 2:
                    search_dir, search_tries, stuck = -search_dir, search_tries + 1, 0
                else:
                    raise WheelError(f"滚不到「{target}」，当前停在「{cur.text}」（可能超出可选范围）")

            ti, ci = to_int(target), to_int(cur.text)
            if ti is not None and ci is not None and not self.text_mode:
                direction = self._direction or 1
                diff = (ti - ci) * direction  # >0 表示目标在下方，需要内容上移
                self.log(f"      当前 {cur.text} → 目标 {target}，拖动 {diff:+d} 格")
                self._drag_items(diff, item_h)
            else:
                self._drag_items(2 * search_dir, item_h)
        raise WheelError(f"尝试次数过多仍未选中「{target}」")


def blind_set(col: Column, drag: Dragger, current: int, target: int, item_height: float,
              direction: int = 1, max_chunk: int = 3):
    """读不到任何内容时，按已知初始值和格高盲拨。"""
    drv = WheelColumnDriver(col, lambda c: [], drag, item_height=item_height, max_chunk=max_chunk)
    drv._drag_items((target - current) * direction, item_height)


# -----------------------------------------------------------------------------
# 列的划分
# -----------------------------------------------------------------------------
def split_area(area: tuple[int, int, int, int], count: int, weights: Sequence[float] | None = None) -> list[Column]:
    l, t, r, b = area
    weights = list(weights) if weights else [1] * count
    total = sum(weights)
    cols, x = [], float(l)
    for w in weights:
        nx = x + (r - l) * w / total
        cols.append(Column(int(x), t, int(nx), b))
        x = nx
    return cols


def resolve_area(area, screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """area 可以写像素 [x1,y1,x2,y2]，也可以写 0~1 的比例 [0, 0.65, 1, 0.95]"""
    vals = [float(v) for v in area]
    if all(0 <= v <= 1 for v in vals):
        return int(vals[0] * screen_w), int(vals[1] * screen_h), int(vals[2] * screen_w), int(vals[3] * screen_h)
    return tuple(int(v) for v in vals)  # type: ignore[return-value]


def columns_from_tokens(tokens, need: int, screen_w: int) -> list[Column]:
    """
    从整屏 OCR 结果中找滚轮：竖向排成一列、间距均匀的数字（至少 3 个）就是一列滚轮。
    tokens: [(text, left, top, right, bottom)]
    """
    pts = []
    for text, l, t, r, b in tokens:
        if looks_like_item(text):
            pts.append({"text": text, "l": l, "r": r, "cx": (l + r) / 2, "cy": (t + b) / 2})
    pts.sort(key=lambda p: p["cx"])
    clusters: list[list[dict]] = []
    for p in pts:
        if clusters and abs(p["cx"] - statistics.mean(q["cx"] for q in clusters[-1])) < screen_w * 0.06:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    cands = []
    for c in clusters:
        c.sort(key=lambda p: p["cy"])
        # 只保留间距均匀的最长一段
        best = []
        for i in range(len(c)):
            for j in range(i + 2, len(c)):
                seg = c[i:j + 1]
                gaps = [b["cy"] - a["cy"] for a, b in zip(seg, seg[1:])]
                g = statistics.median(gaps)
                if g > 20 and all(0.6 * g <= x <= 1.5 * g for x in gaps) and len(seg) > len(best):
                    best = seg
        if len(best) >= 3:
            gaps = [b["cy"] - a["cy"] for a, b in zip(best, best[1:])]
            cands.append((best, statistics.median(gaps)))
    if not cands:
        return []
    # 取数量最多的一组，再找与它竖直范围重叠的其它列
    cands.sort(key=lambda x: -len(x[0]))
    main_top = min(p["cy"] for p in cands[0][0])
    main_bot = max(p["cy"] for p in cands[0][0])
    picked = [c for c in cands if min(p["cy"] for p in c[0]) < main_bot and max(p["cy"] for p in c[0]) > main_top]
    picked = sorted(picked[:max(need, 1) + 3], key=lambda x: statistics.mean(p["cx"] for p in x[0]))

    cxs = [statistics.mean(p["cx"] for p in c) for c, _ in picked]
    cols = []
    for i, (c, g) in enumerate(picked):
        left = (cxs[i - 1] + cxs[i]) / 2 if i else max(0, min(p["l"] for p in c) - g)
        right = (cxs[i] + cxs[i + 1]) / 2 if i + 1 < len(cxs) else min(screen_w, max(p["r"] for p in c) + g)
        top = min(p["cy"] for p in c) - g * 0.7
        bot = max(p["cy"] for p in c) + g * 0.7
        mid = (top + bot) / 2
        center = min((p["cy"] for p in c), key=lambda y: abs(y - mid))
        cols.append(Column(int(left), int(top), int(right), int(bot), center_y=center))
    return cols

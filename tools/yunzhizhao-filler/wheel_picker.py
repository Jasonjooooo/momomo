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
                 max_chunk: int = 3, max_iter: int = 80, log: Callable[[str], None] | None = None):
        self.col = col
        self.read = read
        self.drag = drag
        self.item_height = item_height
        self.visible_items = visible_items
        self.max_chunk = max_chunk
        self.max_iter = max_iter
        self.log = log or (lambda s: None)
        self._direction = None  # 数字随 y 增大而增大为 +1，减小为 -1

    # --- 辅助 ---
    def _items(self) -> list[Item]:
        its = [i for i in self.read(self.col) if self.col.top - 5 <= i.cy <= self.col.bottom + 5 and str(i.text).strip()]
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

    def _center_item(self, items: list[Item]) -> Item | None:
        if not items:
            return None
        return min(items, key=lambda i: abs(i.cy - self.col.cy))

    def _drag_items(self, n_items: float, item_h: float):
        """内容向上移动 n_items 格（n<0 则向下）。分段拖动，每段不超过 max_chunk 格。"""
        remaining = n_items
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
            if abs(cur.cy - self.col.cy) <= item_h / 2 and same(cur.text, target):
                return cur.text

            # 目标已可见：直接把它拖到选中线
            hit = [i for i in items if same(i.text, target)]
            if hit:
                h = min(hit, key=lambda i: abs(i.cy - self.col.cy))
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
            if ti is not None and ci is not None:
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

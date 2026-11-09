"""用模拟滚轮测试拨动算法：python -m pytest tests 或 python tests/test_wheel.py"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wheel_picker import Column, Item, WheelColumnDriver, WheelError, blind_set, same, split_value  # noqa: E402


class SimWheel:
    """模拟一个滚轮：拖动后吸附到最近的一格，可模拟循环、倒序、只显示中间一格、拖动误差"""

    def __init__(self, values, start, *, cyclic=False, item_h=100, visible=5, only_center=False, jitter=0.0, fmt="{:02d}"):
        self.values = [fmt.format(v) if isinstance(v, int) else v for v in values]
        self.pos = start
        self.cyclic, self.item_h, self.visible, self.only_center, self.jitter = cyclic, item_h, visible, only_center, jitter
        self.col = Column(100, 1000, 300, 1000 + item_h * visible)
        self.drags = 0

    def value(self):
        return self.values[self.pos % len(self.values)]

    def read(self, col):
        half = self.visible // 2
        ks = [0] if self.only_center else range(-half, half + 1)
        out = []
        for k in ks:
            i = self.pos + k
            if not self.cyclic and not (0 <= i < len(self.values)):
                continue
            out.append(Item(self.values[i % len(self.values)], self.col.cy + k * self.item_h))
        return out

    def drag(self, x, y0, y1):
        self.drags += 1
        moved = (y0 - y1) / self.item_h * (1 + random.uniform(-self.jitter, self.jitter))
        self.pos = round(self.pos + moved)
        if not self.cyclic:
            self.pos = max(0, min(len(self.values) - 1, self.pos))


def run(sim, target, **kw):
    WheelColumnDriver(sim.col, sim.read, sim.drag, **kw).set(target)
    assert same(sim.value(), target), (sim.value(), target)
    return sim.drags


def test_split():
    assert split_value("2026-11-01") == ["2026", "11", "01"]
    assert split_value("09:30") == ["09", "30"]
    assert split_value("2026年11月1日 9:05") == ["2026", "11", "1", "9", "05"]
    assert split_value("下午 3:20", r"(上午|下午)\s*(\d+):(\d+)") == ["下午", "3", "20"]


def test_minutes_far():
    sim = SimWheel(range(60), 7, cyclic=True)
    run(sim, "52")


def test_hours_backward():
    sim = SimWheel(range(24), 20)
    run(sim, "3")


def test_year_text_suffix():
    sim = SimWheel([f"{y}年" for y in range(2000, 2041)], 26)
    run(sim, "2019")


def test_descending():
    sim = SimWheel(list(range(2040, 1999, -1)), 14, fmt="{}")  # 2026 在中间，倒序
    run(sim, "2031")
    run(sim, "2003")


def test_numberpicker_only_center():
    sim = SimWheel(range(1, 32), 8, item_h=120, visible=3, only_center=True, fmt="{}")
    run(sim, "27", visible_items=3)
    run(sim, "2", visible_items=3)


def test_drag_jitter():
    random.seed(1)
    for t in ("00", "59", "33", "12"):
        sim = SimWheel(range(60), 30, cyclic=True, jitter=0.2)
        run(sim, t)


def test_text_options():
    sim = SimWheel(["上午", "下午"], 0, fmt="{}")
    run(sim, "下午")


def test_out_of_range():
    sim = SimWheel(range(0, 60, 5), 2)  # 分钟间隔 5
    try:
        WheelColumnDriver(sim.col, sim.read, sim.drag).set("32")
    except WheelError:
        return
    raise AssertionError("应当报错")


def test_blind():
    sim = SimWheel(range(60), 10, cyclic=True)
    blind_set(sim.col, sim.drag, 10, 47, sim.item_h)
    assert sim.value() == "47"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("✓", name)

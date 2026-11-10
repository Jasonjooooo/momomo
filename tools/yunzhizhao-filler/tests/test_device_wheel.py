"""用假的 uiautomator2 设备（输出界面 XML）测试 Device.wheel 的完整流程：
识别列（可滚动列表 / NumberPicker）→ 读取文字 → 拖动 → 点确定"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filler  # noqa: E402

W, H, IH = 1080, 2400, 100


class FakeTouch:
    def __init__(self, dev): self.dev, self.start = dev, None
    def down(self, x, y): self.start = (x, y)
    def move(self, x, y): pass
    def up(self, x, y): self.dev.on_drag(self.start[0], self.start[1], y)


class FakeSel:
    def __init__(self, dev, kw): self.dev, self.kw = dev, kw
    @property
    def exists(self): return self.kw.get("text") == "确定"
    @property
    def info(self): return {"bounds": {"left": 900, "top": 1450, "right": 1050, "bottom": 1530}}
    def click(self): self.dev.confirmed = True


class FakeU2:
    """三列滚轮（年/月/日），kind = scrollable 或 numberpicker"""

    def __init__(self, kind, start):
        self.kind, self.pos, self.confirmed = kind, list(start), False
        self.ranges = [list(range(2000, 2041)), list(range(1, 13)), list(range(1, 32))]
        self.touch = FakeTouch(self)
        self.top = 1550
        self.vis = 3 if kind == "numberpicker" else 5

    def window_size(self): return W, H
    def __call__(self, **kw): return FakeSel(self, kw)

    def shell(self, cmd):
        """模拟 adb shell input：motionevent 手势 与 tap 点击"""
        import re as _re
        self.shell_cmds = getattr(self, "shell_cmds", 0) + 1
        down = None
        for part in cmd.split(";"):
            m = _re.search(r"input motionevent (DOWN|MOVE|UP) (-?\d+) (-?\d+)", part)
            if m:
                act, x, y = m.group(1), int(m.group(2)), int(m.group(3))
                if act == "DOWN":
                    down = (x, y)
                elif act == "UP" and down:
                    self.on_shell_drag(down[0], down[1], y)
            m = _re.search(r"input tap (\d+) (\d+)", part)
            if m:
                self.on_tap(int(m.group(1)), int(m.group(2)))
        return SimpleNamespace(output="")

    def on_shell_drag(self, x, y0, y1):
        self.on_drag(x, y0, y1)

    def on_tap(self, x, y):
        if 900 <= x <= 1050 and 1450 <= y <= 1530:
            self.confirmed = True

    def col_bounds(self, c):
        return c * W // 3, self.top, (c + 1) * W // 3, self.top + IH * self.vis

    def dump_hierarchy(self, compressed=False):
        out = ['<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" text="" scrollable="false">',
               '<node class="android.widget.TextView" text="确定" bounds="[900,1450][1050,1530]" scrollable="false"/>']
        for c in range(3):
            l, t, r, b = self.col_bounds(c)
            cls = "android.widget.NumberPicker" if self.kind == "numberpicker" else "androidx.recyclerview.widget.RecyclerView"
            sc = "false" if self.kind == "numberpicker" else "true"
            out.append(f'<node class="{cls}" text="" scrollable="{sc}" bounds="[{l},{t}][{r},{b}]">')
            cy = (t + b) // 2
            ks = [0] if self.kind == "numberpicker" else range(-2, 3)
            for k in ks:
                i = self.pos[c] + k
                if 0 <= i < len(self.ranges[c]):
                    y = cy + k * IH
                    cls2 = "android.widget.EditText" if self.kind == "numberpicker" else "android.widget.TextView"
                    out.append(f'<node class="{cls2}" text="{self.ranges[c][i]}" scrollable="false" bounds="[{l},{y - 40}][{r},{y + 40}]"/>')
            out.append("</node>")
        out.append("</node></hierarchy>")
        return "".join(out)

    def on_drag(self, x, y0, y1):
        c = int(x // (W / 3))
        self.pos[c] = max(0, min(len(self.ranges[c]) - 1, self.pos[c] + round((y0 - y1) / IH)))

    def value(self):
        return "%d-%02d-%02d" % tuple(self.ranges[c][self.pos[c]] for c in range(3))


def make(kind, start):
    dev = object.__new__(filler.Device)
    dev.d = FakeU2(kind, start)
    dev.timeout, dev.max_scrolls, dev.step_delay = 1, 0, 0
    return dev


def test_scrollable():
    dev = make("scrollable", [26, 10, 8])
    dev.wheel({"read": "text"}, "2019-03-28")
    assert dev.d.value() == "2019-03-28", dev.d.value()
    assert dev.d.confirmed


def test_numberpicker():
    dev = make("numberpicker", [26, 10, 8])
    dev.wheel({"read": "text"}, "2031-01-05")
    assert dev.d.value() == "2031-01-05", dev.d.value()


def test_guess_area_from_confirm():
    dev = make("scrollable", [26, 10, 8])
    # 去掉 scrollable 标记，模拟只能靠「确定」按钮位置猜区域
    orig = dev.d.dump_hierarchy
    dev.d.dump_hierarchy = lambda compressed=False: orig().replace('scrollable="true"', 'scrollable="false"')
    dev.wheel({"read": "text", "area_bottom": (1550 + 500) / H}, "2008-12-31")
    assert dev.d.value() == "2008-12-31", dev.d.value()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("✓", name)

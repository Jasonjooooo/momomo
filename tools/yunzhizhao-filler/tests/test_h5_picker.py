"""复现云执照「出发时刻」滚轮：H5 自绘时/分滚轮，界面树里只有「时」「分」「确定」「取消」
和被弹窗盖住的页面文字（如 ILS CAT1），数字只能靠 OCR 读取。需要 rapidocr_onnxruntime。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import filler  # noqa: E402

FONT = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
W, H, IH = 1260, 2720, 120
HEAD_Y, TOP = 1900, 2000  # 表头「时 分」所在行、滚轮区域顶部


class Sel:
    def __init__(self, dev, kw): self.dev, self.kw = dev, kw
    @property
    def exists(self): return self.kw.get("text") == "确定"
    @property
    def info(self): return {"bounds": {"left": 1080, "top": 1780, "right": 1220, "bottom": 1860}}
    def click(self): self.dev.confirmed = True


class Touch:
    def __init__(self, d): self.d = d
    def down(self, x, y): self.s = (x, y)
    def move(self, x, y): pass
    def up(self, x, y): self.d.drag(self.s[0], self.s[1], y)


class FakeH5:
    def __init__(self, h, m):
        self.pos, self.confirmed, self.touch = [h, m], False, Touch(self)
        self.bottom = int(H * 0.97)

    def window_size(self): return W, H
    def __call__(self, **kw): return Sel(self, kw)

    def dump_hierarchy(self, compressed=False):
        bg = "".join(f'<node class="android.view.View" text="{t}" bounds="[620,{y}][1176,{y + 90}]" scrollable="false"/>'
                     for t, y in (("ILS CAT1", 2100), ("00:00", 2250), ("请选择(Please Select)", 2400)))
        return ('<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">' + bg +
                '<node class="android.view.View" text="时" bounds="[290,1870][340,1930]" scrollable="false"/>'
                '<node class="android.view.View" text="分" bounds="[920,1870][970,1930]" scrollable="false"/>'
                '<node class="android.view.View" text="确定" bounds="[1080,1780][1220,1860]" scrollable="false"/>'
                '<node class="android.view.View" text="取消" bounds="[40,1780][180,1860]" scrollable="false"/>'
                '</node></hierarchy>')

    def cols(self):
        return [(0, 630), (630, W)]

    def screenshot(self):
        img = Image.new("RGB", (W, H), "#eee")
        d = ImageDraw.Draw(img)
        f, fs = ImageFont.truetype(FONT, 60), ImageFont.truetype(FONT, 46)
        d.text((620, 2100), "ILS CAT1", fill="#999", font=fs)  # 被盖住前的页面（这里画在弹窗外，模拟干扰）
        d.rectangle((0, 1760, W, H), fill="white")
        d.text((60, 1790), "取消", font=fs, fill="#666"); d.text((1100, 1790), "确定", font=fs, fill="#1677ff")
        d.text((315, HEAD_Y), "时", font=fs, fill="#333", anchor="mm"); d.text((945, HEAD_Y), "分", font=fs, fill="#333", anchor="mm")
        cy = (TOP + self.bottom) / 2
        for c, (l, r) in enumerate(self.cols()):
            for k in range(-2, 3):
                v = (self.pos[c] + k) % (24 if c == 0 else 60)
                d.text(((l + r) / 2, cy + k * IH), f"{v:02d}", font=f if k == 0 else fs, fill="#000" if k == 0 else "#bbb", anchor="mm")
        return img

    def drag(self, x, y0, y1):
        c = 0 if x < 630 else 1
        self.pos[c] = (self.pos[c] + round((y0 - y1) / IH)) % (24 if c == 0 else 60)

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
        self.adb_drags = getattr(self, "adb_drags", 0) + 1
        self.drag(x, y0, y1)

    def on_tap(self, x, y):
        if 1700 < y < 1900 and x > 900 or 2580 < y < 2700 and x > 630:  # 「确定」
            self.confirmed = True

    def swipe_points(self, pts, duration):
        """模拟 uiautomator2 的一次性手势：必须在终点停留（末尾有重复点）才不会甩动"""
        assert pts[-1] == pts[-2], "终点没有停留，会产生惯性"
        self.swipes = getattr(self, "swipes", 0) + 1
        self.drag(pts[0][0], pts[0][1], pts[-1][1])


def main():
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        print("未安装 rapidocr_onnxruntime，跳过"); return
    dev = object.__new__(filler.Device)
    dev.d, dev.timeout, dev.max_scrolls, dev.step_delay = FakeH5(0, 0), 1, 0, 0
    dev.wheel({"confirm": "确定"}, "09:30")
    assert dev.d.pos == [9, 30], dev.d.pos
    assert dev.d.confirmed
    assert getattr(dev.d, "adb_drags", 0) > 0 and not getattr(dev.d, "swipes", 0), "应使用 adb motionevent"
    dev.wheel({"confirm": "确定"}, "23:05")
    assert dev.d.pos == [23, 5], dev.d.pos
    dev.wheel({"confirm": "确定", "cyclic": [24, 60]}, "09:30")
    assert dev.d.pos == [9, 30], dev.d.pos
    print("✓ H5 时/分滚轮：00:00 → 09:30 → 23:05")


if __name__ == "__main__":
    main()


class FakeH5ButtonsBottom(FakeH5):
    """另一种布局：滚轮在屏幕中部，「时」「分」紧跟在选中数字右侧，「取消/确定」在最底部，
    旧的按表头/按钮位置推测都会失败，只能靠整屏 OCR 找滚轮"""
    TOP2, IH2 = 1500, 120

    def dump_hierarchy(self, compressed=False):
        return ('<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">'
                '<node class="android.view.View" text="ILS CAT1" bounds="[620,1270][1176,1364]" scrollable="false"/>'
                '<node class="android.view.View" text="时" bounds="[400,1755][450,1805]" scrollable="false"/>'
                '<node class="android.view.View" text="分" bounds="[1030,1755][1080,1805]" scrollable="false"/>'
                '<node class="android.view.View" text="确定" bounds="[700,2600][1200,2690]" scrollable="false"/>'
                '<node class="android.view.View" text="取消" bounds="[60,2600][560,2690]" scrollable="false"/>'
                '</node></hierarchy>')

    def screenshot(self):
        img = Image.new("RGB", (W, H), "#eee")
        d = ImageDraw.Draw(img)
        f, fs = ImageFont.truetype(FONT, 60), ImageFont.truetype(FONT, 46)
        d.text((620, 1300), "ILS CAT1", fill="#999", font=fs)
        d.text((620, 1000), "00:00", fill="#999", font=fs)
        d.rectangle((0, 1400, W, H), fill="white")
        cy = 1780
        for c, (l, r) in enumerate(self.cols()):
            for k in range(-3, 4):
                v = (self.pos[c] + k) % (24 if c == 0 else 60)
                d.text(((l + r) / 2 - 40, cy + k * self.IH2), f"{v:02d}", font=f if k == 0 else fs,
                       fill="#000" if k == 0 else "#aaa", anchor="mm")
            d.text(((l + r) / 2 + 60, cy), "时" if c == 0 else "分", font=fs, fill="#000", anchor="mm")
        d.text((250, 2640), "取消", font=fs, fill="#666", anchor="mm"); d.text((950, 2640), "确定", font=fs, fill="#1677ff", anchor="mm")
        return img

    def drag(self, x, y0, y1):
        c = 0 if x < 630 else 1
        self.pos[c] = (self.pos[c] + round((y0 - y1) / self.IH2)) % (24 if c == 0 else 60)


def test_buttons_bottom():
    dev = object.__new__(filler.Device)
    dev.d, dev.timeout, dev.max_scrolls, dev.step_delay = FakeH5ButtonsBottom(14, 7), 1, 0, 0
    dev.wheel({"confirm": "确定"}, "09:30")
    assert dev.d.pos == [9, 30], dev.d.pos
    print("✓ 按钮在底部的布局：14:07 → 09:30（整屏 OCR 定位）")


if __name__ == "__main__":
    test_buttons_bottom()

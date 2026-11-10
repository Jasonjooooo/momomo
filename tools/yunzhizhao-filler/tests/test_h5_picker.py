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


class FakeH5NeedsTwoTaps(FakeH5):
    """真机现象：上一个输入框还在输入状态时，第一下点「出发时刻」只会取消焦点，第二下才弹出滚轮"""
    def __init__(self, h, m):
        super().__init__(h, m)
        self.open, self.taps = False, 0

    def dump_hierarchy(self, compressed=False):
        if self.open:
            return super().dump_hierarchy()
        return ('<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">'
                '<node class="android.view.View" text="出发时刻" bounds="[143,1094][918,1155]"/>'
                '<node class="android.widget.EditText" resource-id="TAKETIME-time" text="" bounds="[1033,1077][1176,1173]"/>'
                '</node></hierarchy>')

    def on_tap(self, x, y):
        if not self.open:
            self.taps += 1
            if self.taps >= 2 and 1033 <= x <= 1176 and 1077 <= y <= 1173:
                self.open = True
            return
        super().on_tap(x, y)


def test_open_needs_two_taps():
    dev = object.__new__(filler.Device)
    dev.d, dev.timeout, dev.max_scrolls, dev.step_delay, dev.fast = FakeH5NeedsTwoTaps(0, 0), 1, 0, 0, True
    dev.wheel({"open": {"id": "TAKETIME-time", "label": "出发时刻"}, "confirm": "确定", "offscreen": True}, "09:30")
    assert dev.d.taps == 2 and dev.d.pos == [9, 30], (dev.d.taps, dev.d.pos)
    print("✓ 第一下点击未弹出滚轮时自动再点一次")


if __name__ == "__main__":
    test_open_needs_two_taps()


class FakeTextPicker(FakeH5):
    """真机「职位」：点开后是自绘文字滚轮（请选择/机长(PIC)/副驾驶(F/O)…），选项不在界面数据里"""
    OPTS = ["请选择(Please Select)", "机长(PIC)", "副驾驶(F/O)", "学员(Student)", "教员(Instructor)", "观察员(Observer)"]

    def __init__(self):
        super().__init__(0, 0)
        self.open, self.idx, self.value = False, 0, self.OPTS[0]

    def dump_hierarchy(self, compressed=False):
        bg = ('<node class="android.view.View" text="职位" bounds="[53,1389][617,1446]"/>'
              f'<node class="android.widget.EditText" resource-id="JOB_dummy" text="{self.value}" bounds="[620,1371][1176,1464]"/>')
        pk = ('<node class="android.view.View" text="确定" bounds="[1080,1690][1220,1770]"/>'
              '<node class="android.view.View" text="取消" bounds="[40,1690][180,1770]"/>') if self.open else ""
        return f'<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">{bg}{pk}</node></hierarchy>'

    CY = 2249

    def screenshot(self):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        f, fs = ImageFont.truetype(FONT, 52), ImageFont.truetype(FONT, 46)
        d.text((150, 1400), "职位", font=fs, fill="#000"); d.text((700, 1400), self.value, font=fs, fill="#000")
        if self.open:
            d.rectangle((0, 1620, W, H), fill="#f6f6f6")
            d.text((40, 1700), "取消", font=fs, fill="#1677ff"); d.text((1100, 1700), "确定", font=fs, fill="#1677ff")
            for k in range(-3, 4):
                i = self.idx + k
                if 0 <= i < len(self.OPTS):
                    d.text((W / 2, self.CY + k * 174), self.OPTS[i], font=f if k == 0 else fs,
                           fill="#000" if k == 0 else "#aaa", anchor="mm")
        return img

    def on_tap(self, x, y):
        if not self.open:
            if 620 <= x <= 1176 and 1371 <= y <= 1464:
                self.open = True
        elif 1080 <= x <= 1220 and 1690 <= y <= 1770:
            self.value, self.open = self.OPTS[self.idx], False

    def drag(self, x, y0, y1):
        self.idx = max(0, min(len(self.OPTS) - 1, self.idx + round((y0 - y1) / 174)))


def test_select_text_wheel():
    dev = object.__new__(filler.Device)
    dev.d, dev.timeout, dev.max_scrolls, dev.step_delay, dev.fast = FakeTextPicker(), 1, 0, 0, True
    dev.select({"id": "JOB_dummy", "label": "职位"}, "机长")
    assert dev.d.value == "机长(PIC)", dev.d.value
    dev.select({"id": "JOB_dummy", "label": "职位"}, "教员")
    assert dev.d.value == "教员(Instructor)", dev.d.value
    print("✓ 文字滚轮下拉：请选择 → 机长(PIC) → 教员(Instructor)")


if __name__ == "__main__":
    test_select_text_wheel()


class FakeListPicker(FakeTextPicker):
    """真机「进近方式」：点选式列表（蓝色为选中项，点一下才选中），页面上该行本身显示着当前值 ILS CAT1"""
    OPTS = ["ILS CAT1", "ILS CAT2", "ILS CAT3", "RNP LNAV", "RNP L/VNAV", "NDB", "VOR", "目视(visual)", "LOC/DME", "其它(other)"]
    TOP = 1900

    def __init__(self):
        super().__init__()
        self.scroll, self.sel = 0, 0

    def dump_hierarchy(self, compressed=False):
        bg = ('<node class="android.view.View" text="进近方式" bounds="[53,1389][617,1446]"/>'
              f'<node class="android.view.View" resource-id="YIBIAOTYPE" text="{self.value}" bounds="[613,1400][624,1411]"/>'
              f'<node class="android.widget.EditText" resource-id="YIBIAOTYPE_dummy" text="{self.value}" bounds="[620,1371][1176,1464]"/>')
        pk = ('<node class="android.view.View" text="确定" bounds="[1080,1690][1220,1770]"/>'
              '<node class="android.view.View" text="取消" bounds="[40,1690][180,1770]"/>') if self.open else ""
        return f'<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">{bg}{pk}</node></hierarchy>'

    def item_y(self, i):
        return self.TOP + i * 172 - self.scroll

    def screenshot(self):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        fs = ImageFont.truetype(FONT, 50)
        d.text((150, 1400), "进近方式", font=fs, fill="#000"); d.text((800, 1400), self.value, font=fs, fill="#000")
        if self.open:
            d.rectangle((0, 1620, W, H), fill="#f6f6f6")
            d.text((40, 1700), "取消", font=fs, fill="#1677ff"); d.text((1100, 1700), "确定", font=fs, fill="#1677ff")
            for i, o in enumerate(self.OPTS):
                y = self.item_y(i)
                if 1800 < y < H - 20:
                    d.text((W / 2, y), o, font=fs, fill="#1677ff" if i == self.sel else "#000", anchor="mm")
        return img

    def on_tap(self, x, y):
        if not self.open:
            if 620 <= x <= 1176 and 1371 <= y <= 1464:
                self.open = True
            return
        if 1080 <= x <= 1220 and 1690 <= y <= 1770:
            self.value, self.open = (self.OPTS[self.sel] if self.sel is not None else "请选择"), False
            return
        for i in range(len(self.OPTS)):
            if abs(self.item_y(i) - y) < 80:
                # 真机现象：再点一次已选中的项会取消选中；已有选中项时点别的项无效，要先取消
                if self.sel == i:
                    self.sel = None
                elif self.sel is None:
                    self.sel = i

    def drag(self, x, y0, y1):
        max_scroll = max(0, self.TOP + len(self.OPTS) * 172 - (H - 100))
        self.scroll = max(0, min(max_scroll, self.scroll + (y0 - y1)))


def test_select_list_picker():
    for want, expect in (("VOR", "VOR"), ("ILS CAT1", "ILS CAT1"), ("其它", "其它(other)"), ("RNP L/VNAV", "RNP L/VNAV")):
        dev = object.__new__(filler.Device)
        dev.d, dev.timeout, dev.max_scrolls, dev.step_delay, dev.fast = FakeListPicker(), 1, 0, 0, True
        dev.d.value = "ILS CAT1"
        opened = []
        orig = dev.d.on_tap
        dev.d.on_tap = lambda x, y, _o=orig: (opened.append(1) if not dev.d.open else None, _o(x, y))
        dev.d.sel = 0  # 新页面默认选中 ILS CAT1
        dev.select({"id": "YIBIAOTYPE_dummy", "label": "进近方式"}, want, deselect_current=True, assume_current="ILS CAT1")
        if want == "ILS CAT1":
            assert not opened, "当前已是 ILS CAT1，不应再打开选择框"
        assert dev.d.value == expect and not dev.d.open, (want, dev.d.value, dev.d.open)
    print("✓ 点选式列表：VOR / ILS CAT1 / 其它(other) / RNP L/VNAV 都能选中并确定")


if __name__ == "__main__":
    test_select_list_picker()

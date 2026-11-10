"""模拟系统日历对话框（DatePickerDialog）：标题「2026年11月」、左右箭头、日期格子、清除/取消/设置。
两种情况：界面数据里能读到（系统控件）/ 只能 OCR（需要 rapidocr_onnxruntime）"""
import calendar
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import filler  # noqa: E402

FONT = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
W, H = 1260, 2720
GX, GY, CW, CH = 200, 1290, 127, 144   # 日期格子起点与间距（按真机截图换算）


class FakeCalendar:
    def __init__(self, y=2026, m=11, hier=True):
        self.y, self.m, self.hier = y, m, hier
        self.open, self.sel, self.value, self.taps = False, None, "", 0

    def window_size(self): return W, H

    def cells(self):
        first, n = calendar.monthrange(self.y, self.m)
        col0 = (first + 1) % 7  # 周日为第一列
        out = {}
        for d in range(1, n + 1):
            i = col0 + d - 1
            cx, cy = GX + (i % 7) * CW, GY + (i // 7) * CH
            out[d] = (cx - 50, cy - 50, cx + 50, cy + 50)
        return out

    def dump_hierarchy(self, compressed=False):
        out = ['<hierarchy><node class="android.webkit.WebView" text="" bounds="[0,130][1260,2720]">',
               '<node class="android.view.View" text="日期*" bounds="[53,388][617,445]"/>',
               '<node class="android.view.View" text="转场时间" bounds="[53,2400][617,2460]"/>',
               '<node class="android.view.View" text="12" bounds="[900,2500][960,2560]"/>']  # 背景页面里的数字，不能点
        if self.open:
            out.append('<node class="android.widget.Button" text="设置" bounds="[930,2150][1050,2240]"/>')
            out.append('<node class="android.widget.Button" text="取消" bounds="[700,2150][820,2240]"/>')
            if self.hier:
                out.append(f'<node class="android.view.View" text="{self.y}年{self.m}月" bounds="[480,960][780,1030]"/>')
                out.append('<node class="android.widget.ImageButton" resource-id="android:id/prev" content-desc="上个月" bounds="[200,950][300,1040]"/>')
                out.append('<node class="android.widget.ImageButton" resource-id="android:id/next" content-desc="下个月" bounds="[960,950][1060,1040]"/>')
                for d, b in self.cells().items():
                    out.append(f'<node class="android.view.View" text="{d}" bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]"/>')
        out.append("</node></hierarchy>")
        return "".join(out)

    def screenshot(self):
        img = Image.new("RGB", (W, H), "#888")
        dr = ImageDraw.Draw(img)
        f = ImageFont.truetype(FONT, 46)
        dr.text((100, 2510), "12", font=f, fill="#000")
        if self.open:
            dr.rectangle((110, 540, 1150, 2310), fill="white")
            dr.text((630, 995), f"{self.y}年{self.m}月", font=f, fill="#000", anchor="mm")
            dr.text((250, 995), "<", font=f, fill="#000", anchor="mm"); dr.text((1010, 995), ">", font=f, fill="#000", anchor="mm")
            for d, b in self.cells().items():
                dr.text(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), str(d), font=f, fill="#000", anchor="mm")
            dr.text((760, 2195), "取消", font=f, fill="#099", anchor="mm"); dr.text((990, 2195), "设置", font=f, fill="#099", anchor="mm")
        return img

    def shell(self, cmd):
        m = re.match(r"input tap (\d+) (\d+)", cmd)
        if m:
            self.tap(int(m.group(1)), int(m.group(2)))
        return SimpleNamespace(output="")

    def tap(self, x, y):
        self.taps += 1
        if not self.open:
            if 620 < x < 1260 and 388 <= y <= 445:
                self.open = True
            return
        if 200 <= x <= 300 and 950 <= y <= 1040:
            self.m -= 1
            if self.m == 0:
                self.y, self.m = self.y - 1, 12
            return
        if 960 <= x <= 1060 and 950 <= y <= 1040:
            self.m += 1
            if self.m == 13:
                self.y, self.m = self.y + 1, 1
            return
        if 930 <= x <= 1050 and 2150 <= y <= 2240:
            if self.sel:
                self.value = "%d-%02d-%02d" % self.sel
            self.open = False
            return
        for d, b in self.cells().items():
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                self.sel = (self.y, self.m, d)


def make(**kw):
    dev = object.__new__(filler.Device)
    dev.d = FakeCalendar(**kw)
    dev.timeout, dev.max_scrolls, dev.step_delay, dev.fast = 1, 0, 0, True
    return dev


def test_hierarchy():
    for target in ("2026-11-01", "2026-09-30", "2025-12-25", "2027-02-28"):
        dev = make()
        dev.date({"open": {"label": "日期"}, "confirm": "设置"}, target)
        assert dev.d.value == target and not dev.d.open, (target, dev.d.value)
    print("✓ 系统日历（界面数据）：本月 / 往前翻 / 跨年往前 / 跨年往后 都选对")


def test_ocr_only():
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        print("未安装 rapidocr_onnxruntime，跳过"); return
    for target in ("2026-11-12", "2026-10-03", "2026-11-01", "2026-12-07"):
        dev = make(hier=False)
        dev.date({"open": {"label": "日期"}, "confirm": "设置"}, target)
        assert dev.d.value == target and not dev.d.open, (target, dev.d.value)
    print("✓ 日历（只能 OCR）：选对，未误点背景里的数字")


def test_grid_inference():
    """单独测网格推算：只给部分日期，推算 3 日、30 日的位置"""
    fc = FakeCalendar(2026, 11)
    cells = fc.cells()
    seen = {d: [cells[d]] for d in (10, 11, 18, 26)}
    for d in (1, 3, 30):
        x, y = filler.calendar_grid_position(2026, 11, d, seen)
        b = cells[d]
        assert b[0] <= x <= b[2] and b[1] <= y <= b[3], (d, x, y, b)
    print("✓ 网格推算：只读到 4 个日期也能算出 1/3/30 日的位置")


if __name__ == "__main__":
    test_grid_inference()
    test_hierarchy()
    test_ocr_only()

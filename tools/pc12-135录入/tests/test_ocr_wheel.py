"""端到端：模拟一个「自绘」时间滚轮（界面树里没有文字），通过截图 OCR 读值并拨到目标时间。
需要 rapidocr_onnxruntime；未安装则跳过。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import filler  # noqa: E402
from wheel_picker import Column, WheelColumnDriver, same  # noqa: E402

FONT = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
W, H, TOP, IH, VIS = 1080, 2400, 1700, 110, 5


class CanvasPicker:
    """两列（时、分）滚轮，绘制成截图；拖动后吸附到整格"""

    def __init__(self, hour, minute):
        self.pos = [hour, minute]
        self.mod = [24, 60]
        self.suffix = ["时", "分"]
        self.cols = [Column(0, TOP, W // 2, TOP + IH * VIS), Column(W // 2, TOP, W, TOP + IH * VIS)]

    def screenshot(self):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        big, small = ImageFont.truetype(FONT, 56), ImageFont.truetype(FONT, 44)
        d.text((60, TOP - 110), "取消", fill="#888", font=small)
        d.text((W - 160, TOP - 110), "确定", fill="#1677ff", font=small)
        for c, col in enumerate(self.cols):
            for k in range(-2, 3):
                v = (self.pos[c] + k) % self.mod[c]
                cy = col.cy + k * IH
                f = big if k == 0 else small
                d.text((col.cx, cy), f"{v:02d}{self.suffix[c]}", fill="#111" if k == 0 else "#aaa", font=f, anchor="mm")
        d.line((0, col.cy - IH / 2, W, col.cy - IH / 2), fill="#ddd", width=2)
        d.line((0, col.cy + IH / 2, W, col.cy + IH / 2), fill="#ddd", width=2)
        return img

    def drag(self, x, y0, y1):
        c = 0 if x < W / 2 else 1
        self.pos[c] = (self.pos[c] + round((y0 - y1) / IH)) % self.mod[c]


def main():
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        print("未安装 rapidocr_onnxruntime，跳过")
        return
    pk = CanvasPicker(14, 7)
    dev = object.__new__(filler.Device)  # 不连手机，只借用它的 OCR 读取逻辑
    dev.d = pk
    t0 = time.time()
    for part, col, target in ((0, pk.cols[0], "09"), (1, pk.cols[1], "45")):
        WheelColumnDriver(col, dev._read_ocr, pk.drag, log=print).set(target)
        assert same(f"{pk.pos[part]:02d}", target), (pk.pos, target)
    pk.screenshot().save(Path(__file__).with_name("ocr_wheel_result.png"))
    print(f"✓ OCR 滚轮拨到 {pk.pos[0]:02d}:{pk.pos[1]:02d}，耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

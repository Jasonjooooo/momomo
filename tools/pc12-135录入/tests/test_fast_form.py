"""用模拟的云执照表单页测试快速模式（adb 点击 + 输入法广播）：
字段超出屏幕需要滚动、标题栏遮挡、预填内容需清空、职位下拉弹窗、中文备注、结束时切回原输入法"""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filler  # noqa: E402

W, H, HEADER = 1260, 2720, 338
ROW = 145
FIELDS = ["AIRNUM", "AIRMODEL", "AIRREGNUM", "TAKEADD", "ARRVADD", "JOB_dummy", "YIBIAOTYPE",
          "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "ZHUOLUZHOUJIAN", "ZHUOLUYEJIAN", "REMARK", "subbtn"]
LABELS = {"AIRNUM": "航班号", "AIRMODEL": "航空器型号*", "AIRREGNUM": "航空器登记号*", "TAKEADD": "起飞站",
          "ARRVADD": "降落站", "JOB_dummy": "职位", "ZHUOLUYEJIAN": "夜间", "REMARK": "备注"}
JOBS = ["请选择(Please Select)", "机长(PIC)", "副驾驶(SIC)", "学员(Student)", "教员(Instructor)"]


class FakeForm:
    def __init__(self):
        self.values = {f: "" for f in FIELDS}
        self.values["AIRMODEL"] = "PC-12"
        self.values["JOB_dummy"] = JOBS[0]
        self.scroll = 0
        self.focus = None
        self.dialog = False
        self.saved = False
        self.ime = "com.huawei.ohos.inputmethod/.SoftKeyboard"
        self.cmds = 0
        self.page = "form"   # form | list
        self.saved_rows = []
        self.plus_taps = 0

    def window_size(self): return W, H

    def y_of(self, f):
        return 500 + FIELDS.index(f) * ROW - self.scroll

    def dump_hierarchy(self, compressed=False):
        if self.page == "dialog":
            return ('<hierarchy><node class="android.widget.FrameLayout" text="" bounds="[0,0][1260,2720]">'
                    '<node class="android.widget.TextView" text="提示" bounds="[170,1200][330,1290]"/>'
                    '<node class="android.widget.TextView" text="提交成功" bounds="[170,1320][420,1400]"/>'
                    '<node class="android.widget.Button" text="关闭" bounds="[920,1560][1130,1670]"/>'
                    '</node></hierarchy>')
        if self.page == "list":
            return ('<hierarchy><node class="android.webkit.WebView" text="" bounds="[0,130][1260,2720]">'
                    '<node class="android.view.View" text="飞行记录" bounds="[400,180][860,290]"/>'
                    '<node class="android.view.View" text="" bounds="[1120,180][1230,290]"/>'
                    '</node></hierarchy>')
        out = ['<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">']
        for f in FIELDS:
            y = self.y_of(f)
            t, b = max(y, HEADER), min(y + 95, H)
            if b <= HEADER or t >= H:
                continue  # WebView 只暴露可见的控件
            cls = "android.widget.Button" if f == "subbtn" else "android.widget.EditText"
            txt = "保\xa0\xa0存" if f == "subbtn" else self.values[f]
            if f in LABELS:
                out.append(f'<node class="android.view.View" text="{LABELS[f]}" bounds="[53,{t}][617,{b}]"/>')
            # 真机现象：起飞站处于输入状态时，降落站输入框从界面数据里消失
            if f == "ARRVADD" and self.focus == "TAKEADD":
                continue
            # 真机现象：登记号处于输入状态时，起飞站整行（含标签）都从界面数据里消失
            if f == "TAKEADD" and self.focus == "AIRREGNUM":
                out.pop()  # 去掉刚加的标签
                continue
            if f == "subbtn" and self.focus == "REMARK":  # 备注输入中，保存按钮从界面数据消失
                continue
            if f == "JOB_dummy":  # 隐藏的 <select>，只有 11px
                out.append(f'<node class="android.view.View" resource-id="JOB" text="{txt}" bounds="[613,{t + 60}][624,{t + 71}]"/>')
            out.append(f'<node class="{cls}" resource-id="{f}" text="{txt}" bounds="[620,{t}][1176,{b}]" scrollable="false"/>')
        if self.dialog:
            out.append('<node class="android.widget.ListView" scrollable="true" bounds="[100,900][1160,1700]">')
            for i, j in enumerate(JOBS):
                out.append(f'<node class="android.widget.CheckedTextView" text="{j}" bounds="[100,{900 + i * 150}][1160,{1040 + i * 150}]"/>')
            out.append("</node>")
        out.append("</node></hierarchy>")
        return "".join(out)

    def at(self, x, y):
        for f in FIELDS:
            t = self.y_of(f)
            if t <= y <= t + 95 and 620 <= x <= 1176 and y >= HEADER:
                return f
        return None

    def shell(self, cmd):
        self.cmds += 1
        if cmd.startswith("settings get secure default_input_method"):
            return SimpleNamespace(output=self.ime + "\n")
        m = re.match(r"ime set (\S+)", cmd)
        if m:
            self.ime = m.group(1)
        m = re.match(r"input tap (\d+) (\d+)", cmd)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            if self.page == "dialog":
                if 900 < x < 1150 and 1550 < y < 1680:  # 「关闭」
                    self.page = "list"
                return SimpleNamespace(output="")
            if self.page == "list":
                if 1060 < x < 1200 and 310 < y < 460:  # 「飞行经历」行右侧的「+」（真机位置）
                    self.plus_taps += 1
                    self.__init_form()
                return SimpleNamespace(output="")
            if self.dialog:
                i = (y - 900) // 150
                if 0 <= i < len(JOBS):
                    self.values["JOB_dummy"] = JOBS[i]
                self.dialog = False
                return SimpleNamespace(output="")
            f = self.at(x, y)
            assert f is not None, f"点到了空白处 ({x},{y})，被遮挡或位置不对"
            if f == "JOB_dummy":
                self.dialog = True
            elif f == "subbtn":
                self.saved = True
                self.saved_rows.append(dict(self.values))
                self.page = "dialog"   # 真机：弹出「提示 / 提交成功 / 关闭」
            else:
                self.focus = f
        if cmd.startswith("input keyevent KEYCODE_MOVE_END"):
            n = cmd.count("KEYCODE_DEL")
            self.values[self.focus] = self.values[self.focus][:-n] if n < len(self.values[self.focus]) else ""
        ms = re.findall(r"input motionevent (DOWN|UP) (-?\d+) (-?\d+)", cmd)
        if ms:
            y0, y1 = int(ms[0][2]), int(ms[-1][2])
            self.scroll = max(0, min(2000, self.scroll + (y0 - y1)))
        return SimpleNamespace(output="")

    def __init_form(self):
        keep = (self.saved_rows, self.plus_taps, self.ime, self.cmds)
        self.__init__()
        self.saved_rows, self.plus_taps, self.ime, self.cmds = keep

    def screenshot(self):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (W, H), "white")
        dr = ImageDraw.Draw(img)
        f = ImageFont.truetype("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 46)
        dr.rectangle((0, 0, W, HEADER), fill="#1a3a8a")
        if self.page == "dialog":
            dr.text((250, 1360), "提交成功", font=f, fill="#000"); dr.text((1020, 1610), "关闭", font=f, fill="#099", anchor="mm")
            return img
        if self.page == "list":
            dr.text((630, 235), "记录本", font=f, fill="white", anchor="mm"); dr.text((1127, 380), "+", font=f, fill="#333", anchor="mm")
            return img
        sb = self.y_of("subbtn")
        if HEADER < sb < H - 95:
            dr.text((900, sb + 20), "保  存", font=f, fill="#000")
        for fld in FIELDS:
            y = self.y_of(fld)
            if HEADER < y and y + 95 < H and fld in LABELS:
                dr.text((53, y + 20), LABELS[fld], font=f, fill="#000")
                dr.text((800, y + 20), self.values[fld] or "请输入内容", font=f, fill="#888")
        return img

    # uiautomator2 输入法接口
    def set_input_ime(self, enable=True):
        self.ime = "com.github.uiautomator/.AdbKeyboard"

    def send_keys(self, text):
        assert self.ime.endswith("AdbKeyboard"), "未切换到输入法就输入"
        self.values[self.focus] += text

    def _broadcast(self, action, extras={}):
        return SimpleNamespace(code=-1, data="success")


def make():
    dev = object.__new__(filler.Device)
    dev.d = FakeForm()
    dev.timeout, dev.max_scrolls, dev.step_delay, dev.fast, dev._orig_ime = 1, 6, 0, True, None
    return dev


def test_fill_and_select():
    dev = make()
    row = {"航班号": "TEST01", "航空器型号": "C208", "登记号": "B-1234", "起飞站": "ZBAA",
           "降落站": "ZSSS", "职位": "机长", "夜间": "", "备注": "测试备注"}
    steps = [
        {"fill": {"id": "AIRNUM", "label": "航班号", "column": "航班号"}},
        {"fill": {"id": "AIRMODEL", "label": "航空器型号", "column": "航空器型号"}},
        {"fill": {"id": "AIRREGNUM", "label": "航空器登记号", "column": "登记号"}},
        {"fill": {"id": "TAKEADD", "label": "起飞站", "column": "起飞站"}},
        {"fill": {"id": "ARRVADD", "label": "降落站", "column": "降落站"}},
        {"select": {"id": "JOB_dummy", "label": "职位", "column": "职位"}},
        {"fill": {"id": "ZHUOLUYEJIAN", "label": "夜间", "column": "夜间"}},
        {"fill": {"id": "REMARK", "label": "备注", "column": "备注"}},
        {"hide_keyboard": None},
        {"click": {"id": "subbtn", "ocr": "保存"}},
        {"wait_for": {"text": "提交成功", "timeout": 3}},
        {"click": {"text": "关闭"}},
    ]
    filler.run_steps(dev, steps, row, interactive=False)
    v = dev.d.values
    assert (v["AIRNUM"], v["AIRMODEL"], v["AIRREGNUM"], v["TAKEADD"], v["ARRVADD"]) == ("TEST01", "C208", "B-1234", "ZBAA", "ZSSS"), v
    assert v["JOB_dummy"] == "机长(PIC)", v["JOB_dummy"]
    assert v["REMARK"] == "测试备注" and v["ZHUOLUYEJIAN"] == ""
    assert dev.d.saved, "没有点到保存（在页面最底部，需要滚动）"
    dev.restore()
    assert dev.d.ime == "com.huawei.ohos.inputmethod/.SoftKeyboard", dev.d.ime
    print(f"✓ 快速填写整页（共 {dev.d.cmds} 条 adb 命令），结束后已切回原输入法")


def test_multi_rows():
    """连续两条：第一条已在新增页（不点+）；备注后保存按钮从界面数据消失，靠截图文字点保存；
    保存后回列表页，点右上角+进入下一条"""
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    keep = lambda steps: [st for st in steps if next(iter(st)) not in ("date", "wheel", "select")]  # noqa: E731
    dev = make()
    dev.d.page = "form"
    rows = [{"航班号": "A1", "航空器登记号": "B-1", "起飞站": "ZBAA", "降落站": "ZSSS", "备注": "第一条",
             "昼间着陆次数": "1", "夜间着陆次数": ""},
            {"航班号": "A2", "航空器登记号": "B-2", "起飞站": "ZSSS", "降落站": "ZBAA", "备注": "第二条",
             "昼间着陆次数": "2", "夜间着陆次数": ""}]
    for row in rows:
        filler.run_steps(dev, cfg["before_each"], row, False)
        filler.run_steps(dev, keep(cfg["record_steps"]), row, False)
        filler.run_steps(dev, cfg["submit_steps"], row, False)
    saved = dev.d.saved_rows
    assert [r["AIRNUM"] for r in saved] == ["A1", "A2"], saved
    assert [r["REMARK"] for r in saved] == ["第一条", "第二条"]
    assert saved[1]["AIRMODEL"] == "PC-12" and dev.d.plus_taps == 1, (saved[1]["AIRMODEL"], dev.d.plus_taps)
    dev.restore()
    print("✓ 连续两条：保存 → 回列表 → 点「+」→ 第二条，两条都已保存")


if __name__ == "__main__":
    test_fill_and_select()
    test_multi_rows()

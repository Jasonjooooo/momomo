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
FIELDS = ["AIRNUM", "AIRMODEL", "AIRREGNUM", "TAKEADD", "ARRVADD", "JOB", "YIBIAOTYPE",
          "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "ZHUOLUZHOUJIAN", "ZHUOLUYEJIAN", "REMARK", "subbtn"]
JOBS = ["请选择(Please Select)", "机长(PIC)", "副驾驶(SIC)", "学员(Student)", "教员(Instructor)"]


class FakeForm:
    def __init__(self):
        self.values = {f: "" for f in FIELDS}
        self.values["AIRMODEL"] = "PC-12"
        self.values["JOB"] = JOBS[0]
        self.scroll = 0
        self.focus = None
        self.dialog = False
        self.saved = False
        self.ime = "com.huawei.ohos.inputmethod/.SoftKeyboard"
        self.cmds = 0

    def window_size(self): return W, H

    def y_of(self, f):
        return 500 + FIELDS.index(f) * ROW - self.scroll

    def dump_hierarchy(self, compressed=False):
        out = ['<hierarchy><node class="android.webkit.WebView" text="" scrollable="true" bounds="[0,130][1260,2720]">']
        for f in FIELDS:
            y = self.y_of(f)
            t, b = max(y, HEADER), min(y + 95, H)
            if b <= HEADER or t >= H:
                continue  # WebView 只暴露可见的控件
            cls = "android.widget.Button" if f == "subbtn" else "android.widget.EditText"
            txt = "保\xa0\xa0存" if f == "subbtn" else self.values[f]
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
            if self.dialog:
                i = (y - 900) // 150
                if 0 <= i < len(JOBS):
                    self.values["JOB"] = JOBS[i]
                self.dialog = False
                return SimpleNamespace(output="")
            f = self.at(x, y)
            assert f is not None, f"点到了空白处 ({x},{y})，被遮挡或位置不对"
            if f == "JOB":
                self.dialog = True
            elif f == "subbtn":
                self.saved = True
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
        {"fill": {"id": "AIRNUM", "column": "航班号"}},
        {"fill": {"id": "AIRMODEL", "column": "航空器型号"}},
        {"fill": {"id": "AIRREGNUM", "column": "登记号"}},
        {"fill": {"id": "TAKEADD", "column": "起飞站"}},
        {"fill": {"id": "ARRVADD", "column": "降落站"}},
        {"select": {"id": "JOB", "column": "职位"}},
        {"fill": {"id": "ZHUOLUYEJIAN", "column": "夜间"}},
        {"fill": {"id": "REMARK", "column": "备注"}},
        {"hide_keyboard": None},
        {"click": {"id": "subbtn"}},
    ]
    filler.run_steps(dev, steps, row, interactive=False)
    v = dev.d.values
    assert (v["AIRNUM"], v["AIRMODEL"], v["AIRREGNUM"], v["TAKEADD"], v["ARRVADD"]) == ("TEST01", "C208", "B-1234", "ZBAA", "ZSSS"), v
    assert v["JOB"] == "机长(PIC)", v["JOB"]
    assert v["REMARK"] == "测试备注" and v["ZHUOLUYEJIAN"] == ""
    assert dev.d.saved, "没有点到保存（在页面最底部，需要滚动）"
    dev.restore()
    assert dev.d.ime == "com.huawei.ohos.inputmethod/.SoftKeyboard", dev.d.ime
    print(f"✓ 快速填写整页（共 {dev.d.cmds} 条 adb 命令），结束后已切回原输入法")


if __name__ == "__main__":
    test_fill_and_select()

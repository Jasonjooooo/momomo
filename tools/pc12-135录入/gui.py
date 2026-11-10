#!/usr/bin/env python3
"""
pc12-135录入 —— 图形界面（双击「启动 pc12-135录入.command」打开）

不需要敲命令：选表格 → 检测手机 → 开始录入。
界面只是外壳，实际录入仍由 filler.py 完成（以子进程运行，输出实时显示在下方）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
APP = "pc12-135录入"
SUBTITLE = "云执照 · PC-12 · 135飞机单发陆地-航班飞行"
REPO = "jasonjooooo/momomo"
BRANCH = "claude/eloquent-goldberg-y2f16h"
REMOTE_DIR = "tools/pc12-135录入"
UPDATE_FILES = ["filler.py", "wheel_picker.py", "report_to_excel.py", "gui.py", "config.yaml",
                "requirements.txt", "README.md", "任务书转换规则.md", "flights_template.xlsx",
                "启动 pc12-135录入.command"]
STATE_FILE = HERE / ".gui_state.json"
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def child_env() -> dict:
    """子进程环境：补上 adb 常见安装位置（双击启动时 PATH 里可能没有）"""
    env = dict(os.environ)
    extra = [str(Path.home() / "Downloads" / "platform-tools"), "/opt/homebrew/bin", "/usr/local/bin"]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(d: dict):
    try:
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue = queue.Queue()
        self.rows: list[dict] = []
        self.current = None          # 正在录入的行（从 0 数）
        self.state = load_state()
        root.title(APP)
        root.geometry("920x760")
        root.minsize(780, 620)
        self._build()
        path = self.state.get("excel")
        if path and Path(path).exists():
            self.load_excel(path)
        root.after(100, self._poll)
        root.after(300, self.check_phone)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ 界面
    def _build(self):
        r = self.root
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("", 15, "bold"), padding=(18, 10))
        style.configure("Title.TLabel", font=("", 20, "bold"))
        style.configure("Sub.TLabel", foreground="#666")
        style.configure("Treeview", rowheight=26)

        # 工具栏固定在窗口底部（先 pack，窗口变矮时也不会被挤掉）
        f5 = ttk.Frame(r, padding=(16, 4, 16, 10))
        f5.pack(side="bottom", fill="x")
        for text, cmd in (("首次安装 / 修复环境", self.install_deps), ("初始化手机", self.init_phone),
                          ("检查更新", self.update), ("打开出错截图", self.open_shots)):
            ttk.Button(f5, text=text, command=cmd).pack(side="left", padx=(0, 8))

        head = ttk.Frame(r, padding=(16, 12, 16, 4))
        head.pack(fill="x")
        ttk.Label(head, text=APP, style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  " + SUBTITLE, style="Sub.TLabel").pack(side="left", pady=(8, 0))

        # ① 手机
        f1 = ttk.LabelFrame(r, text=" ① 手机 ", padding=10)
        f1.pack(fill="x", padx=16, pady=6)
        self.phone_dot = tk.Label(f1, text="●", fg="#999", font=("", 18))
        self.phone_dot.pack(side="left")
        self.phone_lbl = ttk.Label(f1, text="正在检测…")
        self.phone_lbl.pack(side="left", padx=6)
        ttk.Button(f1, text="重新检测", command=self.check_phone).pack(side="right")

        # ② 表格
        f2 = ttk.LabelFrame(r, text=" ② 航班表格 ", padding=10)
        f2.pack(fill="both", expand=True, padx=16, pady=6)
        top = ttk.Frame(f2)
        top.pack(fill="x")
        ttk.Button(top, text="选择 Excel 表格…", command=self.pick_excel).pack(side="left")
        self.file_lbl = ttk.Label(top, text="还没有选择表格", style="Sub.TLabel")
        self.file_lbl.pack(side="left", padx=10)
        ttk.Button(top, text="空白模板", command=self.open_template).pack(side="right")

        cols = ("no", "date", "flight", "route", "dep", "arr", "pos", "app", "status")
        names = ("#", "日期", "航班号", "航段", "出发", "到达", "职位", "进近", "状态")
        widths = (36, 96, 90, 120, 60, 60, 60, 90, 110)
        tv_frame = ttk.Frame(f2)
        tv_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.tv = ttk.Treeview(tv_frame, columns=cols, show="headings", height=6, selectmode="browse")
        for c, n, w in zip(cols, names, widths):
            self.tv.heading(c, text=n)
            self.tv.column(c, width=w, anchor="center", stretch=c in ("route", "status"))
        sb = ttk.Scrollbar(tv_frame, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("run", "#fff4cc"), ("ok", "#e3f6e3"), ("err", "#fde2e2")):
            self.tv.tag_configure(tag, background=color)
        self.tv.bind("<Double-1>", self._start_from_selected)

        # ③ 录入
        f3 = ttk.LabelFrame(r, text=" ③ 录入 ", padding=10)
        f3.pack(fill="x", padx=16, pady=6)
        ttk.Label(f3, text="从第").pack(side="left")
        self.start_var = tk.IntVar(value=1)
        self.start_sb = ttk.Spinbox(f3, from_=1, to=999, width=4, textvariable=self.start_var)
        self.start_sb.pack(side="left", padx=4)
        ttk.Label(f3, text="条开始，录").pack(side="left")
        self.count_var = tk.StringVar(value="全部")
        self.count_cb = ttk.Combobox(f3, width=5, textvariable=self.count_var,
                                     values=["全部", "1", "2", "3", "5", "10"])
        self.count_cb.pack(side="left", padx=4)
        ttk.Label(f3, text="条").pack(side="left")
        self.btn_stop = ttk.Button(f3, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="right", padx=(8, 0))
        self.btn_start = ttk.Button(f3, text="▶ 开始录入", style="Big.TButton", command=self.start)
        self.btn_start.pack(side="right")
        ttk.Label(r, text="开始前：手机解锁，打开云执照，停在「记录本」列表页。录入时不要碰手机。",
                  style="Sub.TLabel").pack(anchor="w", padx=20)

        # 日志
        f4 = ttk.LabelFrame(r, text=" 运行记录 ", padding=6)
        f4.pack(fill="both", expand=True, padx=16, pady=6)
        self.log = tk.Text(f4, height=8, wrap="word", font=("Menlo", 11), state="disabled",
                           bg="#1e1e1e", fg="#ddd", insertbackground="#ddd")
        lsb = ttk.Scrollbar(f4, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        for tag, color in (("ok", "#7ee787"), ("err", "#ff7b72"), ("info", "#79c0ff")):
            self.log.tag_configure(tag, foreground=color)


    # ------------------------------------------------------------------ 表格
    def pick_excel(self):
        p = filedialog.askopenfilename(title="选择航班表格", initialdir=str(HERE),
                                       filetypes=[("Excel / CSV", "*.xlsx *.csv"), ("所有文件", "*.*")])
        if p:
            self.load_excel(p)

    def load_excel(self, path: str):
        sys.path.insert(0, str(HERE))
        try:
            import filler
            rows = filler.load_rows(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP, f"读取表格失败：\n{e}")
            return
        self.excel = path
        self.rows = rows
        self.file_lbl.configure(text=f"{Path(path).name}　共 {len(rows)} 条", style="TLabel")
        self.tv.delete(*self.tv.get_children())
        for i, r in enumerate(rows):
            g = r.get
            self.tv.insert("", "end", iid=str(i), values=(
                i + 1, g("日期", ""), g("航班号", ""), f"{g('起飞站', '')}→{g('降落站', '')}",
                g("出发时刻", ""), g("到达时刻", ""), g("职位", "") or "机长", g("进近方式", "") or "ILS CAT1", "待录入"))
        self.start_sb.configure(to=max(1, len(rows)))
        # 恢复同一个表格上次的进度
        prog = self.state.get("progress", {}).get(str(Path(path).resolve()))
        if prog:
            for i in range(min(prog, len(rows))):
                self._set_status(i, "✓ 已录入", "ok")
            if prog < len(rows):
                self.start_var.set(prog + 1)
                self._log(f"这个表格上次录到第 {prog} 条，已自动设为从第 {prog + 1} 条开始。", "info")
        else:
            self.start_var.set(1)
        self.state["excel"] = path
        save_state(self.state)

    def _set_status(self, i: int, text: str, tag: str = ""):
        if 0 <= i < len(self.rows) and self.tv.exists(str(i)):
            vals = list(self.tv.item(str(i), "values"))
            vals[-1] = text
            self.tv.item(str(i), values=vals, tags=(tag,) if tag else ())
            self.tv.see(str(i))

    def _start_from_selected(self, _e=None):
        sel = self.tv.selection()
        if sel:
            self.start_var.set(int(sel[0]) + 1)

    def open_template(self):
        t = HERE / "flights_template.xlsx"
        if t.exists():
            subprocess.Popen(["open", str(t)] if sys.platform == "darwin" else ["xdg-open", str(t)])

    def open_shots(self):
        d = HERE / "screenshots"
        d.mkdir(exist_ok=True)
        subprocess.Popen(["open", str(d)] if sys.platform == "darwin" else ["xdg-open", str(d)])

    # ------------------------------------------------------------------ 手机
    def check_phone(self):
        self.phone_dot.configure(fg="#999")
        self.phone_lbl.configure(text="正在检测…")

        def work():
            adb = shutil.which("adb", path=child_env()["PATH"])
            if not adb:
                self.q.put(("phone", "noadb", ""))
                return
            try:
                out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15, env=child_env()).stdout
            except Exception as e:  # noqa: BLE001
                self.q.put(("phone", "err", str(e)))
                return
            lines = [l.split("\t") for l in out.splitlines()[1:] if "\t" in l]
            ok = [l[0] for l in lines if l[1] == "device"]
            if ok:
                model = ""
                try:
                    model = subprocess.run([adb, "-s", ok[0], "shell", "getprop", "ro.product.model"], capture_output=True,
                                           text=True, timeout=10, env=child_env()).stdout.strip()
                except Exception:
                    pass
                self.q.put(("phone", "ok", model or ok[0]))
            elif any(l[1] == "unauthorized" for l in lines):
                self.q.put(("phone", "unauth", ""))
            else:
                self.q.put(("phone", "none", ""))
        threading.Thread(target=work, daemon=True).start()

    def _show_phone(self, st, info):
        msgs = {
            "ok": ("#2da44e", f"已连接：{info}"),
            "unauth": ("#d29922", "手机上还没点「允许 USB 调试」，请在手机上点允许后重新检测"),
            "none": ("#cf222e", "没检测到手机：请插好数据线、解锁手机，确认开发人员选项里「USB 调试」已打开"),
            "noadb": ("#cf222e", "电脑上没找到 adb，请先按说明安装 platform-tools（放在「下载」文件夹）"),
            "err": ("#cf222e", f"检测出错：{info}"),
        }
        color, text = msgs[st]
        self.phone_dot.configure(fg=color)
        self.phone_lbl.configure(text=text)
        self.phone_ok = st == "ok"

    # ------------------------------------------------------------------ 运行
    def _run(self, args: list[str], title: str, on_done=None, parse=False):
        if self.proc:
            messagebox.showinfo(APP, "正在运行中，请等它结束或点「停止」。")
            return
        self._log(f"\n===== {title} =====", "info")
        try:
            self.proc = subprocess.Popen(args, cwd=str(HERE), env=child_env(), stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace", bufsize=1)
        except Exception as e:  # noqa: BLE001
            self._log(f"启动失败：{e}", "err")
            self.proc = None
            return
        self._busy(True)
        self._parse = parse

        def reader(p=self.proc):
            for line in p.stdout:
                self.q.put(("line", line.rstrip("\n"), None))
            p.wait()
            self.q.put(("exit", p.returncode, on_done))
        threading.Thread(target=reader, daemon=True).start()

    def _busy(self, busy: bool):
        self.btn_start.configure(state="disabled" if busy else "normal")
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def start(self):
        if not self.rows:
            messagebox.showinfo(APP, "请先点「选择 Excel 表格…」选择要录入的表格。")
            return
        if not getattr(self, "phone_ok", False):
            if not messagebox.askyesno(APP, "还没检测到手机，仍然开始吗？"):
                return
        try:
            start = int(self.start_var.get())
        except (tk.TclError, ValueError):
            start = 1
        start = max(1, min(start, len(self.rows)))
        cnt = self.count_var.get().strip()
        n = len(self.rows) - start + 1 if cnt in ("", "全部") else max(1, int(cnt) if cnt.isdigit() else 1)
        n = min(n, len(self.rows) - start + 1)
        if not messagebox.askyesno(APP, f"将录入第 {start} ~ {start + n - 1} 条，共 {n} 条，保存后会真实提交到云执照。\n\n"
                                        "请确认手机停在「记录本」列表页。开始吗？"):
            return
        for i in range(start - 1, start - 1 + n):
            self._set_status(i, "等待", "")
        self.current = None
        args = [sys.executable, "-u", "filler.py", "run", "-d", self.excel, "--start", str(start), "--limit", str(n)]
        self._run(args, f"录入第 {start} ~ {start + n - 1} 条", parse=True)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self._log("正在停止…（会切回手机原来的输入法）", "info")
            try:
                self.proc.send_signal(signal.SIGINT)
            except Exception:
                self.proc.terminate()

    def install_deps(self):
        args = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "rapidocr_onnxruntime", "-i", PIP_MIRROR]
        self._run(args, "安装/修复运行环境（几分钟，请耐心等）",
                  on_done=lambda rc: self._log("环境安装完成 ✓" if rc == 0 else "安装失败，请把上面的记录截图发给技术支持", "ok" if rc == 0 else "err"))

    def init_phone(self):
        self._run([sys.executable, "-u", "-m", "uiautomator2", "init"], "初始化手机（手机上如提示安装 ATX，请点允许）",
                  on_done=lambda rc: self._log("手机初始化完成 ✓" if rc == 0 else "初始化失败，请检查手机连接", "ok" if rc == 0 else "err"))

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(APP, "正在录入，确定要停止并退出吗？"):
                return
            self.stop()
        self.root.destroy()

    # ------------------------------------------------------------------ 更新
    def update(self):
        if self.proc:
            messagebox.showinfo(APP, "请等当前任务结束后再更新。")
            return
        if not messagebox.askyesno(APP, "从网上下载最新版程序吗？（你的表格和截图不会受影响，旧配置会备份为 config.yaml.bak）"):
            return
        self._log("\n===== 检查更新 =====", "info")
        self._busy(True)

        def work():
            ctx = ssl_context()
            try:
                req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{urllib.parse.quote(BRANCH, safe='')}",
                                             headers={"User-Agent": APP, "Accept": "application/vnd.github+json"})
                sha = json.load(urllib.request.urlopen(req, timeout=20, context=ctx))["sha"]
                self.q.put(("line", f"最新版本：{sha[:7]}", None))
                got = 0
                for name in UPDATE_FILES:
                    url = (f"https://raw.githubusercontent.com/{REPO}/{sha}/"
                           f"{urllib.parse.quote(REMOTE_DIR)}/{urllib.parse.quote(name)}")
                    for attempt in range(4):
                        try:
                            data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": APP}),
                                                          timeout=30, context=ctx).read()
                            break
                        except urllib.error.HTTPError as e:
                            if e.code == 404:
                                data = None
                                break
                            if attempt == 3:
                                raise
                        except Exception:
                            if attempt == 3:
                                raise
                    if data is None:
                        continue
                    dst = HERE / name
                    if name == "config.yaml" and dst.exists() and dst.read_bytes() != data:
                        shutil.copy2(dst, HERE / "config.yaml.bak")
                    dst.write_bytes(data)
                    if name.endswith(".command"):
                        dst.chmod(0o755)
                    got += 1
                    self.q.put(("line", f"  已更新 {name}", None))
                self.q.put(("updated", got, sha[:7]))
            except Exception as e:  # noqa: BLE001
                self.q.put(("updfail", str(e), None))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ 消息循环
    def _log(self, text: str, tag: str | None = None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _handle_line(self, line: str):
        st = line.strip()
        if st.startswith("结束") or st.startswith("====="):
            tag = "info"
        else:
            tag = "ok" if "✓" in line else ("err" if ("✗" in line or "出错" in line or "Error" in line) else None)
        self._log(line, tag)
        if not getattr(self, "_parse", False):
            return
        m = re.match(r"^\[(\d+)/(\d+)\]", line.strip())
        if m:
            self.current = int(m.group(1)) - 1
            self._set_status(self.current, "录入中…", "run")
        elif self.current is not None and "✓ 完成" in line:
            self._set_status(self.current, "✓ 已录入", "ok")
            self._save_progress(self.current + 1)
        elif self.current is not None and line.strip().startswith("✗"):
            self._set_status(self.current, "✗ 出错", "err")
            self.start_var.set(self.current + 1)

    def _save_progress(self, done_upto: int):
        prog = self.state.setdefault("progress", {})
        key = str(Path(self.excel).resolve())
        prog[key] = max(prog.get(key, 0), done_upto)
        save_state(self.state)

    def _poll(self):
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "line":
                    self._handle_line(a)
                elif kind == "exit":
                    self.proc = None
                    self._busy(False)
                    if b:
                        b(a)
                    if getattr(self, "_parse", False):
                        self._finish_run()
                    self._parse = False
                elif kind == "phone":
                    self._show_phone(a, b)
                elif kind == "updated":
                    self._busy(False)
                    self._log(f"更新完成（{a} 个文件，版本 {b}）✓  请关闭窗口重新打开以使用新版。", "ok")
                    messagebox.showinfo(APP, "更新完成，请关闭窗口后重新打开。")
                elif kind == "updfail":
                    self._busy(False)
                    self._log(f"更新失败：{a}\n可以稍后再试，或换个网络。", "err")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish_run(self):
        statuses = [self.tv.item(str(i), "values")[-1] for i in range(len(self.rows))]
        ok = sum(s.startswith("✓") for s in statuses)
        err = [i for i, s in enumerate(statuses) if s.startswith("✗")]
        if err:
            i = err[0]
            messagebox.showwarning(APP, f"第 {i + 1} 条出错，已停止。\n\n请在手机上返回「记录本」列表页（放弃那条没保存的记录），"
                                        f"然后直接点「开始录入」，会从第 {i + 1} 条继续。\n\n出错截图可点「打开出错截图」查看。")
        elif self.current is not None:
            messagebox.showinfo(APP, f"录入结束。表格中已录入 {ok} 条。")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

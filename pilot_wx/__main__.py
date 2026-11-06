"""
飞行天气简报

用法：
    WINDY_API_KEY=xxx python -m pilot_wx            # 拉取真实数据
    python -m pilot_wx --mock                        # 用内置模拟数据测试格式
    python -m pilot_wx --no-notify                   # 只生成文件，不推送

输出：
    output/pilot_wx/index.html   离线简报（下载到 iPad/手机后断网可看）
    output/pilot_wx/sms.txt      压缩后的短报文（每行一条，可照着用北斗发送）

⚠️ Windy 为数值模式预报，仅作辅助参考，不能代替官方航空气象资料。
"""

import argparse
import html
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

API_URL = "https://api.windy.com/api/point-forecast/v2"
ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT.parent / "output" / "pilot_wx"

# 气压层 → 近似飞行高度层（标准大气）
LEVEL_FL = {"850h": "FL050", "700h": "FL100", "600h": "FL140", "500h": "FL180",
            "400h": "FL240", "300h": "FL300", "250h": "FL340", "200h": "FL390"}

MS_TO_KT = 1.943844


# ───────────────────────────── 数据获取 ─────────────────────────────

def fetch_point(point: dict, model: str, levels: list[str], key: str) -> dict:
    """调用 Windy Point Forecast API，返回原始 JSON。"""
    body = {
        "lat": point["lat"],
        "lon": point["lon"],
        "model": model,
        "parameters": ["wind", "windGust", "temp", "dewpoint", "rh",
                       "precip", "convPrecip", "cape", "lclouds", "mclouds", "hclouds"],
        "levels": ["surface", *levels],
        "key": key,
    }
    resp = requests.post(API_URL, json=body, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Windy API {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def mock_point(point: dict, levels: list[str]) -> dict:
    """生成模拟数据，用于没有 Key 时测试输出格式。"""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    ts = [int((now + timedelta(hours=3 * i)).timestamp() * 1000) for i in range(8)]
    seed = int(abs(point["lat"] * 100 + point["lon"]))
    n = len(ts)

    def series(base, amp, phase=0.0):
        return [base + amp * math.sin(i / 2 + seed % 7 + phase) for i in range(n)]

    data = {
        "ts": ts,
        "wind_u-surface": series(-6, 4), "wind_v-surface": series(-2, 3, 1),
        "gust-surface": series(13, 5),
        "temp-surface": series(276, 5), "dewpoint-surface": series(270, 3),
        "past3hprecip-surface": [max(0.0, v) for v in series(0.0002, 0.0012)],
        "past3hconvprecip-surface": [max(0.0, v) for v in series(-0.0002, 0.001)],
        "cape-surface": [max(0.0, v) for v in series(300, 900)],
        "lclouds-surface": [min(100, max(0, v)) for v in series(50, 45)],
        "mclouds-surface": [min(100, max(0, v)) for v in series(40, 40, 2)],
        "hclouds-surface": [min(100, max(0, v)) for v in series(30, 30, 3)],
    }
    for lv in levels:
        p = int(lv.rstrip("h"))
        data[f"temp-{lv}"] = series(288 - (1000 - p) * 0.065 * 1.1, 2)
        data[f"wind_u-{lv}"] = series(10 + (1000 - p) / 20, 5)
        data[f"wind_v-{lv}"] = series(2, 4)
        data[f"rh-{lv}"] = [min(100, max(0, v)) for v in series(70, 25, 1.5)]
    return data


# ───────────────────────────── 数据处理 ─────────────────────────────

def wind(u, v):
    """u/v (m/s) → (风向度, 风速 kt)。气象风向：风的来向。"""
    if u is None or v is None:
        return None, None
    spd = math.hypot(u, v) * MS_TO_KT
    direction = (270 - math.degrees(math.atan2(v, u))) % 360
    direction = int(round(direction / 10.0)) * 10 % 360 or 360
    return direction, int(round(spd))


def k_to_c(k):
    return None if k is None else k - 273.15


def get(data, name, i):
    arr = data.get(name)
    if not arr or i >= len(arr):
        return None
    return arr[i]


def analyze(data: dict, levels: list[str], tz: ZoneInfo, hours_ahead: int, step_hours: int) -> list[dict]:
    """抽取未来若干时次的关键要素和风险提示。"""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours_ahead)
    rows, last_taken = [], None
    for i, t_ms in enumerate(data.get("ts", [])):
        t = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
        if t < now - timedelta(hours=step_hours) or t > end:
            continue
        if last_taken and t - last_taken < timedelta(hours=step_hours):
            continue
        last_taken = t

        d, s = wind(get(data, "wind_u-surface", i), get(data, "wind_v-surface", i))
        gust = get(data, "gust-surface", i)
        gust_kt = int(round(gust * MS_TO_KT)) if gust is not None else None
        temp = k_to_c(get(data, "temp-surface", i))
        dew = k_to_c(get(data, "dewpoint-surface", i))
        precip = (get(data, "past3hprecip-surface", i) or 0) * 1000  # m → mm
        conv = (get(data, "past3hconvprecip-surface", i) or 0) * 1000
        cape = get(data, "cape-surface", i) or 0
        lcl = get(data, "lclouds-surface", i)
        mcl = get(data, "mclouds-surface", i)
        hcl = get(data, "hclouds-surface", i)

        warns = []
        if cape >= 1000 or conv >= 2:
            warns.append("雷暴风险高")
        elif cape >= 300 or conv >= 0.5:
            warns.append("有对流")
        if (gust_kt or 0) >= 25 or (s or 0) >= 20:
            warns.append("大风")
        if temp is not None and temp <= 1 and precip >= 0.2:
            warns.append("降雪/冻降水")
        if temp is not None and dew is not None and temp - dew <= 2 and (lcl or 0) >= 70:
            warns.append("低云低能见")

        upper = []
        for lv in levels:
            ld, ls = wind(get(data, f"wind_u-{lv}", i), get(data, f"wind_v-{lv}", i))
            lt = k_to_c(get(data, f"temp-{lv}", i))
            rh = get(data, f"rh-{lv}", i)
            icing = lt is not None and rh is not None and -20 <= lt <= 0 and rh >= 85
            if icing:
                warns.append(f"积冰{LEVEL_FL.get(lv, lv)}")
            upper.append({"level": lv, "fl": LEVEL_FL.get(lv, lv), "dir": ld, "spd": ls,
                          "temp": lt, "rh": rh, "icing": icing})

        rows.append({
            "time": t.astimezone(tz), "dir": d, "spd": s, "gust": gust_kt,
            "temp": temp, "dew": dew, "precip": precip, "cape": cape,
            "lcl": lcl, "mcl": mcl, "hcl": hcl, "upper": upper, "warns": warns,
        })
    return rows


# ───────────────────────────── 输出格式 ─────────────────────────────

def fmt_wind(d, s, g=None):
    if d is None or s is None:
        return "--"
    txt = f"{d:03d}/{s:02d}"
    if g is not None and g >= s + 8:
        txt += f"G{g}"
    return txt


def fmt_t(t):
    return "--" if t is None else f"{t:.0f}"


def build_sms(point: dict, rows: list[dict]) -> str:
    """单个点的一行压缩文本，例：ZBHH 08时270/15G25 11时280/18 FL100:260/30/-02 ⚠大风 积冰FL100"""
    if not rows:
        return f"{point['id']} 无数据"
    parts = [point["id"]]
    for r in rows:
        parts.append(f"{r['time']:%H}时{fmt_wind(r['dir'], r['spd'], r['gust'])}")
    first = rows[0]
    for u in first["upper"]:
        parts.append(f"{u['fl']}:{fmt_wind(u['dir'], u['spd'])}/{u['temp']:03.0f}" if u['temp'] is not None else f"{u['fl']}:{fmt_wind(u['dir'], u['spd'])}")
    warns = []
    for r in rows:
        for w in r["warns"]:
            if w not in warns:
                warns.append(w)
    parts.append("⚠" + " ".join(warns) if warns else "无明显风险")
    return " ".join(parts)


def split_sms(text: str, max_len: int) -> list[str]:
    """按空格把长文本切成不超过 max_len 字的多条。"""
    msgs, cur = [], ""
    for word in text.split(" "):
        cand = f"{cur} {word}".strip()
        if len(cand) <= max_len:
            cur = cand
        else:
            if cur:
                msgs.append(cur)
            cur = word[:max_len]
    if cur:
        msgs.append(cur)
    return msgs


def build_html(results: list[dict], cfg: dict, generated: datetime, mock: bool) -> str:
    esc = html.escape
    cards = []
    for res in results:
        p, rows = res["point"], res["rows"]
        if res.get("error"):
            cards.append(f"<section class='card'><h2>{esc(p['id'])} {esc(p.get('name', ''))}</h2>"
                         f"<p class='err'>获取失败：{esc(res['error'])}</p></section>")
            continue
        level_heads = "".join(f"<th>{esc(LEVEL_FL.get(lv, lv))}<br><small>风/温/湿</small></th>"
                              for lv in cfg["levels"])
        trs = []
        for r in rows:
            uppers = "".join(
                f"<td class='{'warn' if u['icing'] else ''}'>{fmt_wind(u['dir'], u['spd'])}<br>"
                f"{fmt_t(u['temp'])}°C / {fmt_t(u['rh'])}%</td>" for u in r["upper"])
            warn = " ".join(r["warns"]) or "—"
            trs.append(
                f"<tr><td><b>{r['time']:%d日 %H:%M}</b></td>"
                f"<td>{fmt_wind(r['dir'], r['spd'], r['gust'])}<br><small>阵风 {r['gust'] or '--'}kt</small></td>"
                f"<td>{fmt_t(r['temp'])}/{fmt_t(r['dew'])}°C</td>"
                f"<td>{fmt_t(r['lcl'])}/{fmt_t(r['mcl'])}/{fmt_t(r['hcl'])}%</td>"
                f"<td>{r['precip']:.1f}mm<br><small>CAPE {r['cape']:.0f}</small></td>"
                f"{uppers}<td class='{'warn' if r['warns'] else 'ok'}'>{esc(warn)}</td></tr>")
        cards.append(
            f"<section class='card'><h2>{esc(p['id'])} <span>{esc(p.get('name', ''))}</span></h2>"
            f"<p class='sms'>{esc(res['sms'])}</p>"
            f"<div class='scroll'><table><thead><tr><th>时间</th><th>地面风</th><th>温/露</th>"
            f"<th>云 低/中/高</th><th>3h降水</th>{level_heads}<th>提示</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table></div></section>")

    banner = "<p class='mock'>⚠ 当前为模拟数据，仅用于测试格式</p>" if mock else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>飞行天气简报</title>
<style>
:root {{ --bg:#f6f7f9; --fg:#1b1f24; --card:#fff; --muted:#5b6470; --line:#dde1e6; --warn:#b42318; --warnbg:#fdecea; --ok:#1a7f37; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --fg:#e6e8eb; --card:#1b1f24; --muted:#9aa3ad; --line:#2c323a; --warn:#ff8a80; --warnbg:#3a1d1b; --ok:#56d364; }} }}
body {{ margin:0; padding:16px; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }}
h1 {{ font-size:20px; margin:0 0 4px; }} .meta {{ color:var(--muted); font-size:13px; margin:0 0 16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:16px; }}
h2 {{ font-size:18px; margin:0 0 6px; }} h2 span {{ color:var(--muted); font-weight:normal; font-size:14px; }}
.sms {{ font-family:ui-monospace,Menlo,monospace; font-size:13px; background:var(--bg); padding:8px; border-radius:6px; word-break:break-all; }}
.scroll {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:13px; }}
th,td {{ border-bottom:1px solid var(--line); padding:6px; text-align:left; white-space:nowrap; vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; }} small {{ color:var(--muted); }}
.warn {{ color:var(--warn); background:var(--warnbg); font-weight:600; }} .ok {{ color:var(--ok); }}
.err,.mock {{ color:var(--warn); font-weight:600; }}
.foot {{ color:var(--muted); font-size:12px; }}
</style></head><body>
<h1>飞行天气简报</h1>
<p class="meta">生成：{generated:%Y-%m-%d %H:%M} ({esc(cfg['timezone'])}) · 模式：{esc(cfg['model'].upper())} · 数据源：Windy Point Forecast</p>
{banner}
{''.join(cards)}
<p class="foot">风向为真向，风速单位 kt；高度层由气压层按标准大气换算，仅为近似。积冰提示为：该层温度 0 ~ -20°C 且相对湿度 ≥85%。
本简报为数值模式预报，仅作辅助参考，不能代替官方航空气象资料、METAR/TAF 及放行讲解。</p>
</body></html>"""


# ───────────────────────────── 推送 ─────────────────────────────

def notify(title: str, text: str) -> None:
    """推送到已配置的渠道（与 TrendRadar 共用同名环境变量），失败不影响主流程。"""
    env = os.environ.get
    senders = []
    if env("BARK_URL"):
        senders.append(("Bark", lambda: requests.post(env("BARK_URL").rstrip("/"),
                                                      json={"title": title, "body": text}, timeout=15)))
    if env("NTFY_TOPIC"):
        server = (env("NTFY_SERVER_URL") or "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("utf-8")}
        if env("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {env('NTFY_TOKEN')}"
        senders.append(("ntfy", lambda: requests.post(f"{server}/{env('NTFY_TOPIC')}",
                                                      data=text.encode("utf-8"), headers=headers, timeout=15)))
    if env("TELEGRAM_BOT_TOKEN") and env("TELEGRAM_CHAT_ID"):
        senders.append(("Telegram", lambda: requests.post(
            f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
            json={"chat_id": env("TELEGRAM_CHAT_ID"), "text": f"{title}\n{text}"}, timeout=15)))
    if env("FEISHU_WEBHOOK_URL"):
        senders.append(("飞书", lambda: requests.post(env("FEISHU_WEBHOOK_URL"), timeout=15, json={
            "msg_type": "text", "content": {"text": f"{title}\n{text}"}})))
    if env("DINGTALK_WEBHOOK_URL"):
        senders.append(("钉钉", lambda: requests.post(env("DINGTALK_WEBHOOK_URL"), timeout=15, json={
            "msgtype": "text", "text": {"content": f"{title}\n{text}"}})))
    if env("WEWORK_WEBHOOK_URL"):
        senders.append(("企业微信", lambda: requests.post(env("WEWORK_WEBHOOK_URL"), timeout=15, json={
            "msgtype": "text", "text": {"content": f"{title}\n{text}"}})))

    if not senders:
        print("ℹ️ 未配置推送渠道，跳过推送")
    for name, send in senders:
        try:
            r = send()
            print(f"{'✅' if r.ok else '❌'} {name} 推送 {r.status_code}")
        except Exception as e:  # noqa: BLE001
            print(f"❌ {name} 推送失败：{e}")


# ───────────────────────────── 主流程 ─────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="飞行天气简报（Windy）")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--mock", action="store_true", help="使用模拟数据")
    ap.add_argument("--no-notify", action="store_true", help="不推送")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    cfg.setdefault("model", "gfs")
    cfg.setdefault("timezone", "Asia/Shanghai")
    cfg.setdefault("hours_ahead", 6)
    cfg.setdefault("step_hours", 3)
    cfg.setdefault("levels", ["700h", "500h"])
    cfg.setdefault("sms_max_len", 70)
    tz = ZoneInfo(cfg["timezone"])

    key = os.environ.get("WINDY_API_KEY", "").strip()
    mock = args.mock or not key
    if not args.mock and not key:
        print("⚠️ 未设置 WINDY_API_KEY，改用模拟数据")

    results, sms_lines = [], []
    for p in cfg["points"]:
        res = {"point": p, "rows": [], "sms": ""}
        try:
            data = mock_point(p, cfg["levels"]) if mock else fetch_point(p, cfg["model"], cfg["levels"], key)
            res["rows"] = analyze(data, cfg["levels"], tz, cfg["hours_ahead"], cfg["step_hours"])
            res["sms"] = build_sms(p, res["rows"])
            sms_lines.extend(split_sms(res["sms"], cfg["sms_max_len"]))
        except Exception as e:  # noqa: BLE001
            res["error"] = str(e)
            sms_lines.append(f"{p['id']} 获取失败")
            print(f"❌ {p['id']}: {e}")
        results.append(res)

    generated = datetime.now(tz)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(build_html(results, cfg, generated, mock), encoding="utf-8")
    (OUT_DIR / "sms.txt").write_text("\n".join(sms_lines) + "\n", encoding="utf-8")

    title = f"飞行天气简报 {generated:%m-%d %H:%M}" + ("（模拟）" if mock else "")
    body = "\n".join(sms_lines)
    print(title)
    print(body)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## {title}\n\n```\n{body}\n```\n\n完整简报见本次运行的 Artifacts：pilot-wx-briefing\n")

    if not args.no_notify:
        notify(title, body + "\n⚠ 模式预报，仅供参考")

    return 1 if all(r.get("error") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

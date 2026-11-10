# pc12-135录入 —— 云执照飞行记录自动录入（PC-12 · 135飞机单发陆地-航班飞行）

把飞行记录整理在 Excel 里，电脑通过 USB 控制手机，自动在「云执照」App 中逐条新增飞行记录。

## 快速使用（已配置好：135飞机单发陆地-航班飞行）

1. 手机连电脑，打开云执照，停在**飞行记录列表页**（或已打开的空白新增页）。
2. 准备 Excel：直接用模板 `flights_template.xlsx`（职位/进近方式有下拉框，第二个工作表是填写说明）。
   第一行表头，列名如下，没有的列可以不写：

   | 列名 | 说明 |
   | --- | --- |
   | 日期 | 2026-11-01（Excel 日期格式也可以） |
   | 航班号、航空器登记号、起飞站、降落站 | 文字 |
   | 航空器型号 | 不填默认 PC-12 |
   | 出发时刻、到达时刻 | 09:30 |
   | 职位 | 不填默认「机长」；可写 副驾驶 / 教员 等（包含即可） |
   | 进近方式 | 不填默认 ILS CAT1；可写 VOR / RNP LNAV / 目视 等 |
   | 夜间飞行经历时间、真实仪表时间、转场时间 | 02:15，留空则不填 |
   | 昼间着陆次数、夜间着陆次数、备注 | 留空则不填 |

   飞行经历时间由 App 自动计算，不用填。

3. 全自动录入（默认不需要按回车）：
   ```bash
   python3.12 filler.py run -d 我的记录.xlsx
   ```
   想每条填完先检查、按回车才保存，加 `--confirm`（或在 config.yaml 设 `confirm_each: true`）。
   每条流程：点右上角「+」→ 填写 → 保存 → 等「提交成功」→ 关闭 → 下一条。
   中途出错会截图到 `screenshots/` 并**停下**（不会把下一条填进半成品页面）；
   在手机上返回列表页放弃那条未保存的记录，再次运行会提示从断点继续。
   结束（包括出错、Ctrl+C）时自动切回手机原来的输入法。

单独测试某一段：`--only before`（进入新增页）/ `--only record`（只填写）/ `--only submit`（只保存）。

---

> 仅支持 **安卓手机**。iPhone 需要 Mac + Appium/WebDriverAgent，本工具暂不支持。
> 工具只模拟你自己的点击和输入，不破解 App、不调用其接口；请确保录入的是真实飞行记录。

## 一、准备（只需一次）

1. 电脑安装 **Python 3.9+**（Windows 安装时勾选 *Add Python to PATH*）。
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 手机开启 **开发者选项 → USB 调试**（设置 → 关于手机 → 连续点「版本号」7 次可打开开发者选项）。
   小米/OPPO/vivo 等还需打开「USB 调试（安全设置）」/「允许模拟点击」。
4. 用数据线连接电脑，手机上点「允许 USB 调试」。
5. 初始化（会在手机上装一个 ATX 小助手，用于读取界面、输入中文）：
   ```bash
   python -m uiautomator2 init
   ```

## 二、整理数据

```bash
python filler.py template -o flights.csv
```

用 Excel 打开 `flights.csv` 填写，第一行是表头（列名可以随便改，只要和 `config.yaml` 里的 `column` 对应），保存为 `.xlsx` 或 `.csv` 都可以。也可以直接用你现有的飞行记录表。

## 三、按你的 App 界面调整配置（最关键）

各版本 App 的按钮文字、字段名可能不同，`config.yaml` 里的是示例：

1. 手机打开云执照 → 进入「新增飞行记录」页面。
2. 运行：
   ```bash
   python filler.py dump
   ```
   会打印出当前屏幕上所有的文字和输入框。
3. 对照输出修改 `config.yaml`：
   - `before_each`：进入新增页面要点的按钮（如「添加记录」）
   - `record_steps`：每个字段用 `fill`（输入框）或 `select`（点开后选择）
   - `submit_steps`：提交按钮文字
   - 日期 / 时间滚轮用 `wheel`（见下一节）
   - 地图选点这类难以自动操作的控件，用 `manual` 暂停让你手动点一下，其余字段仍自动填写

配置里每种写法都有注释说明。

## 三-B、日期 / 时间滚轮

```yaml
- wheel: {open: {label: "飞行日期"}, column: "飞行日期"}   # "2026-11-01" → 年/月/日三列
- wheel: {open: {label: "起飞时间"}, column: "起飞时间"}   # "09:30" → 时/分两列
```

工作方式：点开滚轮 → 自动识别每一列 → 读出当前值 → 计算差几格，用「按住慢拖、停顿、松手」的方式拖动（不会甩飞）→ 反复校正直到选中目标 → 点「确定」。

- **读取当前值**：先读界面文字；读不到（App 用自绘滚轮）时自动截图 OCR，需额外安装：
  ```bash
  pip install rapidocr_onnxruntime
  ```
- **单独调试滚轮**：手机上先把滚轮打开，然后运行：
  ```bash
  python filler.py wheel "09:30" --confirm 确定
  ```
- 滚轮是「年月日时分」一个整体时，用 `wheels` 指定每段对应第几列，例如只填时间：`wheels: [3, 4]`。
- 自动识别的区域不对时，`dump` 会列出可滚动区域的位置，填到 `area: [x1, y1, x2, y2]`。
- 分钟滚轮如果是 5 分钟一格，数据里的时间也要是 5 的倍数，否则会提示「滚不到」。

## 四、开始录入

先预览，不连手机，检查数据和步骤是否对得上：

```bash
python filler.py run -d flights.xlsx --dry-run
```

先录 1 条试试（默认是**逐条确认**：填好后你在手机上看一眼，回车才提交）：

```bash
python filler.py run -d flights.xlsx --limit 1
```

确认没问题后，全自动批量录入：

```bash
python filler.py run -d flights.xlsx --auto
```

- 中途退出后再次运行，会提示从上次的位置继续；也可以用 `--start 8` 指定从第 8 条开始。
- 某条出错时会自动截图到 `screenshots/`，逐条模式下可选择重试 / 跳过 / 手动完成。
- 手机较慢时，把 `config.yaml` 里的 `step_delay`、`find_timeout` 调大。
- 无线连接：`adb tcpip 5555` 后用 `python filler.py -s 手机IP:5555 run ...`。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到控件 | 用 `dump` 看实际文字，改 `text` / `label`；字段在屏幕下方时会自动滚动查找 |
| 输入框填错位置 | 改用 `hint: "请输入xxx"` 或 `id:` 精确定位 |
| 中文输入不了 | 重新执行 `python -m uiautomator2 init`，并允许其输入法 |
| 选项选不中 | 用 `map:` 把数据里的值换成 App 里显示的选项文字 |
| App 页面是 H5/小程序，dump 看不到文字 | 用 `input_index`，或该字段用 `manual` |
| 滚轮拨不准 / 读不到 | 安装 OCR；调大 `settle`；配置 `area`；实在不行设 `initial: now` 按格数盲拨 |
| 滚轮列识别错位 | 用 `wheels` 指定列号，或 `area` + `count` 手动划分 |

## 测试

```bash
python tests/test_wheel.py          # 滚轮算法（模拟循环、倒序、拖动误差等）
python tests/test_device_wheel.py   # 完整流程（模拟手机界面）
python tests/test_ocr_wheel.py      # OCR 识别自绘滚轮（需安装 rapidocr_onnxruntime）
```

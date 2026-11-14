# TG 视频批量二创

从你指定的 Telegram 频道批量下载视频，自动做统一处理：

| 处理 | 说明 | 默认 |
|---|---|---|
| 去片头片尾 | 去掉开头/结尾固定秒数 | 不去 |
| 裁边 | 按比例裁掉画面边缘（去黑边、贴边角标） | 不裁 |
| 局部模糊 | 模糊指定区域（如频道 logo） | 无 |
| 变速 | 声音不变调 | 1.1 倍 |
| 放大 | 轻微放大后居中裁切 | 不放大 |
| 滤镜 | 明亮 / 电影感 / 复古 / 冷色 / 暖色 / 黑白 + 锐化 + 暗角 | 明亮 + 锐化 |
| 画幅 | 9:16 竖屏 / 16:9 横屏 / 保持原比例，空白处用模糊画面填充 | 竖屏 1080×1920 |
| 标题 | 自动取 TG 帖子文字的第一句（去掉链接、话题、表情），或固定文字 | 开 |
| 水印 | 图片 logo 和/或文字，可调透明度和位置 | 无 |
| 片头片尾 | 自动拼接，尺寸不同会自动适配 | 无 |
| 背景音乐 | 单个文件或文件夹随机挑，可调音量 | 无 |

成品保存在 `output/日期/`，同时生成一份 `清单.csv`（可用 Excel 打开），记录每条视频的原频道、原链接、原文和标题。

---

## 一、第一次安装（Mac M1，只需做一次）

打开「终端」（启动台 → 其他 → 终端），一行一行复制运行：

**1. 安装 Homebrew**（已经装过可跳过）
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
装完后按屏幕提示，把那两行 `eval ...` 命令也复制运行一下。

**2. 安装 ffmpeg 和 Python**
```bash
brew install ffmpeg python
```

**3. 把 `tg-remix` 文件夹放到「文稿」里**，然后进入这个文件夹：
```bash
cd ~/Documents/tg-remix
```

**4. 安装依赖**
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**5. 申请 Telegram API**
1. 打开 https://my.telegram.org ，用手机号登录（验证码发到你的 TG 里）
2. 点 **API development tools**，随便填个应用名（如 `myapp`），创建
3. 记下 **api_id**（数字）和 **api_hash**（一串字母数字）

**6. 填写配置**
```bash
cp config.example.yaml config.yaml && open -e config.yaml
```
填写 `api_id`、`api_hash`，在 `channels` 下面写上要下载的频道，保存。其他参数都有中文说明，按需修改。

---

## 二、日常使用

**方法 1：双击运行（推荐）**
双击文件夹里的 `开始运行.command`。
第一次如果提示「无法打开，因为来自身份不明的开发者」：右键点它 → 打开 → 再点「打开」，以后就能直接双击了。

**方法 2：终端命令**
```bash
cd ~/Documents/tg-remix
.venv/bin/python run.py
```

**第一次运行**会要求登录 Telegram：
- 输入手机号（带国家码，如 `+8613800000000`）
- 输入 TG 里收到的验证码
- 如果开了两步验证，再输入密码

登录后会在文件夹里生成 `tg.session`，以后就不用再登录了。

---

## 三、常用命令

| 命令 | 作用 |
|---|---|
| `.venv/bin/python run.py` | 下载新视频 + 处理（最常用） |
| `.venv/bin/python run.py download` | 只下载 |
| `.venv/bin/python run.py process` | 只处理还没处理过的 |
| `.venv/bin/python run.py process --redo` | 改了参数后，全部重新处理 |
| `.venv/bin/python run.py test 某个视频.mp4` | **试效果**：只处理前 15 秒，放到 `output/试效果/`，不影响记录 |
| `.venv/bin/python run.py process --limit 5` | 本次只处理 5 个 |

**建议**：改完参数先用 `test` 试一个视频，满意了再批量跑。

**处理本地视频**：把视频放进 `downloads/` 下任意一个文件夹（比如新建 `downloads/本地/`），运行 `process` 就会一起处理。

---

## 四、文件夹说明

```
tg-remix/
├── 开始运行.command      双击运行
├── config.yaml           你的配置（从 config.example.yaml 复制）
├── tg.session            TG 登录凭证（⚠️ 等于你的账号，不要发给任何人）
├── state.json            记录已下载/已处理，避免重复
├── assets/               放 logo、片头片尾、背景音乐
├── downloads/频道名/      下载的原视频（+ 同名 .json 记录帖子文字和链接）
└── output/日期/           成品 + 清单.csv
```

## 五、常见问题

- **找不到频道**：确认用户名写对；私密频道需要你的账号先加入。
- **某个视频处理失败**：会跳过继续下一个，失败原因显示在窗口里。
- **想重新下载某个频道**：删掉 `state.json` 里对应频道的记录（或整个删掉 `state.json`）。
- **标题显示方框/乱码**：在 `config.yaml` 的 `process.font` 填一个中文字体文件路径，比如 `/System/Library/Fonts/Hiragino Sans GB.ttc`。
- **处理速度**：M1 默认用苹果硬件编码，一般比视频时长快好几倍；如果硬件编码出错会自动改用软件编码。

## ⚠️ 注意

- TG 上很多视频本身就是转载的，发布前请确认版权，注明来源（`清单.csv` 里有原链接）。
- 下载太频繁可能被 Telegram 暂时限速，程序会自动等待；`limit` 不要设得太大。

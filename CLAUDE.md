# 项目说明（给 Claude）

## 云执照飞行记录录入（tools/yunzhizhao-filler/）

用户发来**飞行任务书 / 飞行报告**照片时：
1. 严格按 `tools/yunzhizhao-filler/任务书转换规则.md` 识别并转换（出发=滑出、到达=滑行终止、真实仪表时间=空中时间、转场等）。
2. 用 `tools/yunzhizhao-filler/report_to_excel.py` 生成 Excel：只有「飞行记录」一个工作表、固定 16 列，**不要加其他列或工作表**。
3. 在回复里列出与手写值不一致、看不清或需要确认的地方（如日期冲突、职位）。
4. 生成的 Excel / JSON 含个人信息，**不要提交到仓库**，通过对话发给用户。

录入工具用法见 `tools/yunzhizhao-filler/README.md`（用户是 Mac + 华为 Mate 60 Pro，命令用 `python3.12`）。

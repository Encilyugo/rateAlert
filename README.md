# rateAlert — 汇率波动提醒

GitHub Actions 定时跑的 Python 脚本，监控 NZD/CNY、NZD/USD、USD/CNY 三对汇率，通过 ntfy.sh 和钉钉机器人双渠道推送提醒到手机。完全免费、无需服务器、自用。

## 三类提醒

| 类型 | 触发条件 | 优先级 | 静默时段处理 |
|---|---|---|---|
| 📊 每日简报 | 工作日北京时间 08:00 一条 | 低（priority=2） | 不在静默内，正常推 |
| ⚡ 突然变化 | 任一币种对跨日变化 ≥ 2.0% | 高（priority=5） | 静默时段内排队，次日 7:00 后聚合补推 |
| 🚀 突破半年极值 | 创 180 天滑动窗口新高/新低 | 最高（priority=5+紧急） | 立即推（事件等不到第二天） |

## 部署步骤

### 1. 准备账号和工具

- **GitHub 账号**（github.com）
- **ntfy App**（手机已装并订阅一个长随机 topic 名，比如 `fx-alert-elyu-x7k9m2p4q-2026`）
- **钉钉**：建一个群（可只有自己一人）→ 群设置 → 智能群助手 → 添加机器人 → 自定义 → 拿 webhook URL
  - 安全设置选「自定义关键词」，关键词填 `汇率`（脚本所有推送 title 都含此词）

### 2. 上传到 GitHub

```bash
cd C:\Project\enci\rateAlert
git init
git add .
git commit -m "Initial commit"
# 在 GitHub 网页建一个名为 rateAlert 的 Public 仓库
git remote add origin https://github.com/YOUR_USERNAME/rateAlert.git
git branch -M main
git push -u origin main
```

### 3. 配置 Secrets

仓库页面 → Settings → Secrets and variables → Actions → New repository secret，添加：

- `NTFY_TOPIC` = 你的 ntfy 私密 topic 名（不带 `https://ntfy.sh/`）
- `DINGTALK_WEBHOOK` = 钉钉机器人完整 webhook URL

### 4. 验证

仓库 → Actions → `Check FX Rates` → **Run workflow** → 看运行日志：
- 第一次跑会回填 180 天历史数据，约 10–30 秒
- 每个币种对应输出 `[FETCH]`，无 `ERROR`
- `state.json` 和 `history.json` 应自动 commit 回仓库

## 本地调试

本机已装 Python 3.10+（3.12 / 3.13 都已验证）的话，可以这样不依赖 GitHub 跑一次：

```bash
cd C:\Project\enci\rateAlert
python -m venv .venv
.venv\Scripts\activate     # Windows
pip install -r requirements.txt

# 不推送，只看汇率
python check_rates.py

# 推送也测：先在 PowerShell 设环境变量
$env:NTFY_TOPIC = "你的topic"
$env:DINGTALK_WEBHOOK = "你的钉钉webhook"
python check_rates.py
```

注意：本地跑会修改 `state.json` 和 `history.json`，调试完记得 `git checkout` 撤掉。

## 配置项（脚本顶部常量）

| 常量 | 默认 | 说明 |
|---|---|---|
| `PAIRS` | NZD/CNY、NZD/USD、USD/CNY | 监控的币种对，追加 tuple 即可 |
| `TIMEZONE` | `Asia/Shanghai` | 本地时区。改 NZ 需手动调 cron（NZ 有夏令时） |
| `DAILY_BRIEF_HOUR` | 8 | 每日简报小时（本地时区） |
| `SUDDEN_MOVE_THRESHOLD_PCT` | 2.0 | 跨日变化触发阈值 % |
| `HISTORY_WINDOW_DAYS` | 180 | 极值滑动窗口 |
| `EXTREME_COOLDOWN_DAYS` | 7 | 极值同向冷却天数 |
| `SUDDEN_MOVE_COOLDOWN_HOURS` | 24 | 突然变化冷却小时数 |
| `QUIET_HOURS_START` / `END` | 23 / 7 | 静默时段（本地小时） |

修改方式：直接在 GitHub 网页编辑 `check_rates.py` → commit。

## 故障排查

| 现象 | 排查 |
|---|---|
| Actions 失败 | 看 Run 日志；常见是 secret 没配 |
| 收不到推送 | 检查 ntfy topic 是否一致、钉钉关键词是否含「汇率」、手机是否给 ntfy App 加了电池白名单 |
| ntfy 锁屏收不到（OPPO 等国产 ROM） | 钉钉作为兜底；ntfy App 内开启「常驻通知/Foreground service」、系统设置允许后台高耗电 |
| 历史数据不全 | 删除 `history.json` 让脚本重新回填 |
| 突然变化连续误报 | 增大 `SUDDEN_MOVE_THRESHOLD_PCT` |
| 想暂停整个脚本 | Actions 页面 → workflow 右上角菜单 → Disable workflow |

## 强制触发测试

部署后想确认推送链路是否通畅：

1. 改 `SUDDEN_MOVE_THRESHOLD_PCT = 0.001` 提交 → 手动 Run → 应收到 `⚡ 大幅波动` 推送 → 改回 `2.0`
2. 改 `HISTORY_WINDOW_DAYS = 1` 提交 → 手动 Run → 应收到 `🚀 突破新高/低` 推送 → 改回 `180`

## 文件说明

```
rateAlert/
├── .github/workflows/check.yml    # GitHub Actions cron 配置
├── check_rates.py                  # 主脚本
├── requirements.txt                # Python 依赖
├── state.json                      # 运行时状态（脚本自动维护）
├── history.json                    # 半年历史汇率（脚本自动维护）
├── .gitignore
└── README.md
```

`state.json` 和 `history.json` 由脚本自动维护并 commit 回仓库，不要手动改（除非要重置冷却或回填）。

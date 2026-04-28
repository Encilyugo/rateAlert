"""
汇率波动提醒脚本
- 每日简报：工作日 08:00（本地时区）一条
- 突然变化：跨日波动 ≥ 阈值
- 极值突破：180 天滑动窗口新高/新低
推送渠道：ntfy.sh + 钉钉机器人 双渠道冗余
数据源：Frankfurter（免费、ECB 数据、不要 key）
"""

import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

# ==================== 配置区（用户调这里） ====================

PAIRS = [("NZD", "CNY"), ("NZD", "USD"), ("USD", "CNY")]
TIMEZONE = "Asia/Shanghai"               # 默认北京时间，可改 "Pacific/Auckland"
DAILY_BRIEF_HOUR = 8                     # 每日简报小时（本地时区，工作日 08:00）
DAILY_BRIEF_WINDOW_HOURS = 2             # 简报命中窗口长度（防 cron 抖动）
SUDDEN_MOVE_THRESHOLD_PCT = 0.001          # 跨日变化阈值 %
HISTORY_WINDOW_DAYS = 180                # 极值滑动窗口天数
EXTREME_COOLDOWN_DAYS = 7                # 极值同向冷却天数
SUDDEN_MOVE_COOLDOWN_HOURS = 24          # 突然变化冷却小时数
QUIET_HOURS_START = 23                   # 静默时段开始（本地小时）
QUIET_HOURS_END = 7                      # 静默时段结束（本地小时）

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")

STATE_FILE = Path("state.json")
HISTORY_FILE = Path("history.json")
FRANKFURTER_BASE = "https://api.frankfurter.app"
HTTP_TIMEOUT = 10

# ==================== 工具函数 ====================

def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def today_local_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def pair_key(base: str, quote: str) -> str:
    return f"{base}/{quote}"


def is_quiet_hours() -> bool:
    h = now_local().hour
    if QUIET_HOURS_START <= QUIET_HOURS_END:
        return QUIET_HOURS_START <= h < QUIET_HOURS_END
    return h >= QUIET_HOURS_START or h < QUIET_HOURS_END


def is_brief_window() -> bool:
    h = now_local().hour
    return DAILY_BRIEF_HOUR <= h < DAILY_BRIEF_HOUR + DAILY_BRIEF_WINDOW_HOURS


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def trend_arrow(pct: float) -> str:
    if pct > 0.001:
        return "▲"
    if pct < -0.001:
        return "▼"
    return "━"


# ==================== 数据获取层 ====================

def fetch_rate(base: str, quote: str) -> tuple[float, str]:
    """返回 (rate, date_string)，date 是 Frankfurter 返回的实际数据日期"""
    r = requests.get(
        f"{FRANKFURTER_BASE}/latest",
        params={"from": base, "to": quote},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data["rates"][quote], data["date"]


def fetch_rate_on_date(base: str, quote: str, date_str: str) -> Optional[float]:
    r = requests.get(
        f"{FRANKFURTER_BASE}/{date_str}",
        params={"from": base, "to": quote},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("rates", {}).get(quote)


def fetch_timeseries(base: str, quote: str, start_date: str, end_date: str) -> dict[str, float]:
    r = requests.get(
        f"{FRANKFURTER_BASE}/{start_date}..{end_date}",
        params={"from": base, "to": quote},
        timeout=HTTP_TIMEOUT
        * 3,
    )
    r.raise_for_status()
    data = r.json()
    rates_by_date: dict[str, float] = {}
    for d, rates in data.get("rates", {}).items():
        if quote in rates:
            rates_by_date[d] = rates[quote]
    return rates_by_date


# ==================== 历史管理 ====================

def ensure_history(history: dict) -> dict:
    """确保每个 PAIR 在 history 里有近 HISTORY_WINDOW_DAYS 天数据，缺失则回填"""
    today = date.today()
    cutoff = today - timedelta(days=HISTORY_WINDOW_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    for base, quote in PAIRS:
        key = pair_key(base, quote)
        existing = history.get(key, {})
        existing = {d: v for d, v in existing.items() if d >= cutoff_str}

        oldest = min(existing.keys()) if existing else None
        if not existing or oldest > cutoff_str:
            try:
                fetched = fetch_timeseries(base, quote, cutoff_str, today_str)
                existing.update(fetched)
                print(f"[BACKFILL] {key} fetched {len(fetched)} days")
            except Exception as e:
                print(f"[BACKFILL ERROR] {key}: {e}")

        history[key] = existing

    return history


def trim_history(history: dict) -> dict:
    today = date.today()
    cutoff = (today - timedelta(days=HISTORY_WINDOW_DAYS)).strftime("%Y-%m-%d")
    for key in history:
        history[key] = {d: v for d, v in history[key].items() if d >= cutoff}
    return history


def get_max_min(history_for_pair: dict[str, float], exclude_date: str | None) -> Optional[tuple[float, str, float, str]]:
    items = [(d, v) for d, v in history_for_pair.items() if d != exclude_date]
    if not items:
        return None
    max_d, max_v = max(items, key=lambda x: x[1])
    min_d, min_v = min(items, key=lambda x: x[1])
    return max_v, max_d, min_v, min_d


# ==================== 推送层 ====================

def push_ntfy(title: str, message: str, priority: int = 5, tags: str = "money_with_wings") -> bool:
    if not NTFY_TOPIC:
        print("[NTFY] skipped: NTFY_TOPIC not set")
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": str(priority),
                "Tags": tags,
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        print(f"[NTFY OK] {title}")
        return True
    except Exception as e:
        print(f"[NTFY ERROR] {e}")
        return False


def push_dingtalk(title: str, message: str) -> bool:
    if not DINGTALK_WEBHOOK:
        print("[DINGTALK] skipped: DINGTALK_WEBHOOK not set")
        return False
    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{message}",
            },
        }
        r = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        resp = r.json()
        if resp.get("errcode") != 0:
            print(f"[DINGTALK ERROR] {resp}")
            return False
        print(f"[DINGTALK OK] {title}")
        return True
    except Exception as e:
        print(f"[DINGTALK ERROR] {e}")
        return False


def push_all(title: str, message: str, priority: int = 5, tags: str = "money_with_wings") -> None:
    push_ntfy(title, message, priority, tags)
    push_dingtalk(title, message)


# ==================== 检测器 ====================

def detect_daily_brief(history: dict) -> str:
    lines = [f"📊 汇率日报 {today_local_str()}", ""]
    for base, quote in PAIRS:
        key = pair_key(base, quote)
        h = history.get(key, {})
        if not h:
            lines.append(f"{key}  (无数据)")
            lines.append("")
            continue

        sorted_dates = sorted(h.keys())
        latest_date = sorted_dates[-1]
        latest = h[latest_date]
        prev_date = sorted_dates[-2] if len(sorted_dates) >= 2 else None
        prev = h[prev_date] if prev_date else latest
        change_pct = (latest - prev) / prev * 100 if prev else 0.0

        mm = get_max_min(h, exclude_date=None)
        if mm:
            max_v, max_d, min_v, min_d = mm
            position = (latest - min_v) / (max_v - min_v) * 100 if max_v > min_v else 50.0
            lines.append(f"{key}  {latest:.4f}  {trend_arrow(change_pct)} {change_pct:+.2f}%")
            lines.append(f"          半年区间 [{min_v:.4f}, {max_v:.4f}]，当前位于 {position:.0f}%")
        else:
            lines.append(f"{key}  {latest:.4f}")
        lines.append("")

    return "\n".join(lines).rstrip()


def detect_sudden_move(base: str, quote: str, history: dict, state: dict) -> Optional[tuple[str, str]]:
    """返回 (title, message) 或 None"""
    key = pair_key(base, quote)
    h = history.get(key, {})
    sorted_dates = sorted(h.keys())
    if len(sorted_dates) < 2:
        return None

    latest_date = sorted_dates[-1]
    prev_date = sorted_dates[-2]
    latest = h[latest_date]
    prev = h[prev_date]
    if prev == 0:
        return None

    change_pct = (latest - prev) / prev * 100
    if abs(change_pct) < SUDDEN_MOVE_THRESHOLD_PCT:
        return None

    cooldown_key = f"sudden_move_{key}"
    last_triggered = state.get("cooldowns", {}).get(cooldown_key)
    if last_triggered:
        last_dt = datetime.fromisoformat(last_triggered)
        if (datetime.now(ZoneInfo(TIMEZONE)) - last_dt).total_seconds() < SUDDEN_MOVE_COOLDOWN_HOURS * 3600:
            return None

    count_in_window = sum(
        1 for i in range(1, len(sorted_dates))
        if h[sorted_dates[i - 1]]
        and abs((h[sorted_dates[i]] - h[sorted_dates[i - 1]]) / h[sorted_dates[i - 1]] * 100) >= SUDDEN_MOVE_THRESHOLD_PCT
    )

    direction = "上涨" if change_pct > 0 else "下跌"
    title = f"⚡ 汇率 {key} 大幅波动"
    message = (
        f"{key} 跨日{direction} {abs(change_pct):.2f}%\n\n"
        f"当前 {latest:.4f}（{prev_date}: {prev:.4f}）\n"
        f"过去 {HISTORY_WINDOW_DAYS} 天内第 {count_in_window} 次单日波动 ≥ {SUDDEN_MOVE_THRESHOLD_PCT}%"
    )

    state.setdefault("cooldowns", {})[cooldown_key] = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    return title, message


def detect_extreme(base: str, quote: str, history: dict, state: dict) -> Optional[tuple[str, str]]:
    key = pair_key(base, quote)
    h = history.get(key, {})
    sorted_dates = sorted(h.keys())
    if len(sorted_dates) < 30:
        return None

    latest_date = sorted_dates[-1]
    latest = h[latest_date]

    mm = get_max_min(h, exclude_date=latest_date)
    if not mm:
        return None
    max_v, max_d, min_v, min_d = mm

    direction: Optional[str] = None
    if latest > max_v:
        direction = "high"
    elif latest < min_v:
        direction = "low"
    else:
        return None

    cooldown_key = f"extreme_{key}_{direction}"
    last_triggered = state.get("cooldowns", {}).get(cooldown_key)
    if last_triggered:
        last_dt = datetime.fromisoformat(last_triggered).date()
        if (date.today() - last_dt).days < EXTREME_COOLDOWN_DAYS:
            return None

    if direction == "high":
        change_from_low = (latest - min_v) / min_v * 100
        title = f"🚀 汇率 {key} 突破半年新高"
        message = (
            f"{key} 当前 {latest:.4f}\n"
            f"上一次半年高点：{max_d} 的 {max_v:.4f}\n"
            f"距离 {HISTORY_WINDOW_DAYS} 天最低 {min_v:.4f}（{min_d}）涨幅 +{change_from_low:.2f}%"
        )
    else:
        change_from_high = (latest - max_v) / max_v * 100
        title = f"📉 汇率 {key} 突破半年新低"
        message = (
            f"{key} 当前 {latest:.4f}\n"
            f"上一次半年低点：{min_d} 的 {min_v:.4f}\n"
            f"距离 {HISTORY_WINDOW_DAYS} 天最高 {max_v:.4f}（{max_d}）跌幅 {change_from_high:.2f}%"
        )

    state.setdefault("cooldowns", {})[cooldown_key] = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    return title, message


# ==================== 静默队列处理 ====================

def flush_pending_quiet_alerts(state: dict) -> None:
    pending = state.get("pending_quiet_alerts", [])
    if not pending or is_quiet_hours():
        return

    lines = [f"🌙 夜间汇率汇总（{len(pending)} 条）", ""]
    for item in pending:
        lines.append(f"【{item['title']}】")
        lines.append(item["message"])
        lines.append("")
    push_all("汇率夜间汇总", "\n".join(lines).rstrip(), priority=4)
    state["pending_quiet_alerts"] = []


def queue_or_push(title: str, message: str, priority: int, force_immediate: bool, state: dict, tags: str = "money_with_wings") -> None:
    if force_immediate or not is_quiet_hours():
        push_all(title, message, priority, tags)
    else:
        state.setdefault("pending_quiet_alerts", []).append({"title": title, "message": message})
        print(f"[QUEUED quiet] {title}")


# ==================== 主流程 ====================

def main() -> int:
    state = load_json(STATE_FILE)
    history = load_json(HISTORY_FILE)

    history = ensure_history(history)

    today_str = today_local_str()
    for base, quote in PAIRS:
        key = pair_key(base, quote)
        try:
            rate, data_date = fetch_rate(base, quote)
            history.setdefault(key, {})[data_date] = rate
            print(f"[FETCH] {key} = {rate:.4f} (data date {data_date})")
        except Exception as e:
            print(f"[FETCH ERROR] {key}: {e}")

    history = trim_history(history)

    flush_pending_quiet_alerts(state)

    for base, quote in PAIRS:
        try:
            sm = detect_sudden_move(base, quote, history, state)
            if sm:
                title, message = sm
                queue_or_push(title, message, priority=5, force_immediate=False, state=state)
                print(f"[SUDDEN MOVE] {pair_key(base, quote)}")
        except Exception as e:
            print(f"[SUDDEN MOVE ERROR] {base}/{quote}: {e}")

    for base, quote in PAIRS:
        try:
            ex = detect_extreme(base, quote, history, state)
            if ex:
                title, message = ex
                push_all(title, message, priority=5, tags="rotating_light,money_with_wings")
                print(f"[EXTREME] {pair_key(base, quote)}")
        except Exception as e:
            print(f"[EXTREME ERROR] {base}/{quote}: {e}")

    if is_brief_window() and state.get("last_brief_date") != today_str:
        try:
            brief = detect_daily_brief(history)
            push_all(f"汇率日报 {today_str}", brief, priority=2, tags="bar_chart")
            state["last_brief_date"] = today_str
            print(f"[BRIEF SENT] {today_str}")
        except Exception as e:
            print(f"[BRIEF ERROR] {e}")
    else:
        if not is_brief_window():
            print(f"[BRIEF] not in window (hour={now_local().hour})")
        else:
            print(f"[BRIEF] already sent today ({today_str})")

    save_json(STATE_FILE, state)
    save_json(HISTORY_FILE, history)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[FATAL] {e}\n{tb}")
        try:
            push_ntfy(
                "⚠️ 汇率监控脚本异常",
                f"脚本运行失败：\n{e}\n\n{tb[:500]}",
                priority=4,
                tags="warning",
            )
            push_dingtalk(
                "⚠️ 汇率监控脚本异常",
                f"脚本运行失败：\n```\n{e}\n```\n\n{tb[:500]}",
            )
        except Exception:
            pass
        sys.exit(1)

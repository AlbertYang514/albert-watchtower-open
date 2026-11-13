#!/usr/bin/env python3
"""
Albert Watchtower
A small, dependency-free server watchroom that sends weather, host health and
website health reports by SMTP email.

The default report titles use the "小鸡值班室" (little chicken duty room) theme,
but every title, location, site and server label is configurable.
"""

import argparse
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config(path=None):
    """Load a JSON config file.

    The path is resolved in this order:
    --config / explicit argument, WATCHTOWER_CONFIG, <script dir>/config.json.
    """
    config_path = path or os.environ.get("WATCHTOWER_CONFIG") or DEFAULT_CONFIG_PATH
    if not os.path.exists(config_path):
        print(f"错误：找不到配置文件 {config_path}")
        print("请复制 config.example.json 为 config.json 并填入真实配置。")
        print("  cp config.example.json config.json")
        print("  然后编辑 config.json 填入你的 SMTP 邮件配置。")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Weather (Open-Meteo, no API key required)
# ---------------------------------------------------------------------------

# WMO weather code -> Chinese description
WMO_CODES = {
    0: "晴",
    1: "少云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "霜雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "中阵雨",
    82: "大阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "冰雹雷暴",
    99: "强冰雹雷暴",
}


def fetch_weather(lat, lon, timezone="auto"):
    """Fetch today's weather from Open-Meteo for given coordinates.

    Returns a short summary string, or an error message on failure.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&current=temperature_2m,weather_code"
        f"&timezone={quote(str(timezone))}"
        "&forecast_days=1"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return f"天气获取失败（{e}）"

    try:
        daily = data.get("daily", {})
        current = data.get("current", {})

        t_max = daily.get("temperature_2m_max", [None])[0]
        t_min = daily.get("temperature_2m_min", [None])[0]
        precip = daily.get("precipitation_probability_max", [None])[0]
        t_now = current.get("temperature_2m")
        code = current.get("weather_code")
        desc = WMO_CODES.get(code, f"天气代码{code}")

        parts = [desc]
        if t_now is not None:
            parts.append(f"当前{t_now}°C")
        if t_max is not None and t_min is not None:
            parts.append(f"{t_min}~{t_max}°C")
        if precip is not None and precip > 0:
            parts.append(f"降雨概率{precip}%")

        return "，".join(parts)
    except Exception as e:
        return f"天气解析失败（{e}）"


# ---------------------------------------------------------------------------
# Host health check (Linux /proc, no psutil)
# ---------------------------------------------------------------------------

def check_vps():
    """Check host uptime, disk and memory.

    Returns a dict with keys: status, uptime_str, disk_pct, mem_pct.
    Values are None when unavailable (for example on macOS).
    """
    result = {"status": "OK", "uptime_str": None, "disk_pct": None, "mem_pct": None}

    # -- uptime --
    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.read().split()[0])
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        if days > 0:
            result["uptime_str"] = f"{days}天{hours}小时"
        else:
            result["uptime_str"] = f"{hours}小时"
    except Exception:
        pass

    # -- disk --
    try:
        stat = os.statvfs("/")
        total_gb = (stat.f_blocks * stat.f_frsize) / (1024 ** 3)
        free_gb = (stat.f_bfree * stat.f_frsize) / (1024 ** 3)
        if total_gb > 0:
            result["disk_pct"] = round((1 - free_gb / total_gb) * 100)
    except Exception:
        pass

    # -- memory --
    try:
        with open("/proc/meminfo", "r") as f:
            meminfo = f.read()
        total = _parse_meminfo(meminfo, "MemTotal")
        available = _parse_meminfo(meminfo, "MemAvailable")
        if total and available:
            result["mem_pct"] = round((1 - available / total) * 100)
    except Exception:
        pass

    if result["disk_pct"] is not None and result["disk_pct"] > 90:
        result["status"] = "WARN"
    if result["mem_pct"] is not None and result["mem_pct"] > 90:
        result["status"] = "WARN"

    return result


def _parse_meminfo(text, key):
    """Extract a value in kB from /proc/meminfo."""
    m = re.search(rf"^{key}:\s+(\d+)\s+kB", text, re.MULTILINE)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Website health check
# ---------------------------------------------------------------------------

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AlbertWatchtower/1.0)"
}


def check_url(url, timeout=10):
    """Check if a URL is reachable. Tries HEAD first, falls back to GET.

    Returns (ok: bool, detail: str).
    """
    try:
        req = urllib.request.Request(url, method="HEAD", headers=_DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status < 400:
                return True, "OK"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # Some servers do not support HEAD; fall through to GET.
        if e.code not in (405, 501):
            return False, f"HTTP {e.code}"
    except Exception:
        pass

    try:
        req = urllib.request.Request(url, method="GET", headers=_DEFAULT_HEADERS)
        req.add_header("Range", "bytes=0-0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status < 400 or resp.status == 206:
                return True, "OK"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}"


# ---------------------------------------------------------------------------
# SMTP email sender
# ---------------------------------------------------------------------------

def _build_subject(body, title):
    """Build email subject.

    Uses [Watchtower OK] for clean reports and [Watchtower ALERT] with a short
    problem list when failures are found.
    """
    fail_lines = re.findall(r"^(.+?)：FAIL", body, re.MULTILINE)
    if fail_lines:
        problems = "、".join(fail_lines[:3])
        if len(fail_lines) > 3:
            problems += f"等{len(fail_lines)}项"
        return f"[Watchtower ALERT] {problems}"
    return f"[Watchtower OK] {title}"


def send_email(config, subject, body):
    """Send a report via SMTP. Returns (success: bool, error_msg: str or None)."""
    email_cfg = config.get("email", {})

    smtp_host = email_cfg.get("smtp_host")
    smtp_port = email_cfg.get("smtp_port", 465)
    use_ssl = email_cfg.get("smtp_ssl", True)
    use_starttls = email_cfg.get("smtp_starttls", False)
    username = email_cfg.get("username")
    password = email_cfg.get("password")
    from_addr = email_cfg.get("from") or username
    to_addr = email_cfg.get("to")

    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            if use_starttls:
                server.starttls()

        if username and password:
            server.login(username, password)
        server.send_message(msg)
        server.quit()
        return True, None
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP 认证失败，请检查 username/password（邮箱服务商通常要求使用授权码而非登录密码）"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Report mode (morning / evening / daily)
# ---------------------------------------------------------------------------

DEFAULT_REPORT_TITLES = {
    "morning": "小鸡值班室早报🐣",
    "evening": "小鸡值班室晚报🐣",
    "daily": "小鸡值班室日报🐣",
}

DEFAULT_SERVER_LABEL = "小鸡"


def get_report_type(hour=None):
    """Determine report type from the current local hour."""
    if hour is None:
        hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    if 18 <= hour <= 23:
        return "evening"
    return "daily"


def report_title(config, report_type):
    """Resolve a report title from config, falling back to the chicken theme."""
    report_cfg = config.get("report", {})
    titles = report_cfg.get("titles", {})
    if isinstance(titles, dict) and titles.get(report_type):
        return str(titles[report_type])
    return DEFAULT_REPORT_TITLES.get(report_type, DEFAULT_REPORT_TITLES["daily"])


def _iter_locations(config):
    """Yield (name, lat, lon, timezone) from either a dict or a list config."""
    locations = config.get("locations", {})
    if isinstance(locations, dict):
        for key, loc in locations.items():
            if not isinstance(loc, dict):
                continue
            yield (
                loc.get("name", key),
                loc.get("lat"),
                loc.get("lon"),
                loc.get("timezone", config.get("timezone", "auto")),
            )
    elif isinstance(locations, list):
        for index, loc in enumerate(locations, start=1):
            if not isinstance(loc, dict):
                continue
            yield (
                loc.get("name", f"地点{index}"),
                loc.get("lat"),
                loc.get("lon"),
                loc.get("timezone", config.get("timezone", "auto")),
            )


def _iter_sites(config):
    """Yield (name, url) from the configured sites list."""
    sites = config.get("sites")
    if isinstance(sites, list):
        for index, site in enumerate(sites, start=1):
            if not isinstance(site, dict):
                continue
            url = site.get("url")
            if url:
                yield site.get("name", f"网站{index}"), url


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(config, report_type="daily"):
    """Assemble the report in plain text."""
    lines = []
    title = report_title(config, report_type)
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(title)
    lines.append(date_str)
    lines.append("")

    # Weather
    lines.append("天气")
    weather_locations = list(_iter_locations(config))
    if not weather_locations:
        lines.append("未配置地点")
    for name, lat, lon, timezone in weather_locations:
        if lat is not None and lon is not None:
            weather = fetch_weather(lat, lon, timezone=timezone)
            lines.append(f"{name}：{weather}")
        else:
            lines.append(f"{name}：缺少经纬度配置")
    lines.append("")

    # Host
    report_cfg = config.get("report", {})
    server_label = report_cfg.get("server_label", DEFAULT_SERVER_LABEL)
    vps = check_vps()
    lines.append(server_label)
    lines.append(f"状态：{vps['status']}")
    uptime = vps.get("uptime_str") or "未知"
    disk = f"{vps['disk_pct']}%" if vps["disk_pct"] is not None else "未知"
    mem = f"{vps['mem_pct']}%" if vps["mem_pct"] is not None else "未知"
    lines.append(f"运行时间：{uptime}")
    lines.append(f"磁盘使用：{disk}")
    lines.append(f"内存使用：{mem}")
    lines.append("")

    # Website health
    sites_heading = report_cfg.get("sites_heading", "网站")
    lines.append(sites_heading)
    sites = list(_iter_sites(config))
    if not sites:
        lines.append("未配置网站")
    for name, url in sites:
        ok, detail = check_url(url)
        if ok:
            lines.append(f"{name}：OK")
        else:
            lines.append(f"{name}：FAIL（{detail}）")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _call_llm(llm_config, prompt, temperature, max_tokens):
    """Shared OpenAI-compatible chat completions call.

    Returns the raw response text, or None on any failure.
    """
    api_key = llm_config.get("api_key", "")
    if not api_key or "YOUR_" in api_key:
        return None

    base_url = llm_config.get("base_url", "").rstrip("/")
    model = llm_config.get("model")
    timeout = llm_config.get("timeout_seconds", 12)
    if not base_url or not model:
        return None

    req_body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    url = f"{base_url}/v1/chat/completions"

    try:
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["choices"][0]["message"]["content"]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LLM observation ("今日观察")
# ---------------------------------------------------------------------------

_FALLBACK_OBSERVATIONS = {
    "morning": "系统状态正常，值班室早班已到岗，站点暂时风平浪静。",
    "evening": "系统状态正常，值班室晚班也还清醒，站点没有塌方。",
    "daily": "系统状态正常，值班室值班中，站点今天还稳。",
}

_OBSERVATION_PROMPT = (
    "当前是一份{report_label}。根据以下摘要，写一句纯文本观察。"
    "请输出一句完整的话，必须以句号、问号或感叹号结尾。"
    "只输出一句中文，不要标题，不要解释，不要 Markdown，不要 emoji，"
    "不要引号，不要换行，50个汉字内。"
    "要具体，有一点干幽默；不要诗歌、不要鸡汤、不要空泛总结；"
    "只能使用摘要里的事实，不能编造。"
    "若有 FAIL，优先点出失败项。"
)

_REPORT_LABELS = {"morning": "早报", "evening": "晚报", "daily": "日报"}


def _clean_observation_output(text):
    """Clean observation output with fragment rejection."""
    result = _clean_llm_output(text)
    if not result:
        return None

    cjk = sum(1 for ch in result if ord(ch) > 0x2E80)
    if cjk < 8:
        return None

    if result[-1].isdigit() and result[-1] not in "。？！?!":
        return None

    if result[-1] not in "。？！?!":
        result += "。"

    return result


def generate_observation(llm_config, report_body, report_type="daily"):
    """Generate a one-sentence observation via LLM.

    Falls back to a local observation when the LLM is unavailable or returns a
    broken / empty result. Returns None only when llm.enabled is false.
    """
    if not llm_config.get("enabled", False):
        return None

    label = _REPORT_LABELS.get(report_type, "日报")
    prompt = _OBSERVATION_PROMPT.format(report_label=label)
    summary = _build_llm_summary(report_body)
    prompt = prompt + "\n\n摘要：\n" + summary
    raw = _call_llm(llm_config, prompt, temperature=0.8, max_tokens=120)

    if raw:
        result = _clean_observation_output(raw)
        if result:
            return result

    return _FALLBACK_OBSERVATIONS.get(report_type, _FALLBACK_OBSERVATIONS["daily"])


# ---------------------------------------------------------------------------
# LLM daily suggestion ("今日建议")
# ---------------------------------------------------------------------------

_FALLBACK_SUGGESTIONS = {
    "morning": "今天先挑一件最小的正事开头，别一早就和世界硬碰硬。",
    "evening": "今晚别和服务器、DDL、人生同时开会，先收工。",
    "daily": "今天挑一件小事推进，剩下的别急着审判自己。",
}

_SUGGESTION_BASE = (
    "你是一个给个人报告生成一句今日建议的小助手。"
    "根据下面的摘要，生成一句今日建议。"
    "可以结合摘要中的天气，但不要编造摘要以外的地点、日程或身份。"
    "只输出一句中文纯文本，不要标题、编号、Markdown、emoji 或引号。"
    "不要鸡汤，不要说多喝水之类的话。可以轻微幽默，但不要油腻。"
    "长度不超过45个中文字符。"
)

_SUGGESTION_ANGLE = {
    "morning": "当前是早间报告。建议偏向今天怎么开始：出门、日程安排或工作节奏。",
    "evening": "当前是晚间报告。建议偏向今天怎么收尾：休息、整理、少折腾。",
    "daily": "建议可以结合整体状态，给一点生活小提醒。",
}


def _clean_suggestion_output(text):
    """Clean suggestion output, more lenient than _clean_llm_output."""
    if not text:
        return None

    result = _clean_llm_output(text)

    if not result:
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            return None
        result = "".join(lines)
        result = re.sub(r"\s+", "", result)
        result = re.sub(r"^#{1,6}\s*", "", result)
        result = re.sub(r"^[-*+>]\s*", "", result)
        result = re.sub(r"^\d+[.)]\s*", "", result)
        result = re.sub(r"^(今日建议[：:]\s*)+", "", result)
        result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
        result = re.sub(r"`(.+?)`", r"\1", result)
        for ch in ("**", "__", "*", "_", "`", '"', '"', '"', '「', '」'):
            result = result.replace(ch, "")
        result = result.strip()
        if not result:
            return None

    max_len = 60
    if len(result) > max_len:
        for i in range(max_len - 1, max(0, max_len - 20), -1):
            if result[i] in "，,；;、":
                result = result[:i]
                break
        else:
            result = result[:max_len]

    if result and result[-1] not in "。？！?!，,；;、":
        result += "。"

    return result if result else None


def generate_daily_suggestion(llm_config, report_body, report_type="daily"):
    """Generate a one-sentence daily suggestion via LLM."""
    if not llm_config.get("enabled", False):
        return None

    angle = _SUGGESTION_ANGLE.get(report_type, _SUGGESTION_ANGLE["daily"])
    summary = _build_llm_summary(report_body)
    prompt = _SUGGESTION_BASE + " " + angle + "\n\n摘要：\n" + summary
    raw = _call_llm(llm_config, prompt, temperature=1.2, max_tokens=80)

    if raw:
        result = _clean_suggestion_output(raw)
        if result:
            return result

    return _FALLBACK_SUGGESTIONS.get(report_type, _FALLBACK_SUGGESTIONS["daily"])


def _build_llm_summary(body):
    """Build a compact, token-efficient summary from the report body."""
    skip_prefixes = (
        "小鸡值班室早报", "小鸡值班室晚报", "小鸡值班室日报", "202",
    )
    skip_exact = {"天气", "小鸡", "网站"}  # common section headers
    lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in skip_exact:
            continue
        if any(stripped.startswith(p) for p in skip_prefixes):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _clean_llm_output(text):
    """Clean LLM output to a single plain-text line."""
    if not text:
        return None

    raw_lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not raw_lines:
        return None

    cleaned = []
    for line in raw_lines:
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*+>]\s*", "", line)
        line = re.sub(r"^\d+[.)]\s*", "", line)
        line = re.sub(r"^(今日观察[：:]\s*)+", "", line)
        line = re.sub(r"^(今日建议[：:]\s*)+", "", line)

        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"__(.+?)__", r"\1", line)
        line = re.sub(r"\*(.+?)\*", r"\1", line)
        line = re.sub(r"_(.+?)_", r"\1", line)
        line = re.sub(r"`(.+?)`", r"\1", line)

        # Strip label prefixes again after formatting markers are removed.
        line = re.sub(r"^(今日观察[：:]\s*)+", "", line)
        line = re.sub(r"^(今日建议[：:]\s*)+", "", line)

        for ch in ("**", "__", "*", "_", "`", '"', """, """, "「", "」"):
            line = line.replace(ch, "")

        line = line.strip()
        if line:
            cleaned.append(line)

    if not cleaned:
        return None

    merged = "".join(cleaned)
    merged = re.sub(r"\s+", "", merged)
    if not merged:
        return None

    m = re.search(r"[。？！?!]", merged)
    if m:
        merged = merged[: m.end()]

    max_len = 70
    if len(merged) <= max_len:
        return merged

    for i in range(max_len - 1, max(0, max_len - 20), -1):
        if merged[i] in "，,；;、":
            return merged[:i] + "……"

    return merged[:max_len] + "……"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Albert Watchtower — 可配置的服务器状态与网站健康邮件报告"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认 config.json，也可用 WATCHTOWER_CONFIG 指定）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成并打印报告 subject + body，不发送邮件",
    )
    parser.add_argument(
        "--report-type",
        choices=("morning", "evening", "daily"),
        default=None,
        help="强制指定报告类型（默认根据本地时间自动判断）",
    )
    args = parser.parse_args()

    report_type = args.report_type or get_report_type()

    config = load_config(args.config)
    title = report_title(config, report_type)
    body = build_report(config, report_type=report_type)
    subject = _build_subject(body, title)

    llm_config = config.get("llm", {})
    observation = generate_observation(llm_config, body, report_type=report_type)
    if observation:
        body = body + "\n\n今日观察\n" + observation

    suggestion = generate_daily_suggestion(llm_config, body, report_type=report_type)
    if suggestion:
        body = body + "\n\n今日建议\n" + suggestion

    if args.dry_run:
        print(f"Subject: {subject}")
        print("---")
        print(body)
        print("---")
        print("dry-run 模式，未发送邮件。")
        return

    email_cfg = config.get("email", {})
    username = email_cfg.get("username", "")
    password = email_cfg.get("password", "")

    if not email_cfg.get("smtp_host") or not username or not email_cfg.get("to"):
        print("错误：email 配置不完整，请编辑 config.json（需配置 smtp_host / username / password / to）")
        sys.exit(1)
    if "YOUR_EMAIL" in username or "YOUR_SMTP" in password:
        print("错误：email 配置仍为示例值，请编辑 config.json")
        sys.exit(1)

    ok, err = send_email(config, subject, body)
    if ok:
        print("邮件发送成功")
    else:
        print(f"邮件发送失败：{err}")
        sys.exit(1)


if __name__ == "__main__":
    main()

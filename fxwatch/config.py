from pathlib import Path
from decimal import Decimal
import os
import tomllib
from .sources import NAMES, CURRENCIES, BOUNDS


def load(path):
    with open(path, "rb") as stream:
        c = tomllib.load(stream)
    root = Path(path).resolve().parent
    c.setdefault("banks", list(NAMES))
    if not c["banks"] or len(set(c["banks"])) != len(c["banks"]) or set(c["banks"]) - NAMES.keys():
        raise ValueError("banks 必须是六家支持银行的不重复非空列表")
    defaults = {"interval_seconds": 300, "max_quote_age_seconds": 900,
                "max_fetch_age_seconds": 660, "future_tolerance_seconds": 120,
                "timeout_seconds": 20, "retention_days": 180,
                "min_comparison_banks": 2, "new_low_cooldown_seconds": 1800,
                "icbc_legacy_tls": True, "daily_low_alert": True,
                "max_median_deviation": "0.10"}
    for k, v in defaults.items():
        c.setdefault(k, v)
    for k in defaults:
        if isinstance(defaults[k], int) and not isinstance(defaults[k], bool):
            if not isinstance(c[k], int) or c[k] <= 0:
                raise ValueError(f"{k} 必须是正整数")
    if c["interval_seconds"] < 60 or c["timeout_seconds"] > 60:
        raise ValueError("采集间隔至少 60 秒；单次请求超时最多 60 秒")
    if c["min_comparison_banks"] < 2 or c["min_comparison_banks"] > len(c["banks"]):
        raise ValueError("至少需两家银行参与比较，且不能超过启用银行数")
    for k in ("icbc_legacy_tls", "daily_low_alert"):
        if not isinstance(c[k], bool):
            raise ValueError(f"{k} 必须为 true/false")
    c["max_median_deviation"] = Decimal(str(c["max_median_deviation"]))
    if not Decimal("0") < c["max_median_deviation"] < Decimal("1"):
        raise ValueError("max_median_deviation 必须在 0 到 1 之间")
    for key, default in (("database", "data/fxwatch.sqlite3"), ("raw_directory", "data/raw")):
        p = Path(c.get(key, default))
        c[key] = p if p.is_absolute() else root / p
    c.setdefault("targets", {})
    if set(c["targets"]) - CURRENCIES.keys():
        raise ValueError("目标币种仅支持 USD/EUR/HKD/JPY")
    c["targets"] = {k: Decimal(str(v)) for k, v in c["targets"].items()}
    for code, value in c["targets"].items():
        lower, upper = map(Decimal, BOUNDS[code])
        if not value.is_finite() or not lower < value < upper:
            raise ValueError(f"{code} 目标价按人民币/1 外币填写，须在 {lower} 到 {upper} 之间")
    c["telegram_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    c["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "")
    if bool(c["telegram_token"]) != bool(c["telegram_chat_id"]):
        raise ValueError("Telegram Token 与 Chat ID 必须同时配置")
    return c

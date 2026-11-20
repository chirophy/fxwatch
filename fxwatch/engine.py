from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
import hashlib
import json
import logging
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from .sources import CN, NAMES, URLS, CURRENCIES, fetch

log = logging.getLogger("fxwatch")


def now_cn():
    return datetime.now(CN)


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=30000;
        CREATE TABLE IF NOT EXISTS observations (
          id INTEGER PRIMARY KEY, bank TEXT NOT NULL, currency TEXT NOT NULL,
          sell TEXT NOT NULL, quoted_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
          raw_sell TEXT NOT NULL, unit TEXT NOT NULL, source TEXT NOT NULL,
          sha256 TEXT NOT NULL, quality TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS obs_latest ON observations(bank,currency,id DESC);
        CREATE INDEX IF NOT EXISTS obs_time ON observations(fetched_at);
        CREATE INDEX IF NOT EXISTS obs_currency_time ON observations(currency,fetched_at);
        CREATE TABLE IF NOT EXISTS sources (
          bank TEXT PRIMARY KEY, attempted_at TEXT, success_at TEXT,
          error TEXT, failures INTEGER DEFAULT 0, next_attempt TEXT);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
          created_at TEXT NOT NULL, expires_at TEXT NOT NULL, sent_at TEXT,
          attempts INTEGER DEFAULT 0, error TEXT);
    """)
    return db


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(db, key, value):
    db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))


def time_quality(quoted_at, fetched_at, now, c):
    age = (now - quoted_at).total_seconds()
    if age < -c["future_tolerance_seconds"]:
        return "future"
    if age > c["max_quote_age_seconds"]:
        return "stale"
    if (now - fetched_at).total_seconds() > c["max_fetch_age_seconds"]:
        return "not_recently_fetched"
    return "ok"


def snapshot(db, c, now=None):
    now = now or now_cn()
    statuses = {r["bank"]: dict(r) for r in db.execute("SELECT * FROM sources")}
    rows = []
    for bank in c["banks"]:
        for cur in CURRENCIES:
            r = db.execute("SELECT * FROM observations WHERE bank=? AND currency=? ORDER BY id DESC LIMIT 1",
                           (bank, cur)).fetchone()
            status = statuses.get(bank, {})
            item = dict(r) if r else {"bank": bank, "currency": cur, "sell": None}
            quality = "missing"
            if r:
                quality = time_quality(datetime.fromisoformat(r["quoted_at"]),
                                       datetime.fromisoformat(r["fetched_at"]), now, c)
                if r["quality"] != "ok":
                    quality = r["quality"]
                if status.get("error"):
                    quality = "fetch_failed"
            item.update(name=NAMES[bank], quality=quality, error=status.get("error"),
                        attempted_at=status.get("attempted_at"), next_attempt=status.get("next_attempt"))
            rows.append(item)
    rankings = {}
    for cur in CURRENCIES:
        valid = [r for r in rows if r["currency"] == cur and r["quality"] == "ok"]
        if len(valid) >= 3:
            mid = median(Decimal(r["sell"]) for r in valid)
            for r in valid:
                if abs(Decimal(r["sell"]) / mid - 1) > c["max_median_deviation"]:
                    r["quality"] = "outlier"
            valid = [r for r in valid if r["quality"] == "ok"]
        valid.sort(key=lambda r: (Decimal(r["sell"]), r["bank"]))
        rankings[cur] = valid
    return {"as_of": now.isoformat(), "rows": rows, "rankings": rankings,
            "enabled_banks": len(c["banks"]), "minimum_banks": c["min_comparison_banks"]}


def enqueue(db, key, body, now, c, quoted_at):
    if not c["telegram_token"]:
        return
    expiry = min(now + timedelta(seconds=c["max_quote_age_seconds"]),
                 datetime.fromisoformat(quoted_at) + timedelta(seconds=c["max_quote_age_seconds"]))
    db.execute("INSERT OR IGNORE INTO alerts(event_key,body,created_at,expires_at) VALUES (?,?,?,?)",
               (key, body, now.isoformat(), expiry.isoformat()))


def evaluate(db, c, now):
    data = snapshot(db, c, now)
    day = now.date().isoformat()
    for cur, valid in data["rankings"].items():
        if len(valid) < c["min_comparison_banks"]:
            continue
        best = valid[0]
        price = Decimal(best["sell"])
        coverage = f"有效银行 {len(valid)}/{len(c['banks'])}；仅比较这些银行"
        body = (f"{cur} 购汇提醒 · 现汇卖出\n{best['name']}：{price} 元/{cur}\n"
                f"银行报价：{best['quoted_at']}\n采集时间：{best['fetched_at']}\n"
                f"{coverage}\n{best['source']}\n公开牌价仅供参考，以银行成交确认页为准。")
        key = f"low:{day}:{cur}"
        state = get_state(db, key)
        if state is None:
            # The first observation establishes a baseline, never a false 'new low'.
            set_state(db, key, {"price": str(price), "notified_at": None})
        elif price < Decimal(state["price"]):
            last = datetime.fromisoformat(state["notified_at"]) if state["notified_at"] else None
            if c["daily_low_alert"] and (last is None or (now-last).total_seconds() >= c["new_low_cooldown_seconds"]):
                enqueue(db, f"{key}:{price}", "监测以来的日内新低\n" + body, now, c, best["quoted_at"])
                state["notified_at"] = now.isoformat()
            state["price"] = str(price)
            set_state(db, key, state)
        target = c["targets"].get(cur)
        if target is None or not c["telegram_token"]:
            continue
        key = f"target:{cur}:{target}"
        state = get_state(db, key, {"below": False, "episode": 0})
        if price <= target and not state["below"]:
            state["episode"] += 1
            state["below"] = True
            enqueue(db, f"{key}:{state['episode']}", f"已达到目标价 ≤ {target}\n" + body, now, c, best["quoted_at"])
        elif price > target and len(valid) == len(c["banks"]):
            # Partial outages must not rearm an alert while a cheap bank is missing.
            state["below"] = False
        set_state(db, key, state)


def safe_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    # Bank requests contain no credentials. Notification errors use a separate sanitizer.
    return f"{type(exc).__name__}: {str(exc)[:220]}"


def collect(db, c, now=None, fetcher=fetch, force=False):
    fixed_time = now
    now = now or now_cn()
    jobs = []
    for bank in c["banks"]:
        row = db.execute("SELECT * FROM sources WHERE bank=?", (bank,)).fetchone()
        if not force and row and row["next_attempt"] and datetime.fromisoformat(row["next_attempt"]) > now:
            continue
        jobs.append(bank)
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = {pool.submit(fetcher, bank, c["timeout_seconds"], c["icbc_legacy_tls"]): bank for bank in jobs}
        for future in as_completed(pending):
            bank = pending[future]
            fetched = fixed_time or now_cn()
            try:
                quotes, raw = future.result()
                digest = hashlib.sha256(raw).hexdigest()
                # Keep only latest raw response for diagnosis, not an unlimited archive.
                directory = Path(c["raw_directory"])
                directory.mkdir(parents=True, exist_ok=True)
                temp = directory / f"{bank}.tmp"
                temp.write_bytes(raw)
                temp.replace(directory / f"{bank}.latest")
                with db:
                    for q in quotes:
                        quality = time_quality(q.quoted_at, fetched, fetched, c)
                        previous = db.execute("SELECT MAX(quoted_at) FROM observations WHERE bank=? AND currency=? AND quality IN ('ok','outlier','stale')",
                                              (bank, q.currency)).fetchone()
                        if previous and previous[0] and q.quoted_at < datetime.fromisoformat(previous[0]):
                            quality = "regressed_timestamp"
                        db.execute("""INSERT INTO observations(bank,currency,sell,quoted_at,fetched_at,
                            raw_sell,unit,source,sha256,quality) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (bank, q.currency, str(q.sell), q.quoted_at.isoformat(), fetched.isoformat(),
                             q.raw_sell, q.unit, q.source, digest, quality))
                    db.execute("""INSERT INTO sources(bank,attempted_at,success_at,error,failures,next_attempt)
                        VALUES (?,?,?,NULL,0,NULL) ON CONFLICT(bank) DO UPDATE SET
                        attempted_at=excluded.attempted_at,success_at=excluded.success_at,error=NULL,failures=0,next_attempt=NULL""",
                        (bank, fetched.isoformat(), fetched.isoformat()))
                log.info("%s 采集成功", NAMES[bank])
            except Exception as exc:
                error = safe_error(exc)
                prev = db.execute("SELECT failures FROM sources WHERE bank=?", (bank,)).fetchone()
                failures = (prev[0] if prev else 0) + 1
                wait = min(c["interval_seconds"] * (2 ** min(failures - 1, 5)), 3600)
                retry = now + timedelta(seconds=wait)
                with db:
                    db.execute("""INSERT INTO sources(bank,attempted_at,error,failures,next_attempt) VALUES (?,?,?,?,?)
                        ON CONFLICT(bank) DO UPDATE SET attempted_at=excluded.attempted_at,
                        error=excluded.error,failures=excluded.failures,next_attempt=excluded.next_attempt""",
                        (bank, now.isoformat(), error, failures, retry.isoformat()))
                log.warning("%s %s；%s 秒后重试", NAMES[bank], error, wait)
    now = fixed_time or now_cn()
    with db:
        for r in snapshot(db, c, now)["rows"]:
            if r["quality"] == "outlier":
                db.execute("UPDATE observations SET quality='outlier' WHERE id=?", (r["id"],))
        evaluate(db, c, now)
        set_state(db, "last_cycle", now.isoformat())
        # Daily pruning; keep at least latest observation per bank/currency for diagnostics.
        if get_state(db, "last_prune") != now.date().isoformat():
            cutoff = (now - timedelta(days=c["retention_days"])).isoformat()
            db.execute("DELETE FROM observations WHERE fetched_at < ? AND id NOT IN (SELECT MAX(id) FROM observations GROUP BY bank,currency)", (cutoff,))
            db.execute("DELETE FROM alerts WHERE created_at < ?", (cutoff,))
            db.execute("DELETE FROM state WHERE key LIKE 'low:%' AND substr(key,5,10) < ?", (cutoff[:10],))
            set_state(db, "last_prune", now.date().isoformat())
    return snapshot(db, c, now)


def send_telegram(token, chat_id, message):
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message,
                                   "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        if data.get("ok") is not True:
            raise RuntimeError("Telegram 返回非成功状态")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram HTTP {exc.code}") from None
    except Exception:
        # Never log the request URL: it contains the bot token.
        raise RuntimeError("Telegram 发送失败，请检查网络、Token 和 Chat ID") from None


def deliver(db, c, now=None, sender=send_telegram):
    now = now or now_cn()
    if not c["telegram_token"]:
        return
    pending = db.execute("SELECT * FROM alerts WHERE sent_at IS NULL AND expires_at > ? ORDER BY id LIMIT 10",
                         (now.isoformat(),)).fetchall()
    for event in pending:
        try:
            sender(c["telegram_token"], c["telegram_chat_id"], event["body"])
        except Exception:
            with db:
                db.execute("UPDATE alerts SET attempts=attempts+1,error=? WHERE id=?",
                           ("Telegram 发送失败（凭据和请求地址已隐藏）", event["id"]))
            log.warning("Telegram 发送失败；提醒将在有效期内下轮重试")
            break
        else:
            with db:
                db.execute("UPDATE alerts SET sent_at=?,attempts=attempts+1,error=NULL WHERE id=?",
                           (now.isoformat(), event["id"]))


QUALITY = {"ok": "有效", "missing": "无数据", "future": "报价时间在未来", "stale": "报价已过期",
           "fetch_failed": "采集失败", "outlier": "异常偏离", "regressed_timestamp": "报价时间倒退",
           "not_recently_fetched": "采集已停止/延迟"}


def format_status(data):
    lines = [f"截至 {data['as_of']}  |  现汇卖出价，人民币/1 外币"]
    for cur, valid in data["rankings"].items():
        lines.append(f"\n{cur}：有效 {len(valid)}/{data['enabled_banks']} 家")
        if len(valid) >= data["minimum_banks"]:
            best = valid[0]
            tied = "、".join(r["name"] for r in valid if Decimal(r["sell"]) == Decimal(best["sell"]))
            spread = Decimal(valid[-1]["sell"]) - Decimal(best["sell"])
            lines.append(f"有效样本最低：{tied} {best['sell']}；最高与最低价差 {spread} 元/单位")
        else:
            lines.append("有效银行不足，暂不判断最低价")
        for r in [r for r in data["rows"] if r["currency"] == cur]:
            lines.append(f"  {r['name']}  {r['sell'] or '—'}  {QUALITY[r['quality']]}  报价 {r.get('quoted_at','—')}")
            if r.get("error"):
                lines.append(f"    {r['error']}")
    return "\n".join(lines)

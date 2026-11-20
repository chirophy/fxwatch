"""Read-only WSGI dashboard. Gunicorn in production, wsgiref for local preview."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server
import csv
import io
import json
import logging
import math
import os
import sqlite3
from .config import load
from .engine import connect, snapshot, now_cn, QUALITY, get_state
from .sources import CURRENCIES, NAMES, URLS

STATIC = Path(__file__).with_name("static")
ASSETS = {"/": ("index.html", "text/html"),
          "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}
SOURCE_PAGES = {**URLS, "cmb": "https://fx.cmbchina.cn/hq/",
                "icbc": "https://www.sh.icbc.com.cn/ICBC/金融信息/行情数据/人民币即期外汇牌价/default.htm"}


def read_database(c):
    if not c["database"].exists():
        return connect(":memory:")
    db = sqlite3.connect(c["database"].resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")  # One coherent snapshot for every response, even during collection.
    return db


def public_snapshot(db, c, now):
    data = snapshot(db, c, now)
    keep = ("bank", "name", "currency", "sell", "quoted_at", "fetched_at", "quality")
    rows = []
    for row in data["rows"]:
        clean = {k: row.get(k) for k in keep}
        clean["source"] = SOURCE_PAGES[row["bank"]]
        clean["status"] = QUALITY[row["quality"]]
        # Raw error strings, Telegram configuration and local paths are not public API fields.
        rows.append(clean)
    summaries = []
    for cur, name in CURRENCIES.items():
        valid = data["rankings"][cur]
        comparable = len(valid) >= c["min_comparison_banks"]
        best = Decimal(valid[0]["sell"]) if comparable else None
        low = db.execute("SELECT sell FROM observations WHERE currency=? AND quality='ok' AND fetched_at>=? AND bank IN (%s)" %
                         ",".join("?" for _ in c["banks"]),
                         (cur, now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), *c["banks"])).fetchall()
        summaries.append({"code": cur, "name": name, "valid_banks": len(valid),
                          "best": str(best) if best is not None else None,
                          "best_banks": [r["bank"] for r in valid if comparable and Decimal(r["sell"]) == best],
                          "spread": str(Decimal(valid[-1]["sell"])-best) if comparable else None,
                          "observed_day_low": str(min(Decimal(r[0]) for r in low)) if low else None})
    return {"as_of": data["as_of"], "last_cycle": get_state(db, "last_cycle"),
            "interval_seconds": c["interval_seconds"], "quote_max_age_seconds": c["max_quote_age_seconds"],
            "fetch_max_age_seconds": c["max_fetch_age_seconds"],
            "minimum_banks": c["min_comparison_banks"],
            "banks": [{"code": b, "name": NAMES[b], "source": SOURCE_PAGES[b]} for b in c["banks"]],
            "currencies": summaries, "rows": rows}


def history(db, c, cur, hours, now):
    start = now-timedelta(hours=hours)
    rows = db.execute("SELECT bank,sell,quoted_at,fetched_at,quality FROM observations WHERE currency=? AND fetched_at>=? AND fetched_at<=? ORDER BY fetched_at,id",
                      (cur, start.isoformat(), now.isoformat())).fetchall()
    series = []
    for bank in c["banks"]:
        points = [{"t": r["fetched_at"], "price": r["sell"] if r["quality"] == "ok" else None,
                   "quoted_at": r["quoted_at"]} for r in rows if r["bank"] == bank]
        segment, previous = 0, None
        for point in points:
            if previous is None or point["price"] is None or previous["price"] is None or (
                datetime.fromisoformat(point["t"])-datetime.fromisoformat(previous["t"])
            ).total_seconds() > c["interval_seconds"]*2:
                segment += 1
            point["segment"] = segment
            previous = point
        # Preserve first/last and extrema, plus a gap marker, within each bucket.
        # At most 5 * 600 points per bank even for minute-frequency week histories.
        step = max(1, math.ceil(len(points)/600))
        sampled = []
        for i in range(0, len(points), step):
            bucket = points[i:i+step]
            indices = {0, len(bucket)-1}
            for j in range(1,len(bucket)):
                if bucket[j]["segment"] != bucket[j-1]["segment"]:
                    indices.update((j-1,j))
            valid = [j for j,p in enumerate(bucket) if p["price"] is not None]
            if valid:
                indices.update((min(valid,key=lambda j:Decimal(bucket[j]["price"])),
                                max(valid,key=lambda j:Decimal(bucket[j]["price"]))))
            invalid = [j for j,p in enumerate(bucket) if p["price"] is None]
            if invalid:
                indices.add(invalid[0])
            sampled.extend(bucket[j] for j in sorted(indices))
        series.append({"bank": bank, "name": NAMES[bank], "points": sampled})
    return {"currency": cur, "hours": hours, "from": start.isoformat(), "to": now.isoformat(),
            "gap_seconds": c["interval_seconds"]*2, "sampled": any(len(s["points"]) >= 600 for s in series),
            "series": series}


class App:
    def __init__(self, config):
        self.config = config

    def __call__(self, environ, start_response):
        status, mime, extra = "200 OK", "application/json", []
        try:
            method = environ.get("REQUEST_METHOD", "GET")
            path = environ.get("PATH_INFO", "/")
            if method not in ("GET", "HEAD"):
                status, payload = "405 Method Not Allowed", {"error": "仅支持读取"}
                extra.append(("Allow", "GET, HEAD"))
            elif path in ASSETS:
                filename, mime = ASSETS[path]
                payload = (STATIC / filename).read_bytes()
            elif path in ("/api/summary", "/api/history", "/api/export.csv", "/api/health"):
                params = parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=10)
                cur = params.get("currency", ["USD"])[0]
                hours = int(params.get("hours", ["24"])[0])
                if cur not in CURRENCIES or hours not in (6, 24, 168):
                    raise ValueError("币种仅支持 USD/EUR/HKD/JPY；时段仅支持 6/24/168 小时")
                c, now = self.config, now_cn()
                db = read_database(c)
                try:
                    if path == "/api/summary":
                        payload = public_snapshot(db,c,now)
                    elif path == "/api/history":
                        payload = history(db,c,cur,hours,now)
                    elif path == "/api/health":
                        s = snapshot(db,c,now)
                        healthy = all(len(rows) >= c["min_comparison_banks"] for rows in s["rankings"].values())
                        payload = {"status": "ok" if healthy else "degraded", "last_cycle": get_state(db,"last_cycle")}
                        if not healthy: status = "503 Service Unavailable"
                    else:
                        buf = io.StringIO(newline="")
                        writer = csv.writer(buf)
                        writer.writerow(["bank", "currency", "CNY_per_1_foreign", "bank_quote_time", "fetched_at", "quality"])
                        records = db.execute("SELECT bank,currency,sell,quoted_at,fetched_at,quality FROM observations WHERE currency=? AND fetched_at>=? ORDER BY id",
                                             (cur,(now-timedelta(hours=hours)).isoformat()))
                        writer.writerows(tuple(r) for r in records if r["bank"] in c["banks"])
                        payload, mime = ("\ufeff"+buf.getvalue()).encode("utf-8"), "text/csv"
                        extra.append(("Content-Disposition", f'attachment; filename="fxwatch-{cur}-{hours}h.csv"'))
                finally:
                    db.close()
            else:
                status, payload = "404 Not Found", {"error": "页面不存在"}
        except (ValueError, TypeError):
            status, payload = "400 Bad Request", {"error": "请求参数无效"}
        except (OSError, sqlite3.Error):
            logging.exception("Dashboard storage or asset unavailable")
            status, payload = "503 Service Unavailable", {"error": "数据暂时不可用，请稍后重试"}
        if not isinstance(payload, bytes):
            payload = json.dumps(payload,ensure_ascii=False).encode("utf-8")
        headers = [("Content-Type", mime+"; charset=utf-8"), ("Content-Length",str(len(payload))),
                   ("Cache-Control","no-store"), ("X-Content-Type-Options","nosniff"),
                   ("Referrer-Policy","no-referrer"), ("X-Frame-Options","DENY"),
                   ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")]
        start_response(status,headers+extra)
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else payload]


def create_app():
    return App(load(os.environ.get("FXWATCH_CONFIG", "config.toml")))


def serve(config, host="127.0.0.1", port=8080):
    with make_server(host,port,App(config)) as server:
        print(f"Local: http://{host}:{port}  (本地预览；服务器部署请使用 Gunicorn)",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

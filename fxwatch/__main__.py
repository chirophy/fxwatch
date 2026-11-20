import argparse
import csv
from datetime import timedelta
import json
import logging
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
from .config import load
from .engine import collect, connect, deliver, format_status, now_cn, snapshot, send_telegram
from .report import render


class ProcessLock:
    """OS file lock, released automatically on crash; only the collector takes it."""
    def __init__(self, database):
        self.path = Path(str(database) + ".lock")

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("另一个采集进程正在运行；请使用 status 查看，或先停止服务") from None
        return self

    def __exit__(self, *args):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description="六家银行官方购汇牌价监测")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="检查配置，不发送通知")
    sub.add_parser("init", help="初始化或升级数据库结构，保留历史，不访问银行")
    once = sub.add_parser("once", help="单次真实采集；默认不发送通知")
    once.add_argument("--notify", action="store_true")
    once.add_argument("--force", action="store_true", help="忽略当前退避等待，手动诊断使用")
    run = sub.add_parser("run", help="每五分钟运行，按配置发送通知")
    run.add_argument("--report", help="每轮生成独立 HTML 报告")
    web = sub.add_parser("serve", help="启动本地 Web 看板（采集服务需另行运行）")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)
    status = sub.add_parser("status", help="读取数据库，不访问银行")
    status.add_argument("--json", action="store_true")
    report = sub.add_parser("report", help="生成包含历史曲线的独立 HTML 报告")
    report.add_argument("--output", default="data/report.html")
    report.add_argument("--hours", type=int, default=24)
    export = sub.add_parser("export", help="导出原始采集历史 CSV")
    export.add_argument("--output", default="data/history.csv")
    export.add_argument("--days", type=int, default=7)
    backup = sub.add_parser("backup", help="通过 SQLite 在线备份 API 安全备份")
    backup.add_argument("--output", required=True)
    sub.add_parser("test-notify", help="主动发送一条 Telegram 测试消息")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        c = load(args.config)
        if args.command == "serve":
            from .web import serve
            if not 1 <= args.port <= 65535:
                raise ValueError("端口须在 1 到 65535 之间")
            serve(c, args.host, args.port)
            return 0
        if args.command == "check":
            print(f"配置有效：{len(c['banks'])} 家银行，{c['interval_seconds']} 秒一次；Telegram "
                  + ("已配置" if c["telegram_token"] else "未配置（仅采集）"))
            return 0
        if args.command == "test-notify":
            if not c["telegram_token"]:
                raise ValueError("请先设置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
            send_telegram(c["telegram_token"], c["telegram_chat_id"], "FX Watch 测试通知：服务器连接成功。")
            print("Telegram 测试消息已发送")
            return 0
        db = connect(c["database"])
        try:
            if args.command == "init":
                print("数据库已初始化，已有历史和提醒状态保留")
            elif args.command == "status":
                data = snapshot(db, c)
                print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else format_status(data))
            elif args.command in ("once", "run"):
                with ProcessLock(c["database"]):
                    if args.command == "once":
                        if not args.notify:
                            c = dict(c, telegram_token="", telegram_chat_id="")
                        data = collect(db, c, force=args.force)
                        if args.notify:
                            deliver(db, c)
                        print(format_status(data))
                        return 0 if all(r["quality"] == "ok" for r in data["rows"]) else 2
                    stop = threading.Event()
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(sig, lambda *_: stop.set())
                    if not c["telegram_token"]:
                        logging.warning("Telegram 未配置，当前仅采集和记录历史")
                    while not stop.is_set():
                        started = time.monotonic()
                        collect(db, c)
                        deliver(db, c)
                        if args.report:
                            render(db, c, args.report, now_cn())
                        stop.wait(max(1, c["interval_seconds"] - (time.monotonic()-started)))
            elif args.command == "report":
                if not 1 <= args.hours <= 168:
                    raise ValueError("报告小时数范围为 1 至 168")
                render(db, c, args.output, now_cn(), args.hours)
                print(f"报告已生成：{Path(args.output).resolve()}")
            elif args.command == "export":
                if not 1 <= args.days <= 366:
                    raise ValueError("导出天数范围为 1 至 366")
                cursor = db.execute("SELECT * FROM observations WHERE fetched_at>=? ORDER BY id",
                                    ((now_cn()-timedelta(days=args.days)).isoformat(),))
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([d[0] for d in cursor.description])
                    writer.writerows(cursor)
                print(f"已导出：{Path(args.output).resolve()}")
            elif args.command == "backup":
                if Path(args.output).resolve() == Path(c["database"]).resolve() or Path(args.output).exists():
                    raise ValueError("备份目标必须是新的文件，避免覆盖已有数据")
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                target = sqlite3.connect(args.output)
                try:
                    db.backup(target)
                finally:
                    target.close()
                print(f"备份已完成：{Path(args.output).resolve()}")
        finally:
            db.close()
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

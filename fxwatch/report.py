"""Standalone HTML report with local SVG plots; no CDN or background requests."""
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from .engine import snapshot, QUALITY
from .sources import NAMES, CURRENCIES

COLORS = ["#007d72", "#2864c4", "#b05c00", "#8e4bb5", "#b94367", "#466275"]


def render(db, c, output, now, hours=24):
    data = snapshot(db, c, now)
    start = now - timedelta(hours=hours)
    cards = []
    for cur, valid in data["rankings"].items():
        enough = len(valid) >= c["min_comparison_banks"]
        price = valid[0]["sell"] if enough else "—"
        best = " / ".join(r["name"] for r in valid if r["sell"] == price) if enough else "有效银行不足"
        cards.append(f'<article><span>{cur} · 有效样本最低</span><h2>¥ {price}</h2><b>{best}</b><p>有效 {len(valid)}/{len(c["banks"])} 家</p></article>')
    table = []
    for r in data["rows"]:
        price = r["sell"] or "—"
        source = r.get("source", "")
        table.append(f'<tr><td>{r["name"]}</td><td>{r["currency"]}</td><td class="number">{price}</td>'
                     f'<td class="{r["quality"]}">{QUALITY[r["quality"]]}</td>'
                     f'<td>{escape(r.get("quoted_at", "—"))}</td><td>{escape(r.get("fetched_at", "—"))}</td>'
                     f'<td><a href="{escape(source, quote=True)}">官方来源</a></td></tr>')
    charts = []
    for cur in CURRENCIES:
        rows = db.execute("SELECT * FROM observations WHERE currency=? AND fetched_at>=? AND quality='ok' ORDER BY id",
                          (cur, start.isoformat())).fetchall()
        rows = [r for r in rows if r["bank"] in c["banks"]]
        if not rows:
            charts.append(f"<section><h3>{cur} · 过去 {hours} 小时</h3><p>暂无有效历史，运行采集后自动积累。</p></section>")
            continue
        values = [float(r["sell"]) for r in rows]
        lo, hi = min(values), max(values)
        pad = max((hi - lo) * .15, .002)
        lo, hi = lo-pad, hi+pad
        svg = [f'<svg viewBox="0 0 960 260" role="img" aria-label="{cur} 汇率历史，按采集时间">']
        for i in range(5):
            y = 20 + i * 45
            value = hi - (hi-lo)*i/4
            svg.append(f'<path d="M75 {y} H930" stroke="#e1e6e4"/><text x="8" y="{y+4}">{value:.4f}</text>')
        legend = []
        for bank, color in zip(c["banks"], COLORS):
            previous = None
            for r in (r for r in rows if r["bank"] == bank):
                t = datetime.fromisoformat(r["fetched_at"])
                x = 75 + (t-start).total_seconds()/(hours*3600)*855
                y = 20 + (hi-float(r["sell"]))/ (hi-lo)*180
                if previous and (t-previous[0]).total_seconds() <= c["interval_seconds"]*2:
                    svg.append(f'<path d="M{previous[1]:.2f} {previous[2]:.2f} L{x:.2f} {y:.2f}" fill="none" stroke="{color}" stroke-width="2"/>')
                svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.3" fill="{color}"><title>{NAMES[bank]} {r["sell"]}，报价 {r["quoted_at"]}</title></circle>')
                previous = t, x, y
            legend.append(f'<span style="color:{color}">● {NAMES[bank]}</span>')
        svg.append(f'<text x="75" y="238">{start:%m-%d %H:%M}</text><text x="825" y="238">{now:%m-%d %H:%M}</text></svg>')
        charts.append(f'<section><h3>{cur} · 过去 {hours} 小时</h3>{"".join(svg)}<div class="legend">{"".join(legend)}</div></section>')
    errors = "".join(f'<li>{NAMES[b]}：{escape(r["error"])}</li>' for b in c["banks"]
                     if (r := db.execute("SELECT error FROM sources WHERE bank=?", (b,)).fetchone()) and r["error"])
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>银行购汇监测 · FX Watch</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f2;color:#20342e;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}}
main{{max-width:1200px;margin:auto;padding:42px 24px}}header{{border-top:5px solid #007d72;padding-top:20px}}h1{{font-size:32px;margin:8px 0}}.muted,article span{{color:#65756d}}
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:28px 0}}article,section{{background:white;border:1px solid #dce3dc;border-radius:12px;padding:24px;margin-bottom:20px}}
h2{{font-size:42px;line-height:1.2;margin:16px 0}}h3{{margin:0 0 18px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}td,th{{padding:12px 15px;border-bottom:1px solid #e8ece8;text-align:left}}th{{color:#637368;font-size:13px}}.number{{font-variant-numeric:tabular-nums;font-weight:650}}a,.ok{{color:#007d72}}.stale,.fetch_failed,.missing,.outlier,.future,.regressed_timestamp,.not_recently_fetched{{color:#9d5b1a}}svg{{width:100%;font:12px system-ui;fill:#637368}}.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px}}footer{{color:#65756d;font-size:13px}}@media(max-width:600px){{main{{padding:22px 14px}}.cards{{grid-template-columns:1fr}}h2{{font-size:36px}}article,section{{padding:18px}}}}
</style><main><header><div class="muted">FX WATCH / 银行官方牌价</div><h1>把每次购汇，放在一起比较。</h1>
<p>美元 · 欧元 · 港币 · 日元 ｜ 现汇卖出价 · 人民币 / 1 单位外币</p><p class="muted">报告生成于 {now:%Y-%m-%d %H:%M:%S}（北京时间）。这是静态快照，请重新生成获取最新状态。</p></header>
<div class="cards">{"".join(cards)}</div><section><h3>各行报价与数据状态</h3><div class="scroll"><table><thead><tr><th>银行</th><th>币种</th><th>现汇卖出</th><th>状态</th><th>银行报价时间</th><th>采集时间</th><th>来源</th></tr></thead><tbody>{"".join(table)}</tbody></table></div></section>
{"".join(charts)}<section><h3>采集诊断</h3>{'<ul>'+errors+'</ul>' if errors else '<p>暂未记录采集错误。</p>'}</section>
<footer>仅比较有效样本，不保证为所有银行最低价。曲线横轴为采集时间，悬停可查看银行报价时间；采集中断时不连线。各行报价并非同时更新；公开牌价仅供参考，实际以银行成交确认页为准。</footer></main></html>'''
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(output) + ".tmp")
    temp.write_text(page, encoding="utf-8")
    temp.replace(output)

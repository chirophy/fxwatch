"""Explicit, fail-closed adapters for the six official bank sources."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re
import ssl
import urllib.request

CN = timezone(timedelta(hours=8))
CURRENCIES = {"USD": "美元", "EUR": "欧元", "HKD": "港币", "JPY": "日元"}
BOUNDS = {"USD": ("1", "30"), "EUR": ("1", "30"),
          "HKD": ("0.1", "3"), "JPY": ("0.001", "0.3")}
NAMES = {"cgb": "广发银行", "hxb": "华夏银行", "boc": "中国银行",
         "icbc": "工商银行", "cmb": "招商银行", "citic": "中信银行"}
URLS = {
    "cgb": "https://www.cgbchina.com.cn/searchExchangePrice.gsp",
    "hxb": "https://psbank.hxb.com.cn/gateway/forexquote.jsp",
    "boc": "https://www.boc.cn/sourcedb/whpj/",
    "icbc": "https://papi.icbc.com.cn/exchanges/ns/getLatest",
    "cmb": "https://fx.cmbchina.cn/api/v1/fx/rate",
    "citic": "https://etrade.citicbank.com/portalweb/cms/getForeignExchRate.htm",
}


class SourceError(ValueError):
    pass


def stamp(value):
    """Require a source-provided date AND time. Never substitute collection time."""
    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?", value)
    if not match:
        raise SourceError("银行未提供可识别的完整报价日期和时间")
    return datetime(*(int(x or 0) for x in match.groups()), tzinfo=CN)


def currency(value):
    for code, name in CURRENCIES.items():
        if code in value.upper() or name in value or (code == "HKD" and "港元" in value):
            return code
    return None


@dataclass(frozen=True)
class Quote:
    bank: str
    currency: str
    sell: Decimal
    quoted_at: datetime
    raw_sell: str
    unit: str
    source: str


def quote(bank, code, price, unit, timestamp):
    try:
        raw, base = Decimal(str(price).strip()), Decimal(str(unit).strip())
        if not raw.is_finite() or not base.is_finite() or base not in (1, 100):
            raise SourceError("无效报价或未知单位")
        value = raw / base
        if code not in BOUNDS:
            raise SourceError("不支持的币种")
        lower, upper = map(Decimal, BOUNDS[code])
        if not lower < value < upper:
            raise SourceError(f"{code} 报价超出校验范围 ({lower}, {upper})，请核实单位")
        return Quote(bank, code, value, stamp(timestamp), str(price), str(unit), URLS[bank])
    except (InvalidOperation, TypeError, ArithmeticError) as exc:
        raise SourceError("无法识别报价数值") from exc


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row, self.cell = [], None
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row, self.cell = None, None


def parse(bank, text):
    results = []
    if bank in ("citic", "cmb", "icbc"):
        data = json.loads(text)
        if bank == "citic":
            if data.get("retCode") != "AAAAAAA":
                raise SourceError("中信接口返回非成功状态")
            rows = data["content"]["resultList"]
            fields = ("curName", "cstexcSellPrice", "quotePriceDate", "quotePriceTime")
        elif bank == "cmb":
            if data.get("returnCode") != "SUC0000":
                raise SourceError("招行接口返回非成功状态")
            rows = data["body"]
            fields = ("ccyNbrEng", "rthOfr", "ratDat", "ratTim")
        else:
            if data.get("code") != 0:
                raise SourceError("工行接口返回非成功状态")
            rows = data["data"]
            fields = ("currencyENName", "foreignSell", "publishDate", "publishTime")
        for row in rows:
            code = currency(row[fields[0]])
            if code:
                results.append(quote(bank, code, row[fields[1]], 100,
                                     row[fields[2]] + " " + row[fields[3]]))
    else:
        parser = TableParser()
        parser.feed(text)
        expected = {"cgb": (2, "基数", 6, "现汇卖出价"),
                    "hxb": (0, "货币种类", 4, "现汇现钞卖出价"),
                    "boc": (0, "货币名称", 3, "现汇卖出价")}[bank]
        header_found = False
        for row in parser.rows:
            a, av, b, bv = expected
            if len(row) > max(a, b) and av in row[a] and bv in row[b]:
                header_found = True
                continue
            if not header_found or not row or not (code := currency(row[0])):
                continue
            if bank == "cgb":
                if len(row) != 8 or currency(row[1]) != code:
                    raise SourceError("广发表格列结构改变")
                match = re.search(r"发布时间为[：:]\s*([^<]+)", text)
                if not match:
                    raise SourceError("广发缺少发布时间")
                results.append(quote(bank, code, row[6], row[2], match[1]))
            elif bank == "hxb":
                if len(row) != 7:
                    raise SourceError("华夏表格列结构改变")
                results.append(quote(bank, code, row[4], 100, row[6]))
            else:
                if len(row) != 8:
                    raise SourceError("中行表格列结构改变")
                results.append(quote(bank, code, row[3], 100, row[6] + " " + row[7]))
    # Duplicated historical rows: select newest quote, reject conflicting same-time values.
    latest = {}
    for q in results:
        old = latest.get(q.currency)
        if old and q.quoted_at == old.quoted_at and q.sell != old.sell:
            raise SourceError("同币种同时间出现冲突报价")
        if old is None or q.quoted_at > old.quoted_at:
            latest[q.currency] = q
    if set(latest) != set(CURRENCIES):
        raise SourceError("未完整找到 USD/EUR/HKD/JPY；可能是页面改版或访问受限")
    return list(latest.values())


def fetch(bank, timeout=20, icbc_legacy_tls=True):
    context = ssl.create_default_context()
    if bank == "icbc" and icbc_legacy_tls:
        # Bank's legacy TLS endpoint; this flag is scoped to this one request.
        # Certificate and hostname validation remain enabled. No global OpenSSL change.
        context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
    request = urllib.request.Request(URLS[bank], data=b"" if bank == "icbc" else None,
        headers={"User-Agent": "FXWatch/1.0 (personal rate monitor)",
                 "Accept": "application/json,text/html", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        if not response.url.startswith("https://"):
            raise SourceError("拒绝 HTTPS 降级重定向")
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise SourceError("响应超过 2MB 限制")
        encoding = response.headers.get_content_charset() or "utf-8"
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            text = raw.decode("gb18030")
    return parse(bank, text), raw

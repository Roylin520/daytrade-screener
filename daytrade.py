"""台股當沖候選每日篩選。只用標準函式庫，需要 Python 3.11 以上。

python -u daytrade.py                  抓今天盤後資料、選股、產生報告
python -u daytrade.py backfill 20      用過去 20 個交易日跑一次規則，累積績效紀錄
DATE=2026-10-30 python -u daytrade.py  指定日期重跑（不寄信）
FORCE=true python -u daytrade.py       休市日也執行
TEST_EMAIL=true python -u daytrade.py  另外寄一封測試信
"""
import csv, json, math, os, re, smtplib, sys, tomllib, time, traceback, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.toml"
DATA = HERE / "data"
QUOTES = DATA / "history_quotes.csv"
PICKS = DATA / "history_picks.csv"
PAPER = DATA / "history_paper.csv"
LATEST = DATA / "latest.json"
REPORT = HERE / "docs" / "index.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TWSE = "https://www.twse.com.tw/{path}?response=json&{query}"
TPEX = "https://www.tpex.org.tw/www/zh-tw/{path}?response=json&{query}"
HOLIDAYS_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
MARKET = {"tse": "上市", "otc": "上櫃"}

Q_COLS = ["date", "code", "name", "market", "open", "high", "low", "close", "change", "volume", "value",
          "dt_volume", "dt_buy", "dt_suspended"]
P_COLS = ["date", "rank", "code", "name", "market", "score", "bias", "close", "range5", "dt_ratio", "dt_buy", "net",
          "long_entry", "long_stop", "long_target", "short_entry", "short_stop", "short_target",
          "next_date", "next_open", "next_high", "next_low", "next_close", "long_pct", "short_pct", "bias_pct"]
PA_COLS = ["date", "strategy", "code", "name", "side", "entry", "stop", "target", "cost_pct",
           "next_date", "next_open", "next_high", "next_low", "next_close", "pct_cons", "pct_opt"]
STRATEGIES = {"current": "現行規則", "near": "近目標＋硬停損"}


class NotReady(Exception):
    """The source has not published this day's data (or the market was closed)."""


# ---------- config and small helpers ----------

def load_config():
    with open(CONFIG, "rb") as f:
        raw = tomllib.load(f)
    cfg = {}
    for section in ("filter", "weights", "plan"):
        for k, v in raw[section].items():
            cfg[k] = float(v)
    cfg["top_n"] = int(raw["report"]["top_n"])
    cfg["retain_days"] = int(raw["report"]["retain_days"])
    cfg["paper"] = raw["paper"]
    return cfg


def get_json(url, retries=2):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(3 * (attempt + 1))


def twse(path, allow_empty=False, **query):
    """TWSE website JSON. Throttled, because TWSE blocks clients that call more than ~3 times in 5 s."""
    time.sleep(2)
    data = get_json(TWSE.format(path=path, query=urllib.parse.urlencode(query)))
    if data.get("stat") != "OK":
        if allow_empty and "沒有符合" in str(data.get("stat")):
            return {"data": []}
        raise NotReady(f'{path} {query}: {data.get("stat")}')
    return data


def tpex(path, **query):
    """TPEx (OTC market) website JSON."""
    time.sleep(1)
    data = get_json(TPEX.format(path=path, query=urllib.parse.urlencode(query)))
    if str(data.get("stat")).lower() != "ok" or not data.get("tables"):
        raise NotReady(f'{path} {query}: {data.get("stat")}')
    return data


def num(s):
    """'1,234.50' / '+15.00' / 'X0.00' (ex-dividend mark) / '--' -> float or None."""
    s = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return float(s)
    except ValueError:
        return None


def roc(s):
    """'115/10/21', '115.10.21' or '1151021' (ROC calendar) -> date."""
    s = re.sub(r"\D", "", s)
    return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))


def lerp(x, lo, hi):
    """How far x sits from lo (0) to hi (1), clamped. Works with lo > hi for smaller-is-better."""
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def tick_size(price):
    for limit, tick in ((10, .01), (50, .05), (100, .1), (500, .5), (1000, 1)):
        if price < limit:
            return tick
    return 5.0


def to_tick(price, up):
    """Round to the valid tick for that price, in the direction that makes a trigger harder to reach."""
    t = tick_size(price)
    steps = price / t
    steps = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    return round(steps * t, 2)


def is_stock(code, name):
    """Ordinary shares only: 4 digits not starting with 0 (no ETF, warrant, preferred) and no depositary receipts."""
    return re.fullmatch(r"[1-9]\d{3}", code) and not name.endswith("-DR")


# ---------- calendar ----------

_holidays = None


def holidays():
    """Closed days from the TWSE list. Entries named "…交易日" (first/last day around a break) are open days."""
    global _holidays
    if _holidays is None:
        _holidays = {roc(h["Date"]): h["Name"] for h in get_json(HOLIDAYS_API) if "交易日" not in h["Name"]}
    return _holidays


def is_trading_day(d):
    return d.weekday() < 5 and d not in holidays()


def next_trading_day(d):
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


# ---------- fetching ----------

def fetch_tse_quotes(d):
    ymd = d.strftime("%Y%m%d")
    data = twse("rwd/zh/afterTrading/MI_INDEX", date=ymd, type="ALLBUT0999")
    table = next(t for t in data["tables"] if t.get("fields") and t["fields"][0] == "證券代號")
    dt_tables = twse("exchangeReport/TWTB4U", date=ymd, selectType="All")["tables"]
    dt = {r[0].strip(): r for r in dt_tables[1]["data"]}
    out = []
    for r in table["data"]:
        code, name = r[0].strip(), r[1].strip()
        if is_stock(code, name):
            # The sign is a separate HTML snippet like '<p style= color:green>-</p>'.
            sign = -1 if "-" in r[9] else 1
            out.append(quote_row(d, code, name, "tse", r[5], r[6], r[7], r[8], sign * (num(r[10]) or 0),
                                 r[2], r[4], dt.get(code)))
    return out


def fetch_otc_quotes(d):
    slash = d.strftime("%Y/%m/%d")
    quotes = tpex("afterTrading/dailyQuotes", date=slash, type="EW")["tables"][0]["data"]
    dt = {r[0].strip(): r for r in tpex("intraday/stat", date=slash, type="Daily")["tables"][1]["data"]}
    out = []
    for r in quotes:
        code, name = r[0].strip(), r[1].strip()
        if is_stock(code, name):
            out.append(quote_row(d, code, name, "otc", r[4], r[5], r[6], r[2], num(r[3]) or 0, r[8], r[9], dt.get(code)))
    return out


def quote_row(d, code, name, market, o, h, l, c, change, volume, value, dt):
    return {"date": d.isoformat(), "code": code, "name": name, "market": market, "open": num(o), "high": num(h),
            "low": num(l), "close": num(c), "change": change, "volume": num(volume), "value": num(value),
            # No day-trading row at all means the stock isn't eligible, which is recorded as suspended.
            "dt_volume": num(dt[3]) if dt else None, "dt_buy": num(dt[4]) if dt else None,
            "dt_suspended": "" if dt and not dt[2].strip() else "Y"}


def fetch_flows(d, errors):
    """Institutional net buys in shares -> {code: (foreign, trust, all three)}. Missing data only costs the chip score."""
    out = {}
    try:
        for r in twse("rwd/zh/fund/T86", date=d.strftime("%Y%m%d"), selectType="ALLBUT0999")["data"]:
            out[r[0].strip()] = (num(r[4]), num(r[10]), num(r[18]))
    except Exception as e:
        errors.append(f"上市三大法人：{type(e).__name__}: {e}")
    try:
        for r in tpex("insti/dailyTrade", type="Daily", sect="EW", date=d.strftime("%Y/%m/%d"))["tables"][0]["data"]:
            out[r[0].strip()] = (num(r[4]), num(r[13]), num(r[23]))
    except Exception as e:
        errors.append(f"上櫃三大法人：{type(e).__name__}: {e}")
    return out


def fetch_blocks(d, errors):
    """Stocks under attention (announced on d) or disposal (covering the next trading day) -> {code: reason}, plus
    the markets whose lists could not be read. A failed list must not read as "nothing to avoid"."""
    blocked, failed = {}, set()
    nxt = next_trading_day(d)
    start = d - timedelta(days=30)

    def covers(period):
        days = re.findall(r"\d+/\d+/\d+", period)
        return len(days) == 2 and roc(days[0]) <= nxt <= roc(days[1])

    try:
        rows = twse("rwd/zh/announcement/notice", allow_empty=True, startDate=f"{d:%Y%m%d}", endDate=f"{d:%Y%m%d}")["data"]
        if not rows:
            errors.append("上市注意股名單是空的，可能尚未公布")
        blocked |= {r[1].strip(): "注意股" for r in rows}
        rows = twse("rwd/zh/announcement/punish", allow_empty=True, startDate=f"{start:%Y%m%d}", endDate=f"{nxt:%Y%m%d}")["data"]
        blocked |= {r[2].strip(): "處置股" for r in rows if covers(r[6])}
    except Exception as e:
        failed.add("tse")
        errors.append(f"上市注意／處置名單：{type(e).__name__}: {e}")
    try:
        # This endpoint ignores a bare date= and returns its latest days, so history needs the range form.
        rows = tpex("bulletin/attention", startDate=f"{d:%Y/%m/%d}", endDate=f"{d:%Y/%m/%d}")["tables"][0]["data"]
        today = [r for r in rows if roc(r[5]) == d]
        if not today:
            errors.append("上櫃注意股名單沒有當日資料，可能尚未公布")
        blocked |= {r[1].strip(): "注意股" for r in today}
        rows = tpex("bulletin/disposal", startDate=f"{start:%Y/%m/%d}", endDate=f"{nxt:%Y/%m/%d}")["tables"][0]["data"]
        blocked |= {r[2].strip(): "處置股" for r in rows if covers(r[5])}
    except Exception as e:
        failed.add("otc")
        errors.append(f"上櫃注意／處置名單：{type(e).__name__}: {e}")
    return blocked, failed


def fetch_market(d, errors):
    """TAIEX close and day-trading share of the market on day d, for the page header."""
    out = {}
    try:
        for r in twse("rwd/zh/afterTrading/FMTQIK", date=d.strftime("%Y%m01"))["data"]:
            if roc(r[0]) == d:
                out |= {"value": num(r[2]), "index": num(r[4]), "change": num(r[5])}
        share = twse("exchangeReport/TWTB4U", date=d.strftime("%Y%m%d"), selectType="All")["tables"][0]["data"][0]
        out["dt_share"] = num(share[3])
    except Exception as e:
        errors.append(f"大盤資料：{type(e).__name__}: {e}")
    return out


# ---------- quote history ----------

def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_quotes():
    """{date: {code: row}} with numbers as floats. Stocks that didn't trade are never stored."""
    out = {}
    if QUOTES.exists():
        with open(QUOTES, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                for k in Q_COLS[4:13]:
                    r[k] = fnum(r[k])
                out.setdefault(r["date"], {})[r["code"]] = r
    return out


def save_quotes(quotes, retain):
    DATA.mkdir(exist_ok=True)
    keep = sorted(quotes)[-retain:]
    with open(QUOTES, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, Q_COLS)
        w.writeheader()
        for day in keep:
            for code in sorted(quotes[day]):
                w.writerow({k: "" if v is None else v for k, v in quotes[day][code].items()})


def add_day(quotes, d, errors, required):
    """Fetch day d for whichever market is missing. Returns False if the market was closed that day."""
    day = quotes.setdefault(d.isoformat(), {})
    have = {r["market"] for r in day.values()}
    closed = False
    for market, fetch, label in (("tse", fetch_tse_quotes, "上市行情"), ("otc", fetch_otc_quotes, "上櫃行情")):
        if market in have:
            continue
        try:
            for r in fetch(d):
                if r["close"] is not None and r["open"] is not None and r["volume"]:
                    day[r["code"]] = r
        except NotReady as e:
            if required:
                raise
            closed = True       # An older weekday without data is a closure the holiday list didn't show.
            print(f"  {d} 沒有{label}：{e}")
        except Exception as e:
            errors.append(f"{label} {d}：{type(e).__name__}: {e}")
    if not day:
        del quotes[d.isoformat()]
    return not closed


def ensure_history(quotes, d, errors, need=6):
    """Make sure d and the trading days before it are stored."""
    add_day(quotes, d, errors, required=True)
    got, back = 1, d
    for _ in range(need * 3):
        if got >= need:
            break
        back -= timedelta(days=1)
        if is_trading_day(back):
            print(f"  檢查 {back} 行情")
            if add_day(quotes, back, errors, required=False):
                got += 1


# ---------- screening ----------

def analyse(cfg, quotes, d, flows, blocked, failed):
    """Filter and score every stock for day d -> (ranked candidates, hot stocks that were excluded)."""
    today = quotes[d.isoformat()]
    past = sorted(k for k in quotes if k <= d.isoformat())[-6:]
    picks, excluded = [], []
    for code, q in today.items():
        hist = [quotes[k][code] for k in past if code in quotes[k]]
        last5 = hist[-5:]
        ranges = [(r["high"] - r["low"]) / (r["close"] - r["change"]) * 100 for r in last5 if r["close"] - r["change"] > 0]
        close, volume = q["close"], q["volume"]
        range5 = sum(ranges) / len(ranges) if ranges else 0
        prev = close - q["change"]
        pct = q["change"] / prev * 100 if prev > 0 else 0
        dt_ratio = (q["dt_volume"] or 0) / volume * 100
        dt_days = [r for r in hist if r["dt_volume"] is not None]
        dt_ratio6 = sum(r["dt_volume"] for r in dt_days) / sum(r["volume"] for r in dt_days) * 100 if dt_days else 0
        dt_buy = q["dt_buy"] or 0
        tick_pct = tick_size(close) / close * 100

        reason = None
        if q["market"] in failed:
            reason = "名單未取得，無法確認"
        elif q["dt_suspended"]:
            reason = "暫停先賣後買當沖或非當沖標的"
        elif code in blocked:
            reason = blocked[code]
        elif abs(pct) >= 9.4 and (close == q["high"] if pct > 0 else close == q["low"]):
            reason = "漲停收盤，隔日易跳空" if pct > 0 else "跌停收盤，隔日易跳空"
        elif not cfg["min_price"] <= close <= cfg["max_price"]:
            reason = "股價不在設定範圍"
        elif tick_pct > cfg["max_tick_pct"]:
            reason = f"每檔跳動 {tick_pct:.2f}%，成本過高"
        elif dt_ratio > cfg["max_dt_ratio"]:
            reason = f"當沖占比 {dt_ratio:.0f}%，過熱"
        elif len(ranges) < 3 or range5 < cfg["min_range5"]:
            reason = f"5 日平均振幅 {range5:.1f}%，空間不足"
        if dt_buy < cfg["min_dt_buy"] * 1e8:
            continue            # Too thin to matter; not worth listing as an exclusion either.
        if reason:
            excluded.append({"code": code, "name": q["name"], "market": q["market"], "close": close, "pct": pct,
                             "dt_buy": dt_buy, "reason": reason})
            continue

        net = flows.get(code, (None, None, None))
        chip = (net[2] or 0) / volume
        score = (cfg["volatility"] * lerp(range5, cfg["min_range5"], cfg["min_range5"] + 5)
                 + cfg["liquidity"] * lerp(math.log10(dt_buy), math.log10(cfg["min_dt_buy"] * 1e8), 10)
                 + cfg["tick"] * lerp(tick_pct, cfg["max_tick_pct"], 0.1)
                 + cfg["heat"] * lerp(dt_ratio, cfg["max_dt_ratio"], 50)
                 + cfg["chips"] * lerp(abs(chip), 0, 0.2))
        total = cfg["volatility"] + cfg["liquidity"] + cfg["tick"] + cfg["heat"] + cfg["chips"]

        # Direction: institutional flow, where the close sits in the day's range, and close against the 5-day average.
        span = q["high"] - q["low"]
        loc = (close - q["low"]) / span if span else 0.5
        ma5 = sum(r["close"] for r in last5) / len(last5)
        votes = (1 if chip >= .05 else -1 if chip <= -.05 else 0) + (1 if loc >= .66 else -1 if loc <= .33 else 0) \
            + (1 if close > ma5 else -1)
        bias = "偏多" if votes > 0 else "偏空" if votes < 0 else "中性"

        r = close * range5 / 100
        long_entry = to_tick(min(q["high"], close + cfg["entry_r"] * r), True)
        short_entry = to_tick(max(q["low"], close - cfg["entry_r"] * r), False)
        picks.append({
            "code": code, "name": q["name"], "market": q["market"], "score": round(score / total * 100), "bias": bias,
            "close": close, "pct": round(pct, 2), "range_today": round(span / prev * 100, 2) if prev > 0 else 0,
            "range5": round(range5, 2), "dt_ratio": round(dt_ratio, 1), "dt_ratio6": round(dt_ratio6, 1),
            "dt_buy": dt_buy, "net": net[2], "foreign": net[0], "trust": net[1], "tick_pct": round(tick_pct, 2),
            "breakeven_ticks": math.ceil(cfg["cost_pct"] / tick_pct),
            "long_entry": long_entry, "long_stop": to_tick(long_entry - cfg["stop_r"] * r, False),
            "long_target": to_tick(long_entry + cfg["target_r"] * r, True),
            "short_entry": short_entry, "short_stop": to_tick(short_entry + cfg["stop_r"] * r, True),
            "short_target": to_tick(short_entry - cfg["target_r"] * r, False),
            "warn": dt_ratio >= 60 and dt_ratio6 >= 60})
    picks.sort(key=lambda p: (-p["score"], -p["dt_buy"]))
    excluded.sort(key=lambda e: -e["dt_buy"])
    return picks[:int(cfg["top_n"])], excluded[:15]


# ---------- results: how yesterday's plans would have done ----------

def simulate(entry, stop, target, o, h, l, c, cost, long, best=False):
    """Percent return after costs, None when the plan didn't trigger. Daily bars can't show the order of events, so
    a day touching both stop and target counts as a stop, or as the target with best=True to bracket the truth.
    A gap past the target is not chased."""
    if long:
        if h < entry or o >= target:
            return None
        fill = max(entry, o)
        out = target if best and h >= target else stop if l <= stop else target if h >= target else c
        return (out - fill) / fill * 100 - cost
    if l > entry or o <= target:
        return None
    fill = min(entry, o)
    out = target if best and l <= target else stop if h >= stop else target if l <= target else c
    return (fill - out) / fill * 100 - cost


def read_picks():
    if not PICKS.exists():
        return []
    with open(PICKS, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_picks(rows):
    DATA.mkdir(exist_ok=True)
    with open(PICKS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, P_COLS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["date"], int(r["rank"]))))


def save_picks(d, picks):
    """Replace day d's plans; other days keep their rows, results included."""
    rows = [r for r in read_picks() if r["date"] != d.isoformat()]
    for i, p in enumerate(picks, 1):
        rows.append({k: "" for k in P_COLS} | {k: p.get(k, "") for k in P_COLS} | {"date": d.isoformat(), "rank": i})
    write_picks(rows)


def evaluate(cfg, quotes):
    """Fill in results for plans whose next trading day is now in the history."""
    rows = read_picks()
    days = sorted(quotes)
    changed = False
    for r in rows:
        later = [k for k in days if k > r["date"]]
        # A plan older than the stored history can't be judged: its real next day may have been pruned.
        if r["next_date"] or not later or r["date"] < days[0]:
            continue
        q = quotes[later[0]].get(r["code"])
        if not q:
            continue        # Didn't trade that day (or the market's data is missing); try again next run.
        o, h, l, c = q["open"], q["high"], q["low"], q["close"]
        lp = simulate(float(r["long_entry"]), float(r["long_stop"]), float(r["long_target"]), o, h, l, c, cfg["cost_pct"], True)
        sp = simulate(float(r["short_entry"]), float(r["short_stop"]), float(r["short_target"]), o, h, l, c, cfg["cost_pct"], False)
        chosen = lp if r["bias"] == "偏多" else sp if r["bias"] == "偏空" else None
        fmt = lambda v: "" if v is None else f"{v:.3f}"
        r |= {"next_date": later[0], "next_open": o, "next_high": h, "next_low": l, "next_close": c,
              "long_pct": fmt(lp), "short_pct": fmt(sp), "bias_pct": fmt(chosen)}
        changed = True
    if changed:
        write_picks(rows)


def stats(rows=None, last_days=None):
    """Summary of the plan that follows each pick's bias, simulated as above."""
    rows = read_picks() if rows is None else rows
    done = [r for r in rows if r["next_date"]]
    if last_days:
        keep = sorted({r["date"] for r in done})[-last_days:]
        done = [r for r in done if r["date"] in keep]
    trades = [float(r["bias_pct"]) for r in done if r["bias_pct"]]
    days = sorted({r["date"] for r in done})
    out = {"plans": len(done), "days": len(days), "first": days[0] if days else "", "last": days[-1] if days else "",
           "trades": len(trades)}
    if trades:
        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t <= 0]
        out |= {"win": len(wins) / len(trades) * 100, "avg": sum(trades) / len(trades),
                "avg_win": sum(wins) / len(wins) if wins else 0, "avg_loss": sum(losses) / len(losses) if losses else 0}
    return out


# ---------- paper tracking: plans fixed before the result is known, on data no rule was tuned on ----------

def read_paper():
    if not PAPER.exists():
        return []
    with open(PAPER, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_paper(rows):
    DATA.mkdir(exist_ok=True)
    with open(PAPER, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, PA_COLS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["date"], r["strategy"], r["code"])))


def paper_plans(cfg, p):
    """Both strategies' plan for one pick, following its bias. A neutral pick has no direction, so no paper trade."""
    if p["bias"] == "中性":
        return []
    long = p["bias"] == "偏多"
    side = "long" if long else "short"
    pc = cfg["paper"]
    r = p["close"] * p["range5"] / 100
    entry = p[side + "_entry"]
    # The stop stays within the loss limit, and the target can't round back onto the entry.
    near_stop = to_tick(entry * (1 - pc["max_loss_pct"] / 100), True) if long else to_tick(entry * (1 + pc["max_loss_pct"] / 100), False)
    near_target = to_tick(entry + pc["target_r"] * r, True) if long else to_tick(entry - pc["target_r"] * r, False)
    if near_target == entry:
        near_target = round(entry + (tick_size(entry) if long else -tick_size(entry)), 2)
    cost = cfg["cost_pct"] + 2 * pc["slippage_ticks"] * tick_size(entry) / entry * 100
    base = {"code": p["code"], "name": p["name"], "side": side, "entry": entry, "cost_pct": round(cost, 3)}
    return [base | {"strategy": "current", "stop": p[side + "_stop"], "target": p[side + "_target"]},
            base | {"strategy": "near", "stop": near_stop, "target": near_target}]


def save_paper(cfg, d, picks):
    """Record day d's plans. Once a plan has a result it is never replaced, and days before the start don't count.
    Plans can only be written before the next session opens, so a later manual rerun can't rewrite them with hindsight."""
    if d.isoformat() < cfg["paper"]["start"]:
        return
    if datetime.now() >= datetime.combine(next_trading_day(d), datetime.min.time()).replace(hour=9):
        print(f"  {d} 的下一交易日已開盤，不寫入或更動紙上計畫")
        return
    rows = read_paper()
    settled = {(r["date"], r["strategy"], r["code"]) for r in rows if r["next_date"]}
    rows = [r for r in rows if r["date"] != d.isoformat() or (r["date"], r["strategy"], r["code"]) in settled]
    for p in picks:
        for plan in paper_plans(cfg, p):
            if (d.isoformat(), plan["strategy"], plan["code"]) not in settled:
                rows.append({k: "" for k in PA_COLS} | plan | {"date": d.isoformat()})
    write_paper(rows)


def evaluate_paper(quotes):
    """Fill in results for paper plans whose next trading day is now in the history."""
    rows = read_paper()
    days = sorted(quotes)
    changed = False
    for r in rows:
        later = [k for k in days if k > r["date"]]
        # A plan older than the stored history can't be judged: its real next day may have been pruned.
        if r["next_date"] or not later or r["date"] < days[0]:
            continue
        q = quotes[later[0]].get(r["code"])
        if not q:
            continue        # Didn't trade that day; try again next run.
        o, h, l, c = q["open"], q["high"], q["low"], q["close"]
        args = (float(r["entry"]), float(r["stop"]), float(r["target"]), o, h, l, c, float(r["cost_pct"]), r["side"] == "long")
        cons, opt = simulate(*args), simulate(*args, best=True)
        r |= {"next_date": later[0], "next_open": o, "next_high": h, "next_low": l, "next_close": c,
              "pct_cons": "" if cons is None else f"{cons:.3f}", "pct_opt": "" if opt is None else f"{opt:.3f}"}
        changed = True
    if changed:
        write_paper(rows)


def paper_stats(cfg, strategy, rows=None):
    """One strategy's record and whether it meets all three conditions, judged on the conservative numbers."""
    rows = read_paper() if rows is None else rows
    mine = [r for r in rows if r["strategy"] == strategy]
    cons = [float(r["pct_cons"]) for r in mine if r["pct_cons"]]
    opt = [float(r["pct_opt"]) for r in mine if r["pct_opt"]]
    out = {"plans": len(mine), "settled": sum(1 for r in mine if r["next_date"]), "trades": len(cons)}
    if cons:
        out |= {"win": sum(x > 0 for x in cons) / len(cons) * 100, "avg": sum(cons) / len(cons), "worst": min(cons),
                "win_opt": sum(x > 0 for x in opt) / len(opt) * 100, "avg_opt": sum(opt) / len(opt)}
    pc = cfg["paper"]
    out["checks"] = {"enough": len(cons) >= pc["min_trades"], "positive": bool(cons) and out["avg"] > 0,
                     "loss_ok": bool(cons) and out["worst"] >= -pc["worst_loss_pct"]}
    out["passed"] = all(out["checks"].values())
    return out


def paper_section(cfg, run_date):
    rows = read_paper()
    pc = cfg["paper"]
    body = ""
    for key, label in STRATEGIES.items():
        s = paper_stats(cfg, key, rows)
        if s["trades"]:
            nums = (f'<td>{s["win"]:.0f}%～{s["win_opt"]:.0f}%</td>'
                    f'<td class="{sign_class(s["avg"])}">{s["avg"]:+.2f}%～{s["avg_opt"]:+.2f}%</td><td>{s["worst"]:+.2f}%</td>')
        else:
            nums = "<td>—</td><td>—</td><td>—</td>"
        verdict = "三項條件都達到" if s["passed"] else "未達標"
        body += f'<tr><td>{label}</td><td>{s["trades"]} / {pc["min_trades"]}</td>{nums}<td>{verdict}</td></tr>'
    plans = [r for r in rows if r["date"] == run_date and r["strategy"] == "near"]
    plan_rows = "".join(f'<tr><td>{r["code"]} {escape(r["name"])}</td><td>{"做多" if r["side"] == "long" else "做空"}</td>'
                        f'<td>{float(r["entry"]):g}</td><td>{float(r["stop"]):g}</td><td>{float(r["target"]):g}</td></tr>' for r in plans)
    plan_html = ""
    if plan_rows:
        plan_html = ('<p class="note">下一交易日的「近目標＋硬停損」紙上計畫：</p><div class="wrap"><table class="list">'
                     f'<tr><th>股票</th><th>方向</th><th>進場</th><th>停損</th><th>目標</th></tr>{plan_rows}</table></div>')
    return f"""<h2>紙上追蹤（{pc['start']} 起，不含回測資料）</h2>
<div class="wrap"><table class="list"><tr><th>規則</th><th>觸發筆數</th><th>勝率</th><th>每筆平均</th><th>最差一筆</th><th>結果</th></tr>{body}</table></div>
<p class="note">計畫都在隔日開盤前記錄，不會事後修改。區間是日 K 看不出先後順序時的保守值～樂觀值，每筆已扣成本與來回滑價。達標條件以保守值計算：至少 {pc['min_trades']} 筆、每筆平均為正、最差一筆不超過 {pc['worst_loss_pct']:g}%。沒有實際下單，績效不代表實際成交結果。</p>
{plan_html}"""


# ---------- report ----------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2128;--mute:#667080;--line:#dfe3e8;--up:#d0312d;--down:#118a4e;--warn-bg:#fff5da;--warn:#7a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1d2127;--ink:#e7eaee;--mute:#9aa4b1;--line:#333a43;--up:#ff6b64;--down:#3fc687;--warn-bg:#3a3010;--warn:#f0d27a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "Noto Sans TC","Microsoft JhengHei",system-ui,sans-serif}
main{max-width:1000px;margin:auto;padding:24px 16px 48px}h1{font-size:24px;margin:0}h2{font-size:17px;margin:32px 0 10px}
.sub,.note{color:var(--mute);font-size:13px}.stats{display:flex;flex-wrap:wrap;gap:8px 28px;margin:14px 0}.stats b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
.stats span{color:var(--mute);font-size:12px}.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}.card h3{margin:0;font-size:16px}.card .right{float:right;font-weight:700;font-variant-numeric:tabular-nums}
.meta{color:var(--mute);font-size:13px;margin:4px 0 8px}.plan{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:14px}.plan th,.plan td{padding:3px 4px;text-align:right;border-top:1px solid var(--line)}.plan th:first-child,.plan td:first-child{text-align:left}.plan th{color:var(--mute);font-weight:500;font-size:12px}
.pref td:first-child{font-weight:700}.up{color:var(--up)}.down{color:var(--down)}.tag{display:inline-block;font-size:12px;padding:0 6px;border-radius:4px;background:var(--warn-bg);color:var(--warn);margin-top:6px}
.alert{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:10px 14px;margin:12px 0;font-size:14px}
.wrap{overflow-x:auto}table.list{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-size:14px}table.list th,table.list td{padding:6px 10px;text-align:left;border-top:1px solid var(--line);white-space:nowrap}table.list th{color:var(--mute);font-weight:500;font-size:12px}
"""


def sign_class(v):
    return "up" if v and v > 0 else "down" if v and v < 0 else ""


def lots(shares):
    return "—" if shares is None else f"{shares / 1000:+,.0f}"


def pick_card(p):
    prefer = {"偏多": "long", "偏空": "short"}.get(p["bias"])
    row = lambda key, label: (
        f'<tr class="{"pref" if prefer == key else ""}"><td>{label}</td><td>{p[key + "_entry"]:g}</td>'
        f'<td>{p[key + "_stop"]:g}</td><td>{p[key + "_target"]:g}</td></tr>')
    warn = '<div class="tag">6 日與今日當沖占比都超過 60%，留意被列注意股</div>' if p["warn"] else ""
    return f"""<div class="card">
<span class="right {sign_class(p['pct'])}">{p['close']:g}　{p['pct']:+.2f}%</span>
<h3>{escape(p['name'])} {p['code']}</h3>
<div class="meta">{MARKET[p['market']]}．{p['bias']}．評分 {p['score']}<br>
5 日平均振幅 {p['range5']:.1f}%（今日 {p['range_today']:.1f}%）．當沖占比 {p['dt_ratio']:.0f}%（6 日 {p['dt_ratio6']:.0f}%）<br>
當沖買進 {p['dt_buy'] / 1e8:,.1f} 億．法人 {lots(p['net'])} 張．打平約 {p['breakeven_ticks']} 檔</div>
<table class="plan"><tr><th>計畫</th><th>進場</th><th>停損</th><th>目標</th></tr>{row('long', '突破做多')}{row('short', '跌破做空')}</table>
{warn}</div>"""


def losing_note():
    """Warning shown first on the page and in the email while the rules lose money over the last 20 trading days."""
    s = stats(last_days=20)
    if s.get("trades", 0) >= 10 and s["avg"] < 0:
        return (f'規則近 {s["days"]} 個交易日回測為負：{s["trades"]} 筆平均 {s["avg"]:+.2f}%、勝率 {s["win"]:.0f}%。'
                "目前只適合觀察與紙上驗證，請勿直接照著下單。")
    return ""


def stat_block(s, label):
    if not s.get("trades"):
        return f"<p class=note>{label}：累積資料中。</p>"
    return (f'<div class="note">{label}：{s["first"]}～{s["last"]}，{s["days"]} 天 {s["plans"]} 個計畫，實際觸發 {s["trades"]} 筆</div>'
            f'<div class="stats"><div><b>{s["win"]:.0f}%</b><span>勝率</span></div>'
            f'<div><b class="{sign_class(s["avg"])}">{s["avg"]:+.2f}%</b><span>每筆平均（已扣成本）</span></div>'
            f'<div><b class="up">{s["avg_win"]:+.2f}%</b><span>平均獲利</span></div>'
            f'<div><b class="down">{s["avg_loss"]:+.2f}%</b><span>平均虧損</span></div></div>')


def write_report(cfg, latest, ts, extra_errors=()):
    REPORT.parent.mkdir(exist_ok=True)
    r = latest.get("run") or {}
    errors = list(r.get("errors", [])) + list(extra_errors)
    m = r.get("market", {})
    note = losing_note()
    alert = "".join(f'<div class="alert">⚠ {escape(str(e))}</div>' for e in ([note] if note else []) + errors)
    head = ""
    if m.get("index") is not None:
        head = (f'<div class="stats"><div><b class="{sign_class(m["change"])}">{m["index"]:,.2f}</b>'
                f'<span>加權指數（{m["change"]:+,.2f}）</span></div>'
                f'<div><b>{m["value"] / 1e8:,.0f} 億</b><span>成交金額</span></div>'
                f'<div><b>{m["dt_share"]:.1f}%</b><span>當沖占買進金額</span></div></div>')
    picks = r.get("picks", [])
    cards = "".join(pick_card(p) for p in picks) or "<p>今天沒有符合條件的個股。</p>"
    excl = "".join(f'<tr><td>{e["code"]} {escape(e["name"])}</td><td class="{sign_class(e["pct"])}">{e["pct"]:+.2f}%</td>'
                   f'<td>{e["dt_buy"] / 1e8:,.1f} 億</td><td>{escape(e["reason"])}</td></tr>' for e in r.get("excluded", []))
    excl = (f'<div class="wrap"><table class="list"><tr><th>股票</th><th>漲跌</th><th>當沖買進</th><th>排除原因</th></tr>{excl}</table></div>'
            if excl else "<p class=note>無。</p>")
    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>台股當沖候選</title><style>{CSS}</style></head><body><main>
<h1>台股當沖候選</h1>
<div class="sub">資料日 {r.get('date', '—')}，供 {r.get('next', '—')} 使用．產生於 {ts}</div>
{alert}{head}
<h2>候選股（依評分排序）</h2><div class="cards">{cards}</div>
<p class="note">粗體列是依法人動向、收盤位置和 5 日均線判斷的方向。價位由固定公式算出，並未考慮新聞與跳空；開盤跳空超過目標價就不追。</p>
{paper_section(cfg, r.get('date', ''))}
<h2>規則歷史績效（10 月起回測）</h2>
{stat_block(stats(last_days=20), '近 20 個交易日')}
<p class="note">依日 K 模擬：同一天碰到停損和目標都算停損，只計方向與判斷一致的計畫，每筆扣 {cfg['cost_pct']:g}% 成本。未計滑價與券源，實際成交結果會有差異。</p>
<h2>成交熱絡但被排除</h2>{excl}
<p class="note">當沖數字到 T+2 才定案。系統不看新聞，也不判斷個股大漲大跌的原因；開盤前請再確認公告與券商的當沖資格。本頁為程式依公開資料產生，僅供參考，不構成投資建議。</p>
</main></body></html>"""
    REPORT.write_text(html, encoding="utf-8")


def notify(latest):
    """Email the day's list. Credentials come from SMTP_USER, SMTP_PASS, MAIL_TO (optional SMTP_HOST, SMTP_PORT)."""
    user, pw, to = (os.environ.get(k, "") for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to):
        print("未設定 SMTP_USER／SMTP_PASS／MAIL_TO，不寄信")
        return False
    r = latest["run"]
    note = losing_note()
    lines = ([f"⚠ {note}", ""] if note else []) + [f'資料日 {r["date"]}，供 {r["next"]} 使用', ""]
    for p in r["picks"]:
        lines += [f'{p["code"]} {p["name"]}（{p["bias"]}，評分 {p["score"]}）收 {p["close"]:g}',
                  f'  做多 {p["long_entry"]:g}／停損 {p["long_stop"]:g}／目標 {p["long_target"]:g}',
                  f'  做空 {p["short_entry"]:g}／停損 {p["short_stop"]:g}／目標 {p["short_target"]:g}']
    if r["errors"]:
        lines += ["", "⚠ " + "；".join(r["errors"])]
    lines += ["", "【紙上追蹤】"]
    for key, label in STRATEGIES.items():
        s = paper_stats(load_config(), key)
        lines.append(f'{label}：{s["trades"]} 筆' + (f'，每筆 {s["avg"]:+.2f}%～{s["avg_opt"]:+.2f}%' if s["trades"] else ""))
    owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "").partition("/")
    if repo:
        lines += ["", f"完整報告：https://{owner.lower()}.github.io/{repo}/"]
    lines += ["", "程式依公開資料產生，未考慮新聞，僅供參考，不構成投資建議。"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = f'台股當沖候選 {r["next"]}（{len(r["picks"])} 檔）', user, to
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST") or "smtp.gmail.com", int(os.environ.get("SMTP_PORT") or 465), timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    print("已寄出")
    return True


# ---------- running ----------

def process(cfg, quotes, d, judge=True):
    """One day's whole pipeline -> the result stored in latest.json. Backfill passes judge=False because the days
    after d aren't all stored yet, and a plan must be judged against the very next trading day."""
    errors = []
    ensure_history(quotes, d, errors)
    flows = fetch_flows(d, errors)
    blocked, failed = fetch_blocks(d, errors)
    market = fetch_market(d, errors)
    picks, excluded = analyse(cfg, quotes, d, flows, blocked, failed)
    if failed:
        errors.append("注意／處置名單沒抓到的市場，今天一律不列入候選：" + "、".join(MARKET[m] for m in sorted(failed)))
    save_picks(d, picks)
    if judge:
        evaluate(cfg, quotes)
        evaluate_paper(quotes)
        save_paper(cfg, d, picks)
    save_quotes(quotes, cfg["retain_days"])
    print(f"  {d} 候選：" + ("、".join(f'{p["code"]} {p["name"]}({p["score"]})' for p in picks) or "無"))
    return {"date": d.isoformat(), "next": next_trading_day(d).isoformat(), "market": market, "picks": picks,
            "excluded": excluded, "errors": errors}


def backfill(cfg, days):
    """Run the rules over past trading days, oldest first, using only each day's data for that day's picks."""
    quotes = load_quotes()
    cfg["retain_days"] = max(cfg["retain_days"], days + 10)     # Keep every replayed day until its plans are judged.
    today, todo = date.today(), []
    d = today
    while len(todo) < days:
        d -= timedelta(days=1)
        if is_trading_day(d):
            todo.append(d)
    for d in reversed(todo):
        print(f"[{d}]", flush=True)
        try:
            result = process(cfg, quotes, d, judge=False)
            for e in result["errors"]:
                print(f"  ⚠ {e}")
        except NotReady as e:
            print(f"  略過（休市或無資料）：{e}")
    evaluate(cfg, quotes)
    save_quotes(quotes, load_config()["retain_days"])
    print(stats())


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    cfg = load_config()
    if len(sys.argv) > 2 and sys.argv[1] == "backfill":
        return backfill(cfg, int(sys.argv[2]))

    scheduled = os.environ.get("SCHEDULED") == "true"
    test = os.environ.get("TEST_EMAIL") == "true"
    chosen = os.environ.get("DATE")
    d = date.fromisoformat(chosen) if chosen else now.date()
    print(f"[{ts}] 資料日 {d}")
    if not is_trading_day(d) and os.environ.get("FORCE") != "true":
        print(f"{d} 休市：{holidays().get(d, '週末')}")
        return

    try:
        latest = json.loads(LATEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        latest = {}
    prev = latest.get("run", {})
    if scheduled and not test and prev.get("date") == d.isoformat() and not prev.get("errors"):
        print("今天已完成，略過")
        return
    tries = latest.setdefault("tries", ["", 0])
    if scheduled:
        tries[:] = [d.isoformat(), tries[1] + 1 if tries[0] == d.isoformat() else 1]

    extra = []
    done = False
    try:
        latest["run"] = process(cfg, load_quotes(), d)
        done = True
    except NotReady as e:
        extra.append(f"{d} 的資料還沒公布，以下仍是上一次的結果（{e}）")
    except Exception:
        traceback.print_exc()
        extra.append("未預期的錯誤，以下仍是上一次的結果")
    write_report(cfg, latest, ts, extra)

    # Email once per day. With data missing, the first scheduled try waits for the retry, which sends what it has.
    errors = latest.get("run", {}).get("errors", []) if done else extra
    if done and not chosen and (test or (scheduled and latest.get("emailed") != d.isoformat() and (not errors or tries[1] >= 2))):
        try:
            if notify(latest) and not test:
                latest["emailed"] = d.isoformat()
        except Exception:
            print("寄信失敗（報告已更新）：")
            traceback.print_exc()
    DATA.mkdir(exist_ok=True)
    LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    for e in errors:
        print(f"  ⚠ {e}")
    print("完成" if not errors else f"{len(errors)} 個問題")
    if errors and not scheduled:
        sys.exit(1)


if __name__ == "__main__":
    main()

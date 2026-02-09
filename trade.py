import os, json, subprocess, calendar, re
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


# ===================== CONFIG =====================
SYMBOLS = [
    "IBIT", "ALAB", "AMD", "ARM", "AVGO", "ASML",
    "MRVL", "MU", "NVDA", "PLTR", "TSLA", "TSM",
]

NOTIONAL_BY_SYMBOL = {
    "TSLA": Decimal("100"),
    "NVDA": Decimal("100"),
    # all others default to 60
}

DEFAULT_TRANCHE_NOTIONAL = Decimal("60")
DISCOUNT = Decimal("0.05")               # 5% below last daily close (on tranche creation day)
DATA_FEED = DataFeed.IEX                # Basic plan compatible (note: some tickers may require SIP depending on your plan)
STATE_FILE = "tranches_multi.json"
MONTHLY_DAY = 15

MIN_REPOST_NOTIONAL = Decimal("1.00")   # don't repost tiny leftover
COMPLETE_EPS = Decimal("0.01")          # treat <= 1 cent remaining as complete
LOOKBACK_DAYS_FIRST_SYNC = 180
# ==================================================

NY = ZoneInfo("America/New_York")

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "false").lower() == "true"

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def tranche_notional_for(symbol: str) -> Decimal:
    return NOTIONAL_BY_SYMBOL.get(symbol, DEFAULT_TRANCHE_NOTIONAL)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"schema": 1, "tranches": [], "last_sync_at_utc": None, "seen_order_filled_notional": {}}
    with open(STATE_FILE, "r") as f:
        s = json.load(f)
    s.setdefault("schema", 1)
    s.setdefault("tranches", [])
    s.setdefault("last_sync_at_utc", None)
    s.setdefault("seen_order_filled_notional", {})
    return s


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def commit_state_file_if_possible():
    # Requires workflow permissions: contents: write
    if not os.environ.get("GITHUB_TOKEN"):
        print("No GITHUB_TOKEN; not committing state file.")
        return
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "add", STATE_FILE], check=True)
        subprocess.run(["git", "commit", "-m", "Update tranche state"], check=True)
        subprocess.run(["git", "push"], check=True)
        print("Committed and pushed state file.")
    except subprocess.CalledProcessError as e:
        print("Did not commit/push state (maybe unchanged or missing perms).", e)


def get_last_daily_close(symbol: str) -> Decimal:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        limit=100,
        feed=DATA_FEED,
    )
    resp = data.get_stock_bars(req)
    bars = resp.data.get(symbol, []) if resp and resp.data else []
    if not bars:
        raise RuntimeError(f"No daily bars returned for {symbol}.")
    return Decimal(str(bars[-1].close))


def monthly_anchor_date(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def tranche_key_for_month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def should_create_new_tranche(today: date) -> bool:
    """
    Create monthly tranche on the 15th; if 15th is weekend, create next Monday.
    (Holidays not handled.)
    """
    anchor = monthly_anchor_date(today.year, today.month, MONTHLY_DAY)
    while anchor.weekday() >= 5:
        anchor = anchor + timedelta(days=1)
    return today == anchor


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def make_tranche_id(symbol: str, month_key: str) -> str:
    return f"{symbol}:{month_key}"


def parse_tranche_id_from_client_order_id(coid: str):
    """
    client_order_id format:
      BOT-SYMBOL-YYYY-MM-YYYYMMDD-HHMMSS
    """
    if not coid:
        return None
    parts = coid.split("-")
    # Expect: ["BOT", SYMBOL, "YYYY", "MM", "YYYYMMDD", "HHMMSS"]
    if len(parts) != 6 or parts[0] != "BOT":
        return None
    symbol = parts[1]
    if symbol not in SYMBOLS:
        return None
    month_key = f"{parts[2]}-{parts[3]}"
    if not re.fullmatch(r"\d{4}-\d{2}", month_key):
        return None
    return make_tranche_id(symbol, month_key)


def get_orders_all_symbols(status: QueryOrderStatus, limit=500):
    return trading.get_orders(
        filter=GetOrdersRequest(
            status=status,
            symbols=SYMBOLS,
            nested=True,
            limit=limit,
            direction="desc",
        )
    )


def sync_fills_into_state(state: dict):
    """
    Update remaining_notional per tranche by subtracting *new* filled notional.
    Uses per-order tracking to avoid double-counting.
    """
    tranche_map = {t["id"]: t for t in state.get("tranches", [])}

    last_sync = state.get("last_sync_at_utc")
    if last_sync:
        last_sync_dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
    else:
        last_sync_dt = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS_FIRST_SYNC)

    orders = get_orders_all_symbols(status=QueryOrderStatus.ALL, limit=500)
    seen = state.setdefault("seen_order_filled_notional", {})

    filled_notional_by_tranche = {}

    for o in orders:
        side = getattr(getattr(o, "side", None), "value", str(getattr(o, "side", ""))).lower()
        if side != "buy":
            continue

        tranche_id = parse_tranche_id_from_client_order_id(getattr(o, "client_order_id", "") or "")
        if not tranche_id:
            continue

        tstamp = getattr(o, "filled_at", None) or getattr(o, "updated_at", None) or getattr(o, "created_at", None)
        if not tstamp:
            continue
        if tstamp.astimezone(timezone.utc) < last_sync_dt:
            continue

        filled_qty = getattr(o, "filled_qty", None)
        filled_avg_price = getattr(o, "filled_avg_price", None)
        if filled_qty in (None, "", "0", 0) or filled_avg_price in (None, "", "0", 0):
            continue

        fq = Decimal(str(filled_qty))
        fap = Decimal(str(filled_avg_price))
        filled_notional = (fq * fap).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        oid = str(getattr(o, "id"))
        prev = Decimal(str(seen.get(oid, "0")))
        if filled_notional <= prev:
            continue

        delta = (filled_notional - prev).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        seen[oid] = str(filled_notional)

        filled_notional_by_tranche[tranche_id] = filled_notional_by_tranche.get(tranche_id, Decimal("0")) + delta

    for tranche_id, delta in filled_notional_by_tranche.items():
        t = tranche_map.get(tranche_id)
        if not t:
            # could happen if state file was deleted; ignore
            continue

        rem = Decimal(str(t.get("remaining_notional", "0.00")))
        rem = (rem - delta).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if rem <= COMPLETE_EPS:
            rem = Decimal("0.00")
            t["status"] = "complete"
        t["remaining_notional"] = str(rem)

        total = Decimal(str(t.get("filled_notional_total", "0.00"))) + delta
        t["filled_notional_total"] = str(total.quantize(Decimal("0.01"), rounding=ROUND_DOWN))

        print(f"{t['symbol']} {t['month_key']}: fills delta={delta} remaining={rem} status={t.get('status')}")

    state["last_sync_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def open_order_exists_for_tranche(tranche: dict) -> bool:
    """
    True if any OPEN BUY order exists for this tranche (symbol + month_key), regardless of date.
    """
    symbol = tranche["symbol"]
    month_key = tranche["month_key"]
    prefix = f"BOT-{symbol}-{month_key}-"  # BOT-SYMBOL-YYYY-MM-

    orders = get_orders_all_symbols(status=QueryOrderStatus.OPEN, limit=500)
    for o in orders:
        osym = getattr(o, "symbol", None)
        if osym != symbol:
            continue
        side = getattr(getattr(o, "side", None), "value", str(getattr(o, "side", ""))).lower()
        if side != "buy":
            continue
        coid = getattr(o, "client_order_id", "") or ""
        if coid.startswith(prefix):
            return True
    return False


def submit_day_order(tranche: dict, notional: Decimal):
    symbol = tranche["symbol"]
    month_key = tranche["month_key"]
    limit_price = Decimal(str(tranche["limit_price"])).quantize(Decimal("0.01"))

    today = datetime.now(NY).date()
    yyyymmdd = today.strftime("%Y%m%d")
    hhmmss = datetime.now(NY).strftime("%H%M%S")

    client_order_id = f"BOT-{symbol}-{month_key}-{yyyymmdd}-{hhmmss}"

    order = trading.submit_order(
        LimitOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            notional=float(notional),
            limit_price=float(limit_price),
            client_order_id=client_order_id,
        )
    )
    print(f"Submitted {symbol} tranche={month_key} notional={notional} limit={limit_price} id={order.id}")
    return order


if __name__ == "__main__":
    now_ny = datetime.now(NY)
    today = now_ny.date()
    month_key = tranche_key_for_month(today)
    print(f"NY now: {now_ny.isoformat()} | Mode: {'PAPER' if PAPER else 'LIVE'}")
    print(f"Symbols: {', '.join(SYMBOLS)}")

    state = load_state()
    state.setdefault("tranches", [])

    # 0) Sync fills first so remaining_notional is accurate
    sync_fills_into_state(state)

    # 1) Create new monthly tranche(s) on the 15th (once per symbol per month)
    if should_create_new_tranche(today):
        for sym in SYMBOLS:
            tranche_id = make_tranche_id(sym, month_key)
            exists = any(t.get("id") == tranche_id for t in state["tranches"])
            if exists:
                print(f"{sym} {month_key}: tranche already exists; not creating.")
                continue

            last_close = get_last_daily_close(sym)
            limit_price = (last_close * (Decimal("1") - DISCOUNT)).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            notional = tranche_notional_for(sym)

            state["tranches"].append({
                "id": tranche_id,
                "symbol": sym,
                "month_key": month_key,
                "created_at_ny": now_ny.isoformat(),
                "notional": str(notional),
                "remaining_notional": str(notional),
                "limit_price": str(limit_price),
                "status": "active",
                "filled_notional_total": "0.00",
            })
            print(f"Created tranche: {sym} {month_key} notional={notional} limit={limit_price} last_close={last_close}")
    else:
        print("Not tranche creation day; no new tranches created.")

    # 2) Repost DAY orders for active tranches (weekdays only)
    if not is_weekday(today):
        print("Weekend; skipping order placement.")
        state["updated_at_ny"] = now_ny.isoformat()
        save_state(state)
        commit_state_file_if_possible()
        raise SystemExit(0)

    for t in state["tranches"]:
        if t.get("status") != "active":
            continue

        remaining = Decimal(str(t.get("remaining_notional", "0.00"))).quantize(Decimal("0.01"))

        if remaining <= COMPLETE_EPS:
            t["status"] = "complete"
            t["remaining_notional"] = "0.00"
            print(f"{t['symbol']} {t['month_key']}: complete; skipping.")
            continue

        if remaining < MIN_REPOST_NOTIONAL:
            print(f"{t['symbol']} {t['month_key']}: remaining {remaining} < {MIN_REPOST_NOTIONAL}; not reposting further.")
            continue

        if open_order_exists_for_tranche(t):
            print(f"{t['symbol']} {t['month_key']}: already has an OPEN order; skipping repost.")
            continue

        submit_day_order(t, remaining)

    state["updated_at_ny"] = now_ny.isoformat()
    save_state(state)
    commit_state_file_if_possible()
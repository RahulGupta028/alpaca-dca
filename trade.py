import os
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed


# ====== CONFIG ======
SYMBOL = "TSLA"
NOTIONAL = Decimal("50")
DISCOUNT = Decimal("0.05")  # 5% below last daily close
DATA_FEED = DataFeed.IEX    # Basic plan compatible
# ====================

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "false").lower() == "true"

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def _side_str(side) -> str:
    return getattr(side, "value", str(side)).lower()


def has_buy_order_today(symbol: str) -> bool:
    """
    Avoid duplicates: returns True if *any* BUY order for `symbol` was created
    today (UTC), regardless of status (open/filled/canceled).
    This protects against running twice for DST cron or manual re-runs.
    """
    today_utc = datetime.now(timezone.utc).date()

    orders = trading.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            symbols=[symbol],
            nested=True,
            limit=200,
        )
    )

    for o in orders:
        if getattr(o, "symbol", None) != symbol:
            continue
        if _side_str(getattr(o, "side", "")) != "buy":
            continue

        created = getattr(o, "created_at", None)
        if created is None:
            continue

        if created.astimezone(timezone.utc).date() == today_utc:
            return True

    return False


def get_last_daily_close(symbol: str) -> Decimal:
    """
    Fetch last available daily close using IEX feed (Basic plan).
    Uses a date range to avoid empty results on weekends/holidays.
    Retries a few times then exits gracefully if still empty.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)

    last_err = None
    for attempt in range(1, 4):
        try:
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
            if bars:
                return Decimal(str(bars[-1].close))
            print(f"[Attempt {attempt}/3] No daily bars returned for {symbol}; retrying...")
        except Exception as e:
            last_err = e
            print(f"[Attempt {attempt}/3] Error fetching bars: {e}; retrying...")

    if last_err:
        print(f"Failed to fetch daily bars for {symbol}: {last_err}")
    raise RuntimeError(f"No daily bars returned for {symbol}.")


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    print(f"UTC now: {now.isoformat()}")
    print(f"Mode: {'PAPER' if PAPER else 'LIVE'}")
    print(f"Symbol: {SYMBOL}, Notional: {NOTIONAL}, Discount: {DISCOUNT}, Feed: {DATA_FEED}")

    # Avoid placing a buy if one was already created today (UTC)
    if has_buy_order_today(SYMBOL):
        print(f"A {SYMBOL} BUY order already exists for today (UTC); skipping.")
        raise SystemExit(0)

    # Get last close and compute limit
    try:
        last_close = get_last_daily_close(SYMBOL)
    except RuntimeError as e:
        # If you prefer to "skip gracefully" instead of failing the workflow, use exit(0)
        print(str(e))
        print(f"Skipping order due to missing market data for {SYMBOL}.")
        raise SystemExit(0)

    limit_price = (last_close * (Decimal("1") - DISCOUNT)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )

    print(f"Using last daily close for {SYMBOL}: {last_close}")
    print(f"Submitting DAY LIMIT BUY: ${NOTIONAL} notional @ {limit_price}")

    order = trading.submit_order(
        LimitOrderRequest(
            symbol=SYMBOL,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            notional=float(NOTIONAL),
            limit_price=float(limit_price),
        )
    )

    print("Submitted order id:", order.id)
    print("Created at:", getattr(order, "created_at", None))
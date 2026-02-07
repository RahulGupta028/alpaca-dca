import os
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


SYMBOL = "TSLA"
NOTIONAL = Decimal("50")
DISCOUNT = Decimal("0.05")  # 5%

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "false").lower() == "true"

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def _side_str(side) -> str:
    return getattr(side, "value", str(side)).lower()


def has_buy_order_today(symbol: str) -> bool:
    """
    Returns True if any BUY order for `symbol` was created today (UTC),
    regardless of status (open/filled/canceled), to prevent duplicates when
    cron runs twice or workflow is re-run manually.
    """
    today_utc = datetime.now(timezone.utc).date()

    orders = trading.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL,  # include filled/canceled too
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

        # created_at is usually timezone-aware; normalize to UTC date
        created_date_utc = created.astimezone(timezone.utc).date()
        if created_date_utc == today_utc:
            return True

    return False


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    print(f"UTC now: {now.isoformat()}")
    print(f"Mode: {'PAPER' if PAPER else 'LIVE'}")

    if has_buy_order_today(SYMBOL):
        print(f"A {SYMBOL} BUY order already exists for today (UTC); skipping.")
        raise SystemExit(0)

    # Get the most recent DAILY close (works for weekends/holidays)
    bars_req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame.Day,
        limit=10,
    )
    bars = data.get_stock_bars(bars_req).data.get(SYMBOL, [])
    if not bars:
        raise RuntimeError(f"No daily bars returned for {SYMBOL}.")

    last_close = Decimal(str(bars[-1].close))
    limit_price = (last_close * (Decimal("1") - DISCOUNT)).quantize(
        Decimal("0.01"), rounding=ROUND_DOWN
    )

    print(f"Using last daily close for {SYMBOL}: {last_close}")
    print(f"Submitting DAY LIMIT BUY: ${NOTIONAL} notional @ {limit_price} (5% below close)")

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
import os
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, date

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    QueryOrderStatus,
)

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


def already_has_open_buy_order(symbol: str) -> bool:
    # If the workflow runs twice (DST UTC cron), this prevents double-orders
    orders = trading.get_orders(
        filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol],
            nested=True,
        )
    )
    for o in orders:
        # o.side is an enum-like value; normalize to string safely
        side = getattr(o.side, "value", str(o.side)).lower()
        if o.symbol == symbol and side == "buy":
            return True
    return False


if __name__ == "__main__":
    print(f"UTC now: {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {'PAPER' if PAPER else 'LIVE'}")

    if already_has_open_buy_order(SYMBOL):
        print(f"Existing OPEN {SYMBOL} BUY order found; skipping to avoid duplicates.")
        raise SystemExit(0)

    # Get the most recent DAILY close (works for weekends, premarket, holidays)
    bars_req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame.Day,
        limit=10,  # extra cushion for long weekends/holidays
    )
    bars = data.get_stock_bars(bars_req).data.get(SYMBOL, [])
    if not bars:
        raise RuntimeError(f"No daily bars returned for {SYMBOL}.")

    last_bar = bars[-1]
    last_close = Decimal(str(last_bar.close))

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
    print("Submitted on (UTC date):", date.today().isoformat())

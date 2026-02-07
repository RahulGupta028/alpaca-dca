import os
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

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

# Get the most recent DAILY close (works for weekends, premarket, holidays)
req = StockBarsRequest(
    symbol_or_symbols=SYMBOL,
    timeframe=TimeFrame.Day,
    limit=5,  # enough to cover weekends/holidays
)
bars = data.get_stock_bars(req).data[SYMBOL]
if not bars:
    raise RuntimeError("No daily bars returned for TSLA.")

last_bar = bars[-1]
last_close = Decimal(str(last_bar.close))

limit_price = (last_close * (Decimal("1") - DISCOUNT)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

print(f"UTC now: {datetime.now(timezone.utc).isoformat()}")
print(f"Mode: {'PAPER' if PAPER else 'LIVE'}")
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

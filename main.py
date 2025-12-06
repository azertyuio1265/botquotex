# main.py
import asyncio
import time
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from quotexapi.stable_api import Quotex
from rich.console import Console
import aiohttp
from cryptography.fernet import Fernet

# -------------------------------------------------------
#                     WEB SERVER SETUP
# -------------------------------------------------------
app = FastAPI()
templates = Jinja2Templates(directory="templates")
console = Console()

# -------------------------------------------------------
#                   BOT GLOBAL SETTINGS
# -------------------------------------------------------
CANDLE_INTERVAL       = 60
TRADE_DURATION        = 60
SCAN_INTERVAL         = 0.2
MARTINGALE_MULTIPLIER = 2
MIN_VOLATILITY        = 0.002
TREND_BARS            = 20

MIN_MOMENTUM_BARS = 3
MAX_MOMENTUM_BARS = 5
LOOKBACK_BARS     = MAX_MOMENTUM_BARS + 1

ACTIVATION_SERVER = (
    "https://gist.githubusercontent.com/azerty197358/7c43ed0a9a01035fb67c0d1384e07135/"
    "raw/d97f4edd08f705a27302570ca70d876de9e50088/activation.txt"
)
ENCRYPTION_KEY = b'voEOGCimV0s0bW8gHEmAPxjvI3FksRZvYRNCclYALpY='
cipher_suite   = Fernet(ENCRYPTION_KEY)

client = None


# -------------------------------------------------------
#                   ACTIVATION CHECK
# -------------------------------------------------------
async def check_activation(code: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ACTIVATION_SERVER) as resp:
                if resp.status == 200:
                    encrypted = await resp.read()
                    decrypted = cipher_suite.decrypt(encrypted).decode()
                    return code == decrypted
    except:
        return False
    return False


# -------------------------------------------------------
#                     BOT FUNCTIONS
# -------------------------------------------------------
async def fetch_candles(asset, interval, lookback):
    now = time.time()
    aligned = now - (now % interval)
    end_ts = int(aligned) - 1
    try:
        raw = await client.get_candles(asset, end_ts, interval * lookback, interval)
        if not raw:
            return None
        return [
            {
                'from':  c.get('from', c.get('start', time.time())),
                'open':  float(c.get('open', 0)),
                'high':  float(c.get('max', c.get('high', 0))),
                'low':   float(c.get('min', c.get('low', 0))),
                'close': float(c.get('close', 0))
            }
            for c in raw
        ][-lookback:]
    except:
        return None


def detect_signal(candles):
    if len(candles) >= TREND_BARS:
        closes = [b['close'] for b in candles[-TREND_BARS:]]
        sma = sum(closes) / len(closes)
        trend = 'bull' if candles[-1]['close'] > sma else 'bear'
    else:
        trend = None
    
    highs = [b['high'] for b in candles[-(LOOKBACK_BARS-1):-1]]
    lows  = [b['low']  for b in candles[-(LOOKBACK_BARS-1):-1]]
    if (max(highs) - min(lows)) < MIN_VOLATILITY:
        return None

    for N in range(MAX_MOMENTUM_BARS, MIN_MOMENTUM_BARS - 1, -1):
        if len(candles) < N + 1:
            continue
        segment    = candles[-(N + 1):]
        momentum   = segment[:N]
        correction = segment[-1]

        if all(b['close'] > b['open'] for b in momentum) and correction['close'] < correction['open']:
            if trend in (None, 'bull'):
                return 'call'

        if all(b['close'] < b['open'] for b in momentum) and correction['close'] > correction['open']:
            if trend in (None, 'bear'):
                return 'put'

    return None



async def execute_trade(asset, amount, direction):
    ok, info = await client.buy(amount, asset, direction, TRADE_DURATION)
    if not ok:
        return None, 0.0

    await asyncio.sleep(TRADE_DURATION)

    prices = await client.get_realtime_price(asset)
    final  = prices[-1]['price'] if prices else None
    if final is None:
        return None, 0.0

    if final == info['openPrice']:
        return "Draw", 0.0

    win = ((direction == 'call' and final > info['openPrice']) or
           (direction == 'put'  and final < info['openPrice']))

    return ("Win" if win else "Loss"), (amount if win else -amount)



# -------------------------------------------------------
#              MAIN TRADING LOOP (BOT CORE)
# -------------------------------------------------------
async def run_bot(config):
    global client

    # Activate Bot
    activated = await check_activation(config["activation"])
    if not activated:
        console.print("[red]Activation failed[/red]")
        return

    # Connect
    client = Quotex(email=config["email"], password=config["password"], lang="en")

    while not await client.connect():
        await asyncio.sleep(3)

    client.change_account("REAL" if config["account"] == "real" else "PRACTICE")

    amount      = config["amount"]
    take_profit = config["tp"]
    stop_loss   = config["sl"]
    asset       = config["asset"]

    net_profit  = 0
    last_ts     = None
    multiplier  = 1

    while True:
        candles = await fetch_candles(asset, CANDLE_INTERVAL, LOOKBACK_BARS + TREND_BARS)
        if not candles:
            await asyncio.sleep(SCAN_INTERVAL)
            continue

        current_ts = int(candles[-1]['from'])
        if current_ts != last_ts:
            last_ts = current_ts
            signal = detect_signal(candles)

            if signal:
                result, profit = await execute_trade(asset, amount * multiplier, signal)
                net_profit += profit

                if result == "Loss":
                    multiplier *= 2
                    mres, mprofit = await execute_trade(asset, amount * multiplier, signal)
                    net_profit += mprofit
                    multiplier = 1
                else:
                    multiplier = 1

                if net_profit >= take_profit:
                    return
                if net_profit <= -stop_loss:
                    return

        await asyncio.sleep(SCAN_INTERVAL)



# -------------------------------------------------------
#                   WEB ROUTES
# -------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/start", response_class=HTMLResponse)
async def start(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    activation: str = Form(...),
    amount: float = Form(...),
    tp: float = Form(...),
    sl: float = Form(...),
    asset: str = Form(...),
    account: str = Form(...)
):

    config = {
        "email": email,
        "password": password,
        "activation": activation,
        "amount": amount,
        "tp": tp,
        "sl": sl,
        "asset": asset,
        "account": account.lower()
    }

    asyncio.create_task(run_bot(config))

    return templates.TemplateResponse(
        "started.html",
        {"request": request, "asset": asset}
    )

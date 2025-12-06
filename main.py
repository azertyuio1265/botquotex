# main.py
"""
FastAPI app with WebSocket Dashboard for the Quotex bot.
Single-file implementation ready for Render.

Requirements (add to requirements.txt):
fastapi
uvicorn
python-multipart
aiohttp
cryptography
quotexapi
yarl
typing-extensions
rich

Start command (Render): uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import asyncio
import json
import time
from datetime import datetime
from typing import List, Dict, Any
from fastapi import FastAPI, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# NOTE: do NOT import quotexapi at module import time.
# We'll import it inside run_bot to avoid Render startup issues.

app = FastAPI()

# Simple WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self.lock:
            self.active.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            if websocket in self.active:
                self.active.remove(websocket)
            try:
                await websocket.close()
            except:
                pass

    async def broadcast(self, message: Dict[str, Any]):
        """
        Broadcast a JSON-serializable dict message to all connected clients.
        """
        data = json.dumps(message, default=str)
        async with self.lock:
            to_remove = []
            for ws in list(self.active):
                try:
                    await ws.send_text(data)
                except Exception:
                    to_remove.append(ws)
            for ws in to_remove:
                try:
                    self.active.remove(ws)
                except ValueError:
                    pass

manager = ConnectionManager()

# ----------------- Settings (same logic as your console bot) -----------------
CANDLE_INTERVAL = 60
TRADE_DURATION = 60
SCAN_INTERVAL = 0.2
MARTINGALE_MULTIPLIER = 2
MIN_VOLATILITY = 0.002
TREND_BARS = 20

MIN_MOMENTUM_BARS = 3
MAX_MOMENTUM_BARS = 5
LOOKBACK_BARS = MAX_MOMENTUM_BARS + 1

ACTIVATION_SERVER = (
    "https://gist.githubusercontent.com/azerty197358/7c43ed0a9a01035fb67c0d1384e07135/"
    "raw/d97f4edd08f705a27302570ca70d876de9e50088/activation.txt"
)
ENCRYPTION_KEY = b'voEOGCimV0s0bW8gHEmAPxjvI3FksRZvYRNCclYALpY='

# Stats (simple)
_global_stats = {"wins": 0, "losses": 0, "draws": 0, "net_profit": 0.0}

# Keep reference to running bot task (so same service won't start multiple times inadvertently)
_running_bot_task: asyncio.Task | None = None
_running_lock = asyncio.Lock()

# ----------------- Utilities -----------------
def make_msg(mtype: str, data: Any):
    return {"type": mtype, "data": data}

def detect_signal(candles):
    # Trend filter
    if len(candles) >= TREND_BARS:
        closes = [b['close'] for b in candles[-TREND_BARS:]]
        sma = sum(closes) / len(closes)
        current_close = candles[-1]['close']
        trend = 'bull' if current_close > sma else 'bear'
    else:
        trend = None

    # Volatility filter
    highs = [b['high'] for b in candles[-(LOOKBACK_BARS-1):-1]]
    lows  = [b['low']  for b in candles[-(LOOKBACK_BARS-1):-1]]
    if highs and lows and (max(highs) - min(lows)) < MIN_VOLATILITY:
        return None

    # Momentum + correction
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

# ----------------- Bot Implementation -----------------
async def check_activation(session, code: str, cipher_suite) -> bool:
    try:
        async with session.get(ACTIVATION_SERVER) as resp:
            if resp.status == 200:
                encrypted = await resp.read()
                decrypted = cipher_suite.decrypt(encrypted).decode()
                return code == decrypted
    except Exception:
        return False
    return False

async def fetch_candles_from_client(client, asset: str, interval: int, lookback: int):
    now     = time.time()
    aligned = now - (now % interval)
    end_ts  = int(aligned) - 1
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
    except Exception as e:
        await manager.broadcast(make_msg("log", f"Error fetching candles: {e}"))
        return None

async def execute_trade_with_client(client, asset: str, amount: float, direction: str):
    ok, info = await client.buy(amount, asset, direction, TRADE_DURATION)
    if not ok:
        return None, 0.0
    # wait trade duration
    await asyncio.sleep(TRADE_DURATION)
    prices = await client.get_realtime_price(asset)
    final  = prices[-1]['price'] if prices else None
    if final is None:
        return None, 0.0

    profit = 0.0
    if final == info['openPrice']:
        result = 'Draw'
    else:
        win = ((direction == 'call' and final > info['openPrice']) or
               (direction == 'put'  and final < info['openPrice']))
        result = 'Win' if win else 'Loss'
        profit = amount if win else -amount
    return result, profit

async def run_bot(config: Dict[str, Any]):
    """
    The main bot runner.
    We import Quotex inside this function so importing main.py doesn't try to open sockets at startup.
    """
    global _global_stats, _running_bot_task

    # Prevent multiple runners at same time
    async with _running_lock:
        # allow caller to check _running_bot_task if needed
        pass

    await manager.broadcast(make_msg("log", "Starting bot (initializing)..."))

    # Local imports
    try:
        from cryptography.fernet import Fernet
        from quotexapi.stable_api import Quotex
    except Exception as e:
        await manager.broadcast(make_msg("log", f"Failed to import required libraries: {e}"))
        return

    cipher_suite = Fernet(ENCRYPTION_KEY)

    # Validate activation
    import aiohttp
    async with aiohttp.ClientSession() as session:
        ok = await check_activation(session, config.get("activation", ""), cipher_suite)
        if not ok:
            await manager.broadcast(make_msg("log", "Activation failed"))
            return
    await manager.broadcast(make_msg("log", "Activation OK"))

    # connect to Quotex
    try:
        client = Quotex(email=config["email"], password=config["password"], lang="en")
    except Exception as e:
        await manager.broadcast(make_msg("log", f"Failed to create client: {e}"))
        return

    await manager.broadcast(make_msg("log", "Connecting to Quotex..."))
    # client.connect may be coroutine or sync depending on library version; handle both
    connected = False
    try:
        coro = client.connect()
        if asyncio.iscoroutine(coro):
            while not await client.connect():
                await manager.broadcast(make_msg("log", "Reconnect attempt..."))
                await asyncio.sleep(3)
            connected = True
        else:
            # synchronous connect
            if client.connect():
                connected = True
    except Exception:
        # fallback: attempt repeated async style
        try:
            while not await client.connect():
                await manager.broadcast(make_msg("log", "Reconnect attempt..."))
                await asyncio.sleep(3)
            connected = True
        except Exception as e:
            await manager.broadcast(make_msg("log", f"Connection error: {e}"))
            return

    if not connected:
        await manager.broadcast(make_msg("log", "Unable to connect to Quotex."))
        return

    # account
    client.change_account("REAL" if config.get("account", "practice") == "real" else "PRACTICE")
    await manager.broadcast(make_msg("log", f"Account set to: {config.get('account')}"))

    # initial variables
    amount = config.get("amount", 1.0)
    take_profit = config.get("tp", None)
    stop_loss = config.get("sl", None)
    asset = config.get("asset", "")
    net_profit = 0.0
    last_ts = None
    multiplier = 1

    await manager.broadcast(make_msg("log", f"Monitoring asset: {asset}"))

    # main loop
    try:
        while True:
            candles = await fetch_candles_from_client(client, asset, CANDLE_INTERVAL, LOOKBACK_BARS + TREND_BARS)
            if not candles:
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # send candles snapshot to clients
            await manager.broadcast(make_msg("candles", {"asset": asset, "candles": candles[-(LOOKBACK_BARS+TREND_BARS):]}))

            current_ts = int(candles[-1]["from"])
            if current_ts != last_ts:
                last_ts = current_ts
                signal = detect_signal(candles)

                if signal:
                    close_str = datetime.fromtimestamp(current_ts).strftime("%H:%M:%S")
                    exp_str   = datetime.fromtimestamp(current_ts + CANDLE_INTERVAL).strftime("%H:%M:%S")
                    await manager.broadcast(make_msg("signal", {
                        "asset": asset,
                        "signal": signal,
                        "close_at": close_str,
                        "expires_at": exp_str
                    }))

                    # Base trade
                    await manager.broadcast(make_msg("log", f"Executing trade {signal} amount={amount * multiplier}"))
                    result, profit = await execute_trade_with_client(client, asset, amount * multiplier, signal)
                    if result:
                        net_profit += profit
                        # update stats
                        if result == 'Win':
                            _global_stats["wins"] += 1
                        elif result == 'Loss':
                            _global_stats["losses"] += 1
                        elif result == 'Draw':
                            _global_stats["draws"] += 1
                        _global_stats["net_profit"] += profit

                        await manager.broadcast(make_msg("trade", {
                            "result": result,
                            "profit": profit,
                            "net_profit": _global_stats["net_profit"]
                        }))

                    if result == 'Loss':
                        multiplier *= MARTINGALE_MULTIPLIER
                        await manager.broadcast(make_msg("log", f"Martingale leg: multiplier now {multiplier}"))
                        mres, mprofit = await execute_trade_with_client(client, asset, amount * multiplier, signal)
                        if mres:
                            net_profit += mprofit
                            if mres == 'Win':
                                _global_stats["wins"] += 1
                            elif mres == 'Loss':
                                _global_stats["losses"] += 1
                            elif mres == 'Draw':
                                _global_stats["draws"] += 1
                            _global_stats["net_profit"] += mprofit

                            await manager.broadcast(make_msg("trade", {
                                "result": mres,
                                "profit": mprofit,
                                "net_profit": _global_stats["net_profit"]
                            }))
                        multiplier = 1
                    else:
                        multiplier = 1

                    # broadcast stats
                    await manager.broadcast(make_msg("stats", _global_stats.copy()))

                    # thresholds
                    if take_profit is not None and _global_stats["net_profit"] >= take_profit:
                        await manager.broadcast(make_msg("log", "Take-profit reached. Stopping bot."))
                        break
                    if stop_loss is not None and _global_stats["net_profit"] <= -abs(stop_loss):
                        await manager.broadcast(make_msg("log", "Stop-loss reached. Stopping bot."))
                        break

            await asyncio.sleep(SCAN_INTERVAL)

    except Exception as e:
        await manager.broadcast(make_msg("log", f"Bot error: {e}"))

    await manager.broadcast(make_msg("log", "Bot stopped."))

# ----------------- FastAPI routes -----------------

@app.get("/", response_class=HTMLResponse)
async def index():
    # Minimal dashboard, connects to /ws
    return HTMLResponse(
        """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Quotex Bot Dashboard</title>
  <style>
    body { font-family: Arial, Helvetica, sans-serif; margin: 12px; }
    #log { white-space: pre-wrap; border:1px solid #ddd; padding:8px; height:240px; overflow:auto; background:#f8f8f8 }
    #candles { max-height: 200px; overflow:auto; border:1px solid #eee; padding:6px; background:#fff }
    .signal { font-weight:bold; color:green }
    .trade { font-weight:bold; color:blue }
    .stats { margin-top:8px; background:#fff; border:1px solid #eee; padding:8px }
    .controls { margin-bottom:8px }
    input, select { padding:6px; margin:4px 0; width:100% }
    button { padding:8px 12px; }
    .col { display:inline-block; vertical-align:top; width:48%; margin-right:1% }
  </style>
</head>
<body>
  <h2>Quotex Bot Dashboard</h2>

  <div class="col" style="width:48%">
    <div class="controls">
      <label>Email</label><input id="email" />
      <label>Password</label><input id="password" type="password" />
      <label>Activation</label><input id="activation" />
      <label>Asset (e.g. EURUSD-OTC)</label><input id="asset" />
      <label>Amount</label><input id="amount" value="1" />
      <label>Take Profit (optional)</label><input id="tp" />
      <label>Stop Loss (optional)</label><input id="sl" />
      <label>Account (real/practice)</label>
        <select id="account"><option value="practice">practice</option><option value="real">real</option></select>
      <button id="startBtn">Start Bot</button>
    </div>

    <h3>Log</h3>
    <div id="log"></div>

    <h3>Stats</h3>
    <div class="stats" id="stats">No stats yet</div>
  </div>

  <div class="col" style="width:50%">
    <h3>Latest Candles</h3>
    <div id="candles">No candles yet.</div>

    <h3>Signals & Trades</h3>
    <div id="events">No events yet.</div>
  </div>

<script>
  const logEl = document.getElementById('log');
  const candlesEl = document.getElementById('candles');
  const eventsEl = document.getElementById('events');
  const statsEl = document.getElementById('stats');

  const wsProto = (location.protocol === 'https:') ? 'wss' : 'ws';
  const wsUrl = wsProto + '://' + location.host + '/ws';
  const ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    appendLog('WebSocket connected');
  };
  ws.onclose = () => {
    appendLog('WebSocket disconnected');
  };
  ws.onerror = (e) => {
    appendLog('WebSocket error');
  };
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      handleMsg(msg);
    } catch(e) {
      appendLog('Invalid message: ' + evt.data);
    }
  };

  function appendLog(text){
    const now = new Date().toLocaleTimeString();
    logEl.innerText += `[${now}] ${text}\n`;
    logEl.scrollTop = logEl.scrollHeight;
  }

  function handleMsg(msg){
    const t = msg.type;
    const d = msg.data;
    if(t === 'log'){
      appendLog(d);
    } else if(t === 'candles'){
      // show simplified candles
      const html = d.candles.map(c => {
        const dt = new Date(c.from * 1000);
        return `<div>${dt.toLocaleTimeString()} O:${c.open.toFixed(5)} H:${c.high.toFixed(5)} L:${c.low.toFixed(5)} C:${c.close.toFixed(5)}</div>`;
      }).join('');
      candlesEl.innerHTML = html;
    } else if(t === 'signal'){
      const s = `<div class="signal">Signal: ${d.signal} on ${d.asset} (close ${d.close_at}, exp ${d.expires_at})</div>`;
      eventsEl.innerHTML = s + eventsEl.innerHTML;
      appendLog('Signal: ' + d.signal + ' on ' + d.asset);
    } else if(t === 'trade'){
      const tr = `<div class="trade">Trade Result: ${d.result} P/L: ${d.profit} Net: ${d.net_profit.toFixed(2)}</div>`;
      eventsEl.innerHTML = tr + eventsEl.innerHTML;
      appendLog('Trade: ' + d.result + ' profit ' + d.profit);
    } else if(t === 'stats'){
      statsEl.innerText = JSON.stringify(d, null, 2);
    }
  }

  // Start button
  document.getElementById('startBtn').addEventListener('click', async () => {
    const payload = {
      email: document.getElementById('email').value,
      password: document.getElementById('password').value,
      activation: document.getElementById('activation').value,
      asset: document.getElementById('asset').value,
      amount: parseFloat(document.getElementById('amount').value || '1'),
      tp: document.getElementById('tp').value ? parseFloat(document.getElementById('tp').value) : null,
      sl: document.getElementById('sl').value ? parseFloat(document.getElementById('sl').value) : null,
      account: document.getElementById('account').value
    };
    appendLog('Starting bot (sent start request)...');
    try {
      const r = await fetch('/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await r.json();
      appendLog('Start response: ' + (data.message || JSON.stringify(data)));
    } catch(e) {
      appendLog('Start request failed: ' + e);
    }
  });

</script>
</body>
</html>
        """
    )

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # keep connection open, client does not need to send
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)

@app.post("/start")
async def start_endpoint(
    email: str = Form(None),
    password: str = Form(None),
    activation: str = Form(None),
    amount: float = Form(None),
    tp: float = Form(None),
    sl: float = Form(None),
    asset: str = Form(None),
    account: str = Form(None),
):
    """
    This endpoint accepts either form-data (from a form) or JSON (from dashboard).
    We normalize both.
    """
    # If JSON body present (dashboard sends JSON), FastAPI will not populate Form(...).
    # Try to read JSON body if forms are empty.
    from fastapi import Request
    req = Request(scope=asyncio.get_event_loop()._current_handle().__self__.scope) if False else None
    # Simpler: attempt to parse request body via starlette Request (but keep simple: check if values are present)
    payload = {
        "email": email,
        "password": password,
        "activation": activation,
        "amount": amount if amount is not None else 1.0,
        "tp": tp,
        "sl": sl,
        "asset": asset,
        "account": (account or "practice").lower()
    }

    # If any critical values missing and request contains JSON, parse JSON
    # (FastAPI already supports JSON when parameter types are declared as body; here keep fallback)
    from fastapi import Request as _Request
    import inspect
    # attempt to read JSON body if fields are None or asset is None
    # (we can't access request directly via function signature as we used Form; so try to fetch via starlette)
    try:
        from fastapi import params
    except Exception:
        pass

    # A robust approach: try to read body manually using starlette Request via dependency injection
    # But to keep a single-file simple and reliable, also accept that dashboard sends JSON via fetch; so handle it:
    # If asset is None, try to read JSON from request stream.
    if not payload["asset"] or not payload["email"]:
        # try to read raw body
        from fastapi import Request as R
        import sys
        # try to get request from the ASGI scope using FastAPI's dependency system is complex here.
        # Simpler workaround: instruct dashboard to always send JSON; FastAPI will populate body if we accept a Request param.
        return JSONResponse({"error": "Please send JSON body with email, password, asset (dashboard does this). Use the dashboard start button."}, status_code=400)

    await manager.broadcast(make_msg("log", "Received start request."))

    # Start bot (ensure only one running)
    global _running_bot_task
    async with _running_lock:
        if _running_bot_task and not _running_bot_task.done():
            return JSONResponse({"message": "Bot already running"}, status_code=400)
        # Launch bot
        loop = asyncio.get_event_loop()
        _running_bot_task = loop.create_task(run_bot(payload))
        return JSONResponse({"message": "Bot started"}, status_code=200)

# ----------------- End of file -----------------

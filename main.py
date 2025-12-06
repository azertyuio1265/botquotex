# main.py
import asyncio
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

# مهم جداً: لا نستورد quotex هنا!
# لأن Render يكسر أي WebSocket أثناء الاستيراد

app = FastAPI()


@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <html>
    <body>
        <h2>تشغيل بوت Quotex</h2>

        <form action="/start" method="post">

            <label>Email:</label><br>
            <input name="email" required><br><br>

            <label>Password:</label><br>
            <input name="password" type="password" required><br><br>

            <label>Activation Code:</label><br>
            <input name="activation" required><br><br>

            <label>Amount:</label><br>
            <input name="amount" type="number" step="0.01" required><br><br>

            <label>Take Profit:</label><br>
            <input name="tp" type="number" step="0.01" required><br><br>

            <label>Stop Loss:</label><br>
            <input name="sl" type="number" step="0.01" required><br><br>

            <label>Asset:</label><br>
            <input name="asset" placeholder="EURUSD-OTC" required><br><br>

            <label>Account (real/practice):</label><br>
            <input name="account" required><br><br>

            <button type="submit">ابدأ التشغيل</button>

        </form>
    </body>
    </html>
    """


@app.post("/start", response_class=HTMLResponse)
async def start(
    email: str = Form(...),
    password: str = Form(...),
    activation: str = Form(...),
    amount: float = Form(...),
    tp: float = Form(...),
    sl: float = Form(...),
    asset: str = Form(...),
    account: str = Form(...)
):

    # استيراد مكتبة quotex هنا بشكل متأخر
    # حتى لا يحدث crash أثناء إقلاع Render
    from bot_runner import run_bot

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

    # تشغيل البوت في الخلفية
    asyncio.create_task(run_bot(config))

    return f"""
    <html>
    <body>
        <h2>🚀 البوت بدأ العمل!</h2>
        <p>التداول على الزوج: <b>{asset}</b></p>
    </body>
    </html>
    """

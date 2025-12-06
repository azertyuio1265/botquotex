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


# -----------------------------------------------------------
# ░░ هنا نضع البوت نفسه بدل ملف bot_runner المفقود ░░
# -----------------------------------------------------------

async def run_bot(config):
    """
    الكود الحقيقي للبوت يوضع هنا.
    لكي لا نخلق مشاكل على Render، نستورد quotex داخل هذه الوظيفة فقط.
    """

    import asyncio
    from quotexapi.stable_api import Quotex

    email = config["email"]
    password = config["password"]
    amount = config["amount"]
    account = config["account"]
    tp = config["tp"]
    sl = config["sl"]
    asset = config["asset"]

    print("🚀 بدء تشغيل البوت...")

    # الاتصال
    client = Quotex(email, password)

    if not client.connect():
        print("❌ فشل تسجيل الدخول")
        return

    print("✔ تم تسجيل الدخول بنجاح")

    # اختيار الحساب
    if account == "practice":
        client.change_account("practice")
    else:
        client.change_account("real")

    print(f"💰 الحساب المختار: {account}")

    profit = 0
    loss = 0

    # حلقة التداول الأساسية
    while True:
        try:
            print(f"🔄 تنفيذ الصفقة على {asset} بقيمة {amount}")
            order = client.buy(amount, asset, "turbo")

            if order:
                print("✔ الصفقة أُرسلت")

                # انتظار انتهاء الصفقة
                await asyncio.sleep(40)

                result = client.check_win(order)

                if result > 0:
                    profit += result
                    print(f"🟢 ربح: {result} | إجمالي الأرباح: {profit}")
                else:
                    loss += abs(result)
                    print(f"🔴 خسارة: {abs(result)} | إجمالي الخسائر: {loss}")

                # TP / SL
                if profit >= tp:
                    print("🎉 Take Profit تحقق! إيقاف البوت.")
                    break
                if loss >= sl:
                    print("⛔ Stop Loss تحقق! إيقاف البوت.")
                    break

            else:
                print("⚠ فشل إرسال الصفقة!")

            await asyncio.sleep(3)

        except Exception as e:
            print("⚠ خطأ:", e)
            await asyncio.sleep(5)


# -----------------------------------------------------------


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

    # تجهيز الإعدادات
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

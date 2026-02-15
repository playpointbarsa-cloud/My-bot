import os
import re
import sqlite3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.ext import AIORateLimiter

DB_PATH = "numbers.db"
NUM_RE = re.compile(r"\b\d{6,}\b")

def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS numbers (
            num TEXT PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_prefix6 ON numbers (substr(num,1,6))
    """)
    con.commit()
    con.close()

def db_insert_many(nums):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    inserted = 0
    for n in nums:
        try:
            cur.execute("INSERT INTO numbers(num) VALUES(?)", (n,))
            inserted += 1
        except:
            pass
    con.commit()
    con.close()
    return inserted

def db_find(prefix6, limit=2000):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT num FROM numbers WHERE substr(num,1,6)=? LIMIT ?",
        (prefix6, limit),
    )
    rows = [r[0] for r in cur.fetchall()]
    con.close()
    return rows

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ابعت ملف ارقام او اكتب اول 6 ارقام للبحث.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    content = await file.download_as_bytearray()
    text = content.decode("utf-8", errors="ignore")

    nums = NUM_RE.findall(text)
    inserted = db_insert_many(nums)

    await update.message.reply_text(f"تم حفظ {inserted} رقم جديد.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.isdigit() and len(text) == 6:
        results = db_find(text)
        if not results:
            await update.message.reply_text("لا يوجد نتائج.")
        else:
            msg = "\n".join(results[:100])
            await update.message.reply_text(msg)

def main():
    db_init()

    TOKEN = os.getenv("BOT_TOKEN")
    PORT = int(os.getenv("PORT", 8000))
    WEBHOOK_URL = os.getenv("RAILWAY_STATIC_URL")

    app = Application.builder().token(TOKEN).rate_limiter(AIORateLimiter()).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"https://{WEBHOOK_URL}"
    )

if __name__ == "__main__":
    main()

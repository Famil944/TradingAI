from dotenv import load_dotenv
import os

from bot.telegram_bot import run_telegram_bot

load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")

print("=" * 40)
print("Trading AI Bot")
print("=" * 40)

if not token:
    print("❌ Telegram token NOT found")
    exit()

print("✅ Telegram token loaded")

run_telegram_bot(token)
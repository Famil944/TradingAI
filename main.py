from dotenv import load_dotenv
import os

def main():
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    print("=" * 40)
    print("Trading AI Bot")
    print("=" * 40)

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не найден")

    from bot.telegram_bot import run_telegram_bot

    print("✅ Telegram token loaded")
    run_telegram_bot(token)


if __name__ == "__main__":
    main()

import asyncio
import os
import requests
from dotenv import load_dotenv


load_dotenv()


class TelegramNotifier:

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = None
        self.bot = None

    def setup(self, bot, chat_id):
        self.chat_id = chat_id
        self.bot = bot

    def set_chat_id(self, chat_id):
        self.chat_id = chat_id

    def send(self, text):
        from services.app_settings import AppSettings

        notification_mode = AppSettings().get("notifications")
        if notification_mode == "off":
            return False
        if notification_mode == "trades":
            important_markers = (
                "сделк",
                "позици",
                "ошиб",
                "stop",
                "take profit",
                "pnl",
                "🚀",
                "✅",
                "❌",
            )
            if not any(
                marker in str(text).lower()
                for marker in important_markers
            ):
                return False
        if not self.token:
            print("❌ TELEGRAM_BOT_TOKEN не найден в .env")
            return False

        if not self.chat_id:
            print("⚠️ Chat ID ещё не установлен")
            return False

        url = (
            f"https://api.telegram.org/"
            f"bot{self.token}/sendMessage"
        )

        data = {
            "chat_id": self.chat_id,
            "text": text,
        }

        try:
            response = requests.post(
                url,
                data=data,
                timeout=10,
            )

            response.raise_for_status()
            return True

        except requests.RequestException as error:
            print(
                f"❌ Ошибка Telegram-уведомления: {error}"
            )
            return False

    async def send_async(self, text):
        """Send without blocking the application's asyncio event loop."""
        return await asyncio.to_thread(self.send, text)


notifier = TelegramNotifier()

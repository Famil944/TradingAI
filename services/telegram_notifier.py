class TelegramNotifier:

    def __init__(self):
        self.chat_id = None
        self.bot = None

    def setup(self, bot, chat_id):
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, text: str):
        if not self.bot or not self.chat_id:
            return

        await self.bot.send_message(
            chat_id=self.chat_id,
            text=text
        )
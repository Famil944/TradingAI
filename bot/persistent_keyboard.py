from telegram import ReplyKeyboardMarkup


def persistent_keyboard():
    keyboard = [
        ["🏠 Меню"]
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        is_persistent=True
    )
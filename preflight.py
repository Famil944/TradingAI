"""Offline checks; does not connect or print secret values."""

from importlib.metadata import version

from config.settings import settings

if __name__ == "__main__":
    print("OTCharts", version("otcharts"))
    for name, present in [
        ("TELEGRAM_BOT_TOKEN", bool(settings.telegram_bot_token)),
        ("OTCHARTS_API_KEY", bool(settings.otcharts_api_key)),
        ("OTC_ADMIN_CHAT_ID", settings.admin_chat_id > 0),
    ]:
        print(name + (": задан" if present else ": отсутствует"))
    print(
        "Режим:",
        "тестовый (неизвестная выплата разрешена)" if settings.test_mode else "строгий",
    )
    print(
        "Live-соединения не проверялись. При отсутствии потока тест использует снимки свечей."
    )

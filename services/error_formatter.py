def user_error_message(error):
    code = getattr(error, "error_code", None)
    if code == -2015:
        return (
            "Binance отклонил API-ключ. Проверьте, что ключ создан "
            "для выбранного Demo/Live-счёта, разрешена Futures-торговля "
            "и IP входит в whitelist."
        )
    if code is not None:
        message = getattr(error, "error_message", "Ошибка Binance")
        return f"Binance {code}: {message}"

    text = str(error)
    if "Message is not modified" in text:
        return None
    if len(text) > 500:
        text = text[:500] + "…"
    return text

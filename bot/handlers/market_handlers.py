from bot.inline_menus import market_menu


async def show_market_scan(query, scanner, format_scan_results):
    await query.edit_message_text("⏳ Сканирую рынок...")

    results = scanner.scan_market("1h", 10)
    text = format_scan_results(results)

    await query.edit_message_text(
        text,
        reply_markup=market_menu(),
    )


async def show_btc_price(query, core):
    result = core.analyze_symbol("BTCUSDT", "1h")

    await query.edit_message_text(
        f"📊 BTC/USDT: {result['price']:,.2f} USDT",
        reply_markup=market_menu(),
    )
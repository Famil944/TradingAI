from paper.paper_controller import PaperController


paper = PaperController()
print(paper.on())

signal_data = {
    "symbol": "BTCUSDT",
    "interval": "1h",
    "signal": "🔴 SHORT",
    "price": 100000,
    "score": -65,
    "risk": "Средний"
}

print("Открываем SHORT")
print(paper.try_trade_text(signal_data))

print("\nПроверяем закрытие по TAKE PROFIT")
print(paper.check_position_text(99000))

print("\nСтатус после закрытия")
print(paper.status_text())
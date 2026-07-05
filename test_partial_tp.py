from paper.paper_controller import PaperController


paper = PaperController()
paper.on()

signal = {
    "symbol": "BTCUSDT",
    "interval": "1h",
    "signal": "🟢 LONG",
    "price": 100000,
    "score": 70,
    "risk": "Средний"
}

print("=== Открываем LONG ===")
print(paper.try_trade_text(signal))

position = paper.engine.trader.position

print("\nДо роста:")
print("SL:", position.stop_loss)
print("Amount:", position.amount)

print("\nЦена выросла до 100800")
paper.engine.check_position(100800)

position = paper.engine.trader.position

print("\nПосле Partial TP + Break Even:")
print("SL:", position.stop_loss)
print("Amount:", position.amount)
print("Partial Closed:", position.partial_closed)
print("Balance:", paper.engine.account.get_balance())
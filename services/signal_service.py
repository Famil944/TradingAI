from database.signal_repository import SignalRepository


class SignalService:
    def __init__(self):
        self.repository = SignalRepository()

    def save_signal(self, signal_data: dict):
        try:
            self.repository.save(signal_data)
            return True
        except Exception as e:
            print(f"Ошибка сохранения сигнала: {e}")
            return False
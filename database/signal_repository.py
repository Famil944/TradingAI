from database.db import Database


class SignalRepository:
    def init(self):
        self.db = Database()
        self.db.init_db()

    def save(self, signal_data: dict):
        self.db.save_signal(signal_data)
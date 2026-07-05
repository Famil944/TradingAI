from datetime import datetime
from pathlib import Path


class LoggerService:

    def __init__(self):
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        self.log_file = self.log_dir / "trading_ai.log"

    def log(self, message: str):
        time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{time}] {message}"

        print(line)

        with open(self.log_file, "a", encoding="utf-8") as file:
            file.write(line + "\n")
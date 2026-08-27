import time


class PositionMonitor:

    def __init__(self, paper_engine):
        self.paper_engine = paper_engine
        self.running = False

    def start(self):
        self.running = True

        while self.running:
            try:
                self.paper_engine.update_position()
            except Exception as e:
                print(f"PositionMonitor: {e}")

            time.sleep(5)

    def stop(self):
        self.running = False
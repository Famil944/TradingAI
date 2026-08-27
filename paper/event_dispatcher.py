class EventDispatcher:

    def __init__(self):
        self.listeners = []

    def register(self, listener):
        self.listeners.append(listener)

    def dispatch(self, event):
        for listener in self.listeners:
            try:
                listener(event)
            except Exception as e:
                print(f"EventDispatcher error: {e}")
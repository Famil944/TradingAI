class AutoState:

    def __init__(self):
        self.enabled = False

    def turn_on(self):
        self.enabled = True
        return "🤖 Авто-режим включён"

    def turn_off(self):
        self.enabled = False
        return "⛔ Авто-режим выключен"

    def status(self):
        return "включён" if self.enabled else "выключен"
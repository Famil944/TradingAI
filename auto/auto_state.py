from services.app_settings import AppSettings


class AutoState:

    def __init__(self, settings=None):
        self.settings = settings or AppSettings()
        self.enabled = self.settings.get("auto_enabled") == "true"

    def turn_on(self):
        self.enabled = True
        self.settings.set("auto_enabled", "true")
        return "🤖 Авто-режим включён"

    def turn_off(self):
        self.enabled = False
        self.settings.set("auto_enabled", "false")
        return "⛔ Авто-режим выключен"

    def emergency_stop(self):
        """Immediately persist a block on all new automatic entries."""
        self.enabled = False
        self.settings.set("auto_enabled", "false")
        return (
            "🛑 Аварийная остановка включена.\n"
            "Новые сделки запрещены. Мониторинг и защита уже открытых "
            "позиций продолжают работать."
        )

    def status(self):
        return "включён" if self.enabled else "выключен"

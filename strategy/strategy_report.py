class StrategyReport:

    def build_report(self, symbol, analysis, strategy_check, multi_check):
        direction = strategy_check.get("direction", "UNKNOWN")

        lines = []
        lines.append("🤖 Strategy Report\n")
        lines.append(f"Монета: {symbol}")
        lines.append(f"Направление: {direction}")
        lines.append(f"Сигнал: {analysis['signal']}")
        lines.append(f"Основной Score: {analysis['score']} / 100\n")

        lines.append("Strategy Filter:")
        lines.append(self._status("Стратегия разрешила вход", strategy_check["approved"]))

        lines.append("")
        lines.append("Multi-Timeframe:")
        lines.append(self._status(
            f"{direction} совпадений: {multi_check['match_count']} / 3, нужно {multi_check['required']} / 3",
            multi_check["match_count"] >= multi_check["required"]
        ))
        lines.append(self._status(
            f"Средний Score: {multi_check['avg_score']} / 100, нужно 60",
            abs(multi_check["avg_score"]) >= 60
        ))

        lines.append("")
        lines.append("Итог:")

        if strategy_check["approved"] and multi_check["approved"]:
            lines.append("✅ Сделка разрешена")
        else:
            lines.append("🚫 Сделка отклонена")

        if not strategy_check["approved"]:
            lines.append("")
            lines.append(f"Причина: {strategy_check['reason']}")

        return "\n".join(lines)

    def _status(self, name, passed):
        icon = "✅" if passed else "❌"
        return f"{icon} {name}"
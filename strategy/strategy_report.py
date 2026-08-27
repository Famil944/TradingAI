class StrategyReport:

    def build_report(
        self,
        symbol,
        analysis,
        strategy_check,
        multi_check,
        quality
    ):
        direction = strategy_check.get("direction", "UNKNOWN")

        result = []

        result.append("🤖 Strategy Report\n")

        result.append(f"Монета: {symbol}")
        result.append(f"Направление: {direction}")
        result.append(f"Сигнал: {analysis['signal']}")
        result.append(f"Цена: {analysis['price']}")
        result.append("")

        result.append(
            f"⭐ Quality Score: "
            f"{quality['rating']} "
            f"{quality['score']}/100"
        )

        result.append("")

        result.append("Strategy")

        result.append(
            self._line(
                "Strategy Filter",
                strategy_check["approved"]
            )
        )

        result.append(
            self._line(
                "Multi-Timeframe",
                multi_check["approved"]
            )
        )

        result.append("")

        if strategy_check["approved"] and multi_check["approved"]:
            result.append("✅ Сделка разрешена")
        else:
            result.append("🚫 Сделка отклонена")

        if not strategy_check["approved"]:
            result.append("")
            result.append(strategy_check["reason"])

        return "\n".join(result)

    def _line(self, name, ok):
        icon = "✅" if ok else "❌"
        return f"{icon} {name}"
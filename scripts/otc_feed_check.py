"""Read-only summary of local TradingAI stream logs.

Usage: python -m scripts.otc_feed_check --log logs/termux-bot.log
No network access, broker access, SQLite writes, or API calls.
"""

import argparse
import re
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
REMAINING = re.compile(r"(?:осталось запросов:|запросы:)\s*(\d+)\s*(?:из|/)\s*(\d+)")
TRACKED = (
    ("Реальные обрывы/переподключения", "OTC reconnect:"),
    ("Подтверждения свежего потока", "OTC fresh stream confirmed"),
    ("Плановые замены пар", "OTC pair rotated:"),
    ("Подготовленные замены пар", "OTC pair rotation prepared:"),
    ("Успешные восстановления истории", "OTC history reconciliation:"),
    ("Восстановления после обрыва", "OTC reconnect history refreshed:"),
    ("Ошибки восстановления истории", "OTC history repair failed:"),
    ("Сбои обновления каталога", "OTC catalogue refresh failed:"),
    ("Срабатывания watchdog", "OTC fresh-tick watchdog expired:"),
)


def when(line):
    match = TIMESTAMP.match(line)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def summarize(lines, minutes=30):
    lines = list(lines)
    stamped = [(when(line), line) for line in lines]
    last = next((at for at, _ in reversed(stamped) if at), None)
    if last is None:
        return "Нет строк журнала с датой и временем."
    cutoff = last - timedelta(minutes=minutes)
    recent = [line for at, line in stamped if at and cutoff <= at <= last]
    counts = Counter({title: sum(needle in line for line in recent)
                      for title, needle in TRACKED})
    health = next((line for line in reversed(lines) if "OTC health:" in line), "")
    quota = next((m for line in reversed(lines) if
                  (m := REMAINING.search(line))), None)
    if "Нет свежих котировок" in health:
        state = "Нет свежих котировок (на момент последней записи здоровья)"
    elif "поток активен" in health:
        state = "Поток активен (на момент последней записи здоровья)"
    else:
        state = "Последнее состояние не определено"
    data = [
        "TRADINGAI · состояние потока (только локальный лог)",
        f"Последняя запись журнала: {last:%Y-%m-%d %H:%M:%S} (время устройства)",
        f"Период подсчёта: последние {minutes} мин относительно этой записи",
        f"Статус: {state}",
    ]
    if quota:
        remaining, maximum = map(int, quota.groups())
        data.append(f"Последний снимок API: осталось {remaining} из {maximum} запросов (не realtime)")
    for title, count in counts.items():
        data.append(f"{title}: {count}")
    diagnostics = [
        ("Готовность данных", "OTC readiness gate diagnostics"),
        ("Поток по парам", "OTC per-pair tick diagnostics"),
        ("Окна S2–S4", "OTC S2-S4 candidate production diagnostics"),
    ]
    for title, marker in diagnostics:
        last_diag = next((when(line) for line in reversed(lines) if marker in line), None)
        data.append(
            f"{title}: последнее сообщение {last_diag:%H:%M:%S}"
            if last_diag else f"{title}: пока нет отчёта"
        )
    data.append("Проверки качества и торговые решения не изменяются этим отчётом.")
    return "\n".join(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="logs/termux-bot.log",
                        help="Путь к локальному журналу работы")
    parser.add_argument("--minutes", type=int, default=30,
                        help="Окно подсчёта относительно последней записи")
    args = parser.parse_args(argv)
    if not 1 <= args.minutes <= 1440:
        parser.error("--minutes должно быть от 1 до 1440")
    logfile = Path(args.log)
    try:
        with logfile.open("r", encoding="utf-8", errors="replace") as handle:
            tail = deque(handle, maxlen=25000)
    except OSError as exc:
        parser.error(f"Не удалось открыть лог {logfile}: {type(exc).__name__}")
    print(summarize(tail, args.minutes))


if __name__ == "__main__":
    main()

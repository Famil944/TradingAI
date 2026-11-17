"""Local paper account in integer kopeks. Never places broker orders."""

import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation


def amount(text):
    try:
        value = Decimal(str(text).replace(",", "."))
        if (
            not value.is_finite()
            or value < 0
            or value > 10000000
            or value != value.quantize(Decimal(".01"))
        ):
            raise ValueError(
                "Сумма: от 0 до 10 000 000, не более двух знаков после запятой."
            )
        return int(value * 100)
    except (InvalidOperation, TypeError):
        raise ValueError("Введите число, например 100 или 100,50.") from None


def rub(cents):
    return f"{cents / 100:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


class VirtualWallet:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS wallet(id INTEGER PRIMARY KEY CHECK(id=1), initial INTEGER,balance INTEGER,stakes TEXT,payout INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS cycles(id TEXT PRIMARY KEY,stakes TEXT,payout INTEGER,reserved INTEGER,status TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS attempts(cycle TEXT,step INTEGER,outcome TEXT,delta INTEGER,at REAL,PRIMARY KEY(cycle,step));
            CREATE TABLE IF NOT EXISTS adjustments(at REAL,delta INTEGER,balance INTEGER);
            """)
            db.execute(
                "INSERT OR IGNORE INTO wallet VALUES(1,500000,500000,'[10000,25000,62500]',9200,?)",
                (time.time(),),
            )
            db.commit()

    def state(self):
        with closing(sqlite3.connect(self.path)) as db:
            initial, balance, stakes, payout, created = db.execute(
                "SELECT initial,balance,stakes,payout,created FROM wallet WHERE id=1"
            ).fetchone()
            reserved = db.execute(
                "SELECT COALESCE(sum(reserved),0) FROM cycles"
            ).fetchone()[0]
            profit = db.execute(
                "SELECT COALESCE(sum(delta),0) FROM attempts"
            ).fetchone()[0]
            adjustments = db.execute(
                "SELECT COALESCE(sum(delta),0) FROM adjustments"
            ).fetchone()[0]
            counts = dict(
                db.execute(
                    "SELECT status,count(*) FROM cycles GROUP BY status"
                ).fetchall()
            )
        return dict(
            initial=initial,
            balance=balance,
            stakes=json.loads(stakes),
            payout=payout,
            created=created,
            reserved=reserved,
            available=balance - reserved,
            profit=profit,
            adjustments=adjustments,
            counts=counts,
        )

    def change(self, kind, text):
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("BEGIN IMMEDIATE")
            if kind == "balance":
                value = amount(text)
                reserved = db.execute(
                    "SELECT COALESCE(sum(reserved),0) FROM cycles"
                ).fetchone()[0]
                if value < reserved:
                    raise ValueError(
                        "Баланс не может быть меньше зарезервированной суммы: "
                        + rub(reserved)
                    )
                old = db.execute("SELECT balance FROM wallet WHERE id=1").fetchone()[0]
                db.execute("UPDATE wallet SET balance=? WHERE id=1", (value,))
                db.execute(
                    "INSERT INTO adjustments VALUES(?,?,?)",
                    (time.time(), value - old, value),
                )
            elif kind == "stakes":
                parts = text.split()
                if len(parts) != 3:
                    raise ValueError("Введите три ставки через пробел: 100 250 625")
                stakes = [amount(v) for v in parts]
                if min(stakes) <= 0:
                    raise ValueError("Все три ставки должны быть больше нуля.")
                db.execute(
                    "UPDATE wallet SET stakes=? WHERE id=1", (json.dumps(stakes),)
                )
            elif kind == "payout":
                value = amount(text.rstrip("%"))
                if not 0 < value <= 10000:
                    raise ValueError("Выплата должна быть больше 0 и не выше 100%.")
                db.execute("UPDATE wallet SET payout=? WHERE id=1", (value,))
            else:
                raise ValueError("Неизвестная настройка")
            db.commit()

    def enroll(self, identifier):
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT stakes,payout,status FROM cycles WHERE id=?", (identifier,)
            ).fetchone()
            if existing:
                return dict(
                    stakes=json.loads(existing[0]),
                    payout=existing[1],
                    status=existing[2],
                )
            balance, raw, payout = db.execute(
                "SELECT balance,stakes,payout FROM wallet WHERE id=1"
            ).fetchone()
            stakes = json.loads(raw)
            held = db.execute(
                "SELECT COALESCE(sum(reserved),0) FROM cycles"
            ).fetchone()[0]
            accepted = balance - held >= sum(stakes)
            status = "ACTIVE" if accepted else "SKIPPED"
            db.execute(
                "INSERT INTO cycles VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    raw,
                    payout,
                    sum(stakes) if accepted else 0,
                    status,
                    time.time(),
                ),
            )
            db.commit()
            return dict(stakes=stakes, payout=payout, status=status)

    def cancel(self, identifier):
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute(
                "UPDATE cycles SET reserved=0,status='CANCELLED' WHERE id=? AND status IN ('ACTIVE','SKIPPED') AND NOT EXISTS(SELECT 1 FROM attempts WHERE cycle=?)",
                (identifier, identifier),
            )
            db.commit()

    def settle(self, identifier, outcomes):
        if len(outcomes) != 3:
            raise ValueError("Expected exactly three virtual outcomes")
        with closing(sqlite3.connect(self.path, timeout=2)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT stakes,payout,status FROM cycles WHERE id=?", (identifier,)
            ).fetchone()
            if not row or row[2] != "ACTIVE":
                return
            stakes, payout = json.loads(row[0]), row[1]
            status = "ACTIVE"
            reserve = sum(stakes)
            for i, outcome in enumerate(outcomes):
                if outcome == "PENDING":
                    reserve = sum(stakes[i:])
                    break
                if outcome == "UNKNOWN":
                    status = "UNKNOWN"
                    reserve = stakes[i]
                    break
                if outcome not in ("WIN", "LOSS", "DRAW"):
                    raise ValueError("Invalid virtual outcome")
                delta = (
                    (stakes[i] * payout + 5000) // 10000
                    if outcome == "WIN"
                    else -stakes[i]
                    if outcome == "LOSS"
                    else 0
                )
                previous = db.execute(
                    "SELECT outcome FROM attempts WHERE cycle=? AND step=?",
                    (identifier, i),
                ).fetchone()
                if previous and previous[0] != outcome:
                    raise ValueError("Previously booked virtual outcome cannot change")
                inserted = db.execute(
                    "INSERT OR IGNORE INTO attempts VALUES(?,?,?,?,?)",
                    (identifier, i, outcome, delta, time.time()),
                ).rowcount
                if inserted:
                    db.execute(
                        "UPDATE wallet SET balance=balance+? WHERE id=1", (delta,)
                    )
                if outcome in ("WIN", "DRAW"):
                    status = outcome
                    reserve = 0
                    break
                reserve = sum(stakes[i + 1 :])
                if i == 2:
                    status = "LOSS"
            db.execute(
                "UPDATE cycles SET status=?,reserved=? WHERE id=?",
                (status, reserve, identifier),
            )
            db.commit()

    def delta(self, identifier):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                "SELECT COALESCE(sum(delta),0) FROM attempts WHERE cycle=?",
                (identifier,),
            ).fetchone()[0]

    def text(self):
        s = self.state()
        stakes = " / ".join(rub(v) for v in s["stakes"])
        return (
            f"🧪 Виртуальный счёт · Боллинджер ×3\nБаланс: {rub(s['balance'])}\n"
            f"Результат известных исходов: {rub(s['profit'])}\nСтарт: {rub(s['initial'])} · ручные изменения: {rub(s['adjustments'])}\n"
            f"Свободно: {rub(s['available'])} · резерв: {rub(s['reserved'])}\n"
            f"Ставки: {stakes}\nПрибыль WIN: {s['payout'] / 100:g}% ставки\n"
            f"Неизвестных циклов: {s['counts'].get('UNKNOWN', 0)} · пропуск по балансу: {s['counts'].get('SKIPPED', 0)}\n"
            "UNKNOWN: ставка заморожена, прибыль неполна. Настройки действуют только на новые циклы. Реальных сделок нет."
        )

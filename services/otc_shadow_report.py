"""Matched prospective comparisons using the same observed tick endpoints."""

import json
import math
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from services.otc_candidate_report import net, outcome, readonly, summary
from services.otc_shadow_variants import VERSION


def report(path, since, now):
    result = dict(
        version=VERSION,
        candidates=0,
        pairs=[],
        filters=[],
        cycles=[],
        automatic_promotion=False,
    )
    if not Path(path).exists():
        return result
    with closing(readonly(path)) as db:
        records = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM candidates WHERE entry_at>=? AND observed_at>=? AND observed_at<=? ORDER BY entry_at,symbol",
                (since, since, now),
            )
        ]
        samples = {
            (s, target): price
            for s, target, at, received, price in db.execute(
                "SELECT symbol,target,tick_at,received_at,price FROM samples WHERE target>=? AND received_at<=?",
                (since, now),
            )
            if target <= at <= received <= target + 2
            and math.isfinite(price)
            and price > 0
        }
    records = [
        r
        for r in records
        if r.get("experiment", {}).get("version")
        in (
            VERSION,
            "price-pattern-study-v3",
            "expiry-context-study-v1",
            "expiry-context-study-v2-m2-m3",
        )
        and r["observed_at"] < r["entry_at"]
    ]
    result["candidates"] = len(records)
    cycle_groups = defaultdict(list)
    for row in records:
        seconds = row["experiment"].get("cycle_seconds")
        if seconds:
            outcomes = []
            for attempt in range(3):
                start = row["entry_at"] + attempt * seconds
                end = start + seconds
                outcomes.append(
                    "PENDING"
                    if now < end + 2
                    else outcome(
                        row["direction"],
                        samples.get((row["symbol"], start)),
                        samples.get((row["symbol"], end)),
                    )
                )
            cycle_groups[seconds].append(cycle_outcome(outcomes))
    for seconds, outcomes in sorted(cycle_groups.items()):
        from collections import Counter

        counts = dict(Counter(outcomes))
        w, loss = counts.get("WIN", 0), counts.get("LOSS", 0)
        result["cycles"].append(
            dict(
                seconds=seconds,
                counts=counts,
                total=len(outcomes),
                win_rate=w / (w + loss) if w + loss else None,
            )
        )
    records = [r for r in records if not r["experiment"].get("cycle_seconds")]

    def resolve(row):
        if now < row["expires_at"] + 2:
            return "PENDING"
        return outcome(
            row["direction"],
            samples.get((row["symbol"], row["entry_at"])),
            samples.get((row["symbol"], row["expires_at"])),
        )

    def days(rows):
        return len(
            {
                datetime.fromtimestamp(r["entry_at"], ZoneInfo("Europe/Moscow")).date()
                for r in rows
            }
        )

    singles = {
        (
            r["version"],
            r["symbol"],
            r["entry_at"],
            r["direction"],
            r["expires_at"] - r["entry_at"],
        ): r
        for r in records
        if r["expires_at"] - r["entry_at"] in (60, 120, 900)
        and not r["version"].endswith("-m2-shadow")
    }
    pairs = defaultdict(list)
    for row in records:
        parent = (
            row["experiment"].get("paired_m2_version")
            or row["experiment"].get("paired_m1_version")
            or row["experiment"].get("paired_m15_version")
        )
        base_seconds = (
            120
            if row["experiment"].get("paired_m2_version")
            else 60
            if row["experiment"].get("paired_m1_version")
            else 900
        )
        alternative_seconds = row["expires_at"] - row["entry_at"]
        if parent:
            first = singles.get(
                (parent, row["symbol"], row["entry_at"], row["direction"], base_seconds)
            )
            if first:
                pairs[parent, base_seconds, alternative_seconds].append((first, row))
    for (version, base_seconds, alternative_seconds), group in pairs.items():
        complete = [
            (a, b)
            for a, b in group
            if resolve(a) in ("WIN", "LOSS", "DRAW")
            and resolve(b) in ("WIN", "LOSS", "DRAW")
        ]
        first, second = [a for a, b in complete], [b for a, b in complete]
        deltas = [
            net(b, resolve(b)) - net(a, resolve(a))
            for a, b in complete
            if net(a, resolve(a)) is not None and net(b, resolve(b)) is not None
        ]
        result["pairs"].append(
            dict(
                strategy=version,
                baseline_minutes=base_seconds // 60,
                alternative_minutes=alternative_seconds // 60,
                total=len(group),
                paired=len(complete),
                unresolved=len(group) - len(complete),
                days=days(first),
                **{
                    f"m{base_seconds // 60}": summary(
                        first, [resolve(r) for r in first]
                    ),
                    f"m{alternative_seconds // 60}": summary(
                        second, [resolve(r) for r in second]
                    ),
                },
                delta_per_pair=sum(deltas) / len(deltas) if deltas else None,
            )
        )
    groups = defaultdict(list)
    for row in records:
        seconds = row["expires_at"] - row["entry_at"]
        if seconds in (120, 300, 900) and not row["version"].endswith("-m2-shadow"):
            groups[row["version"], seconds, row["experiment"]["version"]].append(row)
    for (version, seconds, study_version), group in groups.items():
        policies = ("higher_alignment", "regime_entry", "not_extended", "combined")
        if seconds == 120:
            policies = ()
        policies += ("bollinger_context", "min_two_confirmations", "two_families")
        if seconds == 900:
            policies += ("h1_alignment", "m30_h1_alignment")
        if study_version == VERSION:
            policies = tuple(
                dict.fromkeys(
                    key
                    for r in group
                    for key in r["experiment"]["masks"]
                    if key != "baseline"
                )
            )
        for policy in policies:
            # Unknown context is not a successful abstention: remove it from BOTH arms.
            evaluable = [
                r for r in group if r["experiment"]["masks"].get(policy) is not None
            ]
            complete = [r for r in evaluable if resolve(r) in ("WIN", "LOSS", "DRAW")]
            chosen = [r for r in complete if r["experiment"]["masks"][policy]]
            changes = [
                net(r, resolve(r)) * (int(r["experiment"]["masks"][policy]) - 1)
                for r in complete
                if net(r, resolve(r)) is not None
            ]
            result["filters"].append(
                dict(
                    strategy=version,
                    study_version=study_version,
                    seconds=seconds,
                    policy=policy,
                    candidates=len(group),
                    missing_context=len(group) - len(evaluable),
                    unresolved=len(evaluable) - len(complete),
                    selected_pending=sum(
                        bool(r["experiment"]["masks"][policy])
                        and resolve(r) == "PENDING"
                        for r in evaluable
                    ),
                    selected_unknown=sum(
                        bool(r["experiment"]["masks"][policy])
                        and resolve(r) == "UNKNOWN"
                        for r in evaluable
                    ),
                    paired=len(complete),
                    days=days(complete),
                    baseline=summary(complete, [resolve(r) for r in complete]),
                    selected=summary(chosen, [resolve(r) for r in chosen]),
                    delta_per_opportunity=sum(changes) / len(changes)
                    if changes
                    else None,
                )
            )
    return result


def cycle_outcome(outcomes):
    if "WIN" in outcomes:
        return "WIN"
    if "PENDING" in outcomes:
        return "PENDING"
    if "UNKNOWN" in outcomes:
        return "UNKNOWN"
    return "LOSS" if outcomes == ["LOSS"] * 3 else "DRAW"


def format_active_rates(data, seconds):
    lines = [
        f"Фоновый винрейт · M{seconds // 60}",
        "Боллинджер · до трёх попыток в исходном направлении",
    ]
    item = next((c for c in data.get("cycles", []) if c["seconds"] == seconds), None)
    counts = item["counts"] if item else {}
    w, loss = counts.get("WIN", 0), counts.get("LOSS", 0)
    rate = f"{100 * w / (w + loss):.1f}%" if w + loss else "—"
    lines += [
        f"Циклы: WIN {w} / LOSS {loss} · WR {rate}",
        f"Ничьи {counts.get('DRAW', 0)} · ожидают {counts.get('PENDING', 0)} · нет результата {counts.get('UNKNOWN', 0)}",
        "Один WIN завершает цикл; LOSS — только три проигрыша. Без ставок и расчёта прибыли. Новые циклы одной пары/экспирации не пересекаются в пределах трёх попыток.",
    ]
    from services.otc_community_patterns import LABELS

    for policy, label in LABELS.items():
        items = [
            f
            for f in data["filters"]
            if f.get("study_version") == VERSION
            and f["seconds"] == seconds
            and f["policy"] == policy
        ]
        counts = {
            key: sum(f["selected"]["outcomes"].get(key, 0) for f in items)
            for key in ("WIN", "LOSS", "DRAW")
        }
        w, loss = counts["WIN"], counts["LOSS"]
        rate = f"{100 * w / (w + loss):.1f}%" if w + loss else "—"
        lines += [
            f"\n{label}: WIN {w} / LOSS {loss} · WR {rate}",
            f"Ничьи {counts['DRAW']} · ожидают {sum(f['selected_pending'] for f in items)} · нет результата {sum(f['selected_unknown'] for f in items)}",
        ]
    if seconds == 300:
        items = [
            f
            for f in data["filters"]
            if f.get("study_version") == VERSION
            and f["seconds"] == 300
            and f["policy"] == "strict_primary"
        ]
        w = sum(f["selected"]["outcomes"].get("WIN", 0) for f in items)
        loss = sum(f["selected"]["outcomes"].get("LOSS", 0) for f in items)
        rate = f"{100 * w / (w + loss):.1f}%" if w + loss else "—"
        lines += [
            f"\nСовпадение двух основных фильтров M5: WIN {w} / LOSS {loss} · WR {rate}"
        ]
    return "\n".join(lines)


def format_legacy_rates(data, seconds):
    labels = {
        "previous_primary": "Прежний Основной · фон",
        "strict_primary": "Строгий Основной · фон",
        "bollinger_reversal": "Боллинджер: от границы обратно",
        "alternating_four": "Чередование четырёх M1",
        "min_two_confirmations": "Минимум два подтверждения",
        "two_families": "Минимум две группы признаков",
        "not_extended": "Без чрезмерного движения",
    }
    lines = [
        f"Фоновый винрейт · M{seconds // 60}",
        "Текущие проверки после сброса. Виртуальные возможности, не отправленные сигналы.",
    ]
    for policy, label in labels.items():
        if (policy == "alternating_four" and seconds != 120) or (
            policy == "not_extended" and seconds != 300
        ):
            continue
        items = [
            f
            for f in data["filters"]
            if f.get("study_version") == VERSION
            and f["seconds"] == seconds
            and f["policy"] == policy
        ]
        counts = {
            key: sum(f["selected"]["outcomes"].get(key, 0) for f in items)
            for key in ("WIN", "LOSS", "DRAW")
        }
        w, loss, draw = counts["WIN"], counts["LOSS"], counts["DRAW"]
        rate = f"{100 * w / (w + loss):.1f}%" if w + loss else "—"
        pending = sum(f.get("selected_pending", 0) for f in items)
        unknown = sum(f.get("selected_unknown", 0) for f in items)
        lines.extend(
            [
                f"\n{label}",
                f"WIN {w} / LOSS {loss} / ничьи {draw} · WR {rate}",
                f"Ожидают {pending} · нет результата {unknown}",
            ]
        )
    lines.append(
        "\nWR = WIN / (WIN + LOSS). Правила могут пересекаться; их результаты не суммируются. M3 отключён."
    )
    return "\n".join(lines)


def format_report(data):
    lines = [
        "Фон: Боллинджер ×3 и совпадение фильтров M5; прежние варианты — архив · "
        + data["version"],
        "Виртуальные кандидаты, не дополнительные сигналы. Тики в пределах 2 с от цели; отдельно от обычной статистики.",
        "Не учитывают конкуренцию сигналов, паузы и ограничение одной позиции. Выплата условно одинаковая для сравниваемых экспираций; H1 на платформе не подтверждена.",
        "Пропущенные данные исключены из обеих сторон сравнения. Итог условный, ставка 1.",
    ]
    for seconds in (120, 300, 900):
        lines.append(format_active_rates(data, seconds))

    def score(m):
        o = m["outcomes"]
        return (
            f"{o.get('WIN', 0)} WIN / {o.get('LOSS', 0)} LOSS / {o.get('DRAW', 0)} DRAW"
        )

    def delta(value):
        return "—" if value is None else f"{value:+.3f}"

    for item in data["pairs"]:
        base, alt = item["baseline_minutes"], item["alternative_minutes"]
        base_label, alt_label = f"M{base}", "H1" if alt == 60 else f"M{alt}"
        lines.append(
            f"{item['strategy']} · {base_label}→{alt_label}: общих пар {item['paired']}/{item['total']}, дней {item['days']}; {base_label} {score(item[f'm{base}'])}; {alt_label} {score(item[f'm{alt}'])}; разница/пара {delta(item['delta_per_pair'])}; без двух исходов {item['unresolved']}"
        )
    labels = dict(
        regime_router="Выбор метода по состоянию рынка",
        ema_pullback="Откат EMA9/19",
        break_retest="Пробой и ретест",
        bb_rsi_reentry="Боллинджер + RSI · возврат внутрь",
        previous_primary="Прежний Основной",
        strict_primary="Строгий Основной",
        higher_alignment="старший ТФ",
        regime_entry="откат/край флэта",
        not_extended="без чрезмерного движения",
        combined="вместе",
        h1_alignment="согласование H1",
        m30_h1_alignment="согласование M30 и H1",
        bollinger_context="Боллинджер с учётом режима",
        min_two_confirmations="минимум два подтверждения",
        two_families="минимум две группы признаков",
        bollinger_reversal="касание полосы Боллинджера → разворот",
        alternating_four="четыре чередующихся M1 → противоположное направление, экспирация M2",
    )
    for item in data["filters"]:
        archive = "Архив · " if item.get("study_version") != VERSION else ""
        lines.append(
            f"{archive}{item['strategy']} · M{item['seconds'] // 60} · {labels[item['policy']]}: база {score(item['baseline'])}; отобрано {item['selected']['n']}/{item['paired']} ({score(item['selected'])}); разница/возможность {delta(item['delta_per_opportunity'])}; дней {item['days']}; нет контекста {item['missing_context']}, нет исхода {item['unresolved']}"
        )
    if not data["pairs"] and not data["filters"]:
        lines.append(
            "Ожидаем новые наблюдения; прошлые прогнозы не восстанавливаем задним числом."
        )
    lines.append(
        "Автозамены стратегии нет. Проверяем выбранный вариант на следующем независимом периоде; положительная разница пока не доказывает преимущество."
    )
    return "\n".join(lines)

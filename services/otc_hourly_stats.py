"""Descriptive full-cycle results grouped by first entry hour, Moscow time."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from services.otc_public_stats import metrics

MSK = ZoneInfo('Europe/Moscow')


def hourly_groups(rows):
    buckets = [[] for _ in range(24)]
    days = [set() for _ in range(24)]
    missing = 0
    for row in rows:
        value = row.get('entry_at')
        if not isinstance(value, (int,float)) or not math.isfinite(value) or value <= 0:
            missing += 1
            continue
        try:
            at = datetime.fromtimestamp(value, MSK)
        except (ValueError, OverflowError, OSError):
            missing += 1
            continue
        buckets[at.hour].append(row)
        if row['outcome'] in ('WIN','LOSS'):
            days[at.hour].add(at.date())
    groups=[]
    for hour, subset in enumerate(buckets):
        m=metrics(subset)
        unresolved=m['unknown']+m['incomplete']
        finished=m['known']+m['draw']+unresolved
        enough=(m['known']>=20 and len(days[hour])>=3
                and finished>0 and unresolved/finished<=.2)
        groups.append(dict(m,hour=hour,days=len(days[hour]),enough=enough))
    return groups,missing


def hourly_text(rows, title):
    groups,missing=hourly_groups(rows)
    lines=['🕒 Результаты по часам · МСК',title,'WR полного цикла ×3 · час первого входа','']
    eligible=sorted((g for g in groups if g['enough']),key=lambda g:(-g['wr'],-g['known'],g['hour']))
    if eligible:
        lines.append('Лидеры в выбранной истории: '+', '.join(f"{g['hour']:02d}:00 — {g['wr']:.1f}% (N={g['known']})" for g in eligible[:3]))
    else:
        lines.append('Для выделения лучших часов пока недостаточно данных.')
    lines.extend(['Условия отбора: ≥20 WIN/LOSS, ≥3 дней и ≤20% ❔ среди завершённых.',
                  'Это порог для обзора, не доказательство преимущества.','',
                  'Час · WR · ✅/❌ · ❔ · ⏳'])
    for g in groups:
        wr=f"{g['wr']:.1f}%" if g['known'] else '—'
        lines.append(f"{g['hour']:02d}–{(g['hour']+1)%24:02d} · {wr} · {g['win']}/{g['loss']} · {g['unknown']+g['incomplete']} · {g['pending']}" + (' ★' if g['enough'] else ''))
    totals=metrics(rows)
    lines.extend(['',f"Ничьи: {totals['draw']} · без времени входа: {missing}",
                  'WR = WIN/(WIN+LOSS); ❔, ничьи и ожидание исключены.',
                  '★ — выполнены условия отбора. Часы без ★ имеют мало данных или много пропусков.',
                  'Историческое сравнение, не прогноз. Версии стратегий могут различаться.',
                  'Отбор сигналов по времени автоматически не меняется.'])
    return '\n'.join(lines)

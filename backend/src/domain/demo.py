"""澄浦 2 号线的可复现观测台账。"""

from datetime import date, timedelta
from math import exp
from random import Random


_INTERSECTIONS = (
    ("港湾路", "货栈街口", "混行"),
    ("港湾路", "渡口街口", "混行"),
    ("科创大道", "智造街口", "独立通行"),
    ("科创大道", "东环街口", "独立通行"),
)


def _poisson(rng, mean):
    limit = exp(-mean)
    product = rng.random()
    count = 0
    while product > limit:
        product *= rng.random()
        count += 1
    return count


def demo_rows():
    rng = Random(261160)
    rows = []
    for day in range(14):
        stamp = (date(2026, 11, 7) + timedelta(days=day)).isoformat()
        for road, intersection, track in _INTERSECTIONS:
            for shift in ("平峰", "高峰"):
                peak = shift == "高峰"
                exposure = (38 if peak else 20) + rng.randrange(13)
                rate = 0.035 * exp(0.9 * peak + 1.05 * (track == "混行"))
                rows.append({
                    "date": stamp, "road": road, "intersection": intersection,
                    "shift": shift, "track": track, "exposure": exposure,
                    "count": _poisson(rng, exposure * rate),
                })
    return rows


# The signal desk names the four crossings after its own cabinet points, so the
# demo crosswalk has to translate them to the ledger's 道路 / 路口 names.
_CROSSWALK = (
    ("湾1号点", "港湾路", "货栈街口"),
    ("湾2号点", "港湾路", "渡口街口"),
    ("科3号点", "科创大道", "智造街口"),
    ("科4号点", "科创大道", "东环街口"),
)

# (signal point, shift, entry start, entry end) — the morning windows overlap
# on 港湾路, so no single group can guard both.
_SIGNAL_WINDOWS = (
    ("湾1号点", "平峰", "08:00", "10:00"),
    ("湾2号点", "平峰", "09:00", "11:00"),
    ("科3号点", "平峰", "10:30", "12:30"),
    ("科4号点", "平峰", "11:00", "13:00"),
    ("湾1号点", "高峰", "17:00", "19:00"),
    ("湾2号点", "高峰", "18:00", "20:00"),
    ("科3号点", "高峰", "20:00", "22:00"),
    ("科4号点", "高峰", "20:30", "22:30"),
)

_GROUPS = (
    ("甲组", "湾1号点", "07:00", "15:00", 30),
    ("乙组", "湾2号点", "16:30", "23:59", 30),
    ("丙组", "科4号点", "07:00", "13:00", 30),
)


def demo_patrol(days):
    """Cross-system feed for the given holdout days, ready to paste as JSON."""
    return {
        "crosswalk": [
            {"signal_name": name, "road": road, "intersection": intersection}
            for name, road, intersection in _CROSSWALK
        ],
        "signal": [
            {"signal_name": name, "date": day, "shift": shift, "start": start, "end": end}
            for day in days
            for name, shift, start, end in _SIGNAL_WINDOWS
        ],
        "groups": [
            {"name": name, "start": start, "shift_start": shift_start,
             "shift_end": shift_end, "transfer_minutes": transfer}
            for name, start, shift_start, shift_end, transfer in _GROUPS
        ],
        "fixed": [
            {"group": "甲组", "signal_name": "湾2号点", "date": days[0], "shift": "平峰"},
        ],
    }


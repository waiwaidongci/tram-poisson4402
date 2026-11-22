"""Cross-system patrol scheduling built on the holdout-window predictions.

The signal desk records when each intersection may be entered. The patrol
system records each group's shift, starting point and transfer time. Before
scheduling, both sides' intersection names must be matched through a crosswalk
(路口对照表). The planner keeps the agreed (已定) patrols, deducts the transfer
time from the previous stop before deciding whether a group can guard a whole
window, and maximises the total predicted block count covered by the groups.
A window guarded by two groups is counted once.

Issues with an agreed patrol (已定巡查) — an unknown group, an unmapped signal
point, a window outside the holdout, a window it cannot reach in time, or a
duplicate — are blocking conflicts: scheduling stops and they are reported
instead of a partial route. Issues that only affect optional scheduling (a
signal point outside the crosswalk, a window outside the holdout, a group whose
origin cannot be matched) are warnings: the affected record is set aside and the
rest is still scheduled.
"""

from collections import defaultdict
from datetime import date as _date
from math import fsum

from .analysis import project
from .spec import SPEC


_SHIFTS = tuple(SPEC["fields"][3]["options"])
_MAX_GROUPS = 30
_MAX_FEED_ROWS = 5000
_NODE_LIMIT = 200_000
_EPSILON = 1e-12

_INPUT_LABELS = {
    "crosswalk": "路口对照表",
    "signal": "信号台进场窗口",
    "groups": "巡查组班次",
    "fixed": "已定巡查",
}


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #

def _fail(message):
    raise ValueError(message)


def _text(row, key, label, limit=80):
    value = row.get(key) if isinstance(row, dict) else None
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label}不能为空")
    value = value.strip()
    if len(value) > limit:
        _fail(f"{label}最多 {limit} 字符")
    return value


def _minutes(row, key, label):
    value = row.get(key) if isinstance(row, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail(f"{label}须为 HH:MM 格式的时间")
    if isinstance(value, int):
        total = value
    else:
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() and part for part in parts):
            _fail(f"{label}须为 HH:MM 格式的时间")
        hours, minutes = int(parts[0]), int(parts[1])
        if not 0 <= hours <= 23 or not 0 <= minutes <= 59:
            _fail(f"{label}须为 00:00 至 23:59 之间的时间")
        total = hours * 60 + minutes
    if not 0 <= total < 24 * 60:
        _fail(f"{label}须为 00:00 至 23:59 之间的时间")
    return total


def _day(row, key, label):
    value = row.get(key) if isinstance(row, dict) else None
    try:
        if not isinstance(value, str) or len(value) != 10 or _date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        _fail(f"{label}须为有效的 YYYY-MM-DD 日期")
    return value


def _shift(row, key, label):
    value = row.get(key) if isinstance(row, dict) else None
    if value not in _SHIFTS:
        _fail(f"{label}须为{'、'.join(_SHIFTS)}之一")
    return value


def _count(row, key, label, low, high):
    value = row.get(key) if isinstance(row, dict) else None
    if isinstance(value, bool):
        _fail(f"{label}须为 {low} 至 {high} 之间的整数")
    if isinstance(value, float) and not value.is_integer():
        _fail(f"{label}须为 {low} 至 {high} 之间的整数")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            _fail(f"{label}须为 {low} 至 {high} 之间的整数")
    if not isinstance(value, int):
        _fail(f"{label}须为 {low} 至 {high} 之间的整数")
    if not low <= value <= high:
        _fail(f"{label}须为 {low} 至 {high} 之间的整数")
    return value


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _key(row):
    return (row["date"], row["road"], row["intersection"], row["shift"])


def _window_label(key):
    return f"{key[0]} {key[3]} {key[1]}·{key[2]}"


def _window_order(window):
    return (window["date"], window["shift"] != "高峰", window["road"], window["intersection"])


def _record(code, message, **extra):
    return {"code": code, "message": message, **extra}


def parse_inputs(data):
    """Validate the four cross-system feeds.

    Returns (crosswalk, ambiguous, signals, groups, fixed, warnings). Blocking
    conflicts concern agreed patrols only and are raised by the planner; the
    issues found here (a signal point mapped to two intersections, a duplicate
    group) merely exclude the affected record from scheduling.
    """
    if not isinstance(data, dict):
        _fail("跨系统排班底册须为一个对象")
    unknown = set(data) - set(_INPUT_LABELS)
    if unknown:
        _fail(f"跨系统排班底册含未知分区：{'、'.join(sorted(unknown))}")
    feeds = {}
    for name, label in _INPUT_LABELS.items():
        value = data.get(name)
        if value is None:
            value = []
        if not isinstance(value, list):
            _fail(f"{label}（{name}）须为列表")
        if len(value) > _MAX_FEED_ROWS:
            _fail(f"{label}最多 {_MAX_FEED_ROWS} 行")
        feeds[name] = value

    warnings = []
    crosswalk, ambiguous = {}, set()
    for index, row in enumerate(feeds["crosswalk"], start=1):
        name = _text(row, "signal_name", f"路口对照表第 {index} 行信号点名称")
        road = _text(row, "road", f"路口对照表第 {index} 行道路")
        intersection = _text(row, "intersection", f"路口对照表第 {index} 行路口")
        target = (road, intersection)
        if name in crosswalk and crosswalk[name] != target:
            ambiguous.add(name)
            warnings.append(_record(
                "crosswalk_duplicate",
                f"信号点「{name}」被对到多个台账路口，已按未对上处理",
                signal_name=name,
            ))
            continue
        crosswalk[name] = target

    signals = []
    for index, row in enumerate(feeds["signal"], start=1):
        entry = {
            "signal_name": _text(row, "signal_name", f"信号台进场窗口第 {index} 行信号点名称"),
            "date": _day(row, "date", f"信号台进场窗口第 {index} 行日期"),
            "shift": _shift(row, "shift", f"信号台进场窗口第 {index} 行时段"),
            "start": _minutes(row, "start", f"信号台进场窗口第 {index} 行进场开始"),
            "end": _minutes(row, "end", f"信号台进场窗口第 {index} 行进场结束"),
        }
        if entry["end"] <= entry["start"]:
            _fail(f"信号台进场窗口第 {index} 行进场结束须晚于进场开始")
        signals.append(entry)

    groups = []
    seen_groups = set()
    for index, row in enumerate(feeds["groups"], start=1):
        name = _text(row, "name", f"巡查组第 {index} 行班组名称", 60)
        if name in seen_groups:
            warnings.append(_record("group_duplicate", f"巡查组「{name}」重复登记，按首次登记的班次排班", group=name))
            continue
        seen_groups.add(name)
        shift_start = _minutes(row, "shift_start", f"巡查组「{name}」班次开始")
        shift_end = _minutes(row, "shift_end", f"巡查组「{name}」班次结束")
        if shift_end <= shift_start:
            _fail(f"巡查组「{name}」的班次结束须晚于班次开始")
        groups.append({
            "name": name,
            "start": _text(row, "start", f"巡查组「{name}」起点"),
            "shift_start": shift_start,
            "shift_end": shift_end,
            "transfer": _count(row, "transfer_minutes", f"巡查组「{name}」转场时间", 0, 240),
        })
    if len(groups) > _MAX_GROUPS:
        _fail(f"巡查组最多 {_MAX_GROUPS} 个")

    fixed = []
    for index, row in enumerate(feeds["fixed"], start=1):
        fixed.append({
            "group": _text(row, "group", f"已定巡查第 {index} 行班组"),
            "signal_name": _text(row, "signal_name", f"已定巡查第 {index} 行信号点名称"),
            "date": _day(row, "date", f"已定巡查第 {index} 行日期"),
            "shift": _shift(row, "shift", f"已定巡查第 {index} 行时段"),
        })
    return crosswalk, ambiguous, signals, groups, fixed, warnings


# --------------------------------------------------------------------------- #
# Route feasibility
# --------------------------------------------------------------------------- #

def _route_ok(day_keys, group, signal_by_key):
    """Whether one group can guard all these windows on a single day."""
    items = sorted((signal_by_key[key][0], signal_by_key[key][1]) for key in day_keys)
    if not items:
        return True
    if items[0][0] < group["shift_start"] + group["transfer"]:
        return False
    for (_, previous_end), (next_start, _) in zip(items, items[1:]):
        if next_start < previous_end + group["transfer"]:
            return False
    return all(end <= group["shift_end"] for _, end in items)


def _schedule_problems(day_keys, group, signal_by_key):
    """Explain why a fixed set of windows cannot be guarded, one problem each."""
    problems = []
    items = sorted((signal_by_key[key][0], signal_by_key[key][1], key) for key in day_keys)
    earliest = group["shift_start"] + group["transfer"]
    first_start, _, first_key = items[0]
    if first_start < earliest:
        problems.append(
            f"{group['name']} 从起点「{group['start']}」转场需 {group['transfer']} 分钟，"
            f"{_window_label(first_key)} 于 {_clock(first_start)} 开始，最早只能 {_clock(earliest)} 到达"
        )
    for (_, previous_end, previous_key), (next_start, _, next_key) in zip(items, items[1:]):
        if next_start < previous_end + group["transfer"]:
            problems.append(
                f"{group['name']} 在 {_window_label(previous_key)} {_clock(previous_end)} 结束后转场需 "
                f"{group['transfer']} 分钟，赶不上 {_window_label(next_key)} 的 {_clock(next_start)}"
            )
    for _, end, key in items:
        if end > group["shift_end"]:
            problems.append(
                f"{group['name']} 班次 {_clock(group['shift_end'])} 结束，"
                f"守不完 {_window_label(key)} 到 {_clock(end)} 的整窗"
            )
    return problems


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #

def _dedupe(entries):
    seen, output = set(), []
    for entry in entries:
        token = (entry["code"], entry["message"])
        if token in seen:
            continue
        seen.add(token)
        output.append(entry)
    return output


def _tables(route_rows, coverage_rows, summary_rows, blocking, warnings):
    blocking, warnings = _dedupe(blocking), _dedupe(warnings)
    tables = []
    if blocking:
        tables.append({"title": "已定巡查冲突", "columns": [
            {"key": "group", "label": "班组"}, {"key": "code", "label": "冲突类型"},
            {"key": "message", "label": "冲突说明"},
        ], "rows": blocking})
    if warnings:
        tables.append({"title": "排班数据提示", "columns": [
            {"key": "code", "label": "提示类型"}, {"key": "message", "label": "说明"},
        ], "rows": warnings})
    if route_rows:
        tables.append({"title": "班组巡查路线", "columns": [
            {"key": "group", "label": "班组"}, {"key": "date", "label": "日期"},
            {"key": "shift", "label": "时段"}, {"key": "start", "label": "窗口起"},
            {"key": "end", "label": "窗口止"}, {"key": "road", "label": "道路"},
            {"key": "intersection", "label": "路口"}, {"key": "predicted", "label": "预计阻断次数"},
            {"key": "source", "label": "来源"},
        ], "rows": route_rows})
    if coverage_rows:
        tables.append({"title": "窗口覆盖与收益", "columns": [
            {"key": "date", "label": "日期"}, {"key": "shift", "label": "时段"},
            {"key": "start", "label": "窗口起"}, {"key": "end", "label": "窗口止"},
            {"key": "road", "label": "道路"}, {"key": "intersection", "label": "路口"},
            {"key": "signal_name", "label": "信号点"}, {"key": "predicted", "label": "预计阻断次数"},
            {"key": "group", "label": "覆盖班组"}, {"key": "state", "label": "状态"},
        ], "rows": coverage_rows})
    if summary_rows:
        tables.append({"title": "班组覆盖汇总", "columns": [
            {"key": "group", "label": "班组"}, {"key": "shift", "label": "班次"},
            {"key": "start", "label": "起点信号点"}, {"key": "transfer", "label": "转场分钟"},
            {"key": "covered", "label": "覆盖窗口数"}, {"key": "predicted", "label": "覆盖预计次数"},
            {"key": "added", "label": "其中新增次数"}, {"key": "note", "label": "备注"},
        ], "rows": summary_rows})
    return tables


_SECTIONS = [
    {"title": "跨系统对表口径", "text": "信号台按信号点登记可进场时间，巡查系统按班组登记班次、起点与转场时间。"
     "排班前先用路口对照表把信号点对到台账路口；未对上的信号点不参与排班，列为数据提示。"},
    {"title": "守窗与覆盖口径", "text": "窗口起止取自信号台进场时间。班组从起点或上一站转场，先扣转场分钟，"
     "只有到达不晚于窗口开始、班次结束不早于窗口结束，才算守住完整窗口。"
     "同一窗口由两组同守只计一次，目标是各组累计覆盖的预计阻断次数最多。"},
]


def _halt(headline, note, blocking, warnings, metrics):
    return {
        "state": "conflict", "headline": headline, "note": note,
        "conflicts": _dedupe(blocking), "warnings": _dedupe(warnings),
        "metrics": metrics, "charts": [], "routes": [], "coverage": [],
        "tables": _tables([], [], [], blocking, warnings),
        "sections": _SECTIONS,
    }


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #

def plan_patrol(rows, params, data):
    """Build patrol routes for one analysis run, reusing its holdout windows."""
    projected = project(rows, params)
    windows = projected["windows"]
    ledger = {_key(window): window for window in windows}
    weights = {key: window["predicted"] for key, window in ledger.items()}
    crosswalk, ambiguous, signals, groups, fixed, warnings = parse_inputs(data)
    blocking = []

    signal_by_key = {}
    duplicate_signal = set()
    for entry in signals:
        name = entry["signal_name"]
        if name in ambiguous:
            warnings.append(_record(
                "signal_ambiguous",
                f"信号台信号点「{name}」在路口对照表中对应多个路口，已跳过",
                signal_name=name,
            ))
            continue
        target = crosswalk.get(name)
        if target is None:
            warnings.append(_record(
                "crosswalk_missing",
                f"信号台信号点「{name}」未在路口对照表中对上台账路口，已跳过",
                signal_name=name,
            ))
            continue
        key = (entry["date"], target[0], target[1], entry["shift"])
        if key not in ledger:
            warnings.append(_record(
                "signal_outside_holdout",
                f"信号台窗口 {_window_label(key)} 不在本批留出窗口内，已跳过",
                signal_name=name,
            ))
            continue
        if key in signal_by_key:
            duplicate_signal.add(key)
            warnings.append(_record(
                "signal_duplicate",
                f"{_window_label(key)} 有多个信号台进场窗口，无法确定可进场时间，已跳过",
                windows=_window_label(key),
            ))
            continue
        signal_by_key[key] = (entry["start"], entry["end"], name)
    for key in duplicate_signal:
        signal_by_key.pop(key, None)

    groups_by_name = {group["name"]: group for group in groups}
    # A group whose origin cannot be matched to a ledger intersection has no
    # known transfer start, so it cannot guard any window. That only limits
    # optional scheduling (警告); an agreed patrol for such a group, however,
    # cannot be verified and therefore blocks.
    stranded = {}
    for group in groups:
        if group["start"] in ambiguous:
            stranded[group["name"]] = ("group_start_ambiguous",
                                       f"巡查组「{group['name']}」起点「{group['start']}」在对照表中对应多个路口，无法扣除转场时间")
        elif group["start"] not in crosswalk:
            stranded[group["name"]] = ("group_start_unmapped",
                                       f"巡查组「{group['name']}」起点「{group['start']}」未在路口对照表中对上台账路口")
        if group["name"] in stranded:
            code, message = stranded[group["name"]]
            warnings.append(_record(code, f"{message}，该组不参与新增排班", group=group["name"]))

    fixed_pairs = set()
    fixed_windows = set()
    fixed_by_group = defaultdict(list)
    for index, entry in enumerate(fixed, start=1):
        group = groups_by_name.get(entry["group"])
        if group is None:
            blocking.append(_record(
                "fixed_unknown_group",
                f"已定巡查第 {index} 行引用了未登记的巡查组「{entry['group']}」",
                group=entry["group"],
            ))
            continue
        name = entry["signal_name"]
        if name in ambiguous:
            blocking.append(_record(
                "fixed_ambiguous_signal",
                f"已定巡查的信号点「{name}」在对照表中对应多个路口，无法核对",
                group=group["name"], signal_name=name,
            ))
            continue
        target = crosswalk.get(name)
        if target is None:
            blocking.append(_record(
                "fixed_unmapped",
                f"已定巡查的信号点「{name}」未在路口对照表中对上台账路口",
                group=group["name"], signal_name=name,
            ))
            continue
        key = (entry["date"], target[0], target[1], entry["shift"])
        if key not in ledger:
            blocking.append(_record(
                "fixed_missing_window",
                f"已定巡查 {_window_label(key)} 不在本批留出窗口内，无法核对",
                group=group["name"], windows=_window_label(key),
            ))
            continue
        if key not in signal_by_key:
            blocking.append(_record(
                "fixed_missing_signal",
                f"已定巡查 {_window_label(key)} 在信号台没有可进场窗口，无法守完整窗",
                group=group["name"], windows=_window_label(key),
            ))
            continue
        if group["name"] in stranded:
            blocking.append(_record(
                "fixed_stranded_group",
                f"已定巡查的班组「{group['name']}」起点未对上路口，无法核对转场时间",
                group=group["name"], windows=_window_label(key),
            ))
            continue
        if (group["name"], key) in fixed_pairs:
            blocking.append(_record(
                "fixed_duplicate",
                f"已定巡查重复安排「{group['name']}」守 {_window_label(key)}",
                group=group["name"], windows=_window_label(key),
            ))
            continue
        fixed_pairs.add((group["name"], key))
        fixed_windows.add(key)
        fixed_by_group[group["name"]].append(key)

    for name, keys in fixed_by_group.items():
        by_day = defaultdict(list)
        for key in keys:
            by_day[key[0]].append(key)
        for day in sorted(by_day):
            for problem in _schedule_problems(by_day[day], groups_by_name[name], signal_by_key):
                blocking.append(_record("fixed_conflict", f"已定巡查冲突：{problem}", group=name))

    note = (f"训练 {projected['days'][0]} 至 {projected['training'][-1]['date']}，"
            f"留出 {projected['cutoff']} 至 {projected['days'][-1]}。"
            f"巡查沿用留出期逐窗预测，重点阈值 {projected['params']['severity_rate']:g} 次／百车次。")

    if blocking:
        blocking = _dedupe(blocking)
        metrics = [
            {"label": "已定巡查", "value": len(fixed), "detail": f"{len(blocking)} 项冲突，已停止排班"},
            {"label": "留出窗口", "value": len(ledger), "detail": "停排时不计算覆盖收益"},
            {"label": "数据提示", "value": len(_dedupe(warnings)), "detail": "停排时一并列出"},
        ]
        return _halt("已定巡查存在冲突，已停止排班。请先处理冲突后重新排班。", note, blocking, warnings, metrics)

    fixed_value = fsum(weights[key] for key in fixed_windows)

    # Groups whose origin could not be matched are excluded from optional
    # scheduling; they still appear in the summary marked as not schedulable.
    schedulable = [group for group in groups if group["name"] not in stranded]
    candidates = sorted((key for key in signal_by_key if key not in fixed_windows),
                        key=lambda key: (-weights[key], _window_order(ledger[key]), key))
    schedule = {group["name"]: defaultdict(list) for group in groups}
    for name, keys in fixed_by_group.items():
        for key in keys:
            schedule[name][key[0]].append(key)

    def can_add(key, group):
        return _route_ok([*schedule[group["name"]][key[0]], key], group, signal_by_key)

    suffix = [0.0] * (len(candidates) + 1)
    for index in range(len(candidates) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + weights[candidates[index]]

    # Greedy warm start seeds the incumbent so branch-and-bound can prune early.
    warm = {}
    for key in candidates:
        for group in schedulable:
            if can_add(key, group):
                schedule[group["name"]][key[0]].append(key)
                warm[key] = group["name"]
                break
    best_value = fsum(weights[key] for key in warm)
    best_assignment = dict(warm)
    for key in list(warm):
        schedule[warm[key]][key[0]].remove(key)

    assignment = {}
    nodes = [0]
    truncated = [False]

    def search(index, value):
        nonlocal best_value, best_assignment
        if value + suffix[index] <= best_value + _EPSILON:
            return
        if nodes[0] >= _NODE_LIMIT:
            truncated[0] = True
            return
        if index == len(candidates):
            best_value, best_assignment = value, dict(assignment)
            return
        nodes[0] += 1
        key = candidates[index]
        for group in schedulable:
            if can_add(key, group):
                schedule[group["name"]][key[0]].append(key)
                assignment[key] = group["name"]
                search(index + 1, value + weights[key])
                del assignment[key]
                schedule[group["name"]][key[0]].remove(key)
        search(index + 1, value)

    search(0, 0.0)

    covered = fixed_windows | set(best_assignment)
    added_value = fsum(weights[key] for key in best_assignment)
    total_value = fixed_value + added_value

    routes, route_rows, summary_rows = [], [], []
    for group in groups:
        name = group["name"]
        own = sorted((key for key in covered
                      if best_assignment.get(key) == name or (name, key) in fixed_pairs),
                     key=lambda key: (key[0], signal_by_key[key][0], signal_by_key[key][1], key))
        days = defaultdict(list)
        own_value = own_added = 0.0
        for key in own:
            start, end, signal_name = signal_by_key[key]
            source = "已定巡查" if (name, key) in fixed_pairs else "新增排班"
            days[key[0]].append({
                "date": key[0], "shift": key[3], "start": _clock(start), "end": _clock(end),
                "road": key[1], "intersection": key[2], "signal_name": signal_name,
                "predicted": weights[key], "source": source,
            })
            route_rows.append({
                "group": name, "date": key[0], "shift": key[3], "start": _clock(start),
                "end": _clock(end), "road": key[1], "intersection": key[2],
                "predicted": weights[key], "source": source,
            })
            own_value += weights[key]
            if source == "新增排班":
                own_added += weights[key]
        routes.append({
            "group": name, "start": group["start"], "shift_start": _clock(group["shift_start"]),
            "shift_end": _clock(group["shift_end"]), "transfer_minutes": group["transfer"],
            "covered": len(own), "predicted": own_value, "added": own_added,
            "schedulable": name not in stranded,
            "days": [{"date": day, "windows": days[day]} for day in sorted(days)],
        })
        summary_rows.append({
            "group": name, "shift": f"{_clock(group['shift_start'])}–{_clock(group['shift_end'])}",
            "start": group["start"], "transfer": group["transfer"], "covered": len(own),
            "predicted": own_value, "added": own_added,
            "note": "起点未对上路口，未参与排班" if name in stranded else "",
        })

    covering = defaultdict(list)
    for name, key in fixed_pairs:
        covering[key].append(name)
    for key, name in best_assignment.items():
        covering[key].append(name)
    coverage_rows = []
    for key in sorted(ledger, key=lambda item: (_window_order(ledger[item]), item)):
        signal = signal_by_key.get(key)
        names = sorted(set(covering.get(key, ())))
        if names:
            state = "已定巡查" if key in fixed_windows else "排班覆盖"
        elif key not in signal_by_key:
            state = "无进场窗口"
        else:
            state = "未覆盖"
        coverage_rows.append({
            "date": key[0], "shift": key[3], "road": key[1], "intersection": key[2],
            "start": _clock(signal[0]) if signal else "—",
            "end": _clock(signal[1]) if signal else "—",
            "signal_name": signal[2] if signal else "—", "predicted": weights[key],
            "group": "、".join(names) if names else "—", "state": state,
        })

    uncovered = len(ledger) - len(covered)
    share = 100 * total_value / fsum(weights.values()) if weights else 0.0
    schedulable_count = len(schedulable)
    metrics = [
        {"label": "留出窗口", "value": len(ledger), "detail": f"{len(signal_by_key)} 个窗口对上信号台进场时间"},
        {"label": "已覆盖窗口", "value": len(covered), "detail": f"{uncovered} 个窗口未覆盖"},
        {"label": "覆盖预计阻断次数", "value": total_value, "detail": f"占留出期预计总量 {share:.1f}%"},
        {"label": "已定巡查覆盖", "value": fixed_value, "detail": f"{len(fixed_pairs)} 项已定巡查，保留不撤"},
        {"label": "新增覆盖收益", "value": added_value, "detail": "排班在已定巡查之外新增的预计阻断次数"},
        {"label": "参与排班班组", "value": schedulable_count,
         "detail": f"{len(groups) - schedulable_count} 组起点未对上路口，转场时间已从到达时间中扣除" if len(groups) > schedulable_count
         else "转场时间已从到达时间中扣除"},
    ]
    if covered:
        headline = (f"保留 {len(fixed_pairs)} 项已定巡查，{schedulable_count} 组共覆盖 {len(covered)} 个窗口，"
                    f"预计阻断 {total_value:.2f} 次（新增 {added_value:.2f} 次）。")
    else:
        headline = "已定巡查未见冲突，但没有班组能在班次与进场时间内守住完整窗口。"
    charts = [{"title": "各组覆盖的预计阻断次数", "kind": "bar", "series": [
        {"label": row["group"], "value": row["predicted"]} for row in summary_rows if row["covered"]
    ]}]
    sections = list(_SECTIONS)
    if truncated[0]:
        sections.append({"title": "排班求解提示", "text": "窗口较多，搜索达到节点上限，输出的是当前找到的最优可行路线；"
                         "如需进一步核对，可缩小日期范围或减少班组后重排。"})
    return {
        "state": "ok" if covered else "warning",
        "headline": headline, "note": note, "conflicts": [], "warnings": _dedupe(warnings),
        "metrics": metrics, "charts": charts, "routes": routes, "coverage": coverage_rows,
        "tables": _tables(route_rows, coverage_rows, summary_rows, [], warnings),
        "sections": sections,
    }

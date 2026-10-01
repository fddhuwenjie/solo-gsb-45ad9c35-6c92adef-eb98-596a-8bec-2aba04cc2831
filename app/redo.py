"""补做排程引擎（纯标准库，无 Flask 依赖，便于单测）。

背景
====
一次试验的实测湿度复核可能给出三种样本结论：通过 / 复核 / 数据不足。
操作员在已保存复核记录上发起「补做排程」时：

* 只提取结论为「复核」或「数据不足」的样本（通过的样本**不得重复安排**）；
* 该记录对应的**原排程完整保留为不可修改的历史**：其分钟湿度曲线作为
  固定前缀原样钉在时间轴 [0, E] 上（E = 原排程结束分钟），补做任务的
  入箱分钟必须严格晚于 E；
* 补做计划继续遵守全部硬约束：箱容量、耐受区间、不可相邻、每分钟换档速率、
  独立换档上限，并且**沿用原有占用**；
* 新旧时间轴衔接处（原排程末段停留湿度 → 补做首次换档的目标湿度）同样
  校验独立换档上限。

没有失败样本时返回空方案（不保存）；补做无解时返回冲突列表，不保存半成品。
"""
from __future__ import annotations

from typing import Any, Optional

from . import review as review_mod
from .scheduler import (EPS, MAX_HORIZON, RETRY_LIMIT, SchedulingError,
                        _Engine, _Occ, _Sample, _annotate, _candidate_orders,
                        _diagnose, _num, _occ_json, _sample_json,
                        ceil_tenth, compress, fmt_minute,
                        normalize_max_shift_delta, normalize_occ,
                        normalize_sample)


class RedoError(Exception):
    """补做排程输入错误（区别于"约束无解"）。"""


# --------------------------------------------------------------------------- #
# 原排程快照还原
# --------------------------------------------------------------------------- #
def pinned_rh_series(source_schedule: dict) -> list[float]:
    """从原排程快照还原完整分钟湿度曲线 rh[0..E]。"""
    segments = source_schedule.get('segments')
    if not segments:
        raise RedoError("原排程快照缺少时间轴（segments），无法保留历史")
    end_minute = source_schedule.get('end_minute')
    rh = review_mod.plan_series(segments, end_minute)
    if not rh:
        raise RedoError("原排程时间轴为空，无法保留历史")
    return [float(v) for v in rh]


def failed_sample_ids(review_payload: dict) -> list[int]:
    """按复核快照中的样本顺序，返回结论为「复核」或「数据不足」的样本 id。"""
    out: list[int] = []
    for s in review_payload.get('samples') or []:
        if s.get('conclusion') in (review_mod.CONCLUSION_REVIEW,
                                   review_mod.CONCLUSION_INSUFFICIENT):
            sid = s.get('sample_id')
            if sid is None:
                raise RedoError("复核记录中的样本缺少 sample_id，无法补做")
            out.append(int(sid))
    return out


def history_lanes(source_schedule: dict) -> list[dict]:
    """原排程全部样本泳道（不可修改历史，按原 board 排序）。"""
    lanes = [dict(l) for l in (source_schedule.get('lanes') or [])]
    lanes.sort(key=lambda l: (l.get('board') or 0, l.get('sample_id') or 0))
    return lanes


def _cap_loads(lanes: list[dict], occs: list[_Occ], end: int) -> list[int]:
    """历史前缀每分钟的箱内负载（历史在箱样本 + 当时的占用位数）。"""
    load = [0] * (end + 1)
    for lane in lanes:
        b = int(lane.get('board') or 0)
        u = lane.get('unboard')
        u = int(u) if u is not None else end + 1
        for t in range(b, min(u, end + 1)):
            load[t] += 1
    for o in occs:
        for t in range(max(0, o.start), min(o.end, end + 1)):
            load[t] += o.slots
    return load


def _compress_load(load: list[int]) -> list[dict]:
    out: list[dict] = []
    for t, v in enumerate(load):
        if out and out[-1]['load'] == v and out[-1]['end'] == t:
            out[-1]['end'] = t + 1
        else:
            out.append({'start': t, 'end': t + 1, 'load': v})
    return out


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def plan_redo(review_payload: dict,
              samples_raw: list[dict],
              occs_raw: Optional[list[dict]] = None,
              capacity: Any = None,
              max_shift_delta: Any = None,
              horizon: int = MAX_HORIZON) -> dict:
    """基于一条已保存复核记录生成补做排程。

    :param review_payload: 复核记录的完整 payload（须内嵌 source_schedule）
    :param samples_raw: 样本定义（来自当前库；补做沿用原排程快照中的样本定义）
    :param occs_raw: 占用段；None 时使用原排程快照中保存的占用
    :param capacity/max_shift_delta: 箱容量与独立换档上限
    """
    source = review_payload.get('source_schedule')
    if not source or not source.get('segments'):
        raise RedoError("该复核记录未保留原排程快照，无法在不可修改历史上补做")
    if not source.get('feasible'):
        raise RedoError("原排程不可行，无法补做")

    pinned = pinned_rh_series(source)
    pin_end = len(pinned) - 1

    if capacity is None:
        capacity = source.get('capacity')
    if max_shift_delta is None:
        max_shift_delta = source.get('max_shift_delta')
    try:
        cap = int(_num(capacity, "恒湿箱容量", integer=True, lo=1))
        shift_limit = normalize_max_shift_delta(max_shift_delta)
    except SchedulingError as e:
        raise RedoError(str(e))

    if occs_raw is None:
        occs_raw = source.get('occupancies') or []
    try:
        occs = [normalize_occ(d) for d in occs_raw]
    except SchedulingError as e:
        raise RedoError(str(e))

    # 样本定义以原排程快照为准（历史不可修改，即使库里后来改过样本）；
    # 快照缺失的 id 回退到库中当前定义
    snap_by_id = {int(s['id']): s for s in (source.get('samples') or [])
                  if s.get('id') is not None}
    db_by_id = {}
    for d in samples_raw or []:
        if d.get('id') is not None:
            db_by_id[int(d['id'])] = d
    try:
        all_samples = [normalize_sample(snap_by_id[sid] if sid in snap_by_id
                                        else db_by_id[sid])
                       for sid in sorted(set(snap_by_id) | set(db_by_id))]
    except KeyError as e:
        raise RedoError(f"样本 {e.args[0]} 在原排程快照与当前库中均不存在")
    except SchedulingError as e:
        raise RedoError(str(e))
    by_id = {s.id: s for s in all_samples}

    redo_ids = failed_sample_ids(review_payload)
    if not redo_ids:
        return _empty_plan(source, occs, cap, shift_limit, pin_end,
                           review_payload)
    missing = [sid for sid in redo_ids if sid not in by_id]
    if missing:
        raise RedoError(f"需要补做的样本 {missing} 已不存在，无法补做")
    redo_samples = [by_id[sid] for sid in redo_ids]
    # 互斥只保留补做集合内部有效关系（历史任务已结束，不与补做任务重叠）
    redo_set = set(redo_ids)
    for s in redo_samples:
        s.excludes = {x for x in s.excludes if x in redo_set and x != s.id}

    base = _meta(source, occs, cap, shift_limit, pin_end, review_payload)
    base['failed_sample_ids'] = redo_ids

    pre: list[dict] = []
    # 占用相关的静态硬冲突只考虑历史结束之后仍有效的占用段
    future_occs = [o for o in occs if o.end > pin_end + 1]
    for o in future_occs:
        if o.slots > cap:
            pre.append({
                'kind': 'capacity', 'severity': 'hard',
                'message': f"占用段 {fmt_minute(o.start)}-{fmt_minute(o.end)} 需 "
                           f"{o.slots} 位，超过箱容量 {cap}（补做阶段仍生效）",
                'suggestion': {'type': 'capacity',
                               'title': f'把箱容量调到 {o.slots}',
                               'delta': o.slots - cap, 'value': o.slots}})
    for i, a in enumerate(future_occs):
        for b in future_occs[i + 1:]:
            if a.start < b.end and b.start < a.end and abs(a.rh - b.rh) > EPS:
                pre.append({
                    'kind': 'occ-overlap', 'severity': 'hard',
                    'message': '已有占用段时间重叠但湿度不同，固定曲线冲突',
                    'suggestion': {'type': 'occupancy',
                                   'title': '调整其中一段已有占用的时间/湿度',
                                   'delta': 1, 'value': None}})
    # 补做样本两两耐受区间
    for i, a in enumerate(redo_samples):
        for b in redo_samples[i + 1:]:
            if max(a.rh_min, b.rh_min) > min(a.rh_max, b.rh_max) + EPS and \
                    b.id not in a.excludes:
                pre.append({
                    'kind': 'range', 'severity': 'hard',
                    'message': f"样本 {a.code} 与 {b.code} 的耐受区间不相交"
                               f"（[{a.rh_min:g},{a.rh_max:g}] vs "
                               f"[{b.rh_min:g},{b.rh_max:g}]），无法同箱补做",
                    'suggestion': {
                        'type': 'adjacency',
                        'title': f'把 {a.code} 与 {b.code} 标记为不可相邻（错峰）',
                        'delta': 1, 'value': None, 'samples': [a.id, b.id]}})
    # 样本自身相邻目标差超限（任何时刻都无法满足）
    pre += _static_shift_conflicts(redo_samples, shift_limit)
    # 衔接处：历史末段停留湿度 → 补做首个停留湿度的独立换档上限
    pre += _junction_conflicts(pinned, redo_samples, shift_limit)
    if pre:
        base['conflicts'] = pre
        base['minimal_adjustment'] = pre[0]['suggestion']
        return base

    for order in _candidate_orders(redo_samples):
        bt = _try_redo_order(order, occs, cap, horizon, shift_limit,
                             pinned, pin_end)
        if bt is not None:
            return _finalize_redo(source, redo_samples, occs, cap, bt,
                                  horizon, shift_limit, pinned, pin_end,
                                  review_payload)

    # 搜索失败的通用诊断：探针引擎不带固定前缀，因此只传入历史结束之后
    # 仍生效的占用段，避免纯历史占用污染诊断
    diag_occs = [o for o in occs if o.end > pin_end + 1]
    conflicts = _diagnose(redo_samples, diag_occs, cap, horizon, shift_limit)
    # 通用诊断之外，再给一条衔接处专属诊断（探针已含固定前缀时最可靠）
    junction = _junction_probe(redo_samples, occs, cap, horizon, shift_limit,
                               pinned, pin_end)
    if junction:
        conflicts = [junction] + conflicts
    base['conflicts'] = conflicts
    base['minimal_adjustment'] = conflicts[0]['suggestion'] \
        if conflicts else None
    return base


# --------------------------------------------------------------------------- #
# 衔接处独立换档上限
# --------------------------------------------------------------------------- #
def _last_hold_rh(pinned: list[float]) -> float:
    """历史曲线最后一段停留（hold）湿度。"""
    t = len(pinned) - 1
    while t > 0 and abs(pinned[t] - pinned[t - 1]) > EPS:
        t -= 1
    return pinned[t]


def _static_shift_conflicts(samples: list[_Sample],
                            shift_limit: Optional[float]) -> list[dict]:
    """与 scheduler._static_shift_conflicts 同语义（补做样本集合）。"""
    if shift_limit is None:
        return []
    conf: list[dict] = []
    worst = 0.0
    for s in samples:
        chain = [s.eq_rh] + [st['target'] for st in s.steps] + [s.eq_rh]
        for j in range(1, len(chain)):
            d = abs(chain[j] - chain[j - 1])
            if d > shift_limit + EPS:
                worst = max(worst, d)
                step_no = min(j, len(s.steps))
                conf.append({
                    'kind': 'redo-shift', 'severity': 'hard',
                    'message': f"补做样本 {s.code} 第 {step_no} 次换档相邻目标 "
                               f"{chain[j - 1]:g}% → {chain[j]:g}% 差值 {d:g}%RH，"
                               f"超过独立换档上限 {shift_limit:g}%RH",
                    'suggestion': {
                        'type': 'shift-limit',
                        'title': f'把每次换档最大目标湿度差值放宽到 '
                                 f'{ceil_tenth(d):g}%RH',
                        'delta': round(d - shift_limit, 2),
                        'value': ceil_tenth(d)}})
    if conf and worst:
        need = ceil_tenth(worst)
        conf.insert(0, {
            'kind': 'redo-shift', 'severity': 'hard',
            'message': f"补做样本存在 {len(conf)} 处自身相邻目标差值超过独立换档上限 "
                       f"{shift_limit:g}%RH（最大 {worst:g}%RH），任何入箱时刻都无法满足",
            'suggestion': {'type': 'shift-limit',
                           'title': f'把独立换档上限放宽到 {need:g}%RH',
                           'delta': round(worst - shift_limit, 2),
                           'value': round(need, 1)}})
    return conf


def _junction_conflicts(pinned: list[float], samples: list[_Sample],
                        shift_limit: Optional[float]) -> list[dict]:
    """衔接处静态硬冲突：历史末段湿度到每个补做样本的首个停留湿度（入箱平衡
    湿度——空箱只能先预调到某个待入箱样本的平衡湿度）的差值都超限时，
    无论怎样错峰都无法跨过衔接点。"""
    if shift_limit is None:
        return []
    last_rh = _last_hold_rh(pinned)
    worst = 0.0
    for s in samples:
        worst = max(worst, abs(s.eq_rh - last_rh))
    if worst <= shift_limit + EPS:
        return []
    need = ceil_tenth(worst)
    return [{
        'kind': 'redo-junction', 'severity': 'hard',
        'message': f"原排程末段停留湿度为 {last_rh:g}%RH，补做样本的入箱平衡湿度"
                   f"与之差值最大 {worst:g}%RH，超过独立换档上限 "
                   f"{shift_limit:g}%RH；空箱无法插入中间停留段，"
                   f"新旧时间轴衔接处的换档差值无法满足",
        'suggestion': {'type': 'shift-limit',
                       'title': f'把独立换档上限放宽到 {need:g}%RH',
                       'delta': round(worst - shift_limit, 2),
                       'value': need}}]


def _junction_probe(samples: list[_Sample], occs: list[_Occ], cap: int,
                    horizon: int, shift_limit: Optional[float],
                    pinned: list[float], pin_end: int) -> Optional[dict]:
    """全部补做样本在 E+1 入箱的探针：检查衔接处换档是否超限（错峰也无法
    消除的情形给出放宽建议）。"""
    if shift_limit is None:
        return None
    trial_h = min(horizon, pin_end + 1 +
                  2 * max((s.mission_minutes for s in samples), default=0) +
                  6 * 60 + 120)
    res = _Engine(samples, occs, cap, {s.id: pin_end + 1 for s in samples},
                  horizon=trial_h, validate=True,
                  max_shift_delta=shift_limit,
                  fixed_rh=pinned).run()
    junction_v = [v for v in res['violations']
                  if v['kind'] == 'shift-limit' and
                  v['minute'] <= pin_end + 1]
    if not junction_v:
        return None
    v = max(junction_v, key=lambda x: x['delta'])
    need = ceil_tenth(v['delta'])
    return {
        'kind': 'redo-junction', 'severity': 'hard',
        'message': f"新旧时间轴衔接处换档差值 {v['delta']:g}%RH 超过独立换档上限 "
                   f"{shift_limit:g}%RH（{v['from_rh']:g}% → {v['to_rh']:g}%），"
                   f"错峰补做也无法避开",
        'suggestion': {'type': 'shift-limit',
                       'title': f'把独立换档上限放宽到 {need:g}%RH',
                       'delta': round(v['delta'] - shift_limit, 2),
                       'value': need}}


# --------------------------------------------------------------------------- #
# 搜索（固定历史前缀 + 贪心最早入箱分钟）
# --------------------------------------------------------------------------- #
def _try_redo_order(order: tuple[_Sample, ...], occs: list[_Occ], cap: int,
                    horizon: int, shift_limit: Optional[float],
                    pinned: list[float], pin_end: int
                    ) -> Optional[dict[int, int]]:
    board_times: dict[int, int] = {}
    placed: list[_Sample] = []
    for s in order:
        placed_ok = False
        t = pin_end + 1
        tries = 0
        while tries < RETRY_LIMIT and t < horizon:
            trial = dict(board_times)
            trial[s.id] = t
            trial_h = min(horizon,
                          max(len(pinned),
                              t + 2 * s.mission_minutes + 6 * 60 +
                              sum(o.end - o.start for o in occs
                                  if o.end > pin_end) + 120))
            res = _Engine(placed + [s], occs, cap, trial,
                          horizon=trial_h,
                          max_shift_delta=shift_limit,
                          fixed_rh=pinned).run()
            if res['fatal'] is None and not res['violations']:
                board_times[s.id] = t
                placed.append(s)
                placed_ok = True
                break
            later = [v['minute'] for v in res['violations']
                     if v['minute'] >= t]
            jump = min(later, default=None)
            if jump is None and res['fatal']:
                jump = max(t + 1, res['end_minute'])
            t = max(t + 1, jump if jump is not None else t + 1)
            tries += 1
        if not placed_ok:
            return None
    return board_times


def _finalize_redo(source: dict, redo_samples: list[_Sample], occs: list[_Occ],
                   cap: int, board_times: dict[int, int], horizon: int,
                   shift_limit: Optional[float], pinned: list[float],
                   pin_end: int, review_payload: dict) -> dict:
    res = _Engine(redo_samples, occs, cap, board_times, horizon=horizon,
                  max_shift_delta=shift_limit,
                  fixed_rh=pinned).run()
    if res['fatal'] is not None or res['violations']:
        # 理论上不会到达（搜索已验证）；稳妥起见按冲突返回，不产出半成品
        base = _meta(source, occs, cap, shift_limit, pin_end, review_payload)
        base['failed_sample_ids'] = [s.id for s in redo_samples]
        base['conflicts'] = [{'kind': 'redo-junction', 'severity': 'hard',
                              'message': res['fatal'] or '补做校验出现违规',
                              'suggestion': None}]
        base['minimal_adjustment'] = None
        return base

    redo_lanes = res['lanes']
    hist_lanes = history_lanes(source)
    for l in hist_lanes:
        l['timeline'] = 'history'
    redo_ids = {s.id for s in redo_samples}
    for l in redo_lanes:
        l['timeline'] = 'redo'
    all_lanes = sorted(hist_lanes + redo_lanes,
                       key=lambda l: (l.get('board') or 0,
                                      l.get('sample_id') or 0))

    # 在"全部泳道"上重新标注约束来源（历史段/补做段均给出在箱样本）
    annotated = _annotate(
        {'segments': res['segments'], 'lanes': all_lanes}, occs)
    for seg in annotated:
        seg['timeline'] = 'history' if seg['start'] <= pin_end else 'redo'

    # 历史前缀的容量负载用原排程实际在箱情况回填，补做段沿用引擎结果
    hist_load = _cap_loads(hist_lanes, occs, pin_end)
    cap_series = _compress_load(hist_load)
    for c in res['capacity_series']:
        if c['start'] <= pin_end:
            continue
        if cap_series and cap_series[-1]['load'] == c['load'] and \
                cap_series[-1]['end'] == c['start']:
            cap_series[-1]['end'] = c['end']
        else:
            cap_series.append(dict(c))

    end_minute = res['end_minute']
    base = _meta(source, occs, cap, shift_limit, pin_end, review_payload)
    base.update({
        'feasible': True,
        'fatal': None,
        'conflicts': [],
        'minimal_adjustment': None,
        'end_minute': end_minute,
        'segments': annotated,
        'lanes': all_lanes,
        'capacity_series': cap_series,
        'board_times': board_times,
        'redo_samples': [_sample_json(s) for s in redo_samples],
        'failed_sample_ids': [s.id for s in redo_samples],
        'passed_sample_ids': [int(s['sample_id'])
                              for s in review_payload.get('samples') or []
                              if s.get('conclusion') ==
                              review_mod.CONCLUSION_PASS],
        'redo_board_times': dict(board_times),
        'redo_end_minute': max((l.get('unboard') or l['board'])
                               for l in redo_lanes),
        'junction': {
            'minute': pin_end + 1,
            'history_rh': round(_last_hold_rh(pinned), 2),
        },
    })
    return base


# --------------------------------------------------------------------------- #
# 输出骨架
# --------------------------------------------------------------------------- #
def _meta(source: dict, occs: list[_Occ], cap: int,
          shift_limit: Optional[float], pin_end: int,
          review_payload: dict) -> dict:
    return {
        'kind': 'redo',
        'feasible': False,
        'fatal': None,
        'capacity': cap,
        'max_shift_delta': shift_limit,
        'occupancies': [_occ_json(o) for o in occs],
        'samples': source.get('samples') or [],
        'history_end_minute': pin_end,
        'history_segments': source.get('segments') or [],
        'history_lanes': history_lanes(source),
        'review_overall_conclusion': review_payload.get(
            'overall_conclusion'),
        'segments': [], 'lanes': [], 'capacity_series': [],
        'board_times': {}, 'redo_board_times': {},
        'conflicts': [], 'minimal_adjustment': None,
        'failed_sample_ids': [],
        'passed_sample_ids': [int(s['sample_id'])
                              for s in review_payload.get('samples') or []
                              if s.get('conclusion') ==
                              review_mod.CONCLUSION_PASS],
    }


def _empty_plan(source: dict, occs: list[_Occ], cap: int,
                shift_limit: Optional[float], pin_end: int,
                review_payload: dict) -> dict:
    """无失败样本：返回可行的空方案（不安排任何样本，历史保持不变）。"""
    hist_lanes = history_lanes(source)
    for l in hist_lanes:
        l['timeline'] = 'history'
    pinned_segments = compress(pinned_rh_series(source))
    annotated = _annotate(
        {'segments': pinned_segments, 'lanes': hist_lanes}, occs)
    for seg in annotated:
        seg['timeline'] = 'history'
    hist_load = _cap_loads(hist_lanes, occs, pin_end)
    base = _meta(source, occs, cap, shift_limit, pin_end, review_payload)
    base.update({
        'feasible': True,
        'fatal': None,
        'empty': True,
        'message': '复核结论中没有需要补做的样本（全部通过），未安排新任务。',
        'end_minute': pin_end,
        'redo_end_minute': pin_end,
        'segments': annotated,
        'lanes': hist_lanes,
        'capacity_series': _compress_load(hist_load),
        'redo_samples': [],
        'junction': {'minute': pin_end + 1,
                     'history_rh': round(_last_hold_rh(
                         pinned_rh_series(source)), 2)},
    })
    return base

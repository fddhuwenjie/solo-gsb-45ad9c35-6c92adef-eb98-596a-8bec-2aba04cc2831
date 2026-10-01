"""实测湿度复核引擎（纯标准库，无 Flask 依赖，便于单测）。

输入
====
* 计划：一次排程快照（``schedule()`` / ``validate_board_times()`` 的返回），
  其中 ``segments`` 是压缩后的 hold/ramp 段、``lanes`` 给出每个样本的
  入箱/出箱分钟与按阶段切分的 pieces。
* 实测：传感器读数，时间按分钟严格递增（可只粘贴湿度值，从起始分钟起逐分钟）。
* 参数：允许偏差 band（%RH）、累计偏差剂量上限 dose_limit（%RH·分钟）、
  最大采样间隔 max_gap（分钟）。

模型
====
计划与实测都视为**分段线性曲线** P(t)、M(t)：

* 计划在整数分钟节点 t 的值等于引擎分钟序列 rh[t]（hold 段恒定；ramp 段由
  from_rh/end_rh 线性还原），节点之间线性插值；
* 实测在相邻两次采样之间线性插值。

在每个样本的入箱→出箱窗口 [board, unboard] 内：

* 按该样本泳道的计划阶段（入箱/各档换档与保持/冷却）切分；
* 逐分钟（两条曲线在整分钟之间均为线性，故偏差 d=M−P 也是线性）用解析式
  精确积分超出允许带的面积 e(t)=max(|d(t)|−band, 0)，累计为偏差剂量；
* 相邻采样间隔 > max_gap 的区间为**采样缺口**，缺口内不插值、不计剂量，
  凡窗口未被读数完整覆盖（含缺口、首读数晚于入箱、末读数早于出箱）的样本
  结论一律为“数据不足”；
* 数据完整且累计剂量 ≤ 上限 → “通过”；数据完整但剂量超限 → “复核”。
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

EPS = 1e-9

CONCLUSION_PASS = 'pass'              # 通过：数据完整且剂量不超限
CONCLUSION_REVIEW = 'review'          # 复核：数据完整但剂量超限
CONCLUSION_INSUFFICIENT = 'insufficient'  # 数据不足：窗口存在采样缺口/未覆盖

CONCLUSION_LABELS = {
    CONCLUSION_PASS: '通过',
    CONCLUSION_REVIEW: '复核',
    CONCLUSION_INSUFFICIENT: '数据不足',
}


class ReviewError(Exception):
    """复核输入数据错误。"""


# --------------------------------------------------------------------------- #
# 读数解析
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r'[\s,;，；]+')


def parse_readings(text: str, start_minute: int = 0) -> list[tuple[int, float]]:
    """解析粘贴的传感器读数。

    支持两种行格式（可含空行与 ``#``/``//`` 注释、含非数字表头的行会被跳过）：

    * ``分钟,湿度``（两列，分隔符为空白/逗号/分号/中文标点）；
    * 单列 ``湿度``，时间从 ``start_minute`` 起逐分钟递增。

    两种格式不得混用；时间必须为 ≥0 的整数分钟且严格递增，湿度 ∈ [0, 100]。
    """
    if start_minute is None:
        start_minute = 0
    if not isinstance(start_minute, int) or start_minute < 0:
        raise ReviewError("起始分钟必须为 ≥0 的整数")

    rows: list[tuple[Optional[float], float, int]] = []
    for lineno, raw in enumerate(str(text or '').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith('//'):
            continue
        tokens = [t for t in _TOKEN_RE.split(line) if t]
        if not tokens:
            continue
        try:
            nums = [float(t) for t in tokens]
        except ValueError:
            # 含非数字内容：按表头/说明行跳过（传感器导出常见表头）
            continue
        if len(nums) == 1:
            rows.append((None, nums[0], lineno))
        elif len(nums) == 2:
            rows.append((nums[0], nums[1], lineno))
        else:
            raise ReviewError(
                f"第 {lineno} 行字段过多：应为「分钟 湿度」两列或单列湿度")

    if not rows:
        raise ReviewError("未解析到任何传感器读数")

    explicit = [r for r in rows if r[0] is not None]
    bare = [r for r in rows if r[0] is None]
    if explicit and bare:
        ln = bare[0][2]
        raise ReviewError(
            f"第 {ln} 行只有湿度值，但其余行带分钟列：请统一为两列"
            "「分钟,湿度」或全部只粘贴湿度单列")

    out: list[tuple[int, float]] = []
    if explicit:
        for t_raw, v, lineno in explicit:
            if abs(t_raw - round(t_raw)) > EPS:
                raise ReviewError(f"第 {lineno} 行的分钟必须是整数（得到 {t_raw:g}）")
            t = int(round(t_raw))
            if t < 0:
                raise ReviewError(f"第 {lineno} 行的分钟不能为负")
            out.append((t, _check_rh(v, lineno)))
        for (t0, _), (t1, _) in zip(out, out[1:]):
            if t1 <= t0:
                raise ReviewError("读数时间必须按分钟严格递增，请勿重复或倒序")
    else:
        for i, (_, v, lineno) in enumerate(bare):
            out.append((start_minute + i, _check_rh(v, lineno)))
    return out


def _check_rh(v: float, lineno: int) -> float:
    if not math.isfinite(v):
        raise ReviewError(f"第 {lineno} 行湿度不是有效数字")
    if v < -EPS or v > 100 + EPS:
        raise ReviewError(f"第 {lineno} 行湿度 {v:g}% 超出 [0, 100] %RH")
    return min(100.0, max(0.0, v))


def normalize_parameters(tolerance: Any, dose_limit: Any,
                         max_gap: Any) -> tuple[float, float, int]:
    """校验复核参数：允许偏差、剂量上限（≥0）、最大采样间隔（≥1 整数分钟）。"""
    try:
        band = float(tolerance)
    except (TypeError, ValueError):
        raise ReviewError("允许偏差必须是数字（%RH）")
    if not math.isfinite(band) or band < -EPS:
        raise ReviewError("允许偏差不能为负")
    band = max(0.0, band)
    try:
        limit = float(dose_limit)
    except (TypeError, ValueError):
        raise ReviewError("累计偏差剂量上限必须是数字（%RH·分钟）")
    if not math.isfinite(limit) or limit < -EPS:
        raise ReviewError("累计偏差剂量上限不能为负")
    limit = max(0.0, limit)
    try:
        gap = float(max_gap)
    except (TypeError, ValueError):
        raise ReviewError("最大采样间隔必须是整数分钟")
    if not math.isfinite(gap) or abs(gap - round(gap)) > EPS or gap < 1 - EPS:
        raise ReviewError("最大采样间隔必须为 ≥1 的整数分钟")
    return round(band, 6), round(limit, 6), int(round(gap))


# --------------------------------------------------------------------------- #
# 计划曲线还原
# --------------------------------------------------------------------------- #
def plan_series(segments: list[dict], end_minute: Optional[int] = None) -> list[float]:
    """把压缩段还原成整数分钟节点上的计划湿度 rh[t]。

    * hold [s,e)：rh[t]=start_rh；
    * ramp [s,e)：rh[s-1]=from_rh，移动分钟 t=s+k 的值为
      from_rh + (k+1)·(end_rh−from_rh)/(e−s)（同一段内逐分钟差分恒定）。

    返回数组下标即分钟；长度覆盖到最后一段的末端（含 unboard 所在节点）。
    """
    if not segments:
        return []
    last_end = max(int(s['end']) for s in segments)
    if end_minute is not None:
        last_end = max(last_end, int(end_minute) + 1)
    rh: list[Optional[float]] = [None] * last_end
    for seg in segments:
        s, e = int(seg['start']), int(seg['end'])
        if s < 0 or e > last_end or e <= s:
            raise ReviewError("排程快照中的计划段区间非法，无法还原计划曲线")
        if seg.get('kind') == 'hold':
            v = float(seg['start_rh'])
            for t in range(s, e):
                rh[t] = v
        elif seg.get('kind') == 'ramp':
            frm = float(seg.get('from_rh', seg['start_rh']))
            steps = e - s
            slope = (float(seg['end_rh']) - frm) / steps
            for k in range(steps):
                rh[s + k] = frm + (k + 1) * slope
        else:
            raise ReviewError(f"未知计划段类型：{seg.get('kind')!r}")
    missing = [t for t, v in enumerate(rh) if v is None]
    if missing:
        raise ReviewError("排程快照的计划段在时间轴上不连续，无法还原计划曲线")
    return [float(v) for v in rh]  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 线性函数裁剪（解析式精确积分）
# --------------------------------------------------------------------------- #
def _positive_area(f0: float, f1: float, length: float) -> float:
    """∫_0^length max(f0+(f1−f0)·u/length, 0) du 的解析式。"""
    if f0 <= EPS and f1 <= EPS:
        return 0.0
    if f0 >= -EPS and f1 >= -EPS:
        return (f0 + f1) * 0.5 * length
    # 单根：r 为零点占比例（0..1）
    r = f0 / (f0 - f1)
    r = min(1.0, max(0.0, r))
    if f0 > 0:
        return 0.5 * f0 * r * length
    return 0.5 * f1 * (1 - r) * length


def _positive_fracs(f0: float, f1: float) -> list[tuple[float, float]]:
    """线性函数 f 在单位区间上 f>0 的子区间（比例坐标），至多一段。"""
    if f0 > EPS and f1 > EPS:
        return [(0.0, 1.0)]
    if f0 <= EPS and f1 <= EPS:
        return []
    r = f0 / (f0 - f1)
    r = min(1.0, max(0.0, r))
    return [(0.0, r)] if f0 > EPS else [(r, 1.0)]


# --------------------------------------------------------------------------- #
# 复核主流程
# --------------------------------------------------------------------------- #
def review_run(schedule: dict, readings: list[tuple[int, float]],
               tolerance: float, dose_limit: float,
               max_gap: int) -> dict:
    """对一次排程快照执行实测湿度复核。"""
    if not readings:
        raise ReviewError("缺少传感器读数")
    segments = schedule.get('segments') or []
    lanes = schedule.get('lanes') or []
    if not segments or not lanes:
        raise ReviewError("排程快照缺少时间轴（segments/lanes），请先在编排台生成可行排程")
    end_minute = schedule.get('end_minute')
    rh = plan_series(segments, end_minute)

    first_t, last_t = readings[0][0], readings[-1][0]

    # 采样间隔与缺口
    gaps: list[dict] = []
    pairs: list[tuple[int, int, float, float, bool]] = []
    for (t0, v0), (t1, v1) in zip(readings, readings[1:]):
        dt = t1 - t0
        covered = dt <= max_gap
        if not covered:
            gaps.append({'start': t0, 'end': t1, 'interval': dt})
        pairs.append((t0, t1, v0, v1, covered))

    samples_out = []
    for lane in lanes:
        samples_out.append(_review_lane(
            lane, rh, pairs, first_t, last_t, gaps,
            tolerance, dose_limit, max_gap))

    n_pass = sum(s['conclusion'] == CONCLUSION_PASS for s in samples_out)
    n_review = sum(s['conclusion'] == CONCLUSION_REVIEW for s in samples_out)
    n_insuf = sum(s['conclusion'] == CONCLUSION_INSUFFICIENT for s in samples_out)
    return {
        'parameters': {
            'tolerance': tolerance,
            'dose_limit': dose_limit,
            'max_gap': max_gap,
            'start_minute': first_t,
        },
        'schedule': {
            'end_minute': int(end_minute) if end_minute is not None
            else len(rh) - 1,
            'sample_count': len(lanes),
        },
        'plan_series': [[t, _r4(v)] for t, v in enumerate(rh)],
        'readings': [[t, _r4(v)] for t, v in readings],
        'gaps': gaps,
        'samples': samples_out,
        'summary': {'pass': n_pass, 'review': n_review,
                    'insufficient': n_insuf, 'total': len(samples_out)},
    }


def _review_lane(lane: dict, rh: list[float],
                 pairs: list[tuple[int, int, float, float, bool]],
                 first_t: int, last_t: int, gaps: list[dict],
                 band: float, dose_limit: float, max_gap: int) -> dict:
    board = int(lane['board'])
    unboard = lane.get('unboard')
    pieces = lane.get('pieces') or []

    base = {
        'sample_id': lane.get('sample_id'),
        'code': lane.get('code'),
        'board': board,
        'unboard': unboard,
        'window_minutes': None,
        'data_complete': False,
        'dose_limit': _r4(dose_limit),
        'tolerance': _r4(band),
        'max_gap': max_gap,
        'dose': 0.0,
        'max_deviation': 0.0,
        'covered_minutes': 0.0,
        'uncovered': [],
        'excess_intervals': [],
    }

    if unboard is None:
        base['window_minutes'] = None
        base['uncovered'].append(
            {'start': board, 'end': board, 'reason': 'schedule-incomplete',
             'reason_label': '排程中该样本缺少出箱时间'})
        base['conclusion'] = CONCLUSION_INSUFFICIENT
        base['conclusion_label'] = CONCLUSION_LABELS[CONCLUSION_INSUFFICIENT]
        return base

    unboard = int(unboard)
    if not (0 <= board < unboard < len(rh)):
        raise ReviewError(
            f"样本 {lane.get('code')} 的在箱窗口超出计划时间轴范围")
    base['window_minutes'] = unboard - board

    # ---- 覆盖性：首/末读数与采样缺口 ------------------------------------ #
    uncovered: list[dict] = []
    if first_t > board:
        uncovered.append({'start': board, 'end': min(first_t, unboard),
                          'reason': 'before-first',
                          'reason_label': '首条读数晚于入箱分钟'})
    if last_t < unboard:
        uncovered.append({'start': max(last_t, board), 'end': unboard,
                          'reason': 'after-last',
                          'reason_label': '末条读数早于出箱分钟'})
    for g in gaps:
        lo = max(g['start'], board)
        hi = min(g['end'], unboard)
        if hi - lo > EPS:
            uncovered.append({'start': lo, 'end': hi, 'reason': 'gap',
                              'reason_label': f"采样缺口 {g['interval']} 分钟"
                                              f"（> 上限 {max_gap} 分钟，不插值）"})
    uncovered.sort(key=lambda x: x['start'])
    complete = not uncovered
    base['uncovered'] = uncovered
    base['data_complete'] = complete

    # ---- 阶段索引（lane pieces 半开覆盖 [board, unboard)） -------------- #
    def phase_at(t: int) -> dict:
        for p in pieces:
            if p['start'] <= t < p['end']:
                return p
        return pieces[-1] if pieces else {'kind': 'hold', 'step': -1}

    # ---- 逐分钟精确积分 -------------------------------------------------- #
    # 每个整分钟格 [i,i+1]：计划 P 线性（rh[i]→rh[i+1]），实测 M 也线性
    # （处于可插值的采样区间内），偏差 d=M−P 为线性函数。
    fragments: list[dict] = []
    dose = 0.0
    max_dev = 0.0
    covered_minutes = 0.0
    for t0, t1, v0, v1, covered in pairs:
        if not covered:
            continue  # 超上限缺口：不插值
        lo = max(t0, board)
        hi = min(t1, unboard)
        if hi - lo <= EPS:
            continue
        # 读数时间均为整数分钟，窗口端点也是整数 → 切点全在整分钟
        for i in range(lo, hi):
            m0 = v0 + (v1 - v0) * (i - t0) / (t1 - t0)
            m1 = v0 + (v1 - v0) * (i + 1 - t0) / (t1 - t0)
            d0, d1 = m0 - rh[i], m1 - rh[i + 1]
            covered_minutes += 1.0
            max_dev = max(max_dev, abs(d0), abs(d1))
            piece = phase_at(i)
            for side, f0, f1 in (('+', d0 - band, d1 - band),
                                 ('-', -d0 - band, -d1 - band)):
                area = _positive_area(f0, f1, 1.0)
                if area <= EPS:
                    continue
                dose += area
                for r0, r1 in _positive_fracs(f0, f1):
                    ts, te = i + r0, i + r1
                    if te - ts <= EPS:
                        continue
                    dev_s = abs((d0 + (d1 - d0) * r0))
                    dev_e = abs((d0 + (d1 - d0) * r1))
                    fragments.append({
                        'start': ts, 'end': te, 'side': side,
                        'kind': piece.get('kind'),
                        'step': piece.get('step', -1),
                        'phase': _phase_label(piece),
                        'dose': area,
                        'max_deviation': max(dev_s, dev_e),
                    })

    intervals = _merge_fragments(fragments)
    base['dose'] = _r4(dose)
    base['max_deviation'] = _r4(max_dev)
    base['covered_minutes'] = _r4(covered_minutes)
    base['excess_intervals'] = intervals

    if not complete:
        conclusion = CONCLUSION_INSUFFICIENT
    elif dose > dose_limit + 1e-6:
        conclusion = CONCLUSION_REVIEW
    else:
        conclusion = CONCLUSION_PASS
    base['conclusion'] = conclusion
    base['conclusion_label'] = CONCLUSION_LABELS[conclusion]
    return base


def _phase_label(piece: dict) -> str:
    kind = piece.get('kind')
    step = piece.get('step', -1)
    if kind == 'hold' and step is not None and step >= 0:
        return f'第{step + 1}档保持'
    if kind == 'ramp' and step is not None and step >= 0:
        return f'第{step + 1}档换档'
    if kind == 'board':
        return '入箱平衡'
    if kind == 'cooldown':
        return '冷却/平衡保持'
    if kind == 'cooldown-ramp':
        return '冷却换档'
    if kind == 'wait':
        return '等待/偏离'
    return kind or '计划段'


def _merge_fragments(frags: list[dict]) -> list[dict]:
    """把相邻整分钟的同阶段同方向超带碎片合并为可读区间（端点相接即合并）。"""
    out: list[dict] = []
    for f in sorted(frags, key=lambda x: (x['start'], x['end'])):
        if out and out[-1]['end'] >= f['start'] - 1e-7 and \
                out[-1]['side'] == f['side'] and \
                out[-1]['phase'] == f['phase']:
            m = out[-1]
            m['end'] = f['end']
            m['dose'] += f['dose']
            m['max_deviation'] = max(m['max_deviation'], f['max_deviation'])
        else:
            out.append(dict(f))
    for m in out:
        m['start'] = _r3(m['start'])
        m['end'] = _r3(m['end'])
        m['dose'] = _r4(m['dose'])
        m['max_deviation'] = _r4(m['max_deviation'])
        m['start_display'] = fmt_minute_frac(m['start'])
        m['end_display'] = fmt_minute_frac(m['end'])
    return out


# --------------------------------------------------------------------------- #
def _r3(x: float) -> float:
    return round(float(x), 3)


def _r4(x: float) -> float:
    return round(float(x), 4)


def fmt_minute_frac(t: float) -> str:
    """分钟（可为小数）→ 可读时长，如 90.5 → '1h30.5m'。"""
    t = float(t)
    d, r = divmod(t, 24 * 60)
    h, m = divmod(r, 60)
    ms = f'{m:.1f}'.rstrip('0').rstrip('.') if abs(m - round(m)) > 1e-7 \
        else str(int(round(m)))
    out = ''
    if d:
        out += f'{int(d)}d'
    if h:
        out += f'{int(h)}h'
    if ms != '0' or not out:
        out += f'{ms}m'
    return out

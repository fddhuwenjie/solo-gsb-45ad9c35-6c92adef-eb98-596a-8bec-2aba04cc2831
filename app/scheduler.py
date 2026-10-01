"""湿度阶梯试验编排引擎（纯标准库，无 Flask 依赖，便于单测）。

核心模型
========
* 恒湿箱任意时刻只有一个湿度设定值（分钟级整数时间轴，湿度可按速率连续变化，
  每整分钟的位移按速率向下取整，保证不超速）。
* 每个样本 = 一次"任务"：入箱(平衡湿度) → 依次经过若干阶梯
  （目标湿度/保持时长/升降速率）→ 冷却回平衡湿度并保持（冷却/平衡间隔）→ 出箱。
* 多样本同箱共享同一条湿度轨迹，逐分钟检查：
    - 容量：同一时刻在箱样本数 + 占用位数 ≤ 箱容量；
    - 区间兼容：当前湿度落在每个在箱样本的耐受区间内；
    - 换档上限：每分钟位移 ≤ 每个在箱样本当前阶梯声明的速率（取在箱样本最小值）；
    - 独立换档上限（箱设置，可空=不限制）：时间轴上相邻两段目标湿度
      （每次换档的前后停留湿度）差值绝对值不得超过该设置；
    - 不可相邻：互斥样本的在箱时间段不得重叠；
    - 已有占用：占用段内湿度恒定、占用容量，换档不得穿过占用段。
* 换档规则：只有当前湿度上所有在箱样本都"待够"了当前保持，才能换档；
  目标不参与该档的样本可在耐受区间内被"顺路带走"，控制器随后优先把它送回
  自己的目标（away/return）。因此共享箱会增加等待时间，但不破坏任何约束。

搜索：对样本顺序做排列（大样本集用启发式候选），逐样本贪心找最早可行入箱分钟；
全部顺序失败则汇总冲突并给出最小调整建议。
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from itertools import permutations
from typing import Any, Optional

EPS = 0.05                          # 湿度数值相等容差（%RH）
MAX_HORIZON = 2 * 7 * 24 * 60       # 排程时间轴上限：2 周（分钟）
RETRY_LIMIT = 60                    # 单个样本装载的重试分钟点数
ORDER_LIMIT = 7                     # ≤7 个样本做全排列；超过则用启发式顺序


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class _Sample:
    id: Optional[int]
    code: str
    rh_min: float
    rh_max: float
    eq_rh: float                    # 入箱/出箱平衡湿度
    eq_duration: int                # 出箱前冷却/平衡保持时长（分钟）
    steps: list[dict]               # [{target, dwell, rate}]
    excludes: set[int] = field(default_factory=set)

    @property
    def min_rate(self) -> float:
        return min(s['rate'] for s in self.steps)

    @property
    def mission_minutes(self) -> int:
        total = self.eq_duration
        prev = self.eq_rh
        for st in self.steps:
            total += st['dwell'] + ramp_minutes(prev, st['target'], st['rate'])
            prev = st['target']
        total += ramp_minutes(prev, self.eq_rh, self.steps[-1]['rate'])
        return total


@dataclass
class _Occ:
    id: Optional[int]
    start: int
    end: int
    rh: float
    slots: int
    note: str = ''


@dataclass
class _Mission:
    """样本的一次在箱任务（运行时状态）。"""
    s: _Sample
    board: int
    idx: int = -1               # -1 刚入箱；0..n-1 第 idx 档；n 冷却中
    dwelled: int = 0
    away: bool = False         # 被顺路带到非自己目标的湿度
    step_done: bool = False    # 当前档保持是否已完成（不随带离清零）
    unboarded: bool = False
    unboard: Optional[int] = None
    trace: dict[int, tuple[str, int]] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.s.steps)

    @property
    def target(self) -> Optional[float]:
        if 0 <= self.idx < self.n:
            return self.s.steps[self.idx]['target']
        if self.idx >= self.n:
            return self.s.eq_rh
        return None

    @property
    def need_dwell(self) -> int:
        if 0 <= self.idx < self.n:
            return self.s.steps[self.idx]['dwell']
        if self.idx >= self.n:
            return self.s.eq_duration
        return 0

    @property
    def rate_cap(self) -> float:
        """该任务当前允许参与换档的速率上限。"""
        if not self.s.steps:
            return self.s.min_rate
        i = max(0, min(self.idx if self.idx >= 0 else 0, self.n - 1))
        return self.s.steps[i]['rate']


def ramp_minutes(a: float, b: float, rate: float) -> int:
    """从 a 线性换档到 b，每整分钟位移不超过 rate（向下取整）所需的移动分钟数。

    每分钟最大位移 per = floor(rate)；最后一分钟若距离不足 per 则只走余数。
    """
    d = abs(a - b)
    if d < EPS:
        return 0
    per = max(EPS, int(rate + EPS))
    whole, frac = divmod(d, per)
    return int(whole) + (1 if frac > EPS else 0)


def ceil_tenth(x: float) -> float:
    """向上取整到 0.1，并按 EPS 容差消除浮点尾巴（如 ceil(20*10)/10 == 20）。"""
    return round(math.ceil((x - EPS) * 10) / 10, 1)


# --------------------------------------------------------------------------- #
# 输入归一化
# --------------------------------------------------------------------------- #
class SchedulingError(Exception):
    """输入数据错误（区别于"约束无解"）。"""


def _num(v: Any, name: str, lo: float | None = None, hi: float | None = None,
         integer: bool = False) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SchedulingError(f"{name} 必须是数字")
    if integer and abs(x - round(x)) > EPS:
        raise SchedulingError(f"{name} 必须是整数分钟")
    if integer:
        x = float(int(round(x)))
    if x < 0:
        raise SchedulingError(f"{name} 不能为负")
    if lo is not None and x < lo - EPS:
        raise SchedulingError(f"{name} 不能小于 {lo}")
    if hi is not None and x > hi + EPS:
        raise SchedulingError(f"{name} 不能大于 {hi}")
    return x


def normalize_sample(d: dict) -> _Sample:
    sid = d.get('id')
    code = str(d.get('code') or '').strip()
    if not code:
        raise SchedulingError("样本编号不能为空")
    rh_min = _num(d.get('rh_min'), f"样本 {code} 的耐受下限", 0, 100)
    rh_max = _num(d.get('rh_max'), f"样本 {code} 的耐受上限", 0, 100)
    if rh_max < rh_min - EPS:
        raise SchedulingError(f"样本 {code} 的耐受上限小于下限")
    if d.get('eq_rh') in (None, ''):
        eq_rh = max(30.0, min(60.0, rh_min))
    else:
        eq_rh = _num(d.get('eq_rh'), f"样本 {code} 的平衡湿度", 0, 100)
    if not (rh_min - EPS <= eq_rh <= rh_max + EPS):
        raise SchedulingError(f"样本 {code} 的平衡湿度 {eq_rh:g}% 不在耐受区间内")
    eq_duration = int(_num(d.get('eq_duration', 30),
                           f"样本 {code} 的冷却/平衡时长", integer=True))
    steps_raw = d.get('steps') or []
    if not isinstance(steps_raw, list) or not steps_raw:
        raise SchedulingError(f"样本 {code} 至少需要一个阶梯")
    steps = []
    prev = eq_rh
    for i, st in enumerate(steps_raw):
        tgt = _num(st.get('target'), f"样本 {code} 第{i+1}档目标湿度", 0, 100)
        if not (rh_min - EPS <= tgt <= rh_max + EPS):
            raise SchedulingError(
                f"样本 {code} 第{i+1}档目标 {tgt:g}% 超出耐受区间 "
                f"[{rh_min:g}, {rh_max:g}]")
        dwell = int(_num(st.get('dwell'), f"样本 {code} 第{i+1}档保持时长",
                         integer=True))
        rate = _num(st.get('rate'), f"样本 {code} 第{i+1}档升降速率", 0.01)
        steps.append({'target': tgt, 'dwell': dwell, 'rate': rate})
        prev = tgt
    excludes = {int(x) for x in (d.get('excludes') or [])}
    return _Sample(id=sid, code=code, rh_min=rh_min, rh_max=rh_max, eq_rh=eq_rh,
                   eq_duration=eq_duration, steps=steps, excludes=excludes)


def normalize_occ(d: dict) -> _Occ:
    start = int(_num(d.get('start'), "占用起始时间", integer=True))
    end = int(_num(d.get('end'), "占用结束时间", integer=True))
    if end <= start:
        raise SchedulingError("占用结束时间必须晚于起始时间")
    rh = _num(d.get('rh'), "占用湿度", 0, 100)
    slots = int(_num(d.get('slots', 1), "占用容量", integer=True, lo=1))
    return _Occ(id=d.get('id'), start=start, end=end, rh=rh, slots=slots,
                note=str(d.get('note') or ''))


def normalize_max_shift_delta(v: Any) -> Optional[float]:
    """恒湿箱"独立换档上限"：每次换档相邻段目标湿度最大差值（%RH）。

    None / '' / 缺省 = 不限制；否则必须为 (0, 100] 的正数。
    旧数据没有该字段时按不限制处理。
    """
    if v is None or v == '':
        return None
    x = _num(v, "每次换档最大目标湿度差值", lo=0.01, hi=100)
    if x <= EPS:
        raise SchedulingError("每次换档最大目标湿度差值必须为正数（或留空表示不限制）")
    return x


# --------------------------------------------------------------------------- #
# 仿真器
# --------------------------------------------------------------------------- #
class _Engine:
    """确定性分钟级仿真。

    时间约定：rh[t] 为第 t 分钟的设定湿度；hold 决策在第 t 分钟做，
    决定第 t+1 分钟是"继续保持"还是"开始换档"。trace 与 rh 对齐。
    """

    def __init__(self, samples: list[_Sample], occs: list[_Occ],
                 capacity: int, board_times: dict[int, int],
                 horizon: int = MAX_HORIZON, validate: bool = False,
                 max_shift_delta: Optional[float] = None,
                 prefix_rh: Optional[list[float]] = None,
                 prefix_load: Optional[list[int]] = None):
        self.samples = samples
        self.by_id = {s.id: s for s in samples if s.id is not None}
        self.occs = sorted(occs, key=lambda o: o.start)
        self.capacity = capacity
        self.bt = dict(board_times)
        self.horizon = horizon
        self.validate = validate
        self.max_shift_delta = max_shift_delta
        # 补做排程：prefix_rh/prefix_load 给出冻结历史（含两端的分钟节点
        # 0..base），仿真只从 base 之后继续；历史段不可修改、不重新校验。
        if prefix_rh is not None:
            if not prefix_rh or prefix_load is None or \
                    len(prefix_load) != len(prefix_rh):
                raise SchedulingError("补做排程的历史时间轴数据不完整")
            self.base = len(prefix_rh) - 1
            self.prefix_rh = list(prefix_rh)
            self.prefix_load = list(prefix_load)
        else:
            self.base = 0
            self.prefix_rh = None
            self.prefix_load = None
        self.missions: list[_Mission] = []
        self.rh: float = (self.prefix_rh[-1] if self.prefix_rh is not None
                          else 50.0)
        self.ramp: Optional[dict] = None
        self.rh_series: list[float] = []
        self.violations: list[dict] = []
        self.load_series: list[int] = []
        self.fatal: Optional[str] = None

        if self.prefix_rh is None:
            first_board = min(self.bt.values(), default=None)
            if first_board is not None:
                s = next(s for s in samples if s.id in self.bt
                         and self.bt[s.id] == first_board)
                self.rh = s.eq_rh
            elif self.occs:
                self.rh = self.occs[0].rh

    # ---- 辅助 ------------------------------------------------------------ #
    def _active(self, t: int) -> list[_Mission]:
        return [m for m in self.missions if m.board <= t and not m.unboarded]

    def _occs_at(self, t: int) -> list[_Occ]:
        return [o for o in self.occs if o.start <= t < o.end]

    def _occ_load(self, t: int) -> int:
        return sum(o.slots for o in self._occs_at(t))

    def _viol(self, t: int, kind: str, msg: str,
              missions: list[_Mission] | None = None) -> None:
        self.violations.append({
            'minute': t, 'kind': kind, 'message': msg,
            'samples': sorted({m.s.id for m in (missions or [])
                               if m.s.id is not None}),
        })

    def _trace_phase(self, m: _Mission, t: int) -> tuple[str, int]:
        """第 t 分钟该任务所处阶段。piece 与全局段同为半开几何约定。

        到达分钟（本分钟有差分，但任务恰在该分钟进阶到当前目标）标记为 hold；
        换档的第一个移动分钟标记为 ramp，其几何起点（前一 hold 角点）
        在 _lane 压缩时向前并入 ramp piece。
        """
        if m.idx == -1:
            return ('board', -1) if abs(self.rh - m.s.eq_rh) < EPS else ('ramp', 0)
        if m.idx >= m.n:
            if abs(self.rh - m.s.eq_rh) < EPS:
                return ('cooldown', m.n)
            return ('cooldown-ramp', m.n)
        tgt = m.s.steps[m.idx]['target']
        if abs(self.rh - tgt) < EPS:
            return ('hold', m.idx)
        return ('ramp', m.idx)

    # ---- 换档可行性 ------------------------------------------------------ #
    def _can_ramp(self, frm: float, to: float, active: list[_Mission],
                  t: int) -> tuple[bool, str]:
        if abs(to - frm) < EPS:
            return True, ''
        lo, hi = min(frm, to), max(frm, to)
        cap_rate = min((m.rate_cap for m in active), default=100.0)
        duration = ramp_minutes(frm, to, cap_rate)
        per = max(EPS, int(cap_rate + EPS))
        # 换档逐分钟不得穿过占用段（占用段固定湿度，只允许端点相接）
        tt = t
        cur = frm
        for _ in range(duration):
            nxt = cur + (per if to > frm else -per)
            nxt = min(nxt, to) if to > frm else max(nxt, to)
            minute = tt + 1
            for o in self.occs:
                if o.start < minute < o.end or \
                        (o.start == minute and abs(o.rh - nxt) > EPS) or \
                        (o.end == minute and abs(o.rh - cur) > EPS):
                    return False, (f"换档在第 {minute} 分钟被占用段"
                                   f"（{o.start}-{o.end} 分钟，{o.rh:g}%）切断")
            cur = nxt
            tt += 1
        # 所有在箱样本：整段路径在耐受区间内；目标要么是自己的下一档，
        # 要么能容忍（顺路带走，之后送回）
        for m in active:
            if lo < m.s.rh_min - EPS or hi > m.s.rh_max + EPS:
                return False, (f"换档路径 [{lo:g}, {hi:g}]% 超出样本 "
                               f"{m.s.code} 耐受区间")
        return True, ''

    # ---- hold 决策（在第 t 分钟） ---------------------------------------- #
    def _decide(self, t: int) -> None:
        active = self._active(t)

        # 1) 冷却完成 → 出箱（第 t+1 分钟起不在箱）
        for m in active:
            if m.idx >= m.n and abs(self.rh - m.s.eq_rh) < EPS and \
                    not m.away and m.step_done:
                m.unboarded = True
                m.unboard = t + 1
        active = self._active(t)

        # 2) 第 t+1 分钟到点入箱（入箱分钟的阶段在此记录，rh 尚未推进）
        boarding = [_Mission(s, t + 1) for s in self.samples
                    if self.bt.get(s.id) == t + 1]
        for m in boarding:
            if abs(self.rh - m.s.eq_rh) > EPS:
                self._viol(t + 1, 'board',
                           f"样本 {m.s.code} 计划在 {self.rh:g}% 入箱，"
                           f"但其平衡湿度为 {m.s.eq_rh:g}%（入箱只允许在平衡湿度）",
                           [m])
        self.missions.extend(boarding)
        active = self._active(t)
        # 3) 空载：优先把湿度预调到"最近的未来目标"——
        #    最近的到点入箱样本平衡湿度，或最近占用段湿度（取时间更早者）
        if not active:
            upcoming = [(b, self.by_id.get(sid).eq_rh, 'board')
                        for sid, b in self.bt.items()
                        if b > t and self.by_id.get(sid)]
            upcoming += [(o.start, o.rh, 'occ') for o in self.occs if o.start > t]
            if upcoming:
                when, target_rh, _kind = min(upcoming, key=lambda x: x[0])
                if abs(self.rh - target_rh) > EPS:
                    ok, _ = self._can_ramp(self.rh, target_rh, [], t)
                    if ok:
                        self._begin_ramp(target_rh, None, t, advance=False)
            return

        # 占用段内：除出箱/入箱外不做任何换档
        if self._occs_at(t):
            return

        # 4) 被顺路带离当前档目标、且当前档保持未完成的任务需要先送回
        def _needs_return(m: _Mission) -> bool:
            if not m.away:
                return False
            if m.idx == -1:
                return True
            return not m.step_done

        away = [m for m in active if _needs_return(m)]
        if away:
            victim = min(away, key=lambda m: (m.board, m.s.id or 0))
            tgt = victim.s.steps[0]['target'] if victim.idx == -1 else victim.target
            if tgt is not None and abs(self.rh - tgt) > EPS:
                # 其余在当前湿度保持、当前档未完成的任务会阻挡动身
                blockers = [m for m in active if not _needs_return(m) and
                            ((m.idx == -1 and abs(self.rh - m.s.eq_rh) < EPS) or
                             (m.target is not None and
                              abs(self.rh - m.target) < EPS))
                            and not m.step_done]
                if blockers:
                    return
                ok, why = self._can_ramp(self.rh, tgt, active, t)
                if ok:
                    self._begin_ramp(tgt, victim, t, advance=False)
                elif not self.validate:
                    self.fatal = f"无法把样本 {victim.s.code} 送回 " \
                                 f"{tgt:g}%：{why}"
            return

        # 5) 下一占用段马上开始（t+1）：必须立刻让湿度过渡
        nxt = next((o for o in self.occs if o.start > t), None)
        if nxt and nxt.start == t + 1 and abs(self.rh - nxt.rh) > EPS:
            ok, why = self._can_ramp(self.rh, nxt.rh, active, t)
            if not ok:
                self.fatal = (f"无法在占用段开始前到达 {nxt.rh:g}%：{why}")
            else:
                self._begin_ramp(nxt.rh, None, t)
            return

        # 6) 正常推进
        # 已完成当前档的 away 任务不再回当前目标，清除 away 参与下一档
        for m in active:
            if m.away and m.idx >= 0 and m.step_done:
                m.away = False

        def _next_goal(m: _Mission) -> Optional[float]:
            if m.idx == -1:
                return m.s.steps[0]['target']
            if m.idx + 1 < m.n:
                return m.s.steps[m.idx + 1]['target']
            return m.s.eq_rh

        # 阻挡者：在当前湿度上、当前档保持未完成的任务（不能离开该湿度）
        blockers = [m for m in active
                    if not m.away and m.target is not None
                    and abs(self.rh - m.target) < EPS and not m.step_done]
        if blockers:
            return
        # 可驱动者：
        #  - idx=-1 在平衡湿度上；
        #  - 在当前档目标湿度上且当前档已完成（清 away 后统一为非 away）
        ready = []
        for m in active:
            at_eq = m.idx == -1 and abs(self.rh - m.s.eq_rh) < EPS
            at_cur = (m.target is not None and
                      abs(self.rh - m.target) < EPS and m.step_done)
            if not m.away and (at_eq or at_cur):
                ready.append(m)
        if not ready:
            return
        driver = min(ready, key=lambda m: (m.board, m.s.id or 0))
        goal = _next_goal(driver)
        if goal is None:
            return
        if abs(goal - self.rh) < EPS:
            driver.idx = max(0, driver.idx + 1)
            driver.dwelled = 0
            return
        ok, why = self._can_ramp(self.rh, goal, active, t)
        if not ok:
            self._viol(t, 'cannot-ramp',
                       f"样本 {driver.s.code} 需要换档到 {goal:g}%，但 {why}",
                       [driver])
            return
        self._begin_ramp(goal, driver, t)

    def _begin_ramp(self, to: float, driver: Optional[_Mission], t: int,
                    advance: bool = True) -> None:
        active = self._active(t)
        cap_rate = min((m.rate_cap for m in active), default=100.0)
        per = max(EPS, int(cap_rate + EPS))
        dur = ramp_minutes(self.rh, to, cap_rate)
        self.ramp = {'to': to, 'remaining': dur, 'per': per,
                     'up': to > self.rh, 'driver': driver, 'advance': advance}

    def _advance(self, t: int) -> None:
        """推进第 t 分钟的湿度（t≥1），逐分钟走，到达时触发 _on_arrival。"""
        if self.ramp is None:
            return
        info = self.ramp
        step = info['per'] * (1 if info['up'] else -1)
        new_rh = round(self.rh + step, 6)
        new_rh = min(new_rh, info['to']) if info['up'] else max(new_rh, info['to'])
        self.rh = new_rh
        info['remaining'] -= 1
        if info['remaining'] <= 0:
            self.rh = info['to']
            self.ramp = None
            self._on_arrival(info['to'], info['driver'], t,
                             advance=info['advance'])

    def _on_arrival(self, rh: float, driver: Optional[_Mission], t: int,
                    advance: bool = True) -> None:
        """换档到达后的状态推进。

        * advance=True（前进换档）：driver 到达下一档/冷却湿度，idx 前进，
          新档保持计时与完成标记清零；
        * advance=False（送回换档）：driver（被顺路带离者）回到自己的当前档，
          idx 与完成状态不变、清除 away、保持从零重新计时；
        * 其他在箱任务：到达自己当前档目标的清除 away 并重新计时；
          新入箱者(idx=-1)到达第 0 档目标则进入第 0 档；否则标记 away。
        """
        for m in self._active(t):
            if m is driver:
                continue
            if m.idx == -1:
                if abs(m.s.steps[0]['target'] - rh) < EPS:
                    m.idx = 0
                    m.away = False
                    m.dwelled = 0
                    m.step_done = False
                else:
                    m.away = True
                    m.dwelled = 0
                continue
            tgt = m.target
            # 已完成当前档：若到达值正是它的下一档/冷却目标，一同进阶
            if m.step_done and tgt is not None:
                nxt = m.s.steps[m.idx + 1]['target'] \
                    if m.idx + 1 < m.n else m.s.eq_rh
                if abs(nxt - rh) < EPS:
                    m.away = False
                    m.idx += 1
                    m.dwelled = 0
                    m.step_done = False
                    continue
            if tgt is not None and abs(tgt - rh) < EPS:
                was_away = m.away
                m.away = False
                if was_away and not m.step_done:
                    m.dwelled = 0
                elif not was_away and not m.step_done:
                    m.dwelled = 0
            elif tgt is not None:
                m.away = True
                if not m.step_done:
                    m.dwelled = 0
        if driver is not None and driver in self._active(t):
            if advance:
                driver.away = False
                driver.idx = 0 if driver.idx == -1 else driver.idx + 1
                driver.dwelled = 0
                driver.step_done = False
            else:
                if driver.idx == -1 and \
                        abs(driver.s.steps[0]['target'] - rh) < EPS:
                    driver.idx = 0
                    driver.step_done = False
                driver.away = False
                driver.dwelled = 0

    # ---- 逐分钟约束检查 -------------------------------------------------- #
    def _check(self, t: int) -> None:
        rh = self.rh
        active = self._active(t)

        for o in self._occs_at(t):
            if abs(rh - o.rh) > EPS:
                self._viol(t, 'occ-rh',
                           f"占用段「{o.note or o.id}」要求 {o.rh:g}%RH，"
                           f"实际 {rh:g}%", active)
        if len(self._occs_at(t)) > 1:
            self._viol(t, 'occ-overlap', "同一时刻存在重叠的已有占用段", active)

        load = len(active) + self._occ_load(t)
        if load > self.capacity:
            self._viol(t, 'capacity',
                       f"容量超限：样本 {len(active)} 件 + 占用 "
                       f"{self._occ_load(t)} 位 > 箱容量 {self.capacity}", active)

        if t > 0:
            d = rh - self.rh_series[t - 1]
            for m in active:
                if abs(d) > m.rate_cap + EPS:
                    self._viol(t, 'rate',
                               f"样本 {m.s.code} 的换档上限 "
                               f"{m.rate_cap:g}%/min，实际 {abs(d):g}%/min", [m])

        for m in active:
            if rh < m.s.rh_min - EPS or rh > m.s.rh_max + EPS:
                self._viol(t, 'range',
                           f"样本 {m.s.code} 耐受 [{m.s.rh_min:g}, "
                           f"{m.s.rh_max:g}]%，实际 {rh:g}%", [m])

        aboard_ids = {m.s.id for m in active}
        for m in active:
            for x in m.s.excludes:
                if x in aboard_ids:
                    other = self.by_id.get(x)
                    self._viol(t, 'adjacency',
                               f"不可相邻样本 {m.s.code} 与 "
                               f"{other.code if other else x} 同时在箱", [m])

    # ---- 独立换档上限（相邻段目标值之差） -------------------------------- #
    def _check_shift_limit(self, end: int) -> None:
        """比较时间轴相邻段的目标湿度：一次换档（可能由多个连续 ramp 段
        组成——整分钟位移向下取整时各分钟步长可能不同）两端停留湿度之差，
        不得超过箱设置 max_shift_delta（None 表示不限制）。

        自动排程中产生该违规的入箱分钟会被搜索跳过；拖拽校验中它以
        'shift-limit' 违规呈现，并给出相邻段、差值与可调整项。
        """
        if self.max_shift_delta is None:
            return
        segments = compress(self.rh_series[: end + 1])
        # 把连续相邻的 ramp 段合并成"一次换档"，前后最近的 hold 段即相邻段
        i = 0
        while i < len(segments):
            if segments[i]['kind'] != 'ramp':
                i += 1
                continue
            j = i
            while j + 1 < len(segments) and segments[j + 1]['kind'] == 'ramp' \
                    and segments[j + 1]['start'] == segments[j]['end']:
                j += 1
            run = segments[i:j + 1]
            start_min, end_min = run[0]['start'], run[-1]['end']
            # 补做模式：历史段（换档起始分钟 ≤ base）冻结保留，不以当前
            # 设置重新校验；衔接处换档从 base 之后开始，照常校验
            if self.prefix_rh is not None and start_min <= self.base:
                i = j + 1
                continue
            frm = run[0].get('from_rh', run[0]['start_rh'])
            to = run[-1]['end_rh']
            prev_hold = next((x for x in reversed(segments[:i])
                              if x['kind'] == 'hold'), None)
            next_hold = next((x for x in segments[j + 1:]
                              if x['kind'] == 'hold'), None)
            if prev_hold is not None and \
                    abs(prev_hold['start_rh'] - frm) < EPS:
                frm = prev_hold['start_rh']
            if next_hold is not None and \
                    abs(next_hold['start_rh'] - to) < EPS:
                to = next_hold['start_rh']
            delta = abs(to - frm)
            if delta > self.max_shift_delta + EPS:
                active = [m for m in self.missions
                          if m.board <= start_min and
                          (m.unboard is None or m.unboard > start_min)]
                adj = []
                if prev_hold is not None:
                    adj.append({'start': prev_hold['start'],
                                'end': prev_hold['end'],
                                'rh': round(prev_hold['start_rh'], 2)})
                if next_hold is not None:
                    adj.append({'start': next_hold['start'],
                                'end': next_hold['end'],
                                'rh': round(next_hold['start_rh'], 2)})
                if adj:
                    where = (f"前段 {fmt_minute(adj[0]['start'])}-"
                             f"{fmt_minute(adj[0]['end'])}目标 {frm:g}% → "
                             f"后段 {fmt_minute(adj[-1]['start'])}-"
                             f"{fmt_minute(adj[-1]['end'])}目标 {to:g}%")
                else:
                    where = (f"换档 {fmt_minute(start_min)}-{fmt_minute(end_min)} "
                             f"{frm:g}% → {to:g}%")
                adjusts = self._shift_adjustments(delta, frm, to)
                self._viol(start_min, 'shift-limit',
                           f"独立换档上限 {self.max_shift_delta:g}%RH 被突破："
                           f"{where}，差值 {delta:g}%RH 超限。可调整："
                           f"{'；'.join(a['label'] for a in adjusts)}",
                           active)
                self.violations[-1]['delta'] = round(delta, 2)
                self.violations[-1]['limit'] = self.max_shift_delta
                self.violations[-1]['from_rh'] = round(frm, 2)
                self.violations[-1]['to_rh'] = round(to, 2)
                self.violations[-1]['adjacent_segments'] = adj
                self.violations[-1]['adjustments'] = adjusts
            i = j + 1

    def _shift_adjustments(self, delta: float, frm: float,
                           to: float) -> list[dict]:
        """为一次超限换档生成最小可调整项（放宽设置 / 改样本档目标 / 后移占用）。"""
        need = ceil_tenth(delta)
        out: list[dict] = [{
            'type': 'shift-limit',
            'label': f'把每次换档最大目标湿度差值放宽到 {need:g}%RH',
            'value': need,
        }]
        # 端点湿度属于哪个在箱样本的目标/平衡档 → 改该档目标即可缩短差值
        seen = set()
        for m in self.missions:
            chain = [m.s.eq_rh] + [st['target'] for st in m.s.steps]
            for j in range(1, len(chain)):
                a, b = chain[j - 1], chain[j]
                if abs(b - a) <= self.max_shift_delta + EPS or \
                        not (abs(a - frm) < EPS or abs(b - to) < EPS):
                    continue
                key = (m.s.id, j)
                if key in seen:
                    continue
                seen.add(key)
                step_no = j
                label = (f"修改样本 {m.s.code} 第 {step_no} 档目标（相邻目标差 "
                         f"{abs(b - a):g}% 超限）")
                out.append({'type': 'sample-step', 'label': label,
                            'sample_id': m.s.id, 'code': m.s.code,
                            'step': step_no})
        # 端点正是占用段固定湿度 → 可后移/修改占用段
        for o in self.occs:
            if abs(o.rh - frm) < EPS or abs(o.rh - to) < EPS:
                out.append({'type': 'occupancy',
                            'label': f"后移或调整已有占用「{o.note or o.id}」"
                                     f"（{fmt_minute(o.start)}-{fmt_minute(o.end)}）",
                            'occ_id': o.id})
        return out

    # ---- 主循环 ---------------------------------------------------------- #
    def run(self) -> dict:
        # 补做模式：历史为冻结前缀，不在箱的新任务入箱分钟必须严格在 base 之后
        if self.prefix_rh is not None:
            bad_bt = [sid for sid, b in self.bt.items() if b <= self.base]
            if bad_bt:
                self.fatal = (f"补做样本必须从历史结束分钟 {self.base} 之后"
                              f"重新安排（入箱分钟需 > {self.base}）")
            self.rh_series = list(self.prefix_rh)
            self.load_series = list(self.prefix_load)
            if self.base + 1 >= self.horizon:
                if self.fatal is None:
                    self.fatal = "历史已达排程时间上限，无法安排补做"
                return self._build(self.base)
            t_start = self.base
        else:
            self.rh_series = [self.rh]

            # 入箱时间为 0 的任务直接装载
            for s in self.samples:
                if self.bt.get(s.id) == 0:
                    if abs(self.rh - s.eq_rh) > EPS:
                        self._viol(0, 'board',
                                   f"样本 {s.code} 的入箱湿度与箱内初始湿度不一致")
                    m0 = _Mission(s, 0)
                    self.missions.append(m0)
            self.load_series = [len([m for m in self.missions if m.board == 0])
                                + self._occ_load(0)]
            t_start = 0

        stall = 0
        end = self.horizon
        for t in range(t_start, self.horizon):
            if self.ramp is None:
                self._decide(t)
            self._advance(t + 1)
            self.rh_series.append(self.rh)

            active = self._active(t + 1)
            for m in active:
                m.trace[t + 1] = self._trace_phase(m, t + 1)
            # 第 0 分钟装载的任务补记 t=0 阶段
            for m in active:
                if m.board == 0 and 0 not in m.trace:
                    m.trace[0] = self._trace_phase(m, 0)
            # 保持计时：在自己目标湿度上的非 away 分钟都计入；达到要求后标记完成
            if active:
                for m in active:
                    if m.idx < 0:
                        continue
                    at_rh = (m.target is not None and
                             abs(self.rh - m.target) < EPS)
                    if not m.away and at_rh and not m.step_done:
                        m.dwelled += 1
                        if m.dwelled >= m.need_dwell:
                            m.step_done = True
            self._check(t + 1)
            self.load_series.append(
                len(active) + self._occ_load(t + 1))

            # 停滞：无换档，且所有在箱任务既没有在完成保持、也不在占用段事件附近
            progressing = self.ramp is not None or any(
                (not m.away and m.target is not None and
                 abs(self.rh - m.target) < EPS and not m.step_done)
                or (m.away and not m.step_done)
                for m in active)
            near_event = any(t + 1 < o.start <= t + 121 or
                             t + 1 < o.end <= t + 121 for o in self.occs)
            if active and not progressing and not near_event:
                stall += 1
            else:
                stall = 0
            if stall > 120 and not self.validate:
                self.fatal = "排程停滞：换档窗口被占用段/在箱样本区间持续阻挡"
                end = t + 1
                break

            boarded_ids = {m.s.id for m in self.missions}
            unboarded_done = all(m.unboarded for m in self.missions)
            all_accounted = all(sid in boarded_ids for sid in self.bt)
            future = any(bt > t + 1 for bt in self.bt.values())
            future_occ = any(o.end > t + 1 or o.start > t + 1 for o in self.occs)
            # 没有在箱任务、没有未来入箱、箱内湿度又不等于"即将到点入箱者"的
            # 平衡湿度，且没有正在进行的换档 → 永远无法满足，立即判死
            pending_now = [sid for sid, b in self.bt.items() if b == t + 1]
            if not active and not future and not self.ramp and not future_occ:
                if unboarded_done and all_accounted:
                    end = t + 1
                    break
                if unboarded_done and not all_accounted:
                    miss = [self.by_id.get(sid) for sid in self.bt
                            if sid not in boarded_ids]
                    codes = ', '.join(m.code for m in miss if m)
                    self.fatal = f"样本 {codes} 未入箱，且箱内已无可驱动任务"
                    end = t + 1
                    break
            # 到点入箱者因湿度不匹配而被拒（validate 模式下）会记录 board 违规；
            # 非 validate 模式下该样本永远丢失，也判死
            if not self.validate and pending_now:
                for sid in pending_now:
                    s = self.by_id.get(sid)
                    if s and abs(self.rh - s.eq_rh) > EPS:
                        self.fatal = (f"样本 {s.code} 到点({t+1} 分钟)无法入箱："
                                      f"箱内 {self.rh:g}% ≠ 平衡湿度 {s.eq_rh:g}%")
                        end = t + 1
                        break
                if self.fatal:
                    break
            if self.fatal and not self.validate:
                end = t + 1
                break
        else:
            if not all(m.unboarded for m in self.missions):
                self.fatal = f"超出排程时间上限（{self.horizon} 分钟）仍未完成全部样本"

        # 独立换档上限：在完整时间轴上比较相邻段目标值（与速率无关的箱级设置）。
        # 仿真已中断（fatal）时只报原有致命问题，避免对截断的时间轴追加噪声违规。
        if self.fatal is None:
            self._check_shift_limit(end)
        return self._build(end)

    # ---- 输出 ------------------------------------------------------------ #
    def _build(self, end: int) -> dict:
        lanes = [self._lane(m) for m in self.missions]
        result = {
            'feasible': self.fatal is None and not self.violations,
            'fatal': self.fatal,
            'violations': self.violations,
            'end_minute': end,
            'segments': compress(self.rh_series[: end + 1]),
            'lanes': lanes,
            'capacity_series': self._capacity(end),
        }
        return result

    def _lane(self, m: _Mission) -> dict:
        """trace[t] 是第 t 分钟的阶段；压缩相邻同 (kind,step) 的分钟格子。"""
        pieces = []
        end_t = m.unboard if m.unboard is not None else \
            ((max(m.trace) + 1) if m.trace else m.board)
        for t in range(m.board, end_t):
            kind, si = m.trace.get(t, ('wait', 0))
            if pieces and pieces[-1]['kind'] == kind and \
                    pieces[-1]['step'] == si and pieces[-1]['end'] == t:
                pieces[-1]['end'] = t + 1
            else:
                pieces.append({'start': t, 'end': t + 1, 'kind': kind,
                               'step': si})
        return {'sample_id': m.s.id, 'code': m.s.code,
                'rh_min': m.s.rh_min, 'rh_max': m.s.rh_max,
                'eq_rh': m.s.eq_rh, 'board': m.board,
                'unboard': m.unboard, 'pieces': pieces}

    def _capacity(self, end: int) -> list[dict]:
        out = []
        for t, load in enumerate(self.load_series[: end + 1]):
            if out and out[-1]['load'] == load and out[-1]['end'] == t:
                out[-1]['end'] = t + 1
            else:
                out.append({'start': t, 'end': t + 1, 'load': load})
        while len(out) > 1 and out[-1]['load'] == 0:
            out.pop()
        return out


def compress(rh: list[float]) -> list[dict]:
    """把分钟设定值序列压缩成段，统一分钟格子半开区间 [start,end)。

    第 t 分钟设定值为 rh[t]：
    * hold 段 [start,end)：这些分钟设定值恒为 v；
    * ramp 段 [start,end)：start..end-1 全部是换档移动分钟（每分钟有差分），
      渲染斜线应从前一角点 rh[start-1] 画到 rh[end-1]；
      因此同时给出 from_rh=rh[start-1]，start_rh=rh[start]、end_rh=rh[end-1]。
    """
    if not rh:
        return []
    out: list[dict] = []
    n = len(rh)
    i = 0
    while i < n:
        if i == 0 or abs(rh[i] - rh[i - 1]) <= EPS:
            j = i + 1
            while j < n and abs(rh[j] - rh[j - 1]) <= EPS:
                j += 1
            out.append({'start': i, 'end': j, 'kind': 'hold',
                        'start_rh': rh[i], 'end_rh': rh[j - 1]})
            i = j
        else:
            d = rh[i] - rh[i - 1]
            j = i + 1
            while j < n and abs(rh[j] - rh[j - 1] - d) <= EPS:
                j += 1
            out.append({'start': i, 'end': j, 'kind': 'ramp',
                        'start_rh': rh[i], 'end_rh': rh[j - 1],
                        'from_rh': rh[i - 1]})
            i = j
    return out


# --------------------------------------------------------------------------- #
# 编排搜索
# --------------------------------------------------------------------------- #
def _candidate_orders(samples: list[_Sample]) -> list[tuple[_Sample, ...]]:
    if len(samples) <= ORDER_LIMIT:
        return list(permutations(samples))
    keys = [
        lambda s: (s.rh_max - s.rh_min, s.min_rate),                 # 苛刻在前
        lambda s: s.min_rate,
        lambda s: s.rh_min,
        lambda s: (-len(s.excludes), s.rh_min),
        lambda s: -s.mission_minutes,
    ]
    orders = [tuple(sorted(samples, key=k)) for k in keys]
    out, seen = [], set()
    for o in orders:
        k = tuple(s.id for s in o)
        if k not in seen:
            seen.add(k)
            out.append(o)
    return out


def schedule(samples_raw: list[dict], occs_raw: list[dict], capacity: Any,
             horizon: int = MAX_HORIZON,
             max_shift_delta: Any = None) -> dict:
    samples = [normalize_sample(d) for d in samples_raw]
    occs = [normalize_occ(d) for d in occs_raw]
    cap = int(_num(capacity, "恒湿箱容量", integer=True, lo=1))
    shift_limit = normalize_max_shift_delta(max_shift_delta)

    ids = {s.id for s in samples}
    if None in ids and len(samples) != 1:
        raise SchedulingError("样本必须保存后才能编排（缺少 id）")
    for s in samples:
        s.excludes = {x for x in s.excludes if x in ids and x != s.id}

    pre = _static_conflicts(samples, occs, cap)
    if pre:
        return _infeasible(samples, occs, cap, pre, shift_limit)
    pre_shift = _static_shift_conflicts(samples, shift_limit)
    if pre_shift:
        return _infeasible(samples, occs, cap, pre_shift, shift_limit)

    best_fail = None
    for order in _candidate_orders(samples):
        bt = _try_order(order, occs, cap, horizon, shift_limit)
        if bt is not None:
            return _finalize(samples, occs, cap, bt, horizon, shift_limit)
    return _infeasible(samples, occs, cap,
                       _diagnose(samples, occs, cap, horizon, shift_limit),
                       shift_limit)


def _static_conflicts(samples: list[_Sample], occs: list[_Occ],
                      cap: int) -> list[dict]:
    conf = []
    for o in occs:
        if o.slots > cap:
            conf.append({
                'kind': 'capacity', 'severity': 'hard',
                'message': f"占用段 {fmt_minute(o.start)}-{fmt_minute(o.end)} 需 "
                           f"{o.slots} 位，超过箱容量 {cap}",
                'suggestion': {'type': 'capacity', 'title': f'把箱容量调到 {o.slots}',
                               'delta': o.slots - cap, 'value': o.slots}})
    for i, a in enumerate(occs):
        for b in occs[i + 1:]:
            if a.start < b.end and b.start < a.end and abs(a.rh - b.rh) > EPS:
                conf.append({
                    'kind': 'occ-overlap', 'severity': 'hard',
                    'message': '已有占用段时间重叠但湿度不同，固定曲线冲突',
                    'suggestion': {'type': 'occupancy',
                                   'title': '调整其中一段已有占用的时间/湿度',
                                   'delta': 1, 'value': None}})
    # 耐受区间两两不相交且未设互斥 → 永远无法同箱
    for i, a in enumerate(samples):
        for b in samples[i + 1:]:
            if max(a.rh_min, b.rh_min) > min(a.rh_max, b.rh_max) + EPS and \
                    b.id not in a.excludes:
                conf.append({
                    'kind': 'range', 'severity': 'hard',
                    'message': f"样本 {a.code} 与 {b.code} 的耐受区间不相交"
                               f"（[{a.rh_min:g},{a.rh_max:g}] vs "
                               f"[{b.rh_min:g},{b.rh_max:g}]），无法同箱",
                    'suggestion': {
                        'type': 'adjacency',
                        'title': f'把 {a.code} 与 {b.code} 标记为不可相邻（错峰）',
                        'delta': 1, 'value': None, 'samples': [a.id, b.id]}})
    return conf


def _static_shift_conflicts(samples: list[_Sample],
                            shift_limit: Optional[float]) -> list[dict]:
    """单样本自身相邻目标（含入箱平衡→首档、末档→冷却平衡）差值超限的硬冲突：
    无论怎样安排入箱时刻都不可能满足独立换档上限。
    """
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
                    'kind': 'shift-limit', 'severity': 'hard',
                    'message': f"样本 {s.code} 第 {step_no} 次换档相邻目标 "
                               f"{chain[j - 1]:g}% → {chain[j]:g}% 差值 {d:g}%RH，"
                               f"超过独立换档上限 {shift_limit:g}%RH",
                    'suggestion': {
                        'type': 'shift-limit',
                        'title': f'把每次换档最大目标湿度差值放宽到 '
                                 f'{ceil_tenth(d):g}%RH',
                        'delta': round(d - shift_limit, 2),
                        'value': ceil_tenth(d)}})
    if conf and worst:
        # 一条可覆盖全部静态冲突的放宽建议放到首位（一键应用）
        need = ceil_tenth(worst)
        conf.insert(0, {
            'kind': 'shift-limit', 'severity': 'hard',
            'message': f"存在 {len(conf)} 处样本自身相邻目标差值超过独立换档上限 "
                       f"{shift_limit:g}%RH（最大差值 {worst:g}%RH），"
                       f"任何入箱时刻都无法满足",
            'suggestion': {'type': 'shift-limit',
                           'title': f'把独立换档上限放宽到 {need:g}%RH',
                           'delta': round(worst - shift_limit, 2),
                           'value': round(need, 1)}})
    return conf


def _try_order(order: tuple[_Sample, ...], occs: list[_Occ], cap: int,
               horizon: int,
               shift_limit: Optional[float] = None) -> Optional[dict[int, int]]:
    """固定样本装载顺序，逐样本贪心寻找最早可行入箱分钟。

    每次试排在完整时间轴上仿真当前已放置的任务，判定该任务能否完成全程；
    致命停滞/越界或违规（含独立换档上限）都视为该入箱分钟不可行，
    跳到下一个候选分钟。
    """
    board_times: dict[int, int] = {}
    placed_samples: list[_Sample] = []
    for s in order:
        placed = False
        t = 0
        tries = 0
        while tries < RETRY_LIMIT and t < horizon:
            trial = dict(board_times)
            trial[s.id] = t
            # 只需看到"当前任务 + 已放任务"全部出箱；上限给足当前任务两轮时长
            trial_h = min(horizon,
                          t + 2 * s.mission_minutes + 6 * 60 +
                          sum(o.end - o.start for o in occs) + 120)
            trial_h = max(trial_h,
                          max(board_times.values(), default=0) +
                          2 * s.mission_minutes + 120)
            res = _Engine(placed_samples + [s], occs, cap, trial,
                          horizon=trial_h,
                          max_shift_delta=shift_limit).run()
            if res['fatal'] is None and not res['violations']:
                board_times[s.id] = t
                placed_samples.append(s)
                placed = True
                break
            # 入箱湿度不匹配（board）通常随预调/占用结束很快解除，逐分钟推进；
            # 其余违规可跳到最早违规分钟继续尝试
            later = [v['minute'] for v in res['violations']
                    if v['minute'] >= t and v['kind'] != 'board']
            jump = min(later, default=None)
            if jump is None and res['fatal']:
                if res['end_minute'] >= trial_h - 1:
                    # 只是被试排窗口截断：不能按末尾分钟大跳（会越过可行窗），
                    # 逐分钟推进
                    jump = t + 1
                else:
                    jump = max(t + 1, res['end_minute'])
            t = max(t + 1, jump if jump is not None else t + 1)
            tries += 1
        if not placed:
            return None
    return board_times


def _finalize(samples: list[_Sample], occs: list[_Occ], cap: int,
              board_times: dict[int, int], horizon: int,
              shift_limit: Optional[float] = None) -> dict:
    res = _Engine(samples, occs, cap, board_times, horizon=horizon,
                  max_shift_delta=shift_limit).run()
    res['board_times'] = board_times
    res['capacity'] = cap
    res['max_shift_delta'] = shift_limit
    res['samples'] = [_sample_json(s) for s in samples]
    res['occupancies'] = [_occ_json(o) for o in occs]
    res['segments'] = _annotate(res, occs)
    res['conflicts'] = []
    return res


def _annotate(res: dict, occs: list[_Occ],
              split_at: Optional[int] = None,
              extra_lanes: Optional[list[dict]] = None) -> list[dict]:
    """为全局轨迹每段标注约束来源。

    split_at 给定时（补做排程），在该分钟节点处切开跨接历史/补做的段，
    使历史段（end ≤ split_at+1 且 start ≤ split_at）与补做段可分开渲染；
    extra_lanes 提供冻结历史泳道（其 pieces 用于历史段来源标注）。
    """
    raw_segments = res['segments']
    if split_at is not None:
        cut: list[dict] = []
        for seg in raw_segments:
            if seg['start'] < split_at < seg['end'] - 1 \
                    or (seg['start'] < split_at < seg['end']
                        and seg['kind'] == 'ramp'):
                if seg['kind'] == 'hold':
                    a = dict(seg); a['end'] = split_at + 1
                    b = dict(seg); b['start'] = split_at + 1
                    cut.extend([a, b])
                else:
                    # 正常不会发生（衔接处两侧必有停留节点）；按线性切分
                    frm = float(seg.get('from_rh', seg['start_rh']))
                    steps = seg['end'] - seg['start']
                    slope = (float(seg['end_rh']) - frm) / steps
                    mid = frm + slope * (split_at + 1 - seg['start'])
                    a = dict(seg); a['end'] = split_at + 1
                    a['end_rh'] = mid
                    b = dict(seg); b['start'] = split_at + 1
                    b['from_rh'] = mid; b['start_rh'] = mid
                    cut.extend([a, b])
            else:
                cut.append(dict(seg))
        raw_segments = cut
    all_lanes = list(res.get('lanes') or []) + list(extra_lanes or [])
    out = []
    for seg in raw_segments:
        aboard = [lane['code'] for lane in all_lanes
                  if lane['board'] < seg['end'] and
                  (lane.get('unboard') or 10 ** 12) > seg['start']]
        sources = []
        for o in occs:
            if o.start < seg['end'] and o.end > seg['start'] and \
                    seg['kind'] == 'hold' and abs(seg['start_rh'] - o.rh) < EPS:
                sources.append({'type': 'occupancy', 'occ_id': o.id,
                                'label': f"已有占用「{o.note or o.id}」"})
        for lane in all_lanes:
            for p in lane.get('pieces') or []:
                if p['kind'] not in ('ramp', 'hold', 'cooldown', 'cooldown-ramp') \
                        or p['step'] < 0:
                    continue
                if p['start'] < seg['end'] and p['end'] > seg['start']:
                    phase_name = {'ramp': '换档', 'hold': '保持',
                                  'cooldown': '冷却保持',
                                  'cooldown-ramp': '冷却换档'}.get(p['kind'], p['kind'])
                    sources.append({'type': 'sample', 'sample': lane['sample_id'],
                                    'code': lane['code'], 'step': p['step'] + 1,
                                    'phase': phase_name})
        dedup, seen = [], set()
        for s in sources:
            k = tuple(sorted(s.items()))
            if k not in seen:
                seen.add(k)
                dedup.append(s)
        seg2 = dict(seg)
        seg2['aboard'] = aboard
        seg2['sources'] = dedup
        if split_at is not None:
            seg2['era'] = 'history' if seg['end'] <= split_at + 1 and \
                seg['start'] <= split_at else 'redo'
        out.append(seg2)
    return out


def _sample_json(s: _Sample) -> dict:
    return {'id': s.id, 'code': s.code, 'rh_min': s.rh_min, 'rh_max': s.rh_max,
            'eq_rh': s.eq_rh, 'eq_duration': s.eq_duration, 'steps': s.steps,
            'excludes': sorted(s.excludes)}


def _occ_json(o: _Occ) -> dict:
    return {'id': o.id, 'start': o.start, 'end': o.end, 'rh': o.rh,
            'slots': o.slots, 'note': o.note}


# --------------------------------------------------------------------------- #
# 无解诊断 + 最小调整
# --------------------------------------------------------------------------- #
def _diagnose(samples: list[_Sample], occs: list[_Occ], cap: int,
              horizon: int,
              shift_limit: Optional[float] = None) -> list[dict]:
    advice: list[dict] = []

    # 独立换档上限：先把全部样本同时在 0 分钟入箱跑一遍，凡实际时间轴上
    # 出现的超限换档都是"任何错峰都无法消除"的，直接给放宽建议（排在最前）。
    if shift_limit is not None and samples:
        probe = _Engine(samples, occs, cap, {s.id: 0 for s in samples},
                        horizon=horizon, validate=True,
                        max_shift_delta=shift_limit).run()
        shift_v = [v for v in probe['violations']
                   if v['kind'] == 'shift-limit']
        if shift_v:
            worst = max(v['delta'] for v in shift_v)
            need = ceil_tenth(worst)
            advice.insert(0, {
                'kind': 'shift-limit', 'severity': 'hard',
                'message': f"时间轴上有 {len(shift_v)} 处相邻段目标差值超过独立换档上限 "
                           f"{shift_limit:g}%RH（最大 {worst:g}%RH），"
                           f"错峰装载也无法避开：{shift_v[0]['message']}",
                'suggestion': {'type': 'shift-limit',
                               'title': f'把独立换档上限放宽到 {need:g}%RH',
                               'delta': round(worst - shift_limit, 2),
                               'value': need}})

    occ_peak = max((o.slots for o in occs), default=0)
    # 同档目标聚类（对"并行需求"的上界估计；互斥样本本应串行）
    cluster_peak = 1
    max_steps = max((len(s.steps) for s in samples), default=0)
    for i in range(max_steps):
        c = Counter(round(s.steps[i]['target']) for s in samples if i < len(s.steps))
        cluster_peak = max(cluster_peak, max(c.values(), default=1))
    if cluster_peak + occ_peak > cap:
        need = cluster_peak + occ_peak
        advice.append({
            'kind': 'capacity', 'severity': 'hard',
            'message': f"同档阶梯最多有 {cluster_peak} 个样本需要同时在箱，"
                       f"叠加占用峰值 {occ_peak} 位，超过箱容量 {cap}",
            'suggestion': {'type': 'capacity',
                           'title': f'把箱容量调到 {need}',
                           'delta': need - cap, 'value': need}})
    elif occ_peak >= cap:
        advice.append({
            'kind': 'capacity', 'severity': 'hard',
            'message': f"已有占用峰值 {occ_peak} 位已占满箱容量 {cap}，"
                       f"样本无任何并行时间窗",
            'suggestion': {'type': 'capacity',
                           'title': f'把箱容量调到 {occ_peak + 1}',
                           'delta': 1, 'value': occ_peak + 1}})

    # 占用段时间窗压迫：窗口长度不足以完成必要换档
    for o in occs:
        for s in samples:
            need_before = ramp_minutes(s.eq_rh, o.rh, s.min_rate)
            if abs(s.eq_rh - o.rh) > EPS and need_before > o.start:
                advice.append({
                    'kind': 'occupancy-window', 'severity': 'hard',
                    'message': f"占用段 {fmt_minute(o.start)} 起固定 {o.rh:g}%，"
                               f"样本 {s.code} 从平衡湿度 {s.eq_rh:g}% 换档至少 "
                               f"{need_before} 分钟，但段前窗口只有 {o.start} 分钟",
                    'suggestion': {
                        'type': 'occupancy',
                        'title': f'把占用段「{o.note or o.id}」至少后移 '
                                 f'{need_before - o.start} 分钟',
                        'delta': need_before - o.start, 'value': None}})

    total_mission = sum(s.mission_minutes for s in samples)
    usable = max(1, cap - occ_peak)
    if total_mission > horizon * usable:
        advice.append({
            'kind': 'horizon', 'severity': 'soft',
            'message': f"全部样本任务合计 {total_mission} 分钟，超过 "
                       f"{horizon} 分钟 × {usable} 个可用位的总机时",
            'suggestion': {'type': 'capacity', 'title': '增大箱容量或分批排程',
                           'delta': 1, 'value': cap + 1}})

    # 注：速率类冲突无法通过"调大数值"自动修复（速率是安全上限，放宽需修复师确认
    # 纸张耐受数据），故不输出自动 rate 建议；搜索失败时以占用/容量/互斥建议为准。

    if not advice:
        advice.append({
            'kind': 'combination', 'severity': 'hard',
            'message': '所有装载顺序在容量、区间兼容与换档上限的联合约束下均失败，'
                       '通常是换档时间窗被已有占用段切断',
            'suggestion': {'type': 'occupancy',
                           'title': '后移或移除一段已有占用，再重新编排',
                           'delta': 1, 'value': None}})

    rank = {'shift-limit': 0, 'adjacency': 0, 'occupancy': 1,
            'occupancy-window': 1,
            'range': 2, 'rate': 2, 'capacity': 3, 'horizon': 3,
            'combination': 4, 'occ-overlap': 1}
    advice.sort(key=lambda a: rank.get(a['kind'], 9))
    return advice


def _infeasible(samples: list[_Sample], occs: list[_Occ], cap: int,
                conflicts: list[dict],
                shift_limit: Optional[float] = None) -> dict:
    return {
        'feasible': False,
        'fatal': 'NO_FEASIBLE_SCHEDULE',
        'conflicts': conflicts,
        'minimal_adjustment': conflicts[0]['suggestion'] if conflicts else None,
        'segments': [], 'lanes': [], 'violations': [], 'capacity_series': [],
        'samples': [_sample_json(s) for s in samples],
        'occupancies': [_occ_json(o) for o in occs],
        'capacity': cap, 'max_shift_delta': shift_limit,
        'board_times': {},
    }


# --------------------------------------------------------------------------- #
# 手工排程（拖拽）校验
# --------------------------------------------------------------------------- #
def validate_board_times(samples_raw: list[dict], occs_raw: list[dict],
                         capacity: Any, board_times: dict[int, int],
                         horizon: int = MAX_HORIZON,
                         max_shift_delta: Any = None) -> dict:
    samples = [normalize_sample(d) for d in samples_raw]
    occs = [normalize_occ(d) for d in occs_raw]
    cap = int(_num(capacity, "恒湿箱容量", integer=True, lo=1))
    shift_limit = normalize_max_shift_delta(max_shift_delta)
    ids = {s.id for s in samples}
    bt = {int(k): int(v) for k, v in (board_times or {}).items()
          if int(k) in ids and int(v) >= 0}
    res = _Engine(samples, occs, cap, bt, horizon=horizon, validate=True,
                  max_shift_delta=shift_limit).run()
    res['board_times'] = bt
    res['capacity'] = cap
    res['max_shift_delta'] = shift_limit
    res['samples'] = [_sample_json(s) for s in samples]
    res['occupancies'] = [_occ_json(o) for o in occs]
    res['segments'] = _annotate(res, occs)
    res['feasible'] = res['fatal'] is None and not res['violations']
    res['conflicts'] = []
    return res


def fmt_minute(t: int) -> str:
    """分钟 → 可读时长，如 90 → '1h30m'。"""
    d, r = divmod(int(t), 24 * 60)
    h, m = divmod(r, 60)
    out = ''
    if d:
        out += f'{d}d'
    if h:
        out += f'{h}h'
    if m or not out:
        out += f'{m}m'
    return out


# --------------------------------------------------------------------------- #
# 补做排程（基于已保存复核记录）
# --------------------------------------------------------------------------- #
_UNSET = object()

# 复核结论（此处用字面量，避免 scheduler 反向依赖 review 模块）
_REVIEW = 'review'
_INSUFFICIENT = 'insufficient'


def _reconstruct_rh(segments: list[dict]) -> list[float]:
    """从压缩段还原分钟节点湿度 rh[t]（同 review.plan_series，本地实现保持
    引擎零依赖）。"""
    if not segments:
        return []
    last_end = max(int(s['end']) for s in segments)
    rh: list[Optional[float]] = [None] * last_end
    for seg in segments:
        s, e = int(seg['start']), int(seg['end'])
        if s < 0 or e > last_end or e <= s:
            raise SchedulingError("原排程快照中的计划段区间非法")
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
            raise SchedulingError(f"未知计划段类型：{seg.get('kind')!r}")
    if any(v is None for v in rh):
        raise SchedulingError("原排程快照的计划段在时间轴上不连续")
    return [float(v) for v in rh]  # type: ignore[arg-type]


def _series_from_capacity(capacity_series: list[dict],
                          end_minute: int) -> list[int]:
    """把压缩容量占用序列展开为分钟节点序列，长度与 prefix_rh 对齐。"""
    load = [0] * (end_minute + 1)
    for c in capacity_series or []:
        s, e = int(c['start']), int(c['end'])
        for t in range(s, min(e, end_minute + 1)):
            load[t] = int(c['load'])
    return load


def redo_failed_samples(source_schedule: dict,
                        review_samples: list[dict]) -> list[dict]:
    """从复核结果挑出结论为「复核」或「数据不足」的样本；
    通过样本不补做；只保留原排程中实际存在且有完整定义的样本。"""
    lanes = source_schedule.get('lanes') or []
    defs = {int(s['id']): s for s in (source_schedule.get('samples') or [])
            if s.get('id') is not None}
    lane_order = [int(l['sample_id']) for l in lanes
                  if l.get('sample_id') is not None]
    by_conclusion = {int(s['sample_id']): s for s in review_samples
                     if s.get('sample_id') is not None
                     and s.get('conclusion') in (_REVIEW, _INSUFFICIENT)}
    out: list[dict] = []
    for sid in lane_order:
        if sid in by_conclusion and sid in defs:
            out.append(defs[sid])
    # 原排程 lanes 之外但复核中出现的失败样本（防御性，正常不会发生）
    for sid in by_conclusion:
        if sid not in {x['id'] for x in out} and sid in defs:
            out.append(defs[sid])
    return out


def redo_plan(source_schedule: dict, review_samples: list[dict],
              capacity: Any = None,
              max_shift_delta: Any = _UNSET,
              horizon: int = MAX_HORIZON) -> dict:
    """对一次已保存复核生成补做排程。

    * 只取结论为「复核」/「数据不足」的样本，通过样本不重复安排；
    * 原排程时间轴（0..end_minute，含占用）整体冻结为不可修改的历史；
    * 补做任务只能在历史结束分钟之后入箱，继续满足容量、耐受区间、
      不可相邻、换档速率与独立换档上限；历史结束湿度→补做首次换档
      （衔接处）也要校验独立换档上限差值；
    * 没有失败样本 → feasible=True 的空方案；
    * 无解 → feasible=False，携带冲突与最小调整建议，不产生半成品。
    """
    if not isinstance(source_schedule, dict):
        raise SchedulingError("缺少原排程快照")
    segments = source_schedule.get('segments') or []
    lanes = source_schedule.get('lanes') or []
    if not segments or not lanes:
        raise SchedulingError("原排程快照缺少时间轴（segments/lanes）")
    base = int(source_schedule.get('end_minute'))
    if base < 0:
        raise SchedulingError("原排程结束分钟非法")

    if capacity is None:
        capacity = source_schedule.get('capacity')
    cap = int(_num(capacity, "恒湿箱容量", integer=True, lo=1))
    if max_shift_delta is _UNSET:
        shift_limit = normalize_max_shift_delta(
            source_schedule.get('max_shift_delta'))
    else:
        shift_limit = normalize_max_shift_delta(max_shift_delta)
    occs = [normalize_occ(o) for o in
            (source_schedule.get('occupancies') or [])]

    failed_raw = redo_failed_samples(source_schedule, review_samples or [])
    failed = [normalize_sample(d) for d in failed_raw]
    ids = {s.id for s in failed}
    for s in failed:
        # 互斥只保留失败样本之间的关系（通过样本不在箱中，不构成约束）
        s.excludes = {x for x in s.excludes if x in ids and x != s.id}

    hist_rh = _reconstruct_rh(segments)
    if base + 1 > len(hist_rh):
        hist_rh = hist_rh + [hist_rh[-1]] * (base + 1 - len(hist_rh))
    hist_rh = hist_rh[:base + 1]
    hist_load = _series_from_capacity(
        source_schedule.get('capacity_series') or [], base)
    end_rh = hist_rh[base]

    meta = {
        'kind': 'redo',
        'redo': True,
        'base_minute': base,
        'base_display': fmt_minute(base),
        'end_rh': round(end_rh, 2),
        'failed_samples': [_sample_json(s) for s in failed],
        'passed_samples': [int(l['sample_id']) for l in lanes
                           if l.get('sample_id') is not None
                           and l['sample_id'] not in ids],
        'history': {
            'end_minute': base,
            'end_display': fmt_minute(base),
            'end_rh': round(end_rh, 2),
        },
        'capacity': cap,
        'max_shift_delta': shift_limit,
        'occupancies': [_occ_json(o) for o in occs],
    }

    if not failed:
        history_segments = _annotate(
            {'segments': compress(hist_rh), 'lanes': []}, occs, base,
            extra_lanes=lanes)
        return {**meta,
                'feasible': True, 'empty': True,
                'fatal': None, 'violations': [], 'conflicts': [],
                'minimal_adjustment': None,
                'board_times': {}, 'end_minute': base,
                'segments': history_segments,
                'history_segments': history_segments,
                'redo_segments': [],
                'lanes': [], 'history_lanes': list(lanes),
                'capacity_series': _compress_load(hist_load),
                'samples': [],
                'junction': {'base_minute': base,
                             'from_rh': round(end_rh, 2),
                             'to_rh': round(end_rh, 2),
                             'delta': 0.0, 'within_limit': True,
                             'limit': shift_limit,
                             'label': '无失败样本，无需补做'}}

    # 静态硬冲突（占用超容量 / 占用重叠 / 失败样本间区间不相交且未互斥）
    pre = _static_conflicts(failed, occs, cap)
    # 补做区间落在历史之后：仅统计结束分钟晚于 base 的占用窗口压迫
    for c in _redo_occ_window_conflicts(failed, occs, cap, base):
        if not any(_same_conflict(c, x) for x in pre):
            pre.append(c)
    pre_shift = _static_shift_conflicts(failed, shift_limit)
    if pre or pre_shift:
        return _redo_infeasible(meta, hist_rh, hist_load, lanes,
                                pre + pre_shift, occs)
    # 衔接处：从历史结束湿度到任一失败样本平衡湿度都无法一跳到达
    # （且历史之后没有占用段可作中途停留湿度） → 错峰也无法消除
    junction_conf = _redo_junction_conflict(failed, occs, base, end_rh,
                                            shift_limit)
    if junction_conf:
        return _redo_infeasible(meta, hist_rh, hist_load, lanes,
                                [junction_conf], occs)

    best_bt: Optional[dict[int, int]] = None
    for order in _candidate_orders(failed):
        bt = _try_redo_order(order, occs, cap, horizon, shift_limit,
                             base, hist_rh, hist_load)
        if bt is not None:
            best_bt = bt
            break

    if best_bt is None:
        conflicts = _redo_diagnose(failed, occs, cap, horizon, shift_limit,
                                   base, hist_rh, hist_load, end_rh)
        return _redo_infeasible(meta, hist_rh, hist_load, lanes,
                                conflicts, occs)

    res = _Engine(failed, occs, cap, best_bt, horizon=horizon,
                  max_shift_delta=shift_limit,
                  prefix_rh=hist_rh, prefix_load=hist_load).run()
    if res['fatal'] is not None or res['violations']:
        # 理论上不会发生（候选 bt 已通过完整仿真）；保险起见按无解返回
        conflicts = _redo_diagnose(failed, occs, cap, horizon, shift_limit,
                                   base, hist_rh, hist_load, end_rh)
        return _redo_infeasible(meta, hist_rh, hist_load, lanes,
                                conflicts, occs)

    junction = _redo_junction(res['segments'], base, end_rh, shift_limit)
    if shift_limit is not None and not junction['within_limit']:
        # 搜索本应排除这种 bt；保险起见按无解诊断处理，不保存半成品
        conflicts = _redo_diagnose(failed, occs, cap, horizon, shift_limit,
                                   base, hist_rh, hist_load, end_rh)
        return _redo_infeasible(meta, hist_rh, hist_load, lanes,
                                conflicts, occs)

    all_segments = _annotate(res, occs, base, extra_lanes=lanes)
    history_segments = [s for s in all_segments if s.get('era') == 'history']
    redo_segments = [s for s in all_segments if s.get('era') == 'redo']
    redo_lanes = [_lane_json(l) for l in res['lanes']]
    return {**meta,
            'feasible': True, 'empty': False,
            'fatal': None, 'violations': [], 'conflicts': [],
            'minimal_adjustment': None,
            'board_times': best_bt,
            'end_minute': res['end_minute'],
            'segments': all_segments,
            'history_segments': history_segments,
            'redo_segments': redo_segments,
            'lanes': redo_lanes,
            'history_lanes': list(lanes),
            'capacity_series': res['capacity_series'],
            'samples': [_sample_json(s) for s in failed],
            'junction': junction}


def _same_conflict(a: dict, b: dict) -> bool:
    return a.get('kind') == b.get('kind') and a.get('message') == b.get('message')


def _try_redo_order(order: tuple[_Sample, ...], occs: list[_Occ], cap: int,
                    horizon: int, shift_limit: Optional[float],
                    base: int, hist_rh: list[float],
                    hist_load: list[int]) -> Optional[dict[int, int]]:
    """固定装载顺序，逐样本贪心寻找历史结束分钟之后最早可行的入箱分钟。"""
    board_times: dict[int, int] = {}
    placed: list[_Sample] = []
    for s in order:
        placed_bt = False
        t = base + 1
        tries = 0
        while tries < RETRY_LIMIT and t < horizon:
            trial = dict(board_times)
            trial[s.id] = t
            trial_h = min(horizon,
                          t + 2 * s.mission_minutes + 6 * 60 +
                          sum(max(0, o.end - base) for o in occs if o.end > base)
                          + 120)
            trial_h = max(trial_h,
                          max(board_times.values(), default=base + 1) +
                          2 * s.mission_minutes + 120)
            res = _Engine(placed + [s], occs, cap, trial,
                          horizon=trial_h,
                          max_shift_delta=shift_limit,
                          prefix_rh=hist_rh,
                          prefix_load=hist_load).run()
            if res['fatal'] is None and not res['violations']:
                board_times[s.id] = t
                placed.append(s)
                placed_bt = True
                break
            later = [v['minute'] for v in res['violations']
                     if v['minute'] > base and v['kind'] != 'board']
            jump = min(later, default=None)
            if jump is None and res['fatal']:
                jump = max(t + 1, res['end_minute'])
            t = max(t + 1, jump if jump is not None else t + 1)
            tries += 1
        if not placed_bt:
            return None
    return board_times


def _redo_occ_window_conflicts(samples: list[_Sample], occs: list[_Occ],
                               cap: int, base: int) -> list[dict]:
    """补做可用窗口（base 之后）长度不足以完成必要换档的硬冲突。"""
    conf: list[dict] = []
    later = [o for o in occs if o.end > base]
    if not later:
        return conf
    o = min(later, key=lambda x: x.start)
    window = max(0, o.start - base)
    for s in samples:
        need = ramp_minutes(s.eq_rh, o.rh, s.min_rate)
        if abs(s.eq_rh - o.rh) > EPS and need > window:
            conf.append({
                'kind': 'occupancy-window', 'severity': 'hard',
                'message': f"补做从历史结束（{fmt_minute(base)}）开始，"
                           f"首个占用段 {fmt_minute(o.start)} 起固定 {o.rh:g}%，"
                           f"样本 {s.code} 从平衡湿度 {s.eq_rh:g}% 换档至少 "
                           f"{need} 分钟，但衔接窗口只有 {window} 分钟",
                'suggestion': {
                    'type': 'occupancy',
                    'title': f'把占用段「{o.note or o.id}」至少后移 '
                             f'{need - window} 分钟',
                    'delta': need - window, 'value': None}})
    return conf


def _redo_junction_conflict(samples: list[_Sample], occs: list[_Occ],
                            base: int, end_rh: float,
                            shift_limit: Optional[float]) -> Optional[dict]:
    """历史结束湿度无法在独立换档上限内到达任何一个失败样本的平衡湿度，
    且历史之后没有占用段可提供中途停留湿度 → 衔接处无解。"""
    if shift_limit is None:
        return None
    later_rhs = [o.rh for o in occs if o.start >= base]
    worst = 0.0
    reachable_any = False
    for s in samples:
        d = abs(s.eq_rh - end_rh)
        if d <= shift_limit + EPS:
            reachable_any = True
            continue
        # 存在一个未来占用湿度可作"垫步"：end_rh→occ.rh 与 occ.rh→eq_rh
        # 都不超限，即可以两跳到达
        step = any(abs(o_rh - end_rh) <= shift_limit + EPS and
                   abs(s.eq_rh - o_rh) <= shift_limit + EPS
                   for o_rh in later_rhs)
        if step:
            reachable_any = True
        else:
            worst = max(worst, d)
    if reachable_any or worst <= EPS:
        return None
    need = ceil_tenth(worst)
    return {
        'kind': 'shift-limit', 'severity': 'hard',
        'message': f"补做衔接处：历史结束湿度 {end_rh:g}%RH 到失败样本入箱"
                   f"平衡湿度的最大差值为 {worst:g}%RH，超过独立换档上限 "
                   f"{shift_limit:g}%RH，且历史之后没有占用段可作中途停留，"
                   f"任何入箱时刻都无法衔接",
        'suggestion': {'type': 'shift-limit',
                       'title': f'把独立换档上限放宽到 {need:g}%RH',
                       'delta': round(worst - shift_limit, 2),
                       'value': need}}


def _redo_junction(segments: list[dict], base: int, end_rh: float,
                   shift_limit: Optional[float]) -> dict:
    """衔接处换档差值：取从历史末端湿度出发、进入补做后的第一次换档，
    按整次换档（连续 ramp 合并）两端停留湿度计算差值。"""
    i = 0
    while i < len(segments) and segments[i]['start'] < base:
        i += 1
    j = i
    frm = end_rh
    while j < len(segments) and segments[j]['kind'] != 'ramp':
        # 补做开始后若先有一段新的 hold（预调到中途湿度），衔接换档的
        # 起点仍是历史末端湿度
        j += 1
    if j >= len(segments):
        to = end_rh
    else:
        k = j
        while k + 1 < len(segments) and segments[k + 1]['kind'] == 'ramp' \
                and segments[k + 1]['start'] == segments[k]['end']:
            k += 1
        frm = segments[j].get('from_rh', segments[j]['start_rh'])
        to = segments[k]['end_rh']
    delta = abs(to - frm)
    return {'base_minute': base,
            'from_rh': round(frm, 2), 'to_rh': round(to, 2),
            'delta': round(delta, 2),
            'limit': shift_limit,
            'within_limit': shift_limit is None or delta <= shift_limit + EPS,
            'label': f"衔接处换档 {fmt_minute(base)}：{frm:g}% → {to:g}%，"
                     f"差值 {delta:g}%RH" +
                     ('' if shift_limit is None else
                      f"（上限 {shift_limit:g}%RH"
                      + ('，通过）' if delta <= shift_limit + EPS
                         else '，超限）'))}


def _redo_diagnose(samples: list[_Sample], occs: list[_Occ], cap: int,
                   horizon: int, shift_limit: Optional[float],
                   base: int, hist_rh: list[float], hist_load: list[int],
                   end_rh: float) -> list[dict]:
    """补做无解诊断：先在补做窗口上做全样本同时入箱探针，抓"错峰也无法
    消除"的独立换档超限（含衔接处）；再叠加通用容量/占用/机时诊断。"""
    advice: list[dict] = []
    if shift_limit is not None and samples:
        bt = {s.id: base + 1 for s in samples}
        probe = _Engine(samples, occs, cap, bt, horizon=horizon,
                        validate=True, max_shift_delta=shift_limit,
                        prefix_rh=hist_rh, prefix_load=hist_load).run()
        shift_v = [v for v in probe['violations']
                   if v['kind'] == 'shift-limit']
        if shift_v:
            worst = max(v['delta'] for v in shift_v)
            need = ceil_tenth(worst)
            advice.append({
                'kind': 'shift-limit', 'severity': 'hard',
                'message': f"补做时间轴上有 {len(shift_v)} 处相邻段目标差值"
                           f"超过独立换档上限 {shift_limit:g}%RH"
                           f"（最大 {worst:g}%RH），错峰装载也无法避开："
                           f"{shift_v[0]['message']}",
                'suggestion': {'type': 'shift-limit',
                               'title': f'把独立换档上限放宽到 {need:g}%RH',
                               'delta': round(worst - shift_limit, 2),
                               'value': need}})

    generic = [a for a in _diagnose(samples, occs, cap, horizon, shift_limit)
               if a['kind'] != 'shift-limit']
    for a in generic:
        if not any(_same_conflict(a, x) for x in advice):
            advice.append(a)
    return advice or [{
        'kind': 'combination', 'severity': 'hard',
        'message': '补做样本在容量、区间兼容、换档上限与历史衔接的联合约束下'
                   '均无法安排在历史结束分钟之后',
        'suggestion': {'type': 'occupancy',
                       'title': '后移或移除历史之后的已有占用，再重新生成补做',
                       'delta': 1, 'value': None}}]


def _compress_load(load: list[int]) -> list[dict]:
    out: list[dict] = []
    for t, v in enumerate(load):
        if out and out[-1]['load'] == v and out[-1]['end'] == t:
            out[-1]['end'] = t + 1
        else:
            out.append({'start': t, 'end': t + 1, 'load': v})
    while len(out) > 1 and out[-1]['load'] == 0:
        out.pop()
    return out


def _lane_json(lane: dict) -> dict:
    return {'sample_id': lane['sample_id'], 'code': lane['code'],
            'rh_min': lane['rh_min'], 'rh_max': lane['rh_max'],
            'eq_rh': lane['eq_rh'], 'board': lane['board'],
            'unboard': lane['unboard'], 'pieces': lane['pieces']}


def _redo_infeasible(meta: dict, hist_rh: list[float], hist_load: list[int],
                     history_lanes: list[dict], conflicts: list[dict],
                     occs: list[_Occ]) -> dict:
    history_segments = _annotate(
        {'segments': compress(hist_rh), 'lanes': []}, occs,
        len(hist_rh) - 1, extra_lanes=history_lanes)
    return {**meta,
            'feasible': False, 'empty': False,
            'fatal': 'NO_FEASIBLE_REDO',
            'violations': [], 'conflicts': conflicts,
            'minimal_adjustment': conflicts[0]['suggestion']
            if conflicts else None,
            'board_times': {}, 'end_minute': len(hist_rh) - 1,
            'segments': history_segments,
            'history_segments': history_segments,
            'redo_segments': [],
            'lanes': [], 'history_lanes': list(history_lanes),
            'capacity_series': _compress_load(hist_load),
            'samples': meta['failed_samples'],
            'junction': None}

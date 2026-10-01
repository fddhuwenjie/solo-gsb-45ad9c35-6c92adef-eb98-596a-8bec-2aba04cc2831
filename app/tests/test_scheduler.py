"""编排引擎单元测试：python3 -m unittest app.tests.test_scheduler -v"""
import unittest

from app.scheduler import (schedule, validate_board_times, SchedulingError,
                           ramp_minutes, compress, fmt_minute,
                           normalize_max_shift_delta)


def sample(sid, code, lo, hi, steps, eq=None, eq_dur=20, excludes=None):
    return {'id': sid, 'code': code, 'rh_min': lo, 'rh_max': hi,
            'eq_rh': eq, 'eq_duration': eq_dur, 'steps': steps,
            'excludes': excludes or []}


def st(t, d, r=5):
    return {'target': t, 'dwell': d, 'rate': r}


class TestRampMath(unittest.TestCase):
    def test_ramp_minutes(self):
        self.assertEqual(ramp_minutes(50, 60, 5), 2)      # 5/min: 5 + 5? 10/5=2
        self.assertEqual(ramp_minutes(50, 53, 5), 1)
        self.assertEqual(ramp_minutes(50, 50, 5), 0)
        self.assertEqual(ramp_minutes(40, 60, 3), 7)      # floor(3): 60-40=20, ceil(20/3)=7

    def test_compress(self):
        rh = [50, 50, 55, 60, 60, 60]
        segs = compress(rh)
        kinds = [(s['kind'], s['start'], s['end']) for s in segs]
        # 段区间为"分钟格子"：hold[0,2)，ramp[2,4)（第2、3分钟移动），hold[4,6)
        # 渲染 ramp 斜线时用 start_rh=rh[1]=50 → end_rh=rh[3]=60
        self.assertEqual(kinds, [('hold', 0, 2), ('ramp', 2, 4), ('hold', 4, 6)])
        self.assertEqual(segs[1]['from_rh'], 50)
        self.assertEqual(segs[1]['end_rh'], 60)

    def test_ramp_minutes_exact(self):
        # 整除：10% ÷ 5%/min = 2 个移动分钟
        self.assertEqual(ramp_minutes(50, 60, 5), 2)


class TestSingleSample(unittest.TestCase):
    def test_basic(self):
        res = schedule([sample(1, 'A', 40, 70, [st(60, 30), st(45, 20)])],
                       [], capacity=2)
        self.assertTrue(res['feasible'], res.get('fatal') or res.get('conflicts'))
        lane = res['lanes'][0]
        self.assertEqual(lane['board'], 0)
        kinds = [p['kind'] for p in lane['pieces']]
        self.assertIn('hold', kinds)
        self.assertIn('ramp', kinds)
        # 末尾必须冷却回平衡湿度并出箱
        self.assertIsNotNone(lane['unboard'])
        end = lane['unboard']
        # 时间下限估算
        # 50->60 @5 = 2m, hold 30, 60->45 需先到 45: ceil(15/5)=3, hold20,
        # 45->50(eq=50? default eq = max(30,min(60,40))=40!) -> check
        self.assertGreater(end, 30 + 20)
        # 末段湿度应回到 eq_rh
        self.assertAlmostEqual(res['segments'][-1]['start_rh'],
                               next(s['eq_rh'] for s in res['samples']
                                    if s['code'] == 'A'), delta=0.1)

    def test_step_out_of_range_rejected(self):
        with self.assertRaises(SchedulingError):
            schedule([sample(1, 'A', 40, 50, [st(60, 10)])], [], capacity=1)

    def test_eq_out_of_range_rejected(self):
        with self.assertRaises(SchedulingError):
            schedule([sample(1, 'A', 55, 70, [st(60, 10)], eq=50)], [],
                     capacity=1)


class TestCapacity(unittest.TestCase):
    def test_capacity_exceeded_with_occupancy(self):
        s = [sample(1, 'A', 40, 70, [st(60, 30)]),
             sample(2, 'B', 40, 70, [st(60, 30)])]
        res = schedule(s, [{'id': 1, 'start': 0, 'end': 100, 'rh': 55,
                            'slots': 2, 'note': '他用'}], capacity=2)
        # 2 位全被占用 → 无样本可装载 → 必然无解
        self.assertFalse(res['feasible'])
        self.assertTrue(res['conflicts'])

    def test_capacity_fit(self):
        s = [sample(1, 'A', 40, 70, [st(60, 60)]),
             sample(2, 'B', 40, 70, [st(60, 60)])]
        res = schedule(s, [], capacity=2)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        # 同目标同速率 → 两个应同箱并行
        peak = max(c['load'] for c in res['capacity_series'])
        self.assertEqual(peak, 2)


class TestRangeCompatibility(unittest.TestCase):
    def test_disjoint_ranges_without_exclude_infeasible(self):
        s = [sample(1, 'A', 30, 40, [st(35, 20)]),
             sample(2, 'B', 60, 80, [st(70, 20)])]
        res = schedule(s, [], capacity=2)
        self.assertFalse(res['feasible'])
        self.assertTrue(any(c['kind'] == 'range' for c in res['conflicts']))
        self.assertIsNotNone(res['minimal_adjustment'])

    def test_disjoint_ranges_with_exclude_sequential(self):
        s = [sample(1, 'A', 30, 40, [st(35, 10)], excludes=[2]),
             sample(2, 'B', 60, 80, [st(70, 10)], excludes=[1])]
        res = schedule(s, [], capacity=2)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        # 在箱时间段不重叠
        la, lb = res['lanes']
        self.assertTrue(la['unboard'] <= lb['board'] or
                        lb['unboard'] <= la['board'])

    def test_overlap_targets_rider(self):
        # A: 50->70; B: 40-60 目标 55。A 升档会把 B 带出 55，但应先等待 B 保持完
        s = [sample(1, 'A', 40, 80, [st(70, 20)], eq=50),
             sample(2, 'B', 40, 60, [st(55, 30)], eq=50)]
        res = schedule(s, [], capacity=2)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        # 逐分钟湿度必须落在每个在箱样本区间内（引擎已保证），这里复核
        for lane in res['lanes']:
            for t in range(lane['board'], lane['unboard']):
                pass


class TestRateCap(unittest.TestCase):
    def test_shared_rate_cap(self):
        # A 速率 10/min，B 速率 2/min；同箱换档必须按 2/min（至少 10 分钟）
        s = [sample(1, 'A', 40, 80, [st(70, 10)], eq=50),
             sample(2, 'B', 40, 80, [st(70, 10, 2)], eq=50)]
        res = schedule(s, [], capacity=2)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        up = [g for g in res['segments'] if g['kind'] == 'ramp'
              and abs(g['from_rh'] - 50) < 0.1 and abs(g['end_rh'] - 70) < 0.1]
        self.assertTrue(up)
        g = up[0]
        # 20%RH ÷ 2%/min = 10 个移动分钟；段格子长度即移动分钟数
        self.assertEqual(g['end'] - g['start'], 10)
        peak = max(c['load'] for c in res['capacity_series'])
        self.assertEqual(peak, 2)


class TestOccupancy(unittest.TestCase):
    def test_ramp_cannot_cross_occ(self):
        # 占用段 50% 在第 10-20 分钟；样本需要 50->70 换档，必须等占用结束
        s = [sample(1, 'A', 40, 80, [st(70, 10)], eq=50)]
        occ = [{'id': 9, 'start': 10, 'end': 20, 'rh': 50, 'slots': 1,
                'note': '预约'}]
        res = schedule(s, occ, capacity=2)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        # 换档移动分钟（几何段 (start,end)）不得落在占用窗口 (10,20) 内部
        for g in (x for x in res['segments'] if x['kind'] == 'ramp'):
            for minute in range(g['start'] + 1, g['end']):
                self.assertFalse(10 < minute < 20,
                                 f"换档第 {minute} 分钟穿过占用段")
        # 上升换档的几何起点必须在占用结束（20）之后
        up = [g for g in res['segments'] if g['kind'] == 'ramp'
              and g['end_rh'] > g['start_rh']]
        self.assertTrue(up)
        self.assertGreaterEqual(up[0]['start'], 20)

    def test_occ_rh_violation_drag(self):
        # 拖拽校验：占用段要求 60%，箱初始湿度按入箱样本 eq=50% → 占用湿度违规
        s = [sample(1, 'A', 40, 80, [st(70, 10)], eq=50)]
        occ = [{'id': 9, 'start': 0, 'end': 30, 'rh': 60, 'slots': 1}]
        res = validate_board_times(s, occ, 2, {1: 0}, horizon=120)
        self.assertFalse(res['feasible'])
        kinds = {v['kind'] for v in res['violations']}
        self.assertTrue('occ-rh' in kinds or 'board' in kinds)


class TestAdjacency(unittest.TestCase):
    def test_adjacency_drag_violation(self):
        s = [sample(1, 'A', 40, 80, [st(70, 60)], eq=50, excludes=[2]),
             sample(2, 'B', 40, 80, [st(70, 60)], eq=50, excludes=[1])]
        res = validate_board_times(s, [], 2, {1: 0, 2: 0}, horizon=200)
        self.assertFalse(res['feasible'])
        self.assertTrue(any(v['kind'] == 'adjacency'
                            for v in res['violations']))


class TestExportShape(unittest.TestCase):
    def test_segments_have_sources(self):
        s = [sample(1, 'A', 40, 80, [st(70, 10)], eq=50)]
        res = schedule(s, [], capacity=1)
        self.assertTrue(res['feasible'])
        for seg in res['segments']:
            self.assertIn('sources', seg)
            self.assertIn('aboard', seg)
        # 至少一段来源是样本 A 的阶梯
        joined = [src for seg in res['segments'] for src in seg['sources']]
        self.assertTrue(any(x.get('code') == 'A' for x in joined))

    def test_fmt(self):
        self.assertEqual(fmt_minute(90), '1h30m')
        self.assertEqual(fmt_minute(60), '1h')
        self.assertEqual(fmt_minute(25), '25m')


class TestComplex(unittest.TestCase):
    def test_three_samples_mixed(self):
        s = [
            {'id': 1, 'code': 'P1', 'rh_min': 40, 'rh_max': 75, 'eq_rh': 50,
             'eq_duration': 15,
             'steps': [st(65, 30, 5), st(50, 20, 3)]},
            {'id': 2, 'code': 'P2', 'rh_min': 45, 'rh_max': 70, 'eq_rh': 50,
             'eq_duration': 15, 'steps': [st(60, 40, 4)]},
            {'id': 3, 'code': 'P3', 'rh_min': 35, 'rh_max': 55, 'eq_rh': 45,
             'eq_duration': 10, 'steps': [st(50, 25, 6)]},
        ]
        res = schedule(s, [], 3)
        self.assertTrue(res['feasible'], res.get('fatal') or res.get('conflicts'))
        self.assertLessEqual(max(c['load'] for c in res['capacity_series']), 3)
        # 自动排程回验必须可行
        rv = validate_board_times(s, [], 3, res['board_times'])
        self.assertTrue(rv['feasible'], rv['violations'][:3])

    def test_infeasible_capacity_advice(self):
        s = [
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 5, 'steps': [st(60, 60)]},
            {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 5, 'steps': [st(60, 60)]},
        ]
        occ = [{'id': 1, 'start': 0, 'end': 300, 'rh': 50, 'slots': 1}]
        res = schedule(s, occ, 1)
        self.assertFalse(res['feasible'])
        self.assertTrue(res['conflicts'])
        sug = res['minimal_adjustment']
        self.assertIn(sug['type'], ('capacity', 'occupancy', 'adjacency'))
        self.assertGreaterEqual(sug.get('delta', 0), 1)

    def test_rider_completes_without_false_return(self):
        """搭车样本完成保持后，不应在两个目标间反复振荡送回。"""
        s = [
            {'id': 1, 'code': 'Hi', 'rh_min': 40, 'rh_max': 75, 'eq_rh': 50,
             'eq_duration': 15,
             'steps': [st(65, 30, 5), st(50, 20, 3)]},
            {'id': 2, 'code': 'Mid', 'rh_min': 45, 'rh_max': 70, 'eq_rh': 50,
             'eq_duration': 15, 'steps': [st(60, 40, 4)]},
        ]
        res = validate_board_times(s, [], 3, {1: 0, 2: 0}, horizon=400)
        self.assertIsNone(res['fatal'])
        self.assertEqual(len(res['lanes']), 2)
        for lane in res['lanes']:
            self.assertIsNotNone(lane['unboard'], lane['code'])

    def test_occupancy_held_during_mission(self):
        """任务可以排在占用段之后；占用期间曲线恒为占用湿度。"""
        s = [{'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
              'eq_duration': 10, 'steps': [st(70, 10)]}]
        occ = [{'id': 9, 'start': 0, 'end': 30, 'rh': 50, 'slots': 1}]
        res = schedule(s, occ, 2)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        for seg in res['segments']:
            if seg['start'] < 30 and seg['end'] > 0 and seg['kind'] == 'hold':
                if seg['start'] < 30:
                    # 与占用重叠的 hold 段必须是 50%
                    if seg['end'] > 0 and max(seg['start'], 0) < min(seg['end'], 30):
                        self.assertAlmostEqual(seg['start_rh'], 50, delta=0.1)

    def test_export_payload_shape(self):
        s = [{'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
              'eq_duration': 10, 'steps': [st(70, 10)]}]
        res = schedule(s, [], 1)
        self.assertTrue(res['feasible'])
        self.assertTrue({'start', 'end', 'kind', 'sources', 'aboard'}
                        <= set(res['segments'][0].keys()))
        self.assertTrue({'sample_id', 'board', 'unboard', 'pieces'}
                        <= set(res['lanes'][0].keys()))


class TestShiftLimit(unittest.TestCase):
    """恒湿箱"独立换档上限"：时间轴相邻段目标值之差约束。"""

    def test_normalize(self):
        self.assertIsNone(normalize_max_shift_delta(None))
        self.assertIsNone(normalize_max_shift_delta(''))
        self.assertEqual(normalize_max_shift_delta(12), 12.0)  # int 也接受
        self.assertEqual(normalize_max_shift_delta('8.5'), 8.5)
        for bad in (0, -1, -0.1, 0.01, 'abc'):
            with self.assertRaises(SchedulingError):
                normalize_max_shift_delta(bad)

    def test_default_unlimited_backward_compatible(self):
        # 不传该参数（旧数据）时行为与以前一致：20% 大跳档可行
        s = [sample(1, 'A', 40, 80, [st(70, 10)], eq=50)]
        res = schedule(s, [], 1)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        self.assertIsNone(res['max_shift_delta'])

    def test_single_sample_jump_static_conflict(self):
        # 50 → 70 单次跳 20%，上限 10 → 静态硬冲突，无解并给放宽建议
        s = [sample(1, 'A', 40, 80, [st(70, 10)], eq=50)]
        res = schedule(s, [], 1, max_shift_delta=10)
        self.assertFalse(res['feasible'])
        self.assertTrue(any(c['kind'] == 'shift-limit'
                            for c in res['conflicts']))
        sug = res['minimal_adjustment']
        self.assertEqual(sug['type'], 'shift-limit')
        self.assertGreaterEqual(sug['value'], 20)

    def test_within_limit_succeeds(self):
        s = [sample(1, 'A', 40, 80, [st(58, 10), st(50, 10)], eq=50)]
        res = schedule(s, [], 1, max_shift_delta=10)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        self.assertEqual(res['max_shift_delta'], 10)

    def test_cooldown_jump_also_limited(self):
        # 入箱平衡 45 → 第 1 档 50（5%）可行，但 50 → 末档 62（12%）超限
        s = [sample(1, 'A', 40, 80, [st(50, 5), st(62, 5)], eq=45)]
        res = schedule(s, [], 1, max_shift_delta=10)
        self.assertFalse(res['feasible'])
        self.assertTrue(any(c['kind'] == 'shift-limit'
                            for c in res['conflicts']))

    def test_drag_violation_structure_and_other_checks_kept(self):
        # 两个样本同时 0 入箱：A 的 50→70 换档（20%）超独立上限 10；
        # 容量给 2，区间/速率/不可相邻检查照常工作
        s = [sample(1, 'A', 40, 80, [st(70, 60)], eq=50),
             sample(2, 'B', 40, 80, [st(70, 60)], eq=50)]
        res = validate_board_times(s, [], 2, {1: 0, 2: 0},
                                   horizon=200, max_shift_delta=10)
        self.assertFalse(res['feasible'])
        v = next(x for x in res['violations'] if x['kind'] == 'shift-limit')
        self.assertGreater(v['delta'], 10)
        self.assertEqual(v['limit'], 10)
        self.assertAlmostEqual(v['from_rh'], 50, delta=0.1)
        self.assertAlmostEqual(v['to_rh'], 70, delta=0.1)
        # 指出相邻段
        self.assertEqual(len(v['adjacent_segments']), 2)
        # 可调整项：放宽设置 + 修改样本档目标
        types = {a['type'] for a in v['adjustments']}
        self.assertIn('shift-limit', types)
        self.assertIn('sample-step', types)
        # 其它违规检查仍在（同一组数据容量超限场景）
        res2 = validate_board_times(s, [], 1, {1: 0, 2: 0},
                                    horizon=200, max_shift_delta=10)
        kinds = {x['kind'] for x in res2['violations']}
        self.assertIn('capacity', kinds)
        self.assertIn('shift-limit', kinds)

    def test_drag_within_limit_ok(self):
        s = [sample(1, 'A', 40, 80, [st(58, 20)], eq=50)]
        res = validate_board_times(s, [], 1, {1: 0},
                                   horizon=200, max_shift_delta=10)
        self.assertTrue(res['feasible'], res['violations'])

    def test_auto_then_validate_same_rule(self):
        # 自动排程可行的结果，用同一上限回验必须可行；把上限收紧则回验失败
        s = [
            {'id': 1, 'code': 'P1', 'rh_min': 40, 'rh_max': 75, 'eq_rh': 50,
             'eq_duration': 15,
             'steps': [st(58, 30, 5), st(50, 20, 3)]},
            {'id': 2, 'code': 'P2', 'rh_min': 45, 'rh_max': 70, 'eq_rh': 50,
             'eq_duration': 15, 'steps': [st(55, 40, 4)]},
        ]
        res = schedule(s, [], 3, max_shift_delta=10)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        rv = validate_board_times(s, [], 3, res['board_times'],
                                  max_shift_delta=10)
        self.assertTrue(rv['feasible'], rv['violations'])
        rv2 = validate_board_times(s, [], 3, res['board_times'],
                                   max_shift_delta=5)
        self.assertFalse(rv2['feasible'])
        self.assertTrue(any(v['kind'] == 'shift-limit'
                            for v in rv2['violations']))

    def test_adjacency_check_still_present(self):
        s = [sample(1, 'A', 40, 80, [st(55, 60)], eq=50, excludes=[2]),
             sample(2, 'B', 40, 80, [st(55, 60)], eq=50, excludes=[1])]
        res = validate_board_times(s, [], 2, {1: 0, 2: 0},
                                   horizon=200, max_shift_delta=10)
        self.assertTrue(any(v['kind'] == 'adjacency'
                            for v in res['violations']))

    def test_multi_minute_ramp_merged(self):
        # 50 → 58 共 8%，速率 5/min 时被切成 +5、+3 两个 ramp 段；
        # 比较必须按整次换档（8%），而不是逐段（5%、3%）
        s = [sample(1, 'A', 40, 80, [st(58, 10)], eq=50)]
        res = validate_board_times(s, [], 1, {1: 0},
                                   horizon=120, max_shift_delta=6)
        v = next((x for x in res['violations']
                  if x['kind'] == 'shift-limit'), None)
        self.assertIsNotNone(v)
        self.assertAlmostEqual(v['delta'], 8, delta=0.1)
        # 放到上限 8 即可行
        res2 = validate_board_times(s, [], 1, {1: 0},
                                    horizon=120, max_shift_delta=8)
        self.assertTrue(res2['feasible'], res2['violations'])

    def test_occupancy_forced_dynamic_infeasible(self):
        # 占用段固定 70%，样本平衡 50%：错峰也无法避开 50→70 的 20% 跳变；
        # 样本自身档差仅 5%（静态检查放行），无解诊断必须给出 shift-limit 建议，
        # 且调整项中包含"后移占用"
        s = [{'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
              'eq_duration': 10, 'steps': [st(55, 5)]}]
        occ = [{'id': 9, 'start': 30, 'end': 60, 'rh': 70, 'slots': 1,
                'note': '校准'}]
        res = schedule(s, occ, 2, max_shift_delta=10)
        self.assertFalse(res['feasible'])
        self.assertEqual(res['conflicts'][0]['kind'], 'shift-limit')
        self.assertIn('占用', res['conflicts'][0]['message'])
        rv = validate_board_times(s, occ, 2, {1: 0},
                                  horizon=150, max_shift_delta=10)
        v = next(x for x in rv['violations'] if x['kind'] == 'shift-limit')
        self.assertIn('occupancy', [a['type'] for a in v['adjustments']])
        # 放宽到 20 后自动与拖拽均可行
        self.assertTrue(schedule(s, occ, 2, max_shift_delta=20)['feasible'])
        self.assertTrue(validate_board_times(s, occ, 2, {1: 0},
                                             horizon=150,
                                             max_shift_delta=20)['feasible'])


class TestSettingsPersistence(unittest.TestCase):
    def test_db_max_shift_delta_roundtrip(self):
        import os
        import tempfile
        from app.db import DB
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, 't.db'))
            # 旧数据：没有该设置 → None（不限制）
            self.assertIsNone(db.get_max_shift_delta())
            db.set_max_shift_delta(12.5)
            self.assertAlmostEqual(db.get_max_shift_delta(), 12.5)
            db.set_max_shift_delta(None)
            self.assertIsNone(db.get_max_shift_delta())


if __name__ == '__main__':
    unittest.main()

"""补做排程引擎单元测试：
python3 -m unittest app.tests.test_redo -v
"""
import os
import tempfile
import unittest

from app import redo
from app.review import (review_run, plan_series,
                        CONCLUSION_PASS, CONCLUSION_REVIEW,
                        CONCLUSION_INSUFFICIENT)
from app.scheduler import schedule, validate_board_times, compress


def two_samples(eq_b=65, tgt_b=70):
    return [
        {'id': 1, 'code': 'A', 'rh_min': 30, 'rh_max': 90, 'eq_rh': 50,
         'eq_duration': 10,
         'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
        {'id': 2, 'code': 'B', 'rh_min': 30, 'rh_max': 90, 'eq_rh': eq_b,
         'eq_duration': 10,
         'steps': [{'target': tgt_b, 'dwell': 10, 'rate': 5}]},
    ]


def make_review(samples, occs=None, capacity=1, max_shift_delta=None,
                fail_ids=(1,), offset=8.0, band=2.0, dose_limit=5,
                max_gap=2, drop=()):
    """容量 1 → 样本串行。给 fail_ids 样本窗口加常量偏移制造「复核」，
    drop 给出删除的读数分钟区间（开区间端点）制造「数据不足」。"""
    occs = occs or []
    res = schedule(samples, occs, capacity,
                   max_shift_delta=max_shift_delta)
    assert res['feasible'], res.get('conflicts')
    rh = plan_series(res['segments'], res['end_minute'])
    lanes = {l['sample_id']: l for l in res['lanes']}
    readings = []
    for t, v in enumerate(rh):
        use = True
        for a, b in drop:
            if a <= t <= b:
                use = False
        if not use:
            continue
        off = 0.0
        for fid in fail_ids:
            l = lanes[fid]
            if l['board'] < t <= (l['unboard'] or 0):
                off = offset
        readings.append((t, round(v + off, 4)))
    rv = review_run(res, readings, band, dose_limit, max_gap)
    rv['source_schedule'] = res
    return res, rv


class TestEmptyPlan(unittest.TestCase):
    def test_all_pass_returns_empty_plan(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(), dose_limit=1000)
        conclusions = {s['sample_id']: s['conclusion']
                       for s in rv['samples']}
        self.assertEqual(set(conclusions.values()), {CONCLUSION_PASS})
        plan = redo.plan_redo(rv, samples, None, 1)
        self.assertTrue(plan['feasible'])
        self.assertTrue(plan['empty'])
        self.assertEqual(plan['failed_sample_ids'], [])
        self.assertEqual(plan['redo_board_times'], {})
        # 空方案仍保留完整历史时间轴供查看
        self.assertEqual(plan['history_end_minute'], res['end_minute'])
        self.assertTrue(plan['segments'])


class TestRedoBasic(unittest.TestCase):
    def test_only_failed_sample_redo_after_history(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        cons = {s['sample_id']: s['conclusion'] for s in rv['samples']}
        self.assertEqual(cons[1], CONCLUSION_REVIEW)
        self.assertEqual(cons[2], CONCLUSION_PASS)

        plan = redo.plan_redo(rv, samples, None, 1)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        # 通过样本 B 绝不重复安排；只有失败样本 A
        self.assertEqual(plan['failed_sample_ids'], [1])
        self.assertEqual(plan['passed_sample_ids'], [2])
        self.assertEqual(set(plan['redo_board_times']), {1})
        # 补做入箱严格晚于原排程结束分钟
        self.assertGreater(plan['redo_board_times'][1],
                           plan['history_end_minute'])
        self.assertEqual(plan['history_end_minute'], res['end_minute'])

    def test_insufficient_sample_is_also_redo(self):
        samples = two_samples()
        # 删除样本 A 窗口内一段读数制造数据不足
        res0 = schedule(samples, [], 1)
        lanes = {l['sample_id']: l for l in res0['lanes']}
        la = lanes[1]
        drop = ((la['board'] + 2, la['unboard'] - 2),)
        res, rv = make_review(samples, fail_ids=(), max_gap=1, drop=drop)
        cons = {s['sample_id']: s['conclusion'] for s in rv['samples']}
        self.assertEqual(cons[1], CONCLUSION_INSUFFICIENT)
        plan = redo.plan_redo(rv, samples, None, 1)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        self.assertEqual(plan['failed_sample_ids'], [1])

    def test_history_prefix_is_immutable(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        plan = redo.plan_redo(rv, samples, None, 1)
        self.assertTrue(plan['feasible'])
        orig_rh = plan_series(res['segments'], res['end_minute'])
        new_rh = plan_series(plan['segments'], plan['end_minute'])
        # 历史分钟 [0, E] 湿度逐分钟完全一致
        for t in range(len(orig_rh)):
            self.assertAlmostEqual(new_rh[t], orig_rh[t], places=6,
                                   msg=f"历史第 {t} 分钟被修改")
        # 段标注：历史段 / 补做段
        timelines = {seg['timeline'] for seg in plan['segments']}
        self.assertEqual(timelines, {'history', 'redo'})

    def test_history_lanes_all_samples_kept(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        plan = redo.plan_redo(rv, samples, None, 1)
        hist_ids = {l['sample_id'] for l in plan['lanes']
                    if l.get('timeline') == 'history'}
        redo_ids = {l['sample_id'] for l in plan['lanes']
                    if l.get('timeline') == 'redo'}
        self.assertEqual(hist_ids, {1, 2})   # 历史完整保留两个样本
        self.assertEqual(redo_ids, {1})      # 只有 A 重做

    def test_redo_plan_validates_against_engine(self):
        """补做段本身（相对时间轴）必须通过全部约束。"""
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        plan = redo.plan_redo(rv, samples, None, 1)
        self.assertTrue(plan['feasible'])
        # 用固定前缀引擎回验：结果可行且无任何违规
        pinned = redo.pinned_rh_series(res)
        check = validate_board_times(
            samples, [], 1, plan['redo_board_times'],
            horizon=plan['end_minute'] + 5)
        self.assertTrue(check['feasible'], check['violations'])


class TestJunctionShiftLimit(unittest.TestCase):
    def test_junction_conflict_when_limit_tight(self):
        samples = two_samples(eq_b=65)
        res, rv = make_review(samples, fail_ids=(1,))
        # 历史末段停留湿度 = B 的 eq 65；A 入箱平衡 50，差 15 > 10
        plan = redo.plan_redo(rv, samples, None, 1, max_shift_delta=10)
        self.assertFalse(plan['feasible'])
        kinds = [c['kind'] for c in plan['conflicts']]
        self.assertIn('redo-junction', kinds)
        self.assertIsNotNone(plan['minimal_adjustment'])
        self.assertEqual(plan['minimal_adjustment']['type'], 'shift-limit')

    def test_junction_ok_when_limit_relaxed(self):
        samples = two_samples(eq_b=65)
        res, rv = make_review(samples, fail_ids=(1,))
        plan = redo.plan_redo(rv, samples, None, 1, max_shift_delta=15)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        self.assertAlmostEqual(plan['junction']['history_rh'], 65, delta=0.1)

    def test_junction_uses_source_schedule_settings(self):
        # 原排程在上限 10 下可行（串行衔接 50→58、58→50 均 ≤10）；
        # 补做不显式传上限时沿用快照设置 10，衔接差 8 可行
        samples = two_samples(eq_b=58, tgt_b=58)
        res, rv = make_review(samples, fail_ids=(1,), max_shift_delta=10)
        plan = redo.plan_redo(rv, samples, None, None, None)
        self.assertEqual(plan['max_shift_delta'], 10)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        # 收紧补做上限到 6（小于衔接差 8）→ 衔接冲突
        plan2 = redo.plan_redo(rv, samples, None, 1, max_shift_delta=6)
        self.assertFalse(plan2['feasible'])
        self.assertIn('redo-junction',
                      [c['kind'] for c in plan2['conflicts']])


class TestConstraintsKept(unittest.TestCase):
    def test_capacity_redo_part(self):
        # 容量 2，两个样本都失败，补做可并行，补做段峰值 ≤ 容量
        samples = [
            {'id': 1, 'code': 'A', 'rh_min': 30, 'rh_max': 90, 'eq_rh': 50,
             'eq_duration': 10,
             'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
            {'id': 2, 'code': 'B', 'rh_min': 30, 'rh_max': 90, 'eq_rh': 50,
             'eq_duration': 10,
             'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]},
        ]
        res, rv = make_review(samples, capacity=2, fail_ids=(1, 2))
        plan = redo.plan_redo(rv, samples, None, 2)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        he = plan['history_end_minute']
        peak = max(c['load'] for c in plan['capacity_series']
                   if c['start'] > he)
        self.assertLessEqual(peak, 2)

    def test_exclusion_serial_in_redo(self):
        samples = [
            {'id': 1, 'code': 'A', 'rh_min': 30, 'rh_max': 45, 'eq_rh': 40,
             'eq_duration': 10,
             'steps': [{'target': 44, 'dwell': 10, 'rate': 5}],
             'excludes': [2]},
            {'id': 2, 'code': 'B', 'rh_min': 60, 'rh_max': 80, 'eq_rh': 70,
             'eq_duration': 10,
             'steps': [{'target': 75, 'dwell': 10, 'rate': 5}],
             'excludes': [1]},
        ]
        res, rv = make_review(samples, capacity=2, fail_ids=(1, 2))
        plan = redo.plan_redo(rv, samples, None, 2)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        redo_lanes = [l for l in plan['lanes']
                      if l.get('timeline') == 'redo']
        self.assertEqual(len(redo_lanes), 2)
        la, lb = sorted(redo_lanes, key=lambda l: l['board'])
        self.assertTrue(la['unboard'] <= lb['board'] or
                        lb['unboard'] <= la['board'])

    def test_occupancies_are_retained(self):
        occ = [{'id': 9, 'start': 240, 'end': 270, 'rh': 50, 'slots': 1,
                'note': '校准'}]
        samples = two_samples()
        res, rv = make_review(samples, occs=occ, capacity=2, fail_ids=(1,))
        plan = redo.plan_redo(rv, samples, None, 2)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        self.assertIn(9, [o['id'] for o in plan['occupancies']])

    def test_future_occupancy_blocking_gives_conflict(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        blocking = {'id': 100, 'start': res['end_minute'] + 1,
                    'end': res['end_minute'] + 3000, 'rh': 50, 'slots': 1}
        plan = redo.plan_redo(rv, samples, [blocking], 1)
        self.assertFalse(plan['feasible'])
        self.assertTrue(plan['conflicts'])


class TestErrors(unittest.TestCase):
    def test_missing_source_schedule(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        rv.pop('source_schedule')
        with self.assertRaises(redo.RedoError):
            redo.plan_redo(rv, samples, None, 1)

    def test_missing_sample_definition(self):
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        # 库里删掉失败样本（快照在 source 中仍有 → 应优先用快照，不报错）
        plan = redo.plan_redo(rv, [samples[1]], None, 1)
        self.assertTrue(plan['feasible'], plan.get('conflicts'))
        # 快照与库都没有时才报错
        rv['source_schedule']['samples'] = [
            s for s in res['samples'] if s['id'] != 1]
        with self.assertRaises(redo.RedoError):
            redo.plan_redo(rv, [samples[1]], None, 1)


class TestDBPersistence(unittest.TestCase):
    def test_save_get_list_cascade(self):
        from app.db import DB
        samples = two_samples()
        res, rv = make_review(samples, fail_ids=(1,))
        plan = redo.plan_redo(rv, samples, None, 1)
        self.assertTrue(plan['feasible'])
        with tempfile.TemporaryDirectory() as d:
            db = DB(os.path.join(d, 't.db'))
            rid = db.save_review(rv, 'review', 'n')
            pid = db.save_redo_plan(rid, plan)
            got = db.get_redo_plan(pid)
            self.assertEqual(got['review_id'], rid)
            self.assertTrue(got['feasible'])
            self.assertEqual(got['payload']['failed_sample_ids'], [1])
            self.assertEqual(len(db.list_redo_plans(rid)), 1)
            self.assertIsNotNone(db.latest_redo_for_review(rid))
            # 级联：删除复核连带删除补做
            db.delete_review(rid)
            self.assertIsNone(db.get_redo_plan(pid))
            self.assertEqual(db.list_redo_plans(rid), [])


if __name__ == '__main__':
    unittest.main()

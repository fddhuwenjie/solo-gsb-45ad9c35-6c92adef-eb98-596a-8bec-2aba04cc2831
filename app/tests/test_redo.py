"""补做排程引擎单元测试：
python3 -m unittest app.tests.test_redo -v
"""
import unittest

from app.scheduler import schedule, redo_plan, redo_failed_samples
from app.review import plan_series


def sA(sid=1, eq=50, target=60, dwell=20, rate=5, eq_dur=10,
       lo=40, hi=80, code='A', excludes=None):
    return {'id': sid, 'code': code, 'rh_min': lo, 'rh_max': hi,
            'eq_rh': eq, 'eq_duration': eq_dur,
            'steps': [{'target': target, 'dwell': dwell, 'rate': rate}],
            'excludes': excludes or []}


def conclusions(mapping):
    return [{'sample_id': sid, 'conclusion': c} for sid, c in mapping.items()]


class TestRedoSelection(unittest.TestCase):
    def setUp(self):
        self.res = schedule([sA(1, code='A'), sA(2, code='B')], [], 2)
        self.assertTrue(self.res['feasible'])

    def test_pick_only_failed(self):
        failed = redo_failed_samples(self.res, conclusions(
            {1: 'pass', 2: 'review'}))
        self.assertEqual([s['id'] for s in failed], [2])
        failed = redo_failed_samples(self.res, conclusions(
            {1: 'insufficient', 2: 'review'}))
        self.assertEqual(sorted(s['id'] for s in failed), [1, 2])

    def test_all_pass_empty(self):
        r = redo_plan(self.res, conclusions({1: 'pass', 2: 'pass'}))
        self.assertTrue(r['feasible'])
        self.assertTrue(r['empty'])
        self.assertEqual(r['lanes'], [])
        self.assertEqual(r['board_times'], {})
        self.assertEqual(r['end_minute'], self.res['end_minute'])
        self.assertTrue(r['junction']['within_limit'])

    def test_passed_not_replanned(self):
        r = redo_plan(self.res, conclusions({1: 'pass', 2: 'review'}))
        self.assertTrue(r['feasible'])
        ids = {l['sample_id'] for l in r['lanes']}
        self.assertEqual(ids, {2})
        self.assertEqual(r['passed_samples'], [1])


class TestRedoSchedule(unittest.TestCase):
    def test_redos_start_after_history_end(self):
        res = schedule([sA(1), sA(2)], [], 2)
        base = res['end_minute']
        r = redo_plan(res, conclusions({1: 'review', 2: 'review'}))
        self.assertTrue(r['feasible'], r.get('conflicts'))
        self.assertFalse(r['empty'])
        for lane in r['lanes']:
            self.assertGreater(lane['board'], base)
            self.assertGreater(lane['unboard'], base)
        self.assertTrue(all(bt > base for bt in r['board_times'].values()))
        self.assertGreater(r['end_minute'], base)

    def test_history_frozen_unchanged(self):
        res = schedule([sA(1, dwell=30), sA(2, target=65, dwell=20)], [], 2)
        base = res['end_minute']
        hist_rh = plan_series(res['segments'], base)
        r = redo_plan(res, conclusions({1: 'review'}))
        self.assertTrue(r['feasible'])
        new_rh = plan_series(r['history_segments'], base)
        self.assertEqual(len(hist_rh), len(new_rh))
        for a, b in zip(hist_rh, new_rh):
            self.assertAlmostEqual(a, b, places=6)
        # 历史泳道原样保留
        self.assertEqual([l['sample_id'] for l in r['history_lanes']],
                         [l['sample_id'] for l in res['lanes']])
        # 历史段与补做段以 base 为界（base 节点在两段各出现一次）
        self.assertEqual(max(s['end'] for s in r['history_segments']), base + 1)
        self.assertEqual(min(s['start'] for s in r['redo_segments']),
                         base + 1)

    def test_junction_shift_delta_computed(self):
        res = schedule([sA(1, eq=50, target=60)], [], 1)
        r = redo_plan(res, conclusions({1: 'review'}))
        self.assertTrue(r['feasible'])
        j = r['junction']
        # 历史结束湿度 50 → 补做首档 60，差值 10
        self.assertAlmostEqual(j['delta'], 10.0, places=4)
        self.assertEqual(j['base_minute'], res['end_minute'])
        self.assertTrue(j['within_limit'])  # 未启用限制

    def test_junction_violates_shift_limit_infeasible(self):
        # A eq50 长任务最后结束（end_rh=50）；D eq60 自身换档 60<->65 合限 8；
        # 借助早期占用 rh58 原排程可行；历史之后无垫步 → 衔接处 50→60=10>8
        s = [
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 60,
             'steps': [{'target': 55, 'dwell': 200, 'rate': 5}]},
            {'id': 4, 'code': 'D', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 60,
             'eq_duration': 10,
             'steps': [{'target': 65, 'dwell': 20, 'rate': 5}]},
        ]
        occs = [{'id': 9, 'start': 60, 'end': 90, 'rh': 58, 'slots': 1}]
        res = schedule(s, occs, 2, max_shift_delta=8)
        self.assertTrue(res['feasible'], res.get('conflicts'))
        r = redo_plan(res, conclusions({1: 'pass', 4: 'review'}),
                      max_shift_delta=8)
        self.assertFalse(r['feasible'])
        self.assertEqual(r['fatal'], 'NO_FEASIBLE_REDO')
        self.assertTrue(any(c['kind'] == 'shift-limit'
                            for c in r['conflicts']))
        # 无解返回不携带任何补做半成品泳道
        self.assertEqual(r['lanes'], [])
        self.assertEqual(r['redo_segments'], [])
        self.assertIsNone(r['junction'])

    def test_junction_via_stepping_occupancy_feasible(self):
        s = [
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 60,
             'steps': [{'target': 55, 'dwell': 200, 'rate': 5}]},
            {'id': 4, 'code': 'D', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 60,
             'eq_duration': 10,
             'steps': [{'target': 65, 'dwell': 20, 'rate': 5}]},
        ]
        occs = [{'id': 9, 'start': 60, 'end': 90, 'rh': 58, 'slots': 1}]
        res = schedule(s, occs, 2, max_shift_delta=8)
        base = res['end_minute']
        # 历史之后追加 rh54 垫步占用：50→54→60 均 ≤8
        occs2 = occs + [{'id': 10, 'start': base + 40,
                         'end': base + 70, 'rh': 54, 'slots': 1}]
        res2 = schedule(s, occs2, 2, max_shift_delta=8)
        self.assertTrue(res2['feasible'])
        r = redo_plan(res2, conclusions({1: 'pass', 4: 'review'}),
                      max_shift_delta=8)
        self.assertTrue(r['feasible'], r.get('conflicts'))
        self.assertTrue(r['junction']['within_limit'])

    def test_capacity_respected_in_redo(self):
        # 容量 1：两个失败样本的补做必须串行
        res = schedule([sA(1, code='A'), sA(2, code='B')], [], 1)
        r = redo_plan(res, conclusions({1: 'review', 2: 'review'}))
        self.assertTrue(r['feasible'], r.get('conflicts'))
        load_peak = max(c['load'] for c in r['capacity_series']
                        if c['start'] > res['end_minute'])
        self.assertEqual(load_peak, 1)

    def test_occupancies_kept_after_base(self):
        occs = [{'id': 7, 'start': 400, 'end': 440, 'rh': 50, 'slots': 1,
                 'note': '夜间占用'}]
        res = schedule([sA(1), sA(2)], occs, 2)
        self.assertTrue(res['feasible'])
        r = redo_plan(res, conclusions({1: 'review', 2: 'review'}))
        self.assertTrue(r['feasible'], r.get('conflicts'))
        # 补做必须等占用结束之后；占用窗口内只有占用负荷
        for c in r['capacity_series']:
            if c['start'] >= 400 and c['end'] <= 440:
                self.assertEqual(c['load'], 1)
        for lane in r['lanes']:
            self.assertTrue(lane['board'] >= 440 or lane['unboard'] <= 400)

    def test_exclusion_respected_in_redo(self):
        s = [sA(1, code='A', excludes=[2]),
             sA(2, code='B', excludes=[1])]
        res = schedule(s, [], 2)
        self.assertTrue(res['feasible'])
        r = redo_plan(res, conclusions({1: 'review', 2: 'review'}))
        self.assertTrue(r['feasible'], r.get('conflicts'))
        lanes = sorted(r['lanes'], key=lambda l: l['board'])
        # 互斥 → 在箱窗口不得重叠
        for a, b in zip(lanes, lanes[1:]):
            self.assertLessEqual(a['unboard'], b['board'])

    def test_redo_full_mission_again(self):
        # 失败样本必须重走完整程序（含入箱平衡与冷却），不只是失败档
        res = schedule([sA(1, eq_dur=15)], [], 1)
        r = redo_plan(res, conclusions({1: 'review'}))
        self.assertTrue(r['feasible'])
        kinds = [p['kind'] for p in r['lanes'][0]['pieces']]
        self.assertIn('board', kinds)
        self.assertTrue(any(k.startswith('cooldown') for k in kinds))

    def test_passed_sample_not_included(self):
        # 通过样本不参与补做：redo 的 samples 只含失败样本
        res = schedule([sA(1), sA(2)], [], 2)
        r = redo_plan(res, conclusions({1: 'pass', 2: 'insufficient'}))
        self.assertEqual([x['id'] for x in r['samples']], [2])
        self.assertEqual(r['failed_samples'][0]['id'], 2)

    def test_history_internal_shift_not_revalidated(self):
        # 历史（无换档限制排的）含 50→70（差 20）；补做样本 B 只需 50↔60
        # （差 10）。用 15 的限制重做：历史内部的 20 不得重新判定，
        # 只要补做链条与衔接处（10）合限即可行
        s = [
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 30,
             'steps': [{'target': 70, 'dwell': 30, 'rate': 5}]},
            {'id': 2, 'code': 'B', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 10,
             'steps': [{'target': 60, 'dwell': 20, 'rate': 5}]},
        ]
        res = schedule(s, [], 2)  # 无独立换档上限
        self.assertTrue(res['feasible'])
        rv = conclusions({1: 'pass', 2: 'review'})
        r = redo_plan(res, rv, max_shift_delta=15)
        self.assertTrue(r['feasible'], r.get('conflicts'))
        self.assertTrue(r['junction']['within_limit'])
        # 限制收紧到 8：补做链条自身 50↔60=10 即超限 → 仍然无解
        r2 = redo_plan(res, rv, max_shift_delta=8)
        self.assertFalse(r2['feasible'])

    def test_invalid_source_raises(self):
        from app.scheduler import SchedulingError
        with self.assertRaises(SchedulingError):
            redo_plan({}, [{'sample_id': 1, 'conclusion': 'review'}])
        with self.assertRaises(SchedulingError):
            redo_plan({'segments': [], 'lanes': []}, [])


if __name__ == '__main__':
    unittest.main()

"""实测湿度复核引擎单元测试：
python3 -m unittest app.tests.test_review -v
"""
import unittest

from app.scheduler import schedule, _Engine, normalize_sample
from app import review
from app.review import (parse_readings, plan_series, review_run,
                        normalize_parameters, ReviewError,
                        CONCLUSION_PASS, CONCLUSION_REVIEW,
                        CONCLUSION_INSUFFICIENT)


def simple_schedule():
    s = [{'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
          'eq_duration': 10,
          'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]}]
    return schedule(s, [], 1)


class TestParseReadings(unittest.TestCase):
    def test_bare_values_from_zero(self):
        self.assertEqual(parse_readings("50\n50.5\n51"),
                         [(0, 50.0), (1, 50.5), (2, 51.0)])

    def test_bare_values_with_start(self):
        self.assertEqual(parse_readings("50\n51", start_minute=30),
                         [(30, 50.0), (31, 51.0)])

    def test_two_columns(self):
        text = "time,rh\n0 50\n1, 50.5\n2；51"
        self.assertEqual(parse_readings(text),
                         [(0, 50.0), (1, 50.5), (2, 51.0)])

    def test_comments_blank_lines(self):
        self.assertEqual(parse_readings("# c\n\n// c2\n42\n43"),
                         [(0, 42.0), (1, 43.0)])

    def test_mixed_columns_rejected(self):
        with self.assertRaises(ReviewError):
            parse_readings("0 50\n51")

    def test_non_increasing_rejected(self):
        with self.assertRaises(ReviewError):
            parse_readings("5 50\n4 50\n6 50")
        with self.assertRaises(ReviewError):
            parse_readings("5 50\n5 51")

    def test_empty(self):
        with self.assertRaises(ReviewError):
            parse_readings("")
        with self.assertRaises(ReviewError):
            parse_readings("min rh\n")

    def test_rh_range(self):
        with self.assertRaises(ReviewError):
            parse_readings("120")
        with self.assertRaises(ReviewError):
            parse_readings("-1")

    def test_bad_fractional_minute(self):
        with self.assertRaises(ReviewError):
            parse_readings("0.5 50\n1 50")

    def test_normalize_parameters(self):
        self.assertEqual(normalize_parameters(2, 10, 3), (2.0, 10.0, 3))
        with self.assertRaises(ReviewError):
            normalize_parameters(-1, 10, 3)
        with self.assertRaises(ReviewError):
            normalize_parameters(2, -1, 3)
        with self.assertRaises(ReviewError):
            normalize_parameters(2, 10, 0)
        with self.assertRaises(ReviewError):
            normalize_parameters('x', 10, 3)


class TestPlanReconstruction(unittest.TestCase):
    def test_matches_engine_rh_series(self):
        res = simple_schedule()
        recon = plan_series(res['segments'], res['end_minute'])
        samples = [normalize_sample(
            {'id': 1, 'code': 'A', 'rh_min': 40, 'rh_max': 80, 'eq_rh': 50,
             'eq_duration': 10,
             'steps': [{'target': 60, 'dwell': 10, 'rate': 5}]})]
        eng = _Engine(samples, [], 1, res['board_times'],
                      horizon=res['end_minute'] + 1)
        eng.run()
        self.assertEqual(len(recon), len(eng.rh_series))
        for a, b in zip(recon, eng.rh_series):
            self.assertAlmostEqual(a, b, places=6)


def perfect_readings(res):
    rh = plan_series(res['segments'], res['end_minute'])
    return [(t, round(v, 4)) for t, v in enumerate(rh)]


class TestConclusions(unittest.TestCase):
    def test_perfect_data_passes(self):
        res = simple_schedule()
        out = review_run(res, perfect_readings(res), 2, 10, 2)
        sm = out['samples'][0]
        self.assertEqual(sm['conclusion'], CONCLUSION_PASS)
        self.assertTrue(sm['data_complete'])
        self.assertEqual(sm['dose'], 0)
        self.assertEqual(sm['excess_intervals'], [])

    def test_within_band_passes(self):
        res = simple_schedule()
        rh = plan_series(res['segments'], res['end_minute'])
        # 全程偏高 1.5%，未超允许带 2%
        readings = [(t, round(v + 1.5, 4)) for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 10, 2)
        self.assertEqual(out['samples'][0]['conclusion'], CONCLUSION_PASS)
        self.assertEqual(out['samples'][0]['dose'], 0)

    def test_constant_offset_exact_area(self):
        res = simple_schedule()
        rh = plan_series(res['segments'], res['end_minute'])
        # 读数节点 t=10..20 恒定高 4%（带 2%）。两条曲线在整分钟之间均线性：
        # 入带斜坡（第 10 分钟格）面积 1；t=11..20 中：
        #   保持 [11,12) 1 格整宽 2；第1档冷却换档 [12,13) 整宽 2；
        #   冷却保持 [13,20) 7 格整宽 14；
        # 加上 t=20 节点之后回零斜坡在第 20 格 [20,21) 的三角形面积 1，
        # 合计 1+2+2+14+1 = 21（%RH·分钟）。
        readings = [(t, round(v + (4.0 if 10 <= t <= 20 else 0.0), 4))
                    for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 100, 2)
        sm = out['samples'][0]
        self.assertAlmostEqual(sm['dose'], 21, places=5)
        ivs = sm['excess_intervals']
        self.assertTrue(ivs)
        self.assertAlmostEqual(sum(x['dose'] for x in ivs), 21, places=5)
        # 剂量未超 100 → 通过；收紧到 10 → 复核
        self.assertEqual(sm['conclusion'], CONCLUSION_PASS)
        out2 = review_run(res, readings, 2, 10, 2)
        self.assertEqual(out2['samples'][0]['conclusion'],
                         CONCLUSION_REVIEW)

    def test_step_window_area(self):
        # 整段窗口（含出箱端）偏移：在 [board+1, unboard] 节点上恒定 +4，
        # 入箱斜坡三角形（面积 1）之外的全部在箱时间都应超带
        res = simple_schedule()
        lane = res['lanes'][0]
        rh = plan_series(res['segments'], res['end_minute'])
        readings = [(t, round(v + (4.0 if lane['board'] < t <= lane['unboard']
                                   else 0.0), 4))
                    for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 1000, 2)
        sm = out['samples'][0]
        # 换档分钟格内计划本身在爬坡：入箱首格 [0,1) 的偏差由 0 斜升到 4，
        # 超带三角形（高 2、底 0.5）面积 0.5；其余在箱分钟（窗口长 −1）整宽超带
        expect = 2 * (lane['unboard'] - lane['board'] - 1) + 0.5
        self.assertAlmostEqual(sm['dose'], expect, places=5)

    def test_ramp_triangle_exact_area(self):
        # 偏差只在第 6 分钟读数节点跳到 +3（计划该格恒定 60%），两侧节点
        # 偏差为 0：第 [5,6) 格与 [6,7) 格各有超带三角形，
        # 高 1、底 1/3，面积 1/6，合计 1/3
        res = simple_schedule()
        rh = plan_series(res['segments'], res['end_minute'])
        readings = [(t, round(v + (3.0 if t == 6 else 0.0), 4))
                    for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 10, 2)
        sm = out['samples'][0]
        self.assertAlmostEqual(sm['dose'], 1 / 3, places=3)

    def test_negative_side_recorded(self):
        res = simple_schedule()
        rh = plan_series(res['segments'], res['end_minute'])
        readings = [(t, round(v - 4.0, 4)) if t < 15 else (t, round(v, 4))
                    for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 100, 2)
        ivs = out['samples'][0]['excess_intervals']
        self.assertTrue(any(x['side'] == '-' for x in ivs))

    def test_gap_marks_insufficient_and_no_interpolation(self):
        res = simple_schedule()
        # 删掉第 12..18 分钟读数：剩余读数节点为 11 与 19，
        # 间隔 8 分钟 > 上限 3；缺口 [11,19) 内所有分钟格不插值
        readings = [(t, v) for t, v in perfect_readings(res)
                    if not (12 <= t <= 18)]
        out = review_run(res, readings, 2, 100, max_gap=3)
        sm = out['samples'][0]
        self.assertEqual(sm['conclusion'], CONCLUSION_INSUFFICIENT)
        self.assertFalse(sm['data_complete'])
        gap_unc = [u for u in sm['uncovered'] if u['reason'] == 'gap']
        self.assertEqual(len(gap_unc), 1)
        self.assertEqual((gap_unc[0]['start'], gap_unc[0]['end']), (11, 19))
        # 缺口内不产生剂量
        self.assertEqual(sm['dose'], 0)
        self.assertEqual(out['gaps'],
                         [{'start': 11, 'end': 19, 'interval': 8}])

    def test_gap_equal_limit_interpolates(self):
        # 间隔 == max_gap 不属于缺口，照常线性插值
        res = simple_schedule()
        readings = [(t, v) for t, v in perfect_readings(res) if t != 8]
        out = review_run(res, readings, 2, 100, max_gap=2)
        sm = out['samples'][0]
        self.assertTrue(sm['data_complete'])
        self.assertEqual(sm['conclusion'], CONCLUSION_PASS)

    def test_first_reading_after_board(self):
        res = simple_schedule()
        readings = perfect_readings(res)[3:]
        out = review_run(res, readings, 2, 100, 2)
        sm = out['samples'][0]
        self.assertEqual(sm['conclusion'], CONCLUSION_INSUFFICIENT)
        self.assertTrue(any(u['reason'] == 'before-first'
                            for u in sm['uncovered']))

    def test_last_reading_before_unboard(self):
        res = simple_schedule()
        lane = res['lanes'][0]
        readings = [(t, v) for t, v in perfect_readings(res)
                    if t < lane['unboard'] - 4]
        out = review_run(res, readings, 2, 100, 2)
        sm = out['samples'][0]
        self.assertEqual(sm['conclusion'], CONCLUSION_INSUFFICIENT)
        self.assertTrue(any(u['reason'] == 'after-last'
                            for u in sm['uncovered']))

    def test_window_only_uses_aboard_time(self):
        # 出箱之后的恒定偏差不计入该样本剂量：把跳变放在出箱后的第一个
        # 读数节点上，使 [unboard-1, unboard) 窗口末端斜坡保持在计划上
        res = simple_schedule()
        lane = res['lanes'][0]
        rh = plan_series(res['segments'], res['end_minute'])
        readings = [(t, round(v + (10.0 if t > lane['unboard'] else 0.0), 4))
                    for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 100, 2)
        self.assertEqual(out['samples'][0]['dose'], 0)

    def test_intervals_split_by_phase(self):
        # 全程持续超限，超带区间必须按计划阶段切成多段
        res = simple_schedule()
        rh = plan_series(res['segments'], res['end_minute'])
        readings = [(t, round(v + 5.0, 4)) for t, v in enumerate(rh)]
        out = review_run(res, readings, 2, 100000, 2)
        phases = {x['phase'] for x in out['samples'][0]['excess_intervals']}
        self.assertGreater(len(phases), 1)

    def test_sparse_readings_linear_interpolation(self):
        # 每 2 分钟一次读数（间隔恰为上限，允许插值）。读数节点偏差
        # d(6)=1.6、d(8)=3.2，其余节点偏差为 0。引擎必须按读数节点线性
        # 插值，而不是按分钟格子矩形求和。
        # 保持段（计划恒定 60%，t=3..11）内：
        #   [4,6] d 0→1.6 全程在带内（0）；
        #   [6,8] d 1.6→3.2 超带三角形 高1.2、底1.5 → 面积 0.9；
        #   [8,10] d 3.2→0，超带三角形高1.2、底0.75 → 面积 0.45。
        # 之后计划进入冷却换档，插值仍带着残余偏差回到 0，沿斜线再产生
        # 0.2 的微小超带（保持末格 0.1 + 冷却两侧各 0.05），合计 1.55。
        res = simple_schedule()
        end = res['end_minute']
        ub = res['lanes'][0]['unboard']
        rh = plan_series(res['segments'], end)
        offsets = {6: 1.6, 8: 3.2}
        readings = [(t, round(rh[min(t, end)] + offsets.get(t, 0.0), 4))
                    for t in range(0, ub + 2, 2)]
        out = review_run(res, readings, 2, 100, max_gap=2)
        sm = out['samples'][0]
        self.assertTrue(sm['data_complete'])
        self.assertAlmostEqual(sm['dose'], 1.55, places=3)
        # 保持段内的主体超带区间须按解析三角形给出 1.35
        hold_dose = sum(x['dose'] for x in sm['excess_intervals']
                        if x['phase'] == '第1档保持')
        self.assertAlmostEqual(hold_dose, 1.35 + 0.1, places=3)
        self.assertEqual(sm['conclusion'], CONCLUSION_PASS)

    def test_overall_summary(self):
        res = simple_schedule()
        out = review_run(res, perfect_readings(res), 2, 10, 2)
        self.assertEqual(out['summary'],
                         {'pass': 1, 'review': 0, 'insufficient': 0,
                          'total': 1})


class TestFmt(unittest.TestCase):
    def test_fmt_minute_frac(self):
        self.assertEqual(review.fmt_minute_frac(90), '1h30m')
        self.assertEqual(review.fmt_minute_frac(90.5), '1h30.5m')
        self.assertEqual(review.fmt_minute_frac(0), '0m')
        self.assertEqual(review.fmt_minute_frac(1440), '1d')


if __name__ == '__main__':
    unittest.main()

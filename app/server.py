"""Flask 服务：REST API + 静态单页前端。

启动：python -m app.server  （容器内由 waitress 在 8000 端口提供服务）
"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, request, send_from_directory

from . import scheduler
from . import review as review_mod
from . import redo as redo_mod
from .db import DB
from .seed import seed_if_empty

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('HUMIDITY_DB', os.path.join(BASE, 'data', 'humidity.db'))

app = Flask(__name__, static_folder=os.path.join(BASE, 'static'),
            static_url_path='/static')
db = DB(DB_PATH)
seed_if_empty(db)


# --------------------------------------------------------------------------- #
def _sample_payload(d: dict) -> dict:
    return {
        'id': d['id'], 'code': d['code'], 'rh_min': d['rh_min'],
        'rh_max': d['rh_max'], 'eq_rh': d['eq_rh'],
        'eq_duration': d['eq_duration'],
        'steps': [{'target': s['target'], 'dwell': s['dwell'],
                   'rate': s['rate']} for s in d['steps']],
        'excludes': d['excludes'],
    }


def _all_samples() -> list[dict]:
    return [_sample_payload(d) for d in db.list_samples()]


def _all_occ() -> list[dict]:
    return db.list_occ()


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


# --------------------------------------------------------------------------- #
# 样本
# --------------------------------------------------------------------------- #
@app.get('/api/samples')
def list_samples():
    return jsonify(_all_samples())


@app.post('/api/samples')
def save_sample():
    data = request.get_json(force=True, silent=True) or {}
    try:
        # 先经引擎归一化校验
        norm = scheduler.normalize_sample(data)
        payload = {
            'id': data.get('id'), 'code': norm.code, 'rh_min': norm.rh_min,
            'rh_max': norm.rh_max, 'eq_rh': norm.eq_rh,
            'eq_duration': norm.eq_duration,
            'steps': norm.steps,
            'excludes': sorted(int(x) for x in (data.get('excludes') or [])),
        }
        sid = db.upsert_sample(payload)
    except scheduler.SchedulingError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(_sample_payload(db.get_sample(sid))), 200


@app.delete('/api/samples/<int:sid>')
def remove_sample(sid: int):
    db.delete_sample(sid)
    return jsonify({'ok': True})


# --------------------------------------------------------------------------- #
# 占用与设置
# --------------------------------------------------------------------------- #
@app.get('/api/occupancies')
def list_occ():
    return jsonify(_all_occ())


@app.post('/api/occupancies')
def add_occ():
    data = request.get_json(force=True, silent=True) or {}
    try:
        norm = scheduler.normalize_occ(data)
        oid = db.add_occ({'start': norm.start, 'end': norm.end, 'rh': norm.rh,
                          'slots': norm.slots, 'note': norm.note})
    except scheduler.SchedulingError as e:
        return jsonify({'error': str(e)}), 400
    created = next(o for o in db.list_occ() if o['id'] == oid)
    return jsonify(created), 201


@app.delete('/api/occupancies/<int:oid>')
def remove_occ(oid: int):
    db.delete_occ(oid)
    return jsonify({'ok': True})


@app.get('/api/settings')
def get_settings():
    return jsonify({'capacity': db.get_capacity(),
                    'max_shift_delta': db.get_max_shift_delta()})


@app.post('/api/settings')
def set_settings():
    data = request.get_json(force=True, silent=True) or {}
    out = {}
    if 'capacity' in data:
        try:
            cap = int(float(data.get('capacity')))
            if cap < 1:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({'error': '容量必须为 ≥1 的整数'}), 400
        db.set_capacity(cap)
        out['capacity'] = cap
    # max_shift_delta：缺省 → 保持原值；null/空串 → 清除（不限制，兼容旧数据）
    if 'max_shift_delta' in data:
        raw = data.get('max_shift_delta')
        try:
            shift_limit = scheduler.normalize_max_shift_delta(
                None if raw in ('', ) else raw)
        except scheduler.SchedulingError as e:
            return jsonify({'error': str(e)}), 400
        db.set_max_shift_delta(shift_limit)
        out['max_shift_delta'] = shift_limit
    if not out:
        out = {'capacity': db.get_capacity(),
               'max_shift_delta': db.get_max_shift_delta()}
    else:
        out.setdefault('capacity', db.get_capacity())
        out.setdefault('max_shift_delta', db.get_max_shift_delta())
    return jsonify(out)


# --------------------------------------------------------------------------- #
# 排程与校验
# --------------------------------------------------------------------------- #
@app.post('/api/schedule')
def run_schedule():
    body = request.get_json(force=True, silent=True) or {}
    samples = body.get('samples') or _all_samples()
    occs = body.get('occupancies')
    if occs is None:
        occs = _all_occ()
    capacity = body['capacity'] if 'capacity' in body else db.get_capacity()
    shift_limit = (body['max_shift_delta'] if 'max_shift_delta' in body
                   else db.get_max_shift_delta())
    try:
        res = scheduler.schedule(samples, occs, capacity,
                                 max_shift_delta=shift_limit)
    except scheduler.SchedulingError as e:
        return jsonify({'error': str(e)}), 400
    db.save_schedule(res)
    return jsonify(res)


@app.post('/api/validate')
def validate():
    """拖拽后校验：body = {board_times: {sample_id: minute}, ...可选完整数据}"""
    body = request.get_json(force=True, silent=True) or {}
    samples = body.get('samples') or _all_samples()
    occs = body.get('occupancies')
    if occs is None:
        occs = _all_occ()
    capacity = body['capacity'] if 'capacity' in body else db.get_capacity()
    shift_limit = (body['max_shift_delta'] if 'max_shift_delta' in body
                   else db.get_max_shift_delta())
    board_times = body.get('board_times') or {}
    try:
        res = scheduler.validate_board_times(
            samples, occs, capacity, board_times,
            max_shift_delta=shift_limit)
    except scheduler.SchedulingError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(res)


@app.get('/api/schedule/latest')
def latest():
    s = db.latest_schedule()
    if s is None:
        return jsonify({})
    return jsonify(s)


# --------------------------------------------------------------------------- #
# 实测湿度复核
# --------------------------------------------------------------------------- #
def _coerce_readings(body: dict):
    """body 可给 readings_text（粘贴文本 + start_minute）或 readings=[[t,v],…]。"""
    if body.get('readings') is not None:
        raw = body.get('readings')
        if not isinstance(raw, list):
            raise review_mod.ReviewError("readings 必须是 [[分钟,湿度],…] 数组")
        out = []
        for i, item in enumerate(raw):
            try:
                t, v = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                raise review_mod.ReviewError(f"第 {i + 1} 条读数格式非法")
            if abs(t - round(t)) > 1e-9 or t < 0:
                raise review_mod.ReviewError(f"第 {i + 1} 条读数分钟必须为 ≥0 的整数")
            out.append((int(round(t)), v))
        for (t0, _), (t1, _) in zip(out, out[1:]):
            if t1 <= t0:
                raise review_mod.ReviewError("读数时间必须按分钟严格递增")
        return out
    start = body.get('start_minute', 0)
    try:
        start = int(float(start))
    except (TypeError, ValueError):
        raise review_mod.ReviewError("起始分钟必须为整数")
    return review_mod.parse_readings(body.get('readings_text', ''), start)


def _overall_conclusion(res: dict) -> str:
    s = res['summary']
    if s['insufficient'] > 0:
        return review_mod.CONCLUSION_INSUFFICIENT
    if s['review'] > 0:
        return review_mod.CONCLUSION_REVIEW
    return review_mod.CONCLUSION_PASS


@app.post('/api/review')
def run_review():
    """对最近排程（或 body.schedule 覆盖）执行实测湿度复核。

    body: {readings_text, start_minute?, readings?, tolerance, dose_limit,
           max_gap, save?, note?}
    """
    body = request.get_json(force=True, silent=True) or {}
    schedule_obj = body.get('schedule')
    if not schedule_obj:
        schedule_obj = db.latest_schedule()
    if not schedule_obj:
        return jsonify({'error': '尚无排程快照：请先在编排台生成排程'}), 400
    if not schedule_obj.get('feasible'):
        return jsonify({'error': '最近一次排程不可行，无法复核；请先生成可行排程'}), 400
    try:
        readings = _coerce_readings(body)
        band, limit, max_gap = review_mod.normalize_parameters(
            body.get('tolerance'), body.get('dose_limit'),
            body.get('max_gap'))
        res = review_mod.review_run(schedule_obj, readings, band, limit, max_gap)
    except review_mod.ReviewError as e:
        return jsonify({'error': str(e)}), 400
    overall = _overall_conclusion(res)
    res['overall_conclusion'] = overall
    res['overall_conclusion_label'] = \
        review_mod.CONCLUSION_LABELS[overall]
    # 原排程完整快照内嵌进复核记录：补做排程据此保留不可修改历史
    res['source_schedule'] = schedule_obj
    if body.get('save'):
        note = str(body.get('note') or '')
        rid = db.save_review(res, overall, note)
        res['saved'] = True
        res['review_id'] = rid
        created = db.get_review(rid)
        res['created_at'] = created['created_at'] if created else None
    else:
        res['saved'] = False
    return jsonify(res)


@app.get('/api/reviews')
def list_reviews():
    try:
        limit = max(1, min(200, int(request.args.get('limit', 50))))
    except ValueError:
        limit = 50
    items = db.list_reviews(limit)
    # 附带每条复核最近一次补做排程的 id（空方案也保存，便于回看）
    for it in items:
        latest_redo = db.latest_redo_for_review(it['id'])
        it['redo_id'] = latest_redo['id'] if latest_redo else None
    return jsonify(items)


@app.get('/api/reviews/<int:rid>')
def get_review(rid: int):
    rec = db.get_review(rid)
    if rec is None:
        return jsonify({'error': '复核记录不存在'}), 404
    return jsonify(rec)


@app.delete('/api/reviews/<int:rid>')
def remove_review(rid: int):
    db.delete_review(rid)
    return jsonify({'ok': True})


# --------------------------------------------------------------------------- #
# 补做排程
# --------------------------------------------------------------------------- #
@app.post('/api/reviews/<int:rid>/redo')
def run_redo(rid):
    """基于已保存复核记录生成补做排程。

    只安排结论为「复核」或「数据不足」的样本；原排程作为不可修改历史保留。
    无失败样本 → 保存并返回空方案；补做无解 → 返回冲突且不保存半成品。
    body 可携带 capacity / max_shift_delta / samples / occupancies 临时覆盖。
    """
    rec = db.get_review(rid)
    if rec is None:
        return jsonify({'error': '复核记录不存在'}), 404
    body = request.get_json(force=True, silent=True) or {}
    review_payload = rec['payload']
    samples = body.get('samples') or _all_samples()
    occs = body.get('occupancies')
    capacity = body['capacity'] if 'capacity' in body else None
    shift_limit = body['max_shift_delta'] if 'max_shift_delta' in body \
        else None
    try:
        plan = redo_mod.plan_redo(
            review_payload, samples, occs_raw=occs,
            capacity=capacity, max_shift_delta=shift_limit)
    except redo_mod.RedoError as e:
        return jsonify({'error': str(e)}), 400
    # 无解：返回冲突，不落库（不保存半成品）
    if not plan.get('feasible'):
        plan['review_id'] = rid
        return jsonify(plan), 200
    plan['review_id'] = rid
    plan['review_created_at'] = rec['created_at']
    pid = db.save_redo_plan(rid, plan)
    plan['redo_id'] = pid
    created = db.get_redo_plan(pid)
    plan['created_at'] = created['created_at'] if created else None
    return jsonify(plan)


@app.get('/api/redo')
def list_redo():
    review_id = request.args.get('review_id', type=int)
    return jsonify(db.list_redo_plans(review_id))


@app.get('/api/redo/<int:pid>')
def get_redo(pid):
    rec = db.get_redo_plan(pid)
    if rec is None:
        return jsonify({'error': '补做排程不存在'}), 404
    return jsonify(rec)


@app.delete('/api/redo/<int:pid>')
def remove_redo(pid):
    db.delete_redo_plan(pid)
    return jsonify({'ok': True})


@app.errorhandler(500)
def _err(e):
    return jsonify({'error': f'服务器错误：{e}'}), 500


if __name__ == '__main__':
    import waitress
    port = int(os.environ.get('PORT', 8000))
    print(f'古籍修复湿度阶梯试验编排台 → http://0.0.0.0:{port}')
    waitress.serve(app, host='0.0.0.0', port=port, threads=4)

"""Flask 服务：REST API + 静态单页前端。

启动：python -m app.server  （容器内由 waitress 在 8000 端口提供服务）
"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, request, send_from_directory

from . import scheduler
from . import review as review_mod
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
    if body.get('save'):
        note = str(body.get('note') or '')
        # 保存时把本次复核对应的排程快照完整嵌入复核记录，
        # 使该复核日后可独立用于「补做排程」（历史不可变）
        res['source_schedule'] = schedule_obj
        rid = db.save_review(res, overall, note)
        res.pop('source_schedule', None)
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
    return jsonify(db.list_reviews(limit))


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
# 补做排程（基于已保存复核）
# --------------------------------------------------------------------------- #
def _redo_source(review_rec: dict):
    """补做所用的冻结历史 = 保存复核时嵌入的原排程快照；
    极旧记录没有嵌入快照时回退到最近排程快照。"""
    payload = review_rec.get('payload') or review_rec
    src = payload.get('source_schedule')
    if src:
        return src
    latest = db.latest_schedule()
    if latest is None:
        return None
    return latest


@app.post('/api/reviews/<int:rid>/redo')
def run_redo(rid: int):
    """对已保存复核记录生成补做排程。

    只提取结论为「复核」/「数据不足」的样本；原排程完整保留为不可修改
    历史，补做从其结束分钟之后安排；继续满足容量/耐受区间/不可相邻/换档
    速率/独立换档上限（含新旧时间轴衔接处换档差值），沿用原有占用。
    无失败样本 → 空方案（不保存）；无解 → 冲突，不保存半成品；
    有解且非空 → 持久化补做方案并返回 redo_id。
    """
    rec = db.get_review(rid)
    if rec is None:
        return jsonify({'error': '复核记录不存在'}), 404
    payload = rec.get('payload') or {}
    source = _redo_source(rec)
    if not source:
        return jsonify({'error': '该复核记录未携带原排程快照，且库中没有'
                                 '任何排程可作冻结历史，无法补做'}), 400
    if not source.get('feasible'):
        return jsonify({'error': '原排程不可行，无法在其结束分钟之后补做'}), 400
    review_samples = payload.get('samples') or []
    body = request.get_json(force=True, silent=True) or {}
    try:
        kwargs = {}
        if 'capacity' in body:
            kwargs['capacity'] = body['capacity']
        # max_shift_delta 缺省时 redo_plan 沿用原排程保存值；
        # 显式传 null 表示本次不限制（旧数据也按不限制处理）
        if 'max_shift_delta' in body:
            kwargs['max_shift_delta'] = body['max_shift_delta']
        res = scheduler.redo_plan(source, review_samples, **kwargs)
    except scheduler.SchedulingError as e:
        return jsonify({'error': str(e)}), 400

    res['review_id'] = rid
    if res.get('feasible') and not res.get('empty'):
        redo_id = db.save_redo(res, rid)
        res['saved'] = True
        res['redo_id'] = redo_id
        saved = db.get_redo(redo_id)
        res['created_at'] = saved['created_at'] if saved else None
    else:
        res['saved'] = False
        if res.get('empty'):
            res['message'] = '该复核记录没有结论为「复核」或「数据不足」' \
                             '的样本，无需补做（空方案，未保存）'
    return jsonify(res)


@app.get('/api/redo')
def list_redo():
    try:
        limit = max(1, min(200, int(request.args.get('limit', 50))))
    except ValueError:
        limit = 50
    review_id = request.args.get('review_id')
    if review_id:
        try:
            return jsonify(db.list_redo_for_review(int(review_id)))
        except ValueError:
            return jsonify([])
    return jsonify(db.list_redo(limit))


@app.get('/api/redo/<int:redo_id>')
def get_redo(redo_id):
    rec = db.get_redo(redo_id)
    if rec is None:
        return jsonify({'error': '补做排程不存在'}), 404
    return jsonify(rec)


@app.delete('/api/redo/<int:redo_id>')
def remove_redo(redo_id):
    db.delete_redo(redo_id)
    return jsonify({'ok': True})


@app.errorhandler(500)
def _err(e):
    return jsonify({'error': f'服务器错误：{e}'}), 500


if __name__ == '__main__':
    import waitress
    port = int(os.environ.get('PORT', 8000))
    print(f'古籍修复湿度阶梯试验编排台 → http://0.0.0.0:{port}')
    waitress.serve(app, host='0.0.0.0', port=port, threads=4)

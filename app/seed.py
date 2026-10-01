"""首次启动时写入演示数据（仅当 samples 表为空）。

可通过环境变量 HUMIDITY_SEED=0 关闭。
"""
from __future__ import annotations

import os

SEED_SAMPLES = [
    {
        'code': 'P-017 藏经扉页',
        'rh_min': 40, 'rh_max': 75, 'eq_rh': 50, 'eq_duration': 30,
        'steps': [
            {'target': 65, 'dwell': 30, 'rate': 5},
            {'target': 50, 'dwell': 20, 'rate': 3},
        ],
        'excludes_codes': [],
    },
    {
        'code': 'P-022 公文残页',
        'rh_min': 45, 'rh_max': 70, 'eq_rh': 50, 'eq_duration': 20,
        'steps': [
            {'target': 60, 'dwell': 40, 'rate': 4},
        ],
        'excludes_codes': [],
    },
    {
        'code': 'P-031 竹纸衬条',
        'rh_min': 35, 'rh_max': 55, 'eq_rh': 45, 'eq_duration': 15,
        'steps': [
            {'target': 50, 'dwell': 25, 'rate': 6},
        ],
        'excludes_codes': [],
    },
]

SEED_OCC = [
    {'start': 240, 'end': 300, 'rh': 50, 'slots': 1, 'note': '周三校准'},
]


def seed_if_empty(db) -> None:
    if os.environ.get('HUMIDITY_SEED', '1') == '0':
        return
    if db.list_samples():
        return
    code_to_id = {}
    for d in SEED_SAMPLES:
        payload = {**{k: v for k, v in d.items() if k != 'excludes_codes'},
                   'excludes': []}
        code_to_id[d['code']] = db.upsert_sample(payload)
    if db.get_capacity() == 3 and True:
        db.set_capacity(3)
    for o in SEED_OCC:
        db.add_occ(o)

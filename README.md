# 古籍修复湿度阶梯试验编排台

面向纸张修复师的**恒湿箱阶梯程序排程**工具：录入多个纸张样本的耐受湿度区间、
阶梯（目标湿度 / 保持时长 / 升降速率）、冷却/平衡间隔与**不可相邻**条件，
设置恒湿箱容量、**独立换档上限**（每次换档最大目标湿度差值，可留空不限制）
与**已有占用**，系统在以下硬约束同时成立的前提下，
自动把多样本阶梯拆成带冷却/平衡间隔的可执行时间段：

1. **容量约束**：任意分钟在箱样本数 + 占用位数 ≤ 箱容量；
2. **区间兼容**：箱内当前湿度必须同时落在所有在箱样本的耐受区间内；
3. **换档速率上限**：每分钟湿度位移 ≤ 当前在箱样本各自声明的升降速率（取在箱最小值）；
4. **独立换档上限**（箱级设置，与样本速率无关）：时间轴上**相邻两段目标湿度**
   （每次换档的前后停留湿度）差值绝对值 ≤ 设置值；留空（旧数据）表示不限制；
5. **不可相邻**：被标记互斥的样本在箱时间段不得重叠；
6. （附加）**已有占用**：占用段内湿度恒定、占用容量，任何换档不得穿过占用段。

无解时会列出**冲突约束**并给出排序后的**最小可调整项**
（一键应用：加互斥标记 / 调容量 / 放宽独立换档上限 / 提示修改阶梯目标或后移占用）。

页面时间轴支持**拖拽泳道**改变入箱时刻，松手即重新校验，红色标记违规分钟；
每一段都可点击查看**约束来源**（来自哪个样本的哪一档 / 哪条占用）；
排程可导出为 JSON。

⑤ **一次试验实测湿度复核**：从最近排程（含拖拽后的手工排程）载入时间轴，
粘贴按分钟递增的传感器读数，设置**允许偏差**、**累计偏差剂量上限**与
**最大采样间隔**；计划与实测都按分段线性曲线处理，在每个样本的
入箱→出箱窗口内按计划阶段切分，用解析式精确积分超出允许带后的
`|实测−计划|` 面积（%RH·分钟）。采样缺口超过间隔上限的覆盖段
**不插值、不计剂量**，该样本结论为「数据不足」；只有数据完整且
剂量不超限才「通过」，否则「复核」。页面叠加计划/实测两条曲线与
允许带、超带红色重描、采样缺口阴影，逐样本列出超带区间与累计剂量；
复核记录可保存、回看并导出 JSON。

⑥ **补做排程**：在⑤的「已保存复核记录」中选择一条复核，系统**只提取结论为
「复核」或「数据不足」的样本**（通过样本不重复安排）；该复核对应的**原排程
完整保留为不可修改的历史**（含全部泳道与原有占用），补做任务从原排程
**结束分钟之后**重新安排，继续满足箱容量、耐受区间、不可相邻、换档速率与
独立换档上限，且**新旧时间轴衔接处的换档差值也参与校验**。无失败样本时
返回空方案（不保存）；补做无冲突后才落库，无解返回冲突与最小调整建议、
不保存半成品。页面以灰色泳道展示冻结历史、彩色泳道展示补做时间轴并标出
衔接处，可回看已保存补做方案并导出 JSON。

---

## 一条命令启动

```bash
docker compose up --build
```

然后浏览器访问：

> **http://localhost:8000**

数据通过命名卷 `humidity-data` 持久化到 `/app/data/humidity.db`（SQLite），
`docker compose down` 不丢数据；如要清空：`docker compose down -v`。

---

## 技术栈

| 层 | 实现 |
| --- | --- |
| 前端 | 原生 HTML / CSS / JavaScript（无框架、无构建步骤），SVG 画湿度曲线、Pointer Events 拖拽 |
| 后端 | Flask（REST API）+ waitress 生产级 WSGI |
| 存储 | SQLite（样本、阶梯、互斥关系、占用、容量/独立换档上限设置、排程快照、复核记录、补做方案） |
| 编排引擎 | `app/scheduler.py`，纯标准库分钟级确定性仿真 + 排列/启发式搜索（含冻结历史前缀的补做排程），可独立单测 |
| 复核引擎 | `app/review.py`，纯标准库分段线性曲线解析积分（允许带面积 / 采样缺口），可独立单测 |

### 本地不使用 Docker 运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.server          # 默认 8000 端口
```

### 运行引擎单元测试

```bash
python3 -m unittest discover -s app/tests -v
```

---

## 排程模型说明

每个样本在箱内经历：

```
入箱(平衡湿度 eq_rh) → 第1档(换档→目标→保持dwell) → 第2档 → …
                    → 冷却换档回 eq_rh → 平衡保持 eq_duration → 出箱
```

- 时间轴为**整数分钟**，`rh[t]` 表示第 t 分钟箱内设定湿度；
- 换档速率按整分钟位移向下取整（`per = floor(rate)`），最后一分钟可只走余数，
  从不错过任何样本的速率上限；
- **独立换档上限**比较的是每次换档**两端停留湿度**（一次换档可能跨多个移动分钟、
  被切成多段斜线，判定时按整次换档合并计算），因此它约束的是"这一档跳到下一档
  的目标湿度差"，与每分钟走多快无关；
- 多样本共享同一湿度曲线：换档需等"被该湿度约束、当前档未完成"的全部在箱样本
  保持计时完成；目标不同的样本可在耐受区间内**顺路搭车**，若其当前档未完成，
  控制器随后优先把它送回目标湿度重做该档保持；已完成的档不重做。

### 搜索与无解诊断

- ≤7 个样本时枚举全部装载顺序的排列；更多样本用 5 组启发式顺序候选；
- 固定顺序下逐样本贪心找**最早可行入箱分钟**（每次试排跑完整仿真，
  遇到停滞/违规跳到下一候选分钟）；
- 全部顺序失败时，汇总以下冲突并按调整代价排序给出最小建议：
  静态冲突（区间不相交、占用超容量、占用重叠、样本自身相邻目标差超过独立换档
  上限）、占用时间窗压迫、同档聚类的容量缺口、总机时不足、
  错峰也无法避开的独立换档超限、最紧张换档的速率放宽量等。

---

## REST API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/samples` | 列出 / 新建或更新样本（POST body 带 `id` 即更新） |
| DELETE | `/api/samples/{id}` | 删除样本 |
| GET/POST | `/api/occupancies` | 列出 / 新增已有占用 |
| DELETE | `/api/occupancies/{id}` | 删除占用 |
| GET/POST | `/api/settings` | 读 / 写箱容量与独立换档上限（见下） |
| POST | `/api/schedule` | 自动排程（body 可空，用库中数据） |
| POST | `/api/validate` | 手工排程校验，body: `{board_times:{样本id:入箱分钟}}` |
| GET | `/api/schedule/latest` | 最近一次排程快照 |
| POST | `/api/review` | 对最近排程（或 body.`schedule`）做实测湿度复核，见下 |
| GET | `/api/reviews` | 已保存复核记录列表（可带 `?limit=`） |
| GET/DELETE | `/api/reviews/{id}` | 查看单条复核记录（含完整结果与嵌入的原排程快照）/ 删除 |
| POST | `/api/reviews/{id}/redo` | 对该复核生成**补做排程**，见下 |
| GET | `/api/redo` | 已保存补做方案列表（可带 `?review_id=` 过滤） |
| GET/DELETE | `/api/redo/{id}` | 查看 / 删除单条补做方案 |
| GET | `/` | 编排台页面 |

### 样本 JSON 示例

```json
{
  "code": "P-017",
  "rh_min": 40, "rh_max": 75,
  "eq_rh": 50, "eq_duration": 30,
  "steps": [
    {"target": 65, "dwell": 30, "rate": 5},
    {"target": 50, "dwell": 20, "rate": 3}
  ],
  "excludes": [3]
}
```

### 恒湿箱设置

`GET /api/settings` 返回：

```json
{ "capacity": 3, "max_shift_delta": null }
```

`max_shift_delta` 即**独立换档上限**（每次换档相邻段目标湿度最大差值，%RH）：

- `null` / 缺省 = 不启用（**没有该字段的旧数据库与旧排程快照自动按不限制处理**）；
- `POST /api/settings` body `{"max_shift_delta": 15}` 启用并持久化；
- 传 `null`（或空字符串）可清除该设置；只传 `capacity` 时保持原值不变；
- `/api/schedule`、`/api/validate` 的 body 也可临时携带 `max_shift_delta`，
  不传则使用已保存的设置；拖拽重排与自动排程使用**同一条规则**。

超限被拒绝时，违规/冲突信息包含相邻段（起止分钟与目标湿度）、实际差值与
可调整项：放宽 `max_shift_delta`（一键应用）、修改相应样本的相邻档目标、
后移/调整端点占用段。

### 导出的排程 JSON 包含

`feasible`、`capacity`、`max_shift_delta`、`board_times`、`end_minute`、
`chamber_segments[]`（每段含 `constraints` 约束来源与 `aboard` 在箱样本）、
`sample_lanes[]`（每样本入/出箱分钟与阶梯分段）、
`violations[]`（拖拽校验违规；`shift-limit` 类型另含 `delta`、`limit`、
`adjacent_segments`、`adjustments`）、无解时的 `conflicts[]` 与
`minimal_adjustment`。

---

## 实测湿度复核

复核把**计划**与**实测**都视为分段线性曲线：

- 计划在整数分钟节点 `t` 的值等于引擎分钟序列 `rh[t]`（hold 段恒定；
  ramp 段由 `from_rh/end_rh` 线性还原，节点之间线性插值）；
- 实测读数按分钟严格递增；相邻读数之间线性插值。

在每个样本的入箱 `board` → 出箱 `unboard` 窗口 `[board, unboard]` 内：

1. 按该样本泳道阶段（入箱 / 各档换档与保持 / 冷却）切分；
2. 逐分钟格（两条曲线在整分钟之间均为线性，故偏差 `d=M−P` 也是线性）
   用解析式（线性函数正部面积公式，非矩形求和）精确积分
   `e(t)=max(|d(t)|−允许偏差, 0)`，累计为**偏差剂量**（%RH·分钟）；
3. 相邻采样间隔 **> 最大采样间隔** 的区间为**采样缺口**：缺口内不插值、
   不计剂量；凡窗口未被读数完整覆盖（首读数晚于入箱、末读数早于出箱、
   窗口与缺口相交）的样本结论为**数据不足**；
4. 数据完整：累计剂量 ≤ 上限 → **通过**；剂量超限 → **复核**。

### 读数粘贴格式

- 单列湿度（每行一个数）：时间从 `start_minute`（默认 0）起逐分钟递增；
- 两列「分钟,湿度」：分隔符可为空白/逗号/分号/中文标点；
  分钟必须为 ≥0 的整数且严格递增，湿度 ∈ [0,100]；
- 空行、`#`/`//` 注释行与含非数字字段的表头行会被跳过；两种列格式不得混用。

### `POST /api/review` 请求体

```json
{
  "readings_text": "0,50.0\n1,50.2\n…",
  "start_minute": 0,
  "tolerance": 2.0,
  "dose_limit": 30.0,
  "max_gap": 2,
  "save": false,
  "note": "2026-09-28 第一次实测"
}
```

也可用 `"readings": [[0, 50.0], [1, 50.2]]` 代替文本；`schedule` 缺省时
自动使用数据库中**最近一次排程快照**（前端会显式带上当前时间轴，
因此拖拽后的手工排程也能直接复核）。

### 响应/导出 JSON 包含

`parameters`、`schedule`（时间轴分钟数等）、`plan_series[]`（整数分钟
计划节点）、`readings[]`、`gaps[]`（超上限采样缺口）、`overall_conclusion`
（`pass`/`review`/`insufficient`）、`summary`，以及每样本的
`samples[]`：`board/unboard/window_minutes`、`data_complete`、`dose`、
`dose_limit`、`max_deviation`、`uncovered[]`（未覆盖/缺口段及原因）、
`excess_intervals[]`（超带区间：起止分钟、所在计划阶段、偏高/偏低、
该段剂量与最大偏差）、`conclusion`。

`save:true` 时写入 SQLite（`reviews` 表）并返回 `review_id`、`created_at`，
同时把本次复核对应的**完整排程快照**嵌入记录（`source_schedule`），
可经 `GET /api/reviews` 回看、页面内叠加渲染并再次导出 JSON。

---

## 补做排程

`POST /api/reviews/{id}/redo`（body 可空；也可临时携带 `capacity` /
`max_shift_delta` 覆盖，缺省沿用原排程保存值）：

1. 从该复核 `samples[]` 中**只挑选结论为 `review`（复核）/
   `insufficient`（数据不足）**的样本；`pass`（通过）样本不重复安排；
2. 复核保存时嵌入的原排程快照（0..`end_minute` 的完整湿度时间轴、泳道、
   占用）作为**冻结历史**，补做仿真从该结束分钟 `base_minute` 之后继续，
   历史段不重新校验、不可修改；
3. 补做任务在 `base_minute+1` 之后重新入箱，**重走完整程序**（入箱平衡 →
   各档换档/保持 → 冷却/平衡 → 出箱），并继续满足：
   - 箱容量（历史之后的占用位数照常计入）；
   - 各样本耐受区间与换档速率；
   - 失败样本之间的不可相邻关系（通过样本不在箱中，不参与）；
   - 独立换档上限：既校验补做时间轴内部，也校验**衔接处**——历史结束湿度
     到补做首次换档目标停留湿度的差值（历史之后若有占用段，可用其固定
     湿度作"垫步"分两跳过渡）；
4. 结果：
   - 没有失败样本 → `feasible:true, empty:true`（空方案，**不保存**）；
   - 有解且非空 → 持久化到 `redo_schedules` 表，返回 `redo_id`、
     `created_at`；
   - 无解 → `feasible:false, fatal:"NO_FEASIBLE_REDO"`，带 `conflicts[]`
     与 `minimal_adjustment`，**不保存任何半成品**（前端可一键放宽独立换档
     上限/容量后立即重试；样本阶梯与历史占用改动需重新排程+复核）。

### 补做 JSON 包含

`feasible`、`empty`、`review_id`、`redo_id`（已保存时）、
`base_minute`/`base_display`（历史结束分钟）、`end_minute`、
`capacity`、`max_shift_delta`、`failed_samples[]`、`passed_samples[]`、
`board_times`（补做入箱分钟，均 > `base_minute`）、
`junction`（衔接处 `from_rh/to_rh/delta/within_limit/label`）、
`history_segments[]` + `history_lanes[]`（冻结历史，原样保留）、
`redo_segments[]` + `lanes[]`（补做时间轴）、`occupancies[]`（沿用原有
占用）、`capacity_series[]`；无解时为 `conflicts[]` 与
`minimal_adjustment`，且不含任何补做泳道。

删除复核记录会级联删除其全部补做方案；删除补做方案不影响原复核与原排程。
原有的复核、排程、`docker compose up --build` 启动方式与
http://localhost:8000 均保持不变。

---

## 目录

```
app/
  scheduler.py     # 编排引擎（无框架依赖，可独立单测）
  review.py        # 实测湿度复核引擎（分段线性曲线解析积分，可独立单测）
  db.py            # SQLite 持久化（含复核记录）
  server.py        # Flask 路由 + waitress
  static/
    index.html     # 单页界面
    style.css
    app.js         # 时间轴/SVG/拖拽/复核/导出
  tests/test_scheduler.py
  tests/test_review.py
Dockerfile
docker-compose.yml
requirements.txt
README.md
```

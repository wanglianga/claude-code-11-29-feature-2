# 湿地多台站观测记录处理服务

把不同台站听到的同一次声响归并为**同一事件**，给出发生位置、发生时间与仍不确定的候选范围，
避免"一只动物被四台站听到"被统计成四次活动；并在事件库之上生成**活动通道候选集**：
每条通道说明所串联事件、可达约束、竞争事件与未归属观测，但**始终不生成个体身份**，
也**不在观测空档内给出单点坐标**。

- 技术栈：Python 3.12 / FastAPI / PostgreSQL 18 / SQLAlchemy 2 / NumPy
- 交付形态：**后端服务**（无前端页面），因此提供 OpenAPI 3.1 文档与 Swagger UI
- 全部数据为**虚构平面坐标与模拟台站**，不依赖真实硬件，结果**不用于预警**

> 本服务不做音频识别。台站端识别器已经给出「本机时刻 / 声响类别 / 相似性特征向量 / 误差范围」，
> 服务负责去重、校准时钟、归并同一声响、反演候选区域、定稿与修订；通道层在事件库之上
> 用版本化约束册（季节速度上限 / 不可穿越区 / 监听盲区 / 跨站校时不确定度）求解活动通道候选。

---

## 1. 关键设计（与需求的对应关系）

| 需求 | 实现位置 | 说明 |
| --- | --- | --- |
| 每条观测保存站点、启动批次、源序号、本机采集时间、上传时间、类别、特征向量、误差范围 | `app/models.py::Observation`、`POST /stations/{id}/observations` | `local_capture_us` 是**台站时钟读数**，`uploaded_at_us`/`ingest_at_us` 是参考时间；两者从不混用 |
| 同批次同序号内容相同只收一次；内容不同单列冲突，不覆盖 | `app/ingest.py` | 身份 = `(station_id, boot_id, source_seq)`；相同内容 → duplicate；不同内容 → `observation_conflicts`，原文保留；整批内容指纹相同的重放才是 no-op，`batch_key` 复用但内容不同时按记录逐条判定而非整批丢弃 |
| 用已知位置的校准声源建立本机时间 → 参考时间的分段线性映射 | `app/calibration.py` | 先减去 `距离 / 配置传播速度` 得到参考到达时刻，再拟合 `local = a·ref + b`；可按残差增益自动分段（漂移） |
| 重启前后不得共用参数 | `calibration_models` 唯一键 `(station_id, boot_id, version_hash)` | 每个 boot 独立建模；斜率不合理时判定为"把重启跳时当成漂移"，拒绝该模型 |
| 锚点太少 / 残差超限时保留未校准观测，不拿上传时间替代 | `fit_calibration` / `apply_calibration` | 低于最少锚点数、跨度不足、RMS/最大残差超限、超出锚点覆盖并被计入外推，都返回 `uncalibrated` 并写明理由 |
| 以校正后的到达区间筛选候选组合，同站同次事件至多一条 | `app/association.py` | 候选组合是**团（clique）**，逐对校验 |
| 按声类、特征距离、传播时差检查相容性 | `_compatible` | `|Δt| ≤ d(站,站)/v + u_i + u_j`（三角不等式必要条件）+ 特征欧氏距离上限 |
| 不允许仅靠两两邻近传递合并相距很远的声源 | 团枚举 + 整组可定位性 | 每个候选还必须整体通过网格搜索（存在共同位置与时刻），链式传递无法通过 |
| 有限区域内由粗到细网格搜索位置与发声时刻 | `app/localize.py` | 粗网格 → 逐级细化（默认 100 m → 12.5 m），以各站 `到达 − 传播时间` 的离散程度为拟合误差 |
| 满足全部误差约束的候选区域；多个不连通区域全部返回 | `Localization.regions` | 8 邻域连通分量；每个区域给出边界、面积、代表点、单元格 |
| 几何布局不足不能报一个确定坐标 | `position_certain` / `reason` / `warnings` | 共线站、区域过大、单区域但站数不足 → 不声称确定坐标 |
| 多个事件争用同一观测时枚举互斥组合 | `app/solver.py` | 精确集合划分（整数字典序目标）：先最大化可靠解释记录数，再最大化解释总数，最后最小化总残差 |
| 出现同分解需等待人工选择 | `status = awaiting_selection` + `GET /runs/{id}/options` + `POST /runs/{id}/select` | 完全并列时**不自动选**，每套分解各自独立、不共享观测 |
| 按台站已完整上报的采集进度判断可否定稿 | `pipeline._finalization_state` | 仅当"可能听到该事件的每个台站"都以**已校准的**进度声明覆盖窗口才 final；断联站不因时间流逝被视为已报齐 |
| 允许调查员明确排除某站并保存理由 | `POST /events/{id}/exclusions`、`POST /stations/{id}/exclusions` | 记录排除理由与时间范围，只有排除掉最后一个未决台站时才推动定稿 |
| 定稿后迟报/校准修订生成新事件修订及新旧对应 | 每次运行自动 `_link_revisions`；`mode="revision"` 递增 `revision` | 旧事件置 `superseded` 并记 `superseded_by_event_id`；新事件记 `supersedes_events`，并写 `event_revision_links` |
| 处理中断从同一输入截止点继续，输出不混用不同校准参数 | `checkpoint_json` + `cutoff_us` + `calibration_json` | 暂停/恢复复用同一 cutoff 与校准快照；`collect` 阶段把输入快照整体冻结 |
| 能从事件回查原观测、所用锚点、排除理由及候选区域 | `GET /events/{id}`、`/events/{id}/anchors`、`/events/{id}/observations` | 事件详情含观测、残差、校准模型、锚点 ID、候选区域单元格、排除理由、修订链接 |
| 独立按给定声源反算到达时间检验计算 | `verifier/verify.py` | 不 import 任何服务代码，按文档化前向模型重算每条本地读数并与发布结果对比 |

### 时间模型（为什么不用到达时间归并）

```
台站本机读数 local = a_boot · 参考到达时刻 + b_boot        （每个 boot 一组参数）
参考到达时刻       = 发声时刻 + 距离(声源, 台站) / 传播速度
```

校准声源的位置和发声时刻已知，所以先扣掉传播时间，再用最小二乘拟合 `a_boot, b_boot`。
重启产生新的 `boot_id`，`b` 可以任意跳变；同 boot 内允许缓慢漂移（分段线性）。
上传时间只做记录，从不参与归并——这正是断电重启后补传记录会错位的原因。

---

## 1.1 活动通道候选集（本迭代新增）

野外调查中，相似叫声隔着监听盲区出现，不足以证明是一只动物走出的路线。通道层因此在
事件库之上交付**候选集**而非结论：每条通道说明所串联事件、可达约束、竞争事件和没有被
归属的观测；**不生成个体身份，不在空档中给出单点坐标**。

| 需求 | 实现位置 | 说明 |
| --- | --- | --- |
| 事件库提供时间区间、可行位置、特征和修订 | `app/corridor_graph.py::build_event_snapshot` | 通道运行把窗口内未失效事件（含 provisional）冻结成快照：发声区间、区域包围盒、平均特征、台站集合、修订号 |
| 约束册：季节速度上限、不可穿越区、监听盲区、跨站校时不确定度 | `app/models.py::ConstraintBook`、`app/constraints.py` | 约束册**版本化且不可改**：同名再建即 `version+1`；`version_hash` 覆盖约束内容（notes 不参与）；每次通道运行记录所用版本哈希 |
| 季节速度上限 | `constraints.speed_limit_for` | `MM-DD` 季节区间可跨年；类专属规则优先于通配，同级取最严；无规则用册默认 |
| 不可穿越区 | `constraints.detour_distance` | 圆（切线+弧，精确）与**凸**多边形（顶点链，精确）；多区取单区绕行最大值（保守下界，文档化）；非凸多边形建册即拒绝 |
| 监听盲区 | `constraints.blind_zones_for_gap` | 只用于**解释空档**：时间相交且几何压住空档路径才计入；不制造位置 |
| 跨站校时不确定度 | `constraints.clock_uncertainty_us` | 精确台站对规则 > 单站规则 > 册默认；加宽候选边的时间区间 |
| 候选边与可达约束解释 | `corridor_graph.build_edges` | 存在性语义：存在 (时刻, 位置) 组合使 速度 ≤ 上限 即可达；每条边记录速度区间、所用季节规则、校时不确定度、穿越区、盲区、特征距离；不可达边记录理由 |
| 最小代价流 | `app/corridor_flow.py` | 全窗口**最小代价路径覆盖**（SSP+势）：每个事件恰被一条链覆盖，开链付缺测代价 λ，连边付边代价；多通道争用同一事件由全局最优裁决；**必须等整个窗口** |
| 缺测代价 | 运行参数 `miss_cost`（默认 `CORRIDOR_MISS_COST=1.0`） | λ 是敏感旋钮：过大则不同路线并链，过小则一条路线碎成单点；快照进每次运行 |
| 路径假设 + 逐层剪枝 | `app/corridor_hypothesis.py` | 逐层扩展，按可达性/特征理由剪枝（理由直观可查），束宽剪枝的分支逐层计数并**在密集窗口说明未搜索的分支** |
| 近似同分时保留竞争候选 | 流法：禁边重解收交替分解；假设法：主分解+ε 内竞争 | 两种实现都输出 `tie_group`/`alternate`/`tied_competitor`；另有逐位置 `CorridorCompetitor`（换入仍可行且 Δ≤ε） |
| 未归属观测 | `CorridorUnattributed` | `isolated`（无任何可达边，附不可达成对理由）/ `not_selected`（有边但当前 λ 下未被选中） |
| 空档不给单点坐标 | `corridor_pipeline._gap_entries` | 空档条目只有时长区间与盲区引用，**永不携带坐标**；通道只引用事件自身可行区域 |
| 迟到或拆分观测只影响相邻窗口 | `corridor_pipeline.invalidate_for_processing_run`（挂接在 `pipeline._finish_run`） | 事件运行新增/取代的事件决定受影响时间范围；只有窗口相交的通道运行置 `stale`；引用被取代事件的通道转 `pending_review`；窗口不相交的运行不受影响 |
| 人工确认与新证据矛盾时保留冲突 | `CorridorConfirmation`、`POST/DELETE /corridors/{id}/confirm` | 确认只由人撤销；被新证据（事件被取代 / 新运行未复现）否定时置 `conflicted` 并记录细节，**不自动撤销** |

### 通道层计分口径

```
边代价  = 典型速度/季节上限  +  特征权重 × (特征距离/类特征上限)      （典型值，仅供排序）
通道分  = Σ边代价 − 缺测代价λ × (事件数 − 1)                        （两种方法同一口径）
全局目标（流法）= Σ通道分  ≡  λ × 链数 + Σ已用边代价                （最小代价路径覆盖）
```

可行性永远是存在性判断（区域最近距离 ≤ 上限 × 加宽后时间区间），与计分用的典型值无关；
校时不确定度只加宽时间区间，从而只可能让边"更可行"，这正是 `clockskew` 样例验证的行为。

---

## 2. 快速启动

### 2.1 Docker Compose（推荐）

```bash
docker compose up --build -d
curl -s http://127.0.0.1:8000/health
# {"status":"ok","database":"ok","version":"1.0.0"}
```

- API: <http://127.0.0.1:8000>
- Swagger UI: <http://127.0.0.1:8000/docs>
- OpenAPI JSON: <http://127.0.0.1:8000/openapi.json>（仓库内亦存有 `openapi.json`）
- 数据库 PGDATA 在命名卷 `dbdata` 中，`docker compose down -v` 可彻底清空
- 本次交付环境未安装 Docker CLI，因此 `docker compose` 未在本机实际执行；镜像内容是
  `pip install -r requirements.txt` + 复制源码 + `uvicorn app.main:app`，与本地验证过的启动方式一致

### 2.2 本地开发（本机 PostgreSQL）

容器内已预装 PostgreSQL 18 与 Python 3.12。默认连接串为
`postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/wetland_dev`（用环境变量 `DATABASE_URL` 覆盖）。

```bash
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt          # 已预置镜像源
PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d postgres \
  -c 'CREATE DATABASE wetland_dev;'
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

表结构在应用启动时自动创建（`app/db.py::init_db`）。所有受保护接口需要请求头
`X-API-Key: dev-api-key`（`WETLAND_API_KEY` 可改，`WETLAND_AUTH_DISABLED=true` 可关闭）。
`/health`、`/docs`、`/openapi.json` 不需要鉴权。

### 2.3 一键演示 + 自检

```bash
BASE=http://127.0.0.1:8000 ./scripts/demo.sh            # 事件层五个场景
BASE=http://127.0.0.1:8000 ./scripts/demo_corridors.sh  # 通道层四个场景
```

`demo.sh` 对五个事件层场景逐个执行：重置 → 模拟台站分批上传 → 运行处理 → 独立校验。
`demo_corridors.sh` 对四个通道层场景执行：重置 → 上传 → 事件管线 → 创建约束册 →
两种方法求解通道 → 独立通道校验。最后都打印每条检查的通过情况。也可以手动分步执行（见第 5 节）。

---

## 3. 测试账号 / 密钥

本服务是机器对机器的数据接口，没有用户账号体系。唯一的凭据是 API Key：

| 项 | 开发默认值 | 覆盖方式 |
| --- | --- | --- |
| API Key | `dev-api-key` | 环境变量 `WETLAND_API_KEY` / 请求头 `X-API-Key` |
| 数据库 | `ccdev/ccdev@127.0.0.1:5432/wetland_dev` | `DATABASE_URL` |
| Compose 数据库 | `wetland/wetland@db:5432/wetland` | `docker-compose.yml` |
| 关闭鉴权 | 默认开启 | `WETLAND_AUTH_DISABLED=true`（仅限隔离环境） |
| 危险操作 | `/admin/reset?confirm=true` | `WETLAND_ALLOW_RESET=false` 可禁用 |

测试库与开发库分离：`tests/conftest.py` 默认使用 `wetland_test`，测试前自动建表、
结束后删除。创建测试库：

```bash
PGPASSWORD=ccdev /opt/postgresql-18/bin/psql -h 127.0.0.1 -U ccdev -d postgres \
  -c 'CREATE DATABASE wetland_test;'
```

---

## 4. 验证方式（实际执行过的）

```bash
# 1) 单元 + 集成 + 接口契约 + 独立校验器，共 92 个用例
.venv/bin/python -m pytest
# 92 passed

# 2) 五个事件层样例场景的端到端自检（真实 HTTP 服务）
BASE=http://127.0.0.1:8000 ./scripts/demo.sh

# 3) 四个通道层样例场景的端到端自检（约束册 + 双求解方法 + 独立校验）
BASE=http://127.0.0.1:8000 ./scripts/demo_corridors.sh

# 说明：pytest 含 6 个由独立校验器驱动的端到端用例、4 个通道场景校验用例、
# 5 个接口契约用例；demo*.sh 使用真实 uvicorn 进程 + 真实 PostgreSQL。

# 4) 单独校验某个已加载场景（事件层 / 通道层）
.venv/bin/python -m verifier.verify --scenario reboot \
  --base-url http://127.0.0.1:8000 --api-key dev-api-key
.venv/bin/python -m verifier.verify_corridors --scenario clockskew \
  --base-url http://127.0.0.1:8000 --api-key dev-api-key

# 5) 重新生成样例真值文件
.venv/bin/python scripts/generate_samples.py

# 6) 重新导出 OpenAPI 文档
.venv/bin/python scripts/export_openapi.py
```

最近一次完整执行的结果（`scripts/demo.sh` 写出 `samples/<name>/verification.json`，
`scripts/demo_corridors.sh` 写出 `corridor_verification.json`，均为运行产物、已加入 `.gitignore`）：

| 场景 | 校验项 | 结果 | 关键观测 |
| --- | --- | --- | --- |
| `overlapping` 双声源重叠 | 8 | 全部通过 | 6 事件 × 4 台站；位置误差 7.3 m；发声时刻点估计误差 5.2 ms，真值均落在发布区间内 |
| `reboot` 重启跳时 + 迟报 | 9 | 全部通过 | S2 的 `b1`/`b2` 各有独立有效模型（斜率 1.0000596 / 0.9999546）；S3 迟到 20 分钟后补传仍参与全部事件 |
| `collinear` 共线站点 | 6 | 全部通过 | 每个事件返回 2 个镜像候选区域，`position.certain=false`，不报单一坐标 |
| `duplicates` 重复补传 | 10 | 全部通过 | 整批重放 `accepted=0`；冲突记录 1 条且原文未被覆盖；事件数与记录数不变 |
| `revision` 迟报 + 校准修订 | 11 | 全部通过 | 第二次运行校准哈希不同；旧事件 `superseded` 且由新事件 `supersedes_events` 指名；实际活动数 5 条而非叠加 |
| `crossing` 交叉路线（通道） | 10 | 全部通过 | 两条轨迹在 (1400,300) 交叉；换轨分解与纯轨迹分解近似同分，交替分解与竞争事件全部保留；2 个噪声事件未归属且理由为 `isolated` |
| `simultaneous` 同时鸣叫（通道） | 11 | 全部通过 | 近同时（3 s 错位）叫声从不进入同一通道；镜像对称产生并列竞争候选（tie group） |
| `blindgap` 长盲区（通道） | 10 | 全部通过 | 5 事件连成 1 条通道跨越 1200 s 盲区；空档条目引用 `BZ-1` 且不含任何坐标；无盲区的约束册版本下空档无解释 |
| `clockskew` 时钟误差（通道） | 10 | 全部通过 | 1250 m / 900 s 的边仅在 ±120 s 校时不确定度下可行；两版约束册哈希不同，候选边集合恰好差这一条边 |

独立校验器（`verifier/verify.py` 与 `verifier/verify_corridors.py`）不引用 `app/` 下任何模块。
事件层校验按 `arrival = emission + 距离/速度`、`local = slope_boot·arrival + intercept_boot`
从真值反算每条本地读数；通道层校验独立重算约束册版本哈希（sha256 规范化内容）、
逐条重算候选边的可行性（区域最近距离 ≤ 季节上限 × 加宽时间区间），并检查未归属事件、
竞争候选、空档无坐标与无个体身份字段。

---

## 5. 手动走查（curl）

```bash
KEY='X-API-Key: dev-api-key'
BASE=http://127.0.0.1:8000

curl -s -X POST "$BASE/admin/reset?confirm=true"
curl -s -X POST "$BASE/stations" -H "$KEY" -H 'Content-Type: application/json' \
  -d '{"id":"S1","name":"North","x":300,"y":100,"hearing_radius_m":1700}'
curl -s -X POST "$BASE/calibration-sources" -H "$KEY" -H 'Content-Type: application/json' \
  -d '{"id":"C1","x":100,"y":-150,"emissions":["2026-11-13T23:20:00.000000Z"]}'
curl -s -X POST "$BASE/sound-classes" -H "$KEY" -H 'Content-Type: application/json' \
  -d '{"id":"chirp","feature_dim":4,"feature_distance_limit":0.05}'

# 台站上传（local_capture_at 不带时区 —— 它是台站时钟读数）
curl -s -X POST "$BASE/stations/S1/observations" -H "$KEY" -H 'Content-Type: application/json' -d '{
  "station_id":"S1","boot_id":"b1","batch_key":"S1-b1-1",
  "records":[
    {"source_seq":1,"kind":"calibration","class_label":"calibration",
     "local_capture_at":"2026-11-14T09:40:12.345678","features":[0.0,0.1,0.0,0.0],
     "uncertainty_ms":12,
     "calibration":{"source_id":"C1","emission_reference_at":"2026-11-13T23:20:00.000000Z"}},
    {"source_seq":2,"kind":"sound","class_label":"chirp",
     "local_capture_at":"2026-11-14T09:41:00.000000","features":[1.0,0.30,0.20,0.90],
     "uncertainty_ms":25}
  ],
  "progress":{"complete_through_local_at":"2026-11-14T09:50:00.000000","complete":true}
}'

curl -s -X POST "$BASE/runs" -H "$KEY" -H 'Content-Type: application/json' -d '{"wait":true}'
curl -s "$BASE/observations" -H "$KEY" | python -m json.tool
```

上面这条最小样例只有 1 个校准锚点，因此**预期**结果是该观测被保留为
`calibration_status = "uncalibrated"`、`calibration_reason` 说明锚点不足，且不参与归并
——这本身就是一条要验证的约束。完整的可定位流程用模拟器一步到位：

```bash
.venv/bin/python -m simulator.run_simulator --scenario reboot --reset
curl -s -X POST "$BASE/runs" -H "$KEY" -H 'Content-Type: application/json' -d '{"wait":true}'
curl -s "$BASE/events" -H "$KEY" | python -m json.tool         # 事件、位置、候选区域
curl -s "$BASE/events/1" -H "$KEY" | python -m json.tool       # 观测、锚点、排除理由、修订
curl -s "$BASE/events/1/anchors" -H "$KEY" | python -m json.tool
```

其它常用入口：

| 接口 | 作用 |
| --- | --- |
| `GET /observations?calibration_status=uncalibrated` | 查看被保留但未校准的观测及其理由 |
| `GET /conflicts` | 查看同序号异内容的补传 |
| `GET /calibrations?station_id=S2` | 查看每个 boot 的分段线性模型、残差、锚点 |
| `GET /runs/{id}/options` | 求解并列时各套分解的对比 |
| `POST /runs/{id}/select {"option_id":"OPT-02"}` | 人工选定分解 |
| `POST /runs/{id}/resume` | 从原 cutoff 续算 |
| `GET /stats` | 总体计数 |

通道层入口（活动通道候选集）：

| 接口 | 作用 |
| --- | --- |
| `POST /constraint-books` | 创建版本化约束册（季节速度上限/不可穿越区/监听盲区/校时不确定度；不可改，同名再建版本+1） |
| `GET /constraint-books` / `GET /constraint-books/{id}` | 查看约束册各版本与 `version_hash` |
| `POST /corridor-runs` | 在整个窗口上求解通道候选（`method=mincost_flow` 或 `hypothesis_pruning`，可覆盖 `miss_cost` 等参数） |
| `GET /corridor-runs` / `GET /corridor-runs/{id}` | 通道运行状态、约束版本、缺测代价、未搜索分支统计 |
| `GET /corridor-runs/{id}/corridors` | 该运行的通道候选列表（含并列/交替候选） |
| `GET /corridor-runs/{id}/unattributed` | 未被任何通道归属的事件及理由 |
| `GET /corridors/{id}` | 通道详情：串联事件、逐边可达约束解释、竞争事件、空档（无坐标）、确认记录 |
| `POST /corridors/{id}/confirm` | 人工确认某条候选通道 |
| `DELETE /corridors/{id}/confirm` | 人工撤销确认（确认只会被人撤销；被新证据否定时置 `conflicted` 并保留） |

```bash
# 最小通道流程（先加载 crossing 样例并跑完事件管线）
KEY='X-API-Key: dev-api-key'
BASE=http://127.0.0.1:8000
.venv/bin/python -m simulator.run_simulator --scenario crossing --reset
curl -s -X POST "$BASE/runs" -H "$KEY" -H 'Content-Type: application/json' -d '{"wait":true}'
.venv/bin/python -m simulator.corridor_driver --scenario crossing   # 建册 + 两种方法求解
curl -s "$BASE/corridor-runs/1/corridors" -H "$KEY" | python -m json.tool
curl -s "$BASE/corridors/1" -H "$KEY" | python -m json.tool   # 边解释/竞争事件/空档
```

---

## 6. 样例与模拟器

```
samples/
  index.json            五个场景的概要
  <scenario>/truth.json 台站位置、每 boot 真值时钟模型、校准声源时刻、上传波次、真实事件
```

`truth.json` 中的 `waves[].records` 就是模拟器实际 POST 的载荷；`waves[].truth` 记录每条
记录的生成来源（校准源 / 声源+叫次、本地读数、到达时刻），供校验器独立反算。

```bash
# 仅生成真值文件，不访问服务
.venv/bin/python -m simulator.run_simulator --scenario collinear --ground-truth-only

# 只上传前 4 波（模拟处理中断）
.venv/bin/python -m simulator.run_simulator --scenario overlapping --reset --pause-after-wave 4

# 只上传 early 或 late 阶段
.venv/bin/python -m simulator.run_simulator --scenario revision --phases early
.venv/bin/python -m simulator.run_simulator --scenario revision --phases late

# 重放首批并制造一条内容冲突
.venv/bin/python -m simulator.run_simulator --scenario duplicates --reset --retransmit
```

五个事件层内置场景：

1. **overlapping** — 两个相距约 200 m 的同类声源在数秒内各自鸣叫，S2/S3 同时听到两者；
   要求"四站一声 = 1 事件"且"两兽重叠 = 2 事件"。
2. **reboot** — S2 在 t=900 s 断电重启，时钟跳变 1500 s 且漂移反号；S3 断联 20 分钟后补传；
   要求按 boot 分别校准、迟报记录仍参与，未报齐时事件保持 provisional。
3. **collinear** — 四站严格共线，跨线镜像在传播时延上不可区分；要求返回两个不连通区域，
   且不声称单点坐标，但仍正确计数。
4. **duplicates** — 整批重放（应无效）+ 同序号异内容（应入冲突表且不覆盖原文）；
   要求事件与计数不变。
5. **revision** — 先无 S3 数据跑一次，再补传 S3 与迟到校准后跑修订；要求旧事件被取代、
   新事件指名被取代者，而不是一次活动悄悄裂成两条。

四个通道层内置场景（真值中含移动轨迹 `tracks` 与 `corridor` 配置：约束册、求解运行与期望）：

6. **crossing** 交叉路线 — 两条同类轨迹在 (1400, 300) 同时刻交叉（相距 60 m）；
   换轨分解与纯轨迹分解近似同分，要求竞争候选全部保留，两个孤立噪声事件不被归属。
7. **simultaneous** 同时鸣叫 — 两条镜像轨迹在每个叫次上仅差 3 s；
   近同时事件永不进入同一通道，镜像对称处的分配并列必须以 tie group 保留。
8. **blindgap** 长盲区 — 全部台站 t=1200–2400 s 停机维护，轨迹 3 次叫声无记录；
   通道必须跨空档串联，空档引用盲区 `BZ-1` 且不含坐标；无盲区的约束册版本下空档无解释。
9. **clockskew** 时钟误差 — 中间一跳 1250 m / 900 s 压在 1.0 m/s 上限上；
   仅在 ±120 s 跨站校时不确定度（约束册 v1）下可行，±2 s（v2）下断开成两条 2 事件通道。

> 已知边界（事件层）：完全同一时刻的叫声可能被拆成若干 2 台站事件（2 站定位恒有精确解，
> 在残差目标上占优）。通道层验证因此使用 3 s 错位的"近同时"样例——对通道可达性而言
> 3 s 远小于任何站间行程，结论不受影响。

---

## 7. 算法要点与已知边界

**候选组合**：按声类分组 → 时间与特征索引构图 → Bron–Kerbosch 枚举团（限制最大站数）→
每个团做整组网格定位，不可行则尝试去掉 1–2 个成员（覆盖双声源争用同一台站记录的情形）。

**定位**：在配置观测区域内做两级以上网格。对一个候选格心，计算每站
`校正到达 − 距离/速度`，以不确定度加权求均值，要求所有站都落在
`u_i + 全局松弛 + 半对角线时差` 之内（保守扩张，保证不漏掉边界区域）。可行格逐级四等分细化，
最后取 8 邻域连通分量。输出的精度就是实际到达的最细格距；若因格数上限提前停止，
事件上的 `warnings` 会说明"精度低于请求值"。

**求解**：候选 = 观测集合（含权重量）。目标为整数字典序：可靠解释数（其窗口已可定稿的观测）
> 解释总数 > 总残差平方和。中间这一层是必要的实现补充：需求只规定"可靠解释数"与"总残差"两项，
但若台站尚未报齐（可靠数为 0），仅按残差最小会退化为"少报事件"，把已收到的记录无故丢弃；
因此显式加入"解释总数"作为第二优先级，并且仅在可靠数相同时起作用。先按"共享观测"把候选图拆成互不影响的连通分量（分量最优解相加即全局最优），
每个分量做分支定界；精确同分时置 `awaiting_selection`。求解器与暴力枚举在 400 组随机小实例上逐一对拍一致。

**定稿**：`final` 需要"所有可能听到该事件的台站"满足：已校准进度覆盖窗口末尾 + 余量、
或被调查员显式排除（带理由）、或几何上不在听力半径内。断联站与未校准站都会让事件保持
`provisional`，运行状态记为 `awaiting_review`。

**修订**：任何一次重新运行只要重新解释了仍在生效的旧事件记录（或有同声类重叠窗口且共享台站），
就把旧事件置 `superseded`、新事件记录 `supersedes_events`；旧行保留（`include_superseded=true`
可查），因此不会"拆成两条又保留旧计数"。`mode="revision"` 额外把 `revision` 递增。

**通道求解（流法）**：把窗口内事件按时间分层建成流网络——每个事件拆成入/出两点且必须被
恰好一条链覆盖（下界 1 的覆盖边经供需变换处理），开一条链付缺测代价 λ，连边付边代价；
用最短增广路（带 Johnson 势）求最小代价循环流，即全窗口最小代价路径覆盖。多通道争用同一
事件由全局最优裁决，且必须等整个窗口才能求解。交替候选：逐条禁用已用边重解，
总代价不超过最优+ε 的分解全部保留（去重、上限 `CORRIDOR_MAX_TIE_ALTERNATES`）。

**通道求解（假设法）**：按时间层逐层扩展路径假设——可达性/特征不满足的扩展立即剪枝并记录
具体理由（`speed_limit`/`time_order`/`class_mismatch`/`feature_distance`）；每个前沿事件只保留
束宽内最优假设，被束宽剪掉的分支逐层计数为**未搜索分支**写入运行摘要（密集窗口的显式披露）。
收尾时按事件集去重，贪心集合打包得主分解，与已选通道近似同分（≤ε）的重叠通道保留为竞争候选，
其余按 `score_pruned` 计数披露。

**通道失效与确认**：事件管线每次定稿后，以"新增/被取代事件的时间范围"为受影响区间，
仅窗口相交的通道运行置 `stale`（相邻窗口之外不受影响）；引用了被取代事件的通道转
`pending_review`；其上的人工确认置 `conflicted` 并记录矛盾细节，**保留而不自动撤销**，
只能由调查员显式 `DELETE /corridors/{id}/confirm` 撤销。

**边界说明（有意为之的保守行为）**

- 只承诺**既定网格精度内的候选区域**，不做精确反演，也不做统计置信区间。
- 两个站点的记录永远不构成"确定坐标"；共线布局永远返回镜像区域。
- 传播速度取配置常数，不做温度/风场修正；这是题目给定的简化模型。
- 校准不合格的观测被**保留但不参与**归并，接口可按 `calibration_status` 回查原因。
- 迟报记录不会追溯修改已定稿事件本身，而是通过下一次运行的修订关系表达。
- `/runs` 支持 `wait=false` 后台执行以便观察中断（`debug_stop_after_phase`），
  生产使用建议 `wait=true` 或外部任务队列。
- 通道层：只交付候选集——不生成个体身份，不在空档中给出单点坐标；绕行距离是保守下界
  （多区取单区最大值），边的可行性是存在性判断；计分用的"典型速度"仅供排序，不影响可行性。
- 完全同一时刻的叫声在事件层可能被拆成 2 台站小事件（观测层已知边界）；通道层样例用
  3 s 错位的近同时叫声验证。

---

## 8. 目录结构

```
app/
  main.py          FastAPI 应用、鉴权中间件、OpenAPI 定制
  api.py           全部路由（事件层 + 通道层）
  schemas.py       Pydantic 请求/响应
  models.py        SQLAlchemy 表结构（含约束册与通道各表）
  db.py            引擎与会话
  config.py        全部可调阈值（会写入每次运行的快照）
  timeutil.py      整数微秒时间工具
  ingest.py        上传、去重、冲突、进度
  calibration.py   锚点解析与分段线性时钟校准
  localize.py      粗到细网格搜索与候选区域
  physics.py       传播时延与相容性原语
  association.py   候选团枚举
  solver.py        互斥集合划分求解与并列检测
  pipeline.py      阶段化处理、检查点、定稿、修订（挂接通道失效）
  constraints.py   约束册求值：季节速度/绕行几何/盲区/校时不确定度
  corridor_graph.py    事件快照与可达边（含逐边约束解释）
  corridor_flow.py     最小代价路径覆盖（流法）与交替分解
  corridor_hypothesis.py  路径假设逐层剪枝（束宽/理由/未搜索分支）
  corridor_pipeline.py 通道运行编排、失效传播、人工确认
simulator/
  scenarios.py     九个虚构场景与每个 boot 的时钟模型、移动轨迹
  run_simulator.py 真值生成、可暂停的分波上传、重放/冲突注入
  corridor_driver.py  通道场景驱动：建约束册 + 执行配置的通道运行
verifier/
  verify.py        独立前向模型校验器（不引用 app/）
  verify_corridors.py 独立通道校验器：候选边重算、约束版本、未归属、空档
scripts/
  demo.sh          事件层一键演示 + 自检
  demo_corridors.sh 通道层一键演示 + 自检
  generate_samples.py
  export_openapi.py
tests/             92 个用例（导入、校准、定位、关联、求解、流水线、校验器、
                   约束册、通道求解器、通道 API、通道校验器、接口契约）
samples/           九个场景的真值文件
openapi.json       导出的 OpenAPI 3.1 文档
Dockerfile / docker-compose.yml
```

## 9. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/wetland_dev` | 数据库连接 |
| `WETLAND_API_KEY` | `dev-api-key` | 接口密钥 |
| `WETLAND_AUTH_DISABLED` | `false` | 关闭密钥校验（仅隔离环境） |
| `WETLAND_ALLOW_RESET` | `true` | 是否允许 `/admin/reset` |
| `PROPAGATION_SPEED_MPS` | `343.0` | 传播速度 |
| `AREA_X_MIN/X_MAX/Y_MIN/Y_MAX` | `0/2100/-1100/1100` | 观测区域 |
| `CAL_MIN_ANCHORS` / `CAL_MIN_SEGMENT_ANCHORS` | `4` / `3` | 最少锚点数 |
| `CAL_MAX_RESIDUAL_MS` / `CAL_MAX_RMS_RESIDUAL_MS` | `250` / `120` | 残差上限 |
| `CAL_MIN_ANCHOR_SPAN_S` | `60` | 锚点最短跨度 |
| `CAL_COVERAGE_MARGIN_S` | `3600` | 允许的外推余量 |
| `FEATURE_DISTANCE_LIMIT` | `0.25` | 未配置声类时的特征距离上限 |
| `ASSOCIATION_TIME_SLACK_MS` | `150` | 关联松弛 |
| `GRID_COARSE_M` / `GRID_REFINE_LEVELS` | `100` / `3` | 网格精度 |
| `MIN_EVENT_STATIONS` | `2` | 事件最少台站数 |
| `MAX_SOLUTION_OPTIONS` | `5` | 并列/备选分解上限 |
| `HEARING_RADIUS_M` / `PROGRESS_MARGIN_S` | `1600` / `120` | 定稿判定 |
| `CORRIDOR_MISS_COST` | `1.0` | 通道缺测代价 λ（敏感旋钮，见 1.1 节） |
| `CORRIDOR_TIE_EPSILON` | `0.05` | 近似同分保留竞争候选的 ε |
| `CORRIDOR_FEATURE_WEIGHT` | `1.0` | 边代价中特征项权重 |
| `CORRIDOR_MIN_EVENTS` | `2` | 一条通道的最少事件数 |
| `CORRIDOR_MAX_EDGE_GAP_S` | `7200` | 候选边允许的最大时间间隔 |
| `CORRIDOR_GAP_EXPLAIN_S` | `600` | 超过此时长的间隔生成空档条目 |
| `CORRIDOR_BEAM_WIDTH` | `24` | 假设法每个前沿的束宽 |
| `CORRIDOR_MAX_TIE_ALTERNATES` | `8` | 流法交替分解上限 |
| `CORRIDOR_MAX_EVENTS` | `400` | 单次通道运行冻结的事件上限 |

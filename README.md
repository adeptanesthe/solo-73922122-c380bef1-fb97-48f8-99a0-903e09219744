# 提花织带纹版修复与试织验收 API

根据同系列**参考 PNG** 与用透明像素标注缺损的**残片 PNG**，补出可反复织造的
重复单元纹版；并对试织带做**漏纬/重纬双上限验收**。技术栈：FastAPI + Pillow
+ OR-Tools CP-SAT（含内置回溯兜底）+ SQLite。

## 目录结构

```
.
├── app/
│   ├── images.py      # PNG 解析/校验、周期投影、2×2 图块统计
│   ├── acceptance.py  # 试织图解析、纹版标识、最低分对齐 DP 与验收判定
│   ├── solver.py      # CP-SAT 可行性 + 局部搜索优化 + 回溯兜底
│   ├── storage.py     # 草案 SQLite 持久化
│   └── main.py        # FastAPI 路由
├── tests/
│   ├── test_app.py          # 26 个原有单元/端到端测试
│   └── test_acceptance.py   # 验收特性 16 个测试
├── demo/
│   ├── make_demo_data.py    # 生成演示 PNG（含试织带）
│   ├── make_trial_png.py    # 按已建草案现场生成复查场景试织带
│   ├── demo_requests.sh     # 可直接运行的 curl 演示
│   └── *.png                # 演示图片
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
# 健康检查
curl http://127.0.0.1:8000/health
# 跑全套演示请求（创建草案、补录、冲突、领取 PNG）
bash demo/demo_requests.sh
```

SQLite 数据库通过卷挂载持久化在 `./data/drafts.db`。

## 本地运行（无 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 交互式文档：http://127.0.0.1:8000/docs
```

## 约束与规则

- 两张图每边至多 **64 像素**；参考图最多 **4 色**且必须完全不透明。
- 残片透明像素（alpha < 128）表示缺损，不透明像素为完好色点。
- 重复单元每边 **4～12 格**；残片像素 `(x,y)` 投影到纹版格 `(x%W, y%H)`。
- 纹版按环形（toroidal）处理：以每格为左上角的 2×2 图块都必须在参考图
  出现过，**跨边、跨角环绕也算**。
- 目标：在所有可行纹版中，最大化各 2×2 图块在参考图中出现次数（频次）之和。
- 求解限时 **5 秒**（墙钟，含安全余量）。

## 求解状态

| 状态 | 含义 |
|------|------|
| `optimal` | 已证最优（达到理论上界，或 CP-SAT 精确证明） |
| `feasible` | 仅找到可行纹样（未证全局最优） |
| `infeasible` | 已证无解 |
| `unknown` | 待定（限时内连可行性都未判定） |

只有 `optimal` / `feasible` 这类有候选的结果会写入 SQLite 并返回 `draft_id`。

## API

### `POST /api/drafts`
multipart 表单：`reference`（PNG）、`fragment`（PNG）、`unit_w`、`unit_h`。

- 同格异色（周期对齐后）→ `409`，返回冲突格与残片**两处原坐标**，不建草案。
- 正常 → 返回状态、图块频次统计、候选网格；有候选时返回 `draft_id`。

### `POST /api/drafts/{id}/points`
在缺损处补录完好色点。表单：`color=r,g,b`，坐标二选一：
- 残片原坐标 `x,y`（自动取模投影）；
- 纹版格坐标 `cell_x,cell_y`。

补录后重新限时求解并更新草案。同格异色 → `409` 且**草案原样保留**。

### `GET /api/drafts/{id}`
查看草案当前状态、全部色点约束、候选网格与图块使用情况。

### `GET /api/drafts/{id}/pattern.png?scale=1`
领取正式纹版 PNG。仅当草案**已证最优**且纹版**符合当前全部色点**时放行，
否则 `409`。响应头 `X-Pattern-Id` 为**纹版标识**：由单元尺寸与逐格颜色做
SHA-256 得到，与放大倍数无关——任意 `scale` 的放大下载返回同一标识。

### `POST /api/drafts/{id}/acceptance`
试织带开批验收。multipart 表单：

| 字段 | 含义 |
|------|------|
| `pattern_id` | 领图时响应头 `X-Pattern-Id` 拿到的纹版标识 |
| `trial` | 试织 PNG：与单元**等宽**、左边对准首列，高 **2～64** 像素 |
| `max_double_weft` | 重纬次数上限（非负整数） |
| `max_missing_weft` | 漏纬次数上限（非负整数） |

**对齐与计分**：起始纹版行不限；相邻试织行在纹版上循环前进 `0/1/2` 行，
分别表示**重纬/正常/漏纬**。每个颜色不符的可见像素计 1 分，每处重纬或漏纬
各计 1 分；透明像素按原规则忽略（alpha < 128），全透明图退回 `400`。

**判定**（只看全局最低分对齐集合）：

| 判定 | 条件 | 返回字段 |
|------|------|----------|
| `pass` 放行 | 存在一条最低分对齐同时满足两项上限 | `passing_alignment` |
| `rework` 返工 | 每条最低分对齐都至少超一项 | `failing_alignment` |
| `review` 复查 | 达标与超限的最低分对齐都存在 | 两者都返回 |

响应含 `min_total_score`、`verdict`，以及示例对齐的**起始纹版行**、
**每步行数**（`steps`，0/1/2）与步长含义（`step_kinds`）、计分明细
（颜色不符/重纬/漏纬各多少）和是否在上限内。

**求解不枚举完整路径**：完整路径共 `H·3^(K-1)` 条。实现用三层动态规划——
标量最低分 DP + 紧边前驱、紧边上的 `(重纬,漏纬)` 标签 DP（Pareto 剪枝、
计数封顶）判达标对齐、紧边最大计数 DP 判超限对齐，状态数 `O(K·H)`。

**受理与出结果两次核对**：受理时和出结果前都要求正式纹版可领取且与提交的
`pattern_id` 一致；验收期间可继续补录色点，但若纹版变化导致标识不一致，
返回 `409 pattern_repick_required`，提示重新领取后再试织。

## 验收判定示例

```bash
curl -X POST http://127.0.0.1:8000/api/drafts/1/acceptance \
    -F "trial=@demo/trial_double_weft.png;type=image/png" \
    -F "pattern_id=sha256:0496..." \
    -F "max_double_weft=0" -F "max_missing_weft=0"
# -> {"verdict":"rework", "min_total_score":1,
#     "failing_alignment":{"start_pattern_row":0,"steps":[0],
#                          "step_kinds":["double_weft"], ...}}
```

## 求解器设计（三层）

1. **CP-SAT 精确优化（短预算）**：结构简单/约束多时直接证最优。
2. **CP-SAT 纯可行性**：颜色整数变量 + 每位置一条 `add_allowed_assignments`
   四元组允许表（≤256 行），最坏尺寸通常 0.1 秒内判定可行性或严格证无解。
3. **局部搜索**：在可行网格上单格变色爬山 + 随机重启，增量评估只看受影响的
   4 个图块，用满剩余预算冲最高分；达到「格数 × 最高频次」理论上界即声明最优。

无 OR-Tools 时自动退回内置的行编码回溯 + 分支定界（带递归剪枝与时限检查）。

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -v
```

共 42 个测试：原有 26 个纹版修复测试 + 16 个验收测试（试织图校验、纹版标识、
DP 与暴力枚举一致性、放行/复查/返工三种判定、受理与出结果两次标识核对、
纹版变为不可领取、补录色点后验收）。

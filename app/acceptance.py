"""试织带验收：试织 PNG 解析、纹版标识与最低分对齐求解。

对齐模型
========
试织带与纹版单元**等宽**、左边对准首列，共 ``K`` 行（``2 <= K <= 64``）。
起始纹版行 ``r0`` 任意（``0 <= r0 < H``）。相邻试织行在纹版上循环前进
``s ∈ {0, 1, 2}`` 行：

* ``s = 0``：重纬（重复投同一纹版行），每处计 1 分；
* ``s = 1``：正常行进，不扣分；
* ``s = 2``：漏纬（跳过一纹版行），每处计 1 分。

试织行 ``i`` 对准纹版行 ``p_i = (r0 + Σs) mod H``；该行每个与纹版颜色不符的
**可见**像素（alpha >= 128）计 1 分，透明像素按原规则忽略。

判定
====
在全局最低分对齐集合中：

* 存在一条同时满足重纬/漏纬两项上限                 -> ``pass``（放行）
* 每条都至少超一项上限                               -> ``rework``（返工）
* 达标、超限的最低分对齐都存在                        -> ``review``（复查）

求解复杂度
==========
完整路径共 ``H·3^(K-1)`` 条，**不做枚举**。三层动态规划：

1. 标量 DP 求各位置最低分并记录「紧边」前驱（取得最低分的转移），
   状态数 ``O(K·H)``、转移常数 3；
2. 紧边子图上的标签 DP：标签为 ``(重纬数, 漏纬数)``，按 Pareto 支配剪枝，
   计数在 ``上限+1`` 处封顶，用于判定达标对齐是否存在并回溯一条示例；
3. 紧边子图上的最大重纬/漏纬计数 DP，用于判定超限对齐是否存在并回溯示例。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Optional

from .images import ALPHA_THRESHOLD, ImageValidationError, RGB, _load_rgba

MIN_TRIAL_HEIGHT = 2
MAX_TRIAL_HEIGHT = 64

# 相邻试织行在纹版上循环前进的行数
STEP_DOUBLE_WEFT = 0   # 重纬
STEP_NORMAL = 1        # 正常
STEP_MISSING_WEFT = 2  # 漏纬
_STEP_SCORE = {STEP_DOUBLE_WEFT: 1, STEP_NORMAL: 0, STEP_MISSING_WEFT: 1}
STEP_KINDS = {
    STEP_DOUBLE_WEFT: "double_weft",
    STEP_NORMAL: "normal",
    STEP_MISSING_WEFT: "missing_weft",
}

VERDICT_PASS = "pass"      # 放行
VERDICT_REWORK = "rework"  # 返工
VERDICT_REVIEW = "review"  # 复查
VERDICT_LABELS = {
    VERDICT_PASS: "放行",
    VERDICT_REWORK: "返工",
    VERDICT_REVIEW: "复查",
}


@dataclass(frozen=True)
class TrialImage:
    """试织 PNG 解析结果；``rows`` 中 ``None`` 表示透明（忽略）像素。"""

    width: int
    height: int
    rows: tuple[tuple[Optional[RGB], ...], ...]
    visible_count: int


@dataclass(frozen=True)
class Alignment:
    """一条具体对齐：起始纹版行 + 每个间隔的前进步数及其计分明细。"""

    start_row: int
    steps: tuple[int, ...]
    total_score: int
    mismatch_score: int
    double_weft: int
    missing_weft: int


@dataclass(frozen=True)
class AcceptanceResult:
    min_score: int
    verdict: str
    passing_alignment: Optional[Alignment]
    failing_alignment: Optional[Alignment]


# --------------------------------------------------------------------------- #
# 试织图解析
# --------------------------------------------------------------------------- #

def parse_trial(data: bytes, unit_w: int) -> TrialImage:
    """解析试织 PNG。

    要求宽度与纹版单元等宽、高度在 2～64 像素之间；透明像素忽略，
    全透明图没有任何可见像素，按约定退回。
    """
    img = _load_rgba(data)
    width, height = img.size
    if width != unit_w:
        raise ImageValidationError(
            f"试织图宽 {width} 像素，必须与纹版单元宽 {unit_w} 一致（等宽且左对齐首列）")
    if not (MIN_TRIAL_HEIGHT <= height <= MAX_TRIAL_HEIGHT):
        raise ImageValidationError(
            f"试织图高必须在 {MIN_TRIAL_HEIGHT}～{MAX_TRIAL_HEIGHT} 像素之间，"
            f"收到 {height}")

    rows: list[tuple[Optional[RGB], ...]] = []
    visible = 0
    for y in range(height):
        row: list[Optional[RGB]] = []
        for x in range(width):
            r, g, b, a = img.getpixel((x, y))
            if a < ALPHA_THRESHOLD:
                row.append(None)
            else:
                row.append((int(r), int(g), int(b)))
                visible += 1
        rows.append(tuple(row))

    if visible == 0:
        raise ImageValidationError("试织图全部为透明像素，没有可见色点，退回")
    return TrialImage(width, height, tuple(rows), visible)


# --------------------------------------------------------------------------- #
# 纹版标识
# --------------------------------------------------------------------------- #

def pattern_identifier(
    unit_w: int,
    unit_h: int,
    grid: tuple[tuple[RGB, ...], ...],
) -> str:
    """由单元尺寸与逐格颜色决定的纹版标识（与放大倍数无关）。"""
    digest = hashlib.sha256()
    digest.update(struct.pack(">II", unit_w, unit_h))
    for row in grid:
        for color in row:
            digest.update(bytes((color[0], color[1], color[2])))
    return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- #
# 最低分对齐求解（动态规划，不枚举完整路径）
# --------------------------------------------------------------------------- #

def _row_mismatch_table(
    grid: tuple[tuple[RGB, ...], ...],
    trial: TrialImage,
) -> list[list[int]]:
    """``mismatch[p][i]``：试织第 i 行对准纹版第 p 行时的颜色不符像素数。"""
    height = len(grid)
    table = [[0] * trial.height for _ in range(height)]
    for p in range(height):
        pattern_row = grid[p]
        for i in range(trial.height):
            trial_row = trial.rows[i]
            table[p][i] = sum(
                1 for x in range(trial.width)
                if trial_row[x] is not None and trial_row[x] != pattern_row[x]
            )
    return table


def _prune_frontier(labels: list[tuple]) -> list[tuple]:
    """同位置标签按 (D, M) 去重并做 Pareto 剪枝。

    标签格式 ``(D, M, parent_pos, parent_label_idx, step)``。
    支配关系：``(D1, M1)`` 支配 ``(D2, M2)`` 当且仅当
    ``D1 <= D2`` 且 ``M1 <= M2``（且至少一处严格更小）——被支配标签在
    任意上限下都不会先达标。按 D 升序扫描，只保留 M **严格创新低**的
    角点标签（D 更大但 M 更小的标签不可被支配，必须保留）。
    """
    unique: dict[tuple[int, int], tuple] = {}
    for label in labels:
        key = (label[0], label[1])
        if key not in unique:
            unique[key] = label
    ordered = sorted(unique.values(), key=lambda item: (item[0], item[1]))
    frontier: list[tuple] = []
    best_m: Optional[int] = None
    for label in ordered:
        if best_m is None or label[1] < best_m:
            frontier.append(label)
            best_m = label[1]
    return frontier


def _reconstruct_label_path(
    final_pos: int,
    final_label: tuple,
    layers: list[list[list[tuple]]],
) -> tuple[int, list[int]]:
    """从标签 DP 的回溯指针还原 (起始纹版行, 每步行进列表)。"""
    steps_rev: list[int] = []
    pos = final_pos
    label = final_label
    for i in range(len(layers) - 1, 0, -1):
        _d, _m, parent_pos, parent_idx, step = label
        steps_rev.append(step)
        pos = parent_pos
        label = layers[i - 1][parent_pos][parent_idx]
    return pos, list(reversed(steps_rev))


def _reconstruct_max_path(
    final_pos: int,
    backtrack: list[list[Optional[tuple[int, int]]]],
) -> tuple[int, list[int]]:
    """从最大计数 DP 的回溯指针还原 (起始纹版行, 每步行进列表)。"""
    steps_rev: list[int] = []
    pos = final_pos
    for layer in reversed(backtrack):
        parent_pos, step = layer[pos]  # type: ignore[misc]
        steps_rev.append(step)
        pos = parent_pos
    return pos, list(reversed(steps_rev))


def _build_alignment(
    start_row: int,
    steps: list[int],
    grid: tuple[tuple[RGB, ...], ...],
    trial: TrialImage,
    mismatch: list[list[int]],
) -> Alignment:
    pos = start_row
    mismatch_score = mismatch[pos][0]
    double = 0
    missing = 0
    for i, step in enumerate(steps, start=1):
        pos = (pos + step) % len(grid)
        mismatch_score += mismatch[pos][i]
        if step == STEP_DOUBLE_WEFT:
            double += 1
        elif step == STEP_MISSING_WEFT:
            missing += 1
    return Alignment(
        start_row=start_row,
        steps=tuple(steps),
        total_score=mismatch_score + double + missing,
        mismatch_score=mismatch_score,
        double_weft=double,
        missing_weft=missing,
    )


def evaluate_trial(
    grid: tuple[tuple[RGB, ...], ...],
    trial: TrialImage,
    max_double_weft: int,
    max_missing_weft: int,
) -> AcceptanceResult:
    """求最低分对齐并按两项上限给出放行/返工/复查判定。

    状态数 ``O(K·H)``、每状态转移常数 3，不枚举 ``3^(K-1)`` 条完整路径。
    """
    height = len(grid)
    layers_n = trial.height
    mismatch = _row_mismatch_table(grid, trial)

    # ---- 第 1 层：标量最低分 DP + 紧边前驱 -------------------------------- #
    best = [mismatch[p][0] for p in range(height)]
    # tight_preds[i-1][p]：第 i 步行进到 p 且取得最低分的全部 (q, step)
    tight_preds: list[list[list[tuple[int, int]]]] = []
    for i in range(1, layers_n):
        new_best: list[int] = []
        layer_pred: list[list[tuple[int, int]]] = [
            [] for _ in range(height)]
        for p in range(height):
            candidates = []
            for q in range(height):
                for step in (STEP_DOUBLE_WEFT, STEP_NORMAL, STEP_MISSING_WEFT):
                    if (q + step) % height != p:
                        continue
                    candidates.append(
                        (best[q] + _STEP_SCORE[step] + mismatch[p][i], q, step))
            local_min = min(value for value, _q, _s in candidates)
            new_best.append(local_min)
            layer_pred[p] = [(q, s) for value, q, s in candidates
                             if value == local_min]
        best = new_best
        tight_preds.append(layer_pred)

    min_score = min(best)
    final_positions = [p for p in range(height) if best[p] == min_score]

    # ---- 第 2 层：紧边标签 DP，判达标最低分对齐是否存在 -------------------- #
    # 计数在上限 + 1 处封顶：只关心是否超限，封顶同时约束标签表规模。
    d_cap = max_double_weft + 1
    m_cap = max_missing_weft + 1
    # 标签：(D, M, parent_pos, parent_label_idx, step)
    label_layers: list[list[list[tuple]]] = [
        [[(0, 0, -1, -1, -1)] for _ in range(height)]]
    for i in range(1, layers_n):
        next_layer: list[list[tuple]] = [[] for _ in range(height)]
        for p in range(height):
            merged: list[tuple] = []
            for q, step in tight_preds[i - 1][p]:
                add_d = 1 if step == STEP_DOUBLE_WEFT else 0
                add_m = 1 if step == STEP_MISSING_WEFT else 0
                for idx, label in enumerate(label_layers[-1][q]):
                    merged.append((
                        min(d_cap, label[0] + add_d),
                        min(m_cap, label[1] + add_m),
                        q, idx, step,
                    ))
            next_layer[p] = _prune_frontier(merged)
        label_layers.append(next_layer)

    passing_path: Optional[tuple[int, list[int]]] = None
    for p in final_positions:
        for label in label_layers[-1][p]:
            if label[0] <= max_double_weft and label[1] <= max_missing_weft:
                passing_path = _reconstruct_label_path(p, label, label_layers)
                break
        if passing_path is not None:
            break

    # ---- 第 3 层：紧边最大计数 DP，判超限最低分对齐是否存在 ---------------- #
    # 两个目标各跑一遍 DP。状态转移只在「贡献本目标的步长」上增长计数，
    # 其它紧边步长只做等值继承；先处理继承、再用贡献步长严格取大，
    # 保证回溯路径上每一步都来自紧边子图，且计数可达真实最大值。
    def max_count_dp(contributing_step: int):
        values = [0] * height
        back: list[list[Optional[tuple[int, int]]]] = []
        for i in range(1, layers_n):
            nxt = [-1] * height
            layer_back: list[Optional[tuple[int, int]]] = [None] * height
            for p in range(height):
                # 非贡献步长：等值继承
                for q, step in tight_preds[i - 1][p]:
                    if step == contributing_step:
                        continue
                    if values[q] > nxt[p]:
                        nxt[p] = values[q]
                        layer_back[p] = (q, step)
                # 贡献步长：计数 +1 后严格取大
                for q, step in tight_preds[i - 1][p]:
                    if step != contributing_step:
                        continue
                    candidate = values[q] + 1
                    if candidate > nxt[p]:
                        nxt[p] = candidate
                        layer_back[p] = (q, step)
            values = nxt
            back.append(layer_back)
        return values, back

    max_d, d_back = max_count_dp(STEP_DOUBLE_WEFT)
    max_m, m_back = max_count_dp(STEP_MISSING_WEFT)

    failing_path: Optional[tuple[int, list[int]]] = None
    worst_double = max((max_d[p], p) for p in final_positions)
    worst_missing = max((max_m[p], p) for p in final_positions)
    # 重纬、漏纬是两项独立上限，任一超限即存在失败对齐，分别判断
    if worst_double[0] > max_double_weft:
        failing_path = _reconstruct_max_path(worst_double[1], d_back)
    elif worst_missing[0] > max_missing_weft:
        failing_path = _reconstruct_max_path(worst_missing[1], m_back)

    passing = (_build_alignment(*passing_path, grid=grid, trial=trial,
                                mismatch=mismatch)
               if passing_path is not None else None)
    failing = (_build_alignment(*failing_path, grid=grid, trial=trial,
                                mismatch=mismatch)
               if failing_path is not None else None)

    # 回溯路径必须落在最低分集合内（构造上成立，断言兜底）
    assert passing is None or passing.total_score == min_score
    assert failing is None or failing.total_score == min_score

    if passing is not None and failing is None:
        verdict = VERDICT_PASS
    elif passing is None and failing is not None:
        verdict = VERDICT_REWORK
    elif passing is not None and failing is not None:
        verdict = VERDICT_REVIEW
    else:  # 最低分对齐必然存在，理论不可达
        raise RuntimeError("最低分对齐集合为空，判定失败")

    return AcceptanceResult(
        min_score=min_score,
        verdict=verdict,
        passing_alignment=passing,
        failing_alignment=failing,
    )

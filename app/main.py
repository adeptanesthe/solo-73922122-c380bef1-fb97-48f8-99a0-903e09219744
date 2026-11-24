"""提花织带纹版修复 FastAPI 应用。

端点
----
``POST /api/drafts``
    上传参考 PNG、残片 PNG 与重复单元尺寸；校验周期对齐并限时 5 秒求解，
    有候选时落库 SQLite 并返回草案编号。

``POST /api/drafts/{draft_id}/points``
    在缺损处补录完好色点（残片原坐标 + 颜色，或纹版格坐标 + 颜色），
    重新限时求解；色点冲突时返回坐标并保留原草案。

``GET  /api/drafts/{draft_id}``
    查看草案当前状态、约束与候选网格。

``GET  /api/drafts/{draft_id}/pattern.png``
    领取正式纹版 PNG；仅当草案已证最优且与当前全部色点一致时放行。
    响应头 ``X-Pattern-Id`` 为由单元尺寸与逐格颜色决定的纹版标识，
    放大下载（任意 scale）使用同一标识。

``POST /api/drafts/{draft_id}/acceptance``
    试织带验收：传入领图标识、试织 PNG 与重纬/漏纬次数上限，
    动态规划求最低分对齐（不枚举完整路径），返回最低总分与
    放行/返工/复查判定，各附一条达标/超限最低分对齐示例。

``GET  /health``
    健康检查。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.responses import JSONResponse, Response

from .acceptance import (
    STEP_KINDS,
    VERDICT_LABELS,
    AcceptanceResult,
    Alignment,
    evaluate_trial,
    parse_trial,
    pattern_identifier,
)
from .images import (
    ImageValidationError,
    MAX_UNIT,
    MIN_UNIT,
    RGB,
    TileKey,
    grid_to_png,
    parse_fragment,
    parse_reference,
    project_points,
    validate_unit_size,
)
from .solver import (
    STATUS_FEASIBLE,
    STATUS_INFEASIBLE,
    STATUS_OPTIMAL,
    STATUS_UNKNOWN,
    SolveResult,
    solve,
)
from .storage import DEFAULT_DB_PATH, DraftStore

SOLVE_TIME_LIMIT = 5.0

_store: Optional[DraftStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    _store = DraftStore(DEFAULT_DB_PATH)
    yield
    assert _store is not None
    _store.close()


app = FastAPI(
    title="提花织带纹版修复 API",
    version="1.0.0",
    description=(
        "根据同系列参考 PNG 与透明像素标注缺损的残片 PNG，"
        "补出可反复织造的重复单元纹版。"
    ),
    lifespan=lifespan,
)


def store() -> DraftStore:
    assert _store is not None, "DraftStore 尚未初始化"
    return _store


@app.exception_handler(ImageValidationError)
async def _image_validation_handler(_request, exc: ImageValidationError):
    return JSONResponse(status_code=400, content={
        "error": "validation_error",
        "message": str(exc),
    })


class ConflictError(HTTPException):
    def __init__(self, detail: dict):
        super().__init__(status_code=409, detail=detail)


def _load_claimable_pattern(draft_id: int) -> tuple:
    """取当前可领取的正式纹版：已证最优、与当前全部色点一致。

    返回 ``(row, grid, pattern_id)``；不满足任一条件时抛 409，
    并在错误体中提示重新领取后再试织。
    """
    row = store().get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"草案 {draft_id} 不存在"})

    if row["status"] != STATUS_OPTIMAL:
        raise ConflictError({
            "error": "not_optimal",
            "message": "草案尚未证明最优，正式纹版当前不可领取，请重领后重新试织",
            "draft_id": draft_id,
            "status": row["status"],
        })

    grid = DraftStore.decode_grid(row["grid"])
    fixed = DraftStore.decode_points(row["fixed_points"])
    assert grid is not None

    mismatches = [
        {"cell": {"x": x, "y": y}, "expected": list(color),
         "actual": list(grid[y][x])}
        for (x, y), color in sorted(fixed.items())
        if grid[y][x] != color
    ]
    if mismatches:
        raise ConflictError({
            "error": "point_mismatch",
            "message": "纹版与当前色点不一致，正式纹版当前不可领取，"
                       "请重领后重新试织",
            "draft_id": draft_id,
            "mismatches": mismatches,
        })

    return row, grid, pattern_identifier(row["unit_w"], row["unit_h"], grid)


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #

def _tile_counts_payload(tile_counts: dict[TileKey, int]) -> list[dict]:
    return [
        {"tile": [list(c) for c in key], "count": count}
        for key, count in sorted(
            tile_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _tile_counts_from_rows(entries: list[dict]) -> dict[TileKey, int]:
    return {
        tuple(tuple(c) for c in entry["tile"]): entry["count"]  # type: ignore[arg-type]
        for entry in entries
    }


def _conflict_payload(conflicts, unit_w: int, unit_h: int) -> dict:
    return {
        "error": "cell_conflict",
        "message": "完好色点按周期对齐时出现同格异色",
        "unit_size": {"width": unit_w, "height": unit_h},
        "conflicts": [
            {
                "cell": {"x": c.cell[0], "y": c.cell[1]},
                "fragment_positions": [
                    {"x": px, "y": py} for px, py in c.positions
                ],
                "colors": [list(color) for color in c.colors],
            }
            for c in conflicts
        ],
    }


_STATUS_LABELS = {
    STATUS_OPTIMAL: "已证最优",
    STATUS_FEASIBLE: "仅找到可行纹样",
    STATUS_INFEASIBLE: "已证无解",
    STATUS_UNKNOWN: "待定",
}


def _solve_payload(
    result: SolveResult,
    unit_w: int,
    unit_h: int,
    palette: tuple[RGB, ...],
    tile_counts: dict[TileKey, int],
    fixed: dict[tuple[int, int], RGB],
    draft_id: Optional[int],
) -> dict:
    payload = {
        "status": result.status,
        "status_label": _STATUS_LABELS[result.status],
        "unit_size": {"width": unit_w, "height": unit_h},
        "time_limit_seconds": SOLVE_TIME_LIMIT,
        "elapsed_seconds": round(result.elapsed_seconds, 4),
        "solver": result.solver_name,
        "score": result.score,
        "upper_bound": result.upper_bound,
        "fixed_point_count": len(fixed),
        "palette": [list(c) for c in palette],
        "tile_counts": _tile_counts_payload(tile_counts),
        "candidate_grid": (
            [[list(c) for c in row] for row in result.grid]
            if result.grid is not None else None
        ),
        "candidate_tile_usage": result.tile_usage,
    }
    if draft_id is not None:
        payload["draft_id"] = draft_id
        payload["pattern_url"] = f"/api/drafts/{draft_id}/pattern.png"
        if result.grid is not None:
            payload["pattern_id"] = pattern_identifier(
                unit_w, unit_h, result.grid)
    return payload


def _parse_rgb(raw: str) -> RGB:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ImageValidationError("color 需要形如 'r,g,b' 的三色通道")
    try:
        channels = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ImageValidationError("color 通道必须是 0～255 的整数") from exc
    if any(not 0 <= c <= 255 for c in channels):
        raise ImageValidationError("color 通道必须在 0～255 之间")
    return channels  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/drafts")
async def create_draft(
    reference: UploadFile = File(..., description="同系列参考 PNG（≤4 色，不透明）"),
    fragment: UploadFile = File(..., description="残片 PNG，透明像素标缺损"),
    unit_w: int = Form(..., description=f"重复单元宽（格），{MIN_UNIT}～{MAX_UNIT}"),
    unit_h: int = Form(..., description=f"重复单元高（格），{MIN_UNIT}～{MAX_UNIT}"),
):
    validate_unit_size(unit_w, unit_h)
    ref = parse_reference(await reference.read())
    frag = parse_fragment(await fragment.read())

    fixed, conflicts = project_points(frag.points, unit_w, unit_h)
    if conflicts:
        raise ConflictError(_conflict_payload(conflicts, unit_w, unit_h))

    result = solve(ref.colors, ref.tile_counts, unit_w, unit_h, fixed,
                   SOLVE_TIME_LIMIT)

    draft_id: Optional[int] = None
    if result.status in (STATUS_OPTIMAL, STATUS_FEASIBLE):
        draft_id = store().create_draft(
            unit_w=unit_w, unit_h=unit_h, result=result,
            palette=ref.colors, tile_counts=ref.tile_counts,
            fixed_points=fixed,
        )
    return _solve_payload(result, unit_w, unit_h, ref.colors,
                          ref.tile_counts, fixed, draft_id)


@app.post("/api/drafts/{draft_id}/points")
async def add_point(
    draft_id: int = Path(..., ge=1),
    color: str = Form(..., description="完好色点颜色，形如 '255,0,0'"),
    x: Optional[int] = Form(None, description="残片原坐标 x（与 cell_x 二选一）"),
    y: Optional[int] = Form(None, description="残片原坐标 y（与 cell_y 二选一）"),
    cell_x: Optional[int] = Form(None, description="纹版格坐标 x"),
    cell_y: Optional[int] = Form(None, description="纹版格坐标 y"),
):
    row = store().get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"草案 {draft_id} 不存在"})

    rgb = _parse_rgb(color)
    unit_w, unit_h = row["unit_w"], row["unit_h"]

    if cell_x is not None and cell_y is not None:
        target_cell = (cell_x, cell_y)
        source_position = None
    elif x is not None and y is not None:
        target_cell = (x % unit_w, y % unit_h)
        source_position = (x, y)
    else:
        raise ImageValidationError(
            "必须提供残片原坐标 (x,y) 或纹版格坐标 (cell_x,cell_y)")

    cx, cy = target_cell
    if not (0 <= cx < unit_w and 0 <= cy < unit_h):
        raise ImageValidationError(
            f"格坐标超出单元范围：({cx},{cy})，单元 {unit_w}×{unit_h}")

    fixed = DraftStore.decode_points(row["fixed_points"])
    palette = tuple(tuple(c) for c in json.loads(row["palette"]))  # type: ignore[arg-type]
    tile_counts = _tile_counts_from_rows(json.loads(row["tile_counts"]))

    # 同格异色 -> 冲突，草案原样保留
    if target_cell in fixed and fixed[target_cell] != rgb:
        raise ConflictError({
            "error": "cell_conflict",
            "message": "补录色点与该格已有色点颜色冲突，草案保持不变",
            "draft_id": draft_id,
            "conflict": {
                "cell": {"x": cx, "y": cy},
                "existing_color": list(fixed[target_cell]),
                "incoming_color": list(rgb),
                "fragment_position": (
                    {"x": source_position[0], "y": source_position[1]}
                    if source_position else None),
            },
        })

    if rgb not in palette:
        raise ImageValidationError(
            f"颜色 {list(rgb)} 不在参考图调色板 "
            f"{[list(c) for c in palette]} 中")

    fixed[target_cell] = rgb
    result = solve(palette, tile_counts, unit_w, unit_h, fixed,
                   SOLVE_TIME_LIMIT)
    store().update_draft(draft_id, result=result, fixed_points=fixed)

    payload = _solve_payload(result, unit_w, unit_h, palette, tile_counts,
                             fixed, draft_id)
    payload["added"] = {
        "cell": {"x": cx, "y": cy},
        "color": list(rgb),
        "fragment_position": (
            {"x": source_position[0], "y": source_position[1]}
            if source_position else None),
    }
    return payload


@app.get("/api/drafts/{draft_id}")
async def get_draft(draft_id: int = Path(..., ge=1)):
    row = store().get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"草案 {draft_id} 不存在"})
    payload = store().row_to_dict(row)
    # 正式纹版可领取时附上当前纹版标识（与下载响应头同源）
    if row["status"] == STATUS_OPTIMAL and row["grid"]:
        grid = DraftStore.decode_grid(row["grid"])
        assert grid is not None
        payload["pattern_id"] = pattern_identifier(
            row["unit_w"], row["unit_h"], grid)
    return payload


@app.get("/api/drafts/{draft_id}/pattern.png")
async def get_pattern(
    draft_id: int = Path(..., ge=1),
    scale: int = Query(1, ge=1, le=40, description="输出放大倍数"),
):
    row, grid, pid = _load_claimable_pattern(draft_id)

    png = grid_to_png(grid, scale=scale)
    return Response(
        content=png, media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="pattern_{draft_id}.png"'),
            "X-Draft-Id": str(draft_id),
            "X-Pattern-Score": str(row["score"]),
            # 纹版标识：由单元尺寸与逐格颜色决定，放大下载也保持同一标识
            "X-Pattern-Id": pid,
        },
    )


# --------------------------------------------------------------------------- #
# 试织带验收
# --------------------------------------------------------------------------- #

def _nonnegative_limit(raw: str, field: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ImageValidationError(f"{field} 必须是非负整数，收到 {raw!r}") from exc
    if value < 0:
        raise ImageValidationError(f"{field} 必须是非负整数，收到 {value}")
    return value


def _repick_error(draft_id: int, submitted: str, current: Optional[str]) -> dict:
    return {
        "error": "pattern_repick_required",
        "message": "正式纹版不可领取或与提交的纹版标识不一致，请重新领取后再试织",
        "draft_id": draft_id,
        "submitted_pattern_id": submitted,
        "current_pattern_id": current,
    }


def _alignment_payload(alignment: Alignment,
                       max_double_weft: int, max_missing_weft: int) -> dict:
    return {
        "start_pattern_row": alignment.start_row,
        "steps": list(alignment.steps),
        "step_kinds": [STEP_KINDS[s] for s in alignment.steps],
        "total_score": alignment.total_score,
        "score_breakdown": {
            "color_mismatch": alignment.mismatch_score,
            "double_weft": alignment.double_weft,
            "missing_weft": alignment.missing_weft,
        },
        "limits": {
            "max_double_weft": max_double_weft,
            "max_missing_weft": max_missing_weft,
        },
        "within_limits": (
            alignment.double_weft <= max_double_weft
            and alignment.missing_weft <= max_missing_weft),
    }


@app.post("/api/drafts/{draft_id}/acceptance")
async def accept_trial(
    draft_id: int = Path(..., ge=1),
    pattern_id: str = Form(
        ..., description="领图时拿到的纹版标识（X-Pattern-Id 响应头）"),
    trial: UploadFile = File(..., description="试织 PNG，与单元等宽、高 2～64 像素"),
    max_double_weft: str = Form(
        ..., description="重纬次数上限（非负整数）"),
    max_missing_weft: str = Form(..., description="漏纬次数上限（非负整数）"),
):
    # 与 color 字段一致：非法/负数统一走 ImageValidationError -> 400
    limit_double = _nonnegative_limit(max_double_weft, "max_double_weft")
    limit_missing = _nonnegative_limit(max_missing_weft, "max_missing_weft")

    # 受理时：正式纹版必须可领取，且与提交的标识一致
    try:
        row, grid, current_id = _load_claimable_pattern(draft_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise ConflictError(_repick_error(draft_id, pattern_id, None)) from exc
        raise
    if pattern_id != current_id:
        raise ConflictError(
            _repick_error(draft_id, pattern_id, current_id))

    trial_image = parse_trial(await trial.read(), row["unit_w"])
    result: AcceptanceResult = evaluate_trial(
        grid, trial_image, limit_double, limit_missing)

    # 出结果时再次核对：验收期间允许补录色点，但纹版可能已变化或变得
    # 不可领取；任一情况都要求重新领取后再试织
    try:
        _row2, _grid2, current_id2 = _load_claimable_pattern(draft_id)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise ConflictError(
                _repick_error(draft_id, pattern_id, None)) from exc
        raise
    if pattern_id != current_id2:
        raise ConflictError(
            _repick_error(draft_id, pattern_id, current_id2))

    payload = {
        "draft_id": draft_id,
        "pattern_id": current_id2,
        "verdict": result.verdict,
        "verdict_label": VERDICT_LABELS[result.verdict],
        "min_total_score": result.min_score,
        "limits": {
            "max_double_weft": limit_double,
            "max_missing_weft": limit_missing,
        },
        "trial_image": {
            "width": trial_image.width,
            "height": trial_image.height,
            "visible_pixels": trial_image.visible_count,
        },
        "passing_alignment": (
            _alignment_payload(result.passing_alignment,
                               limit_double, limit_missing)
            if result.passing_alignment is not None else None),
        "failing_alignment": (
            _alignment_payload(result.failing_alignment,
                               limit_double, limit_missing)
            if result.failing_alignment is not None else None),
    }
    return payload

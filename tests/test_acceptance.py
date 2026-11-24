"""试织带验收（acceptance）单元与端到端测试。"""

from __future__ import annotations

import io
import itertools

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import acceptance as acc
from app import images, solver, storage
from app.main import app

RED = (220, 60, 60)
BLUE = (60, 90, 220)
GREEN = (60, 180, 90)
A, B, C = RED, BLUE, GREEN


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #

def trial_png(rows) -> bytes:
    """rows：每行是 RGB 或 None（透明）列表。"""
    height, width = len(rows), len(rows[0])
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, color in enumerate(row):
            if color is not None:
                img.putpixel((x, y), (*color, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def solid(color, w=4, h=4):
    return tuple(tuple(color for _ in range(w)) for _ in range(h))


def make_draft(store_obj, grid, fixed=None) -> int:
    """绕过求解器，直接把指定网格落成一张「已证最优」草案。"""
    unit_w, unit_h = len(grid[0]), len(grid)
    tile_counts = images.count_tiles(grid, unit_w, unit_h)
    palette = tuple(sorted({color for row in grid for color in row}))
    result = solver.SolveResult(
        status=solver.STATUS_OPTIMAL, grid=grid, score=1, upper_bound=1,
        elapsed_seconds=0.0, solver_name="test-fixture", tile_usage=[])
    return store_obj.create_draft(
        unit_w=unit_w, unit_h=unit_h, result=result, palette=palette,
        tile_counts=tile_counts, fixed_points=fixed or {})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test_drafts.db"
    monkeypatch.setattr(storage, "DEFAULT_DB_PATH", db)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# 试织图解析
# --------------------------------------------------------------------------- #

def test_trial_width_must_equal_unit_width():
    data = trial_png([[A, A, A], [A, A, A]])
    with pytest.raises(images.ImageValidationError):
        acc.parse_trial(data, unit_w=4)


def test_trial_height_bounds():
    for bad_h in (1, 65):
        rows = [[A] * 4 for _ in range(bad_h)]
        with pytest.raises(images.ImageValidationError):
            acc.parse_trial(trial_png(rows), unit_w=4)
    # 边界 2 与 64 合法
    assert acc.parse_trial(trial_png([[A] * 4, [A] * 4]), 4).height == 2
    tall = [[A] * 4 for _ in range(64)]
    assert acc.parse_trial(trial_png(tall), 4).height == 64


def test_fully_transparent_trial_rejected():
    rows = [[None] * 4 for _ in range(3)]
    with pytest.raises(images.ImageValidationError):
        acc.parse_trial(trial_png(rows), unit_w=4)


def test_trial_transparent_pixels_ignored_and_left_aligned():
    # 等宽即左对齐首列；透明像素不计可见数
    data = trial_png([[A, None, A, None], [None, B, None, B]])
    trial = acc.parse_trial(data, unit_w=4)
    assert trial.visible_count == 4
    assert trial.rows[0][1] is None and trial.rows[1][1] == B


# --------------------------------------------------------------------------- #
# 纹版标识
# --------------------------------------------------------------------------- #

def test_pattern_identifier_deterministic():
    grid = solid(A)
    pid1 = acc.pattern_identifier(4, 4, grid)
    pid2 = acc.pattern_identifier(4, 4, grid)
    assert pid1 == pid2 and pid1.startswith("sha256:")
    # 改一格颜色 -> 标识变化；改尺寸 -> 标识变化
    other = [list(row) for row in grid]
    other[0][0] = B
    assert acc.pattern_identifier(4, 4, tuple(tuple(r) for r in other)) != pid1
    assert acc.pattern_identifier(4, 4, grid) != acc.pattern_identifier(
        4, 3, tuple(grid[:3]))


# --------------------------------------------------------------------------- #
# 算法：与暴力枚举一致（小尺寸，路径数可承受）
# --------------------------------------------------------------------------- #

def _brute_force(grid, trial, md, mm):
    height = len(grid)
    mismatch = acc._row_mismatch_table(grid, trial)
    paths = []
    for r0 in range(height):
        for steps in itertools.product((0, 1, 2), repeat=trial.height - 1):
            pos, score, d, m = r0, mismatch[r0][0], 0, 0
            for i, step in enumerate(steps, 1):
                pos = (pos + step) % height
                score += mismatch[pos][i] + acc._STEP_SCORE[step]
                d += step == 0
                m += step == 2
            paths.append((score, r0, steps, d, m))
    min_score = min(p[0] for p in paths)
    mins = [p for p in paths if p[0] == min_score]
    any_pass = any(p[3] <= md and p[4] <= mm for p in mins)
    any_fail = any(p[3] > md or p[4] > mm for p in mins)
    verdict = (acc.VERDICT_REVIEW if any_pass and any_fail
               else acc.VERDICT_PASS if any_pass else acc.VERDICT_REWORK)
    return min_score, verdict


def test_dp_matches_brute_force_on_small_cases():
    grid = (
        (A, A, A, A),
        (B, B, B, B),
        (A, A, A, A),
        (B, B, A, B),
    )
    trial_rows = [
        [A, A, None, A],
        [A, B, A, None],
        [B, B, B, B],
        [A, None, A, A],
        [A, A, A, B],
    ]
    trial = acc.TrialImage(
        4, len(trial_rows), tuple(tuple(r) for r in trial_rows),
        sum(c is not None for r in trial_rows for c in r))
    for md in range(4):
        for mm in range(4):
            res = acc.evaluate_trial(grid, trial, md, mm)
            bf_score, bf_verdict = _brute_force(grid, trial, md, mm)
            assert res.min_score == bf_score
            assert res.verdict == bf_verdict
            for al in (res.passing_alignment, res.failing_alignment):
                if al is None:
                    continue
                assert al.total_score == res.min_score
                assert al.total_score == (
                    al.mismatch_score + al.double_weft + al.missing_weft)
                within = al.double_weft <= md and al.missing_weft <= mm
                if al is res.passing_alignment:
                    assert within
                else:
                    assert not within


def test_step_semantics_and_scoring():
    # 两行不同的纹版；试织两行
    grid = (
        (A, A, A, A),
        (B, B, B, B),
        (A, A, A, A),
        (B, B, B, B),
    )
    # 试织 = 纹版前两行，正常对齐 0 分
    trial = acc.parse_trial(trial_png([[A] * 4, [B] * 4]), 4)
    res = acc.evaluate_trial(grid, trial, 1, 1)
    assert res.min_score == 0
    assert res.verdict == acc.VERDICT_PASS
    al = res.passing_alignment
    assert al.steps == (1,)
    assert al.double_weft == 0 and al.missing_weft == 0
    assert al.mismatch_score == 0

    # 试织两行相同 A：正常行进第二行 4 个颜色不符；重纬只扣 1 分 -> 最低分 1
    trial2 = acc.parse_trial(trial_png([[A] * 4, [A] * 4]), 4)
    res2 = acc.evaluate_trial(grid, trial2, 0, 0)
    assert res2.min_score == 1
    assert res2.verdict == acc.VERDICT_REWORK
    fail = res2.failing_alignment
    assert fail is not None and fail.steps == (0,)
    assert fail.double_weft == 1 and fail.mismatch_score == 0


# --------------------------------------------------------------------------- #
# 三种判定共用一个夹具：最低分对齐同时含重纬路径与漏纬路径
# --------------------------------------------------------------------------- #

@pytest.fixture()
def review_grid():
    # 经枚举验证：从 r0=0 出发，重纬（对 r0）与漏纬（对 r2）两条路径
    # 恰好并列最低分（各 1 错色 + 1 罚分 = 2 分），且无其它最低分对齐。
    return (
        (A, A, A, A),
        (B, B, B, B),
        (B, A, A, A),
        (B, B, B, B),
    )


def _review_trial():
    # 第一行 = A 行；第二行是「C + 三个 A」，使 r0/r2 都恰有 1 个错色。
    return trial_png([
        [A, A, A, A],
        [C, A, A, A],
    ])


def test_verdicts_pass_review_rework(client, review_grid):
    from app.main import _store
    draft_id = make_draft(_store, review_grid)
    pid = acc.pattern_identifier(4, 4, review_grid)

    def call(md, mm):
        return client.post(
            f"/api/drafts/{draft_id}/acceptance",
            files={"trial": ("t.png", _review_trial(), "image/png")},
            data={"pattern_id": pid,
                  "max_double_weft": md, "max_missing_weft": mm})

    body_pass = call(1, 1).json()
    assert body_pass["verdict"] == "pass"
    assert body_pass["min_total_score"] == 2
    assert body_pass["passing_alignment"] is not None
    assert body_pass["failing_alignment"] is None

    body_review = call(1, 0).json()
    assert body_review["verdict"] == "review"
    pa, fa = (body_review["passing_alignment"],
              body_review["failing_alignment"])
    assert pa["within_limits"] and not fa["within_limits"]
    assert pa["total_score"] == fa["total_score"] == 2
    # 示例注明起始纹版行与每步行数及含义
    assert pa["start_pattern_row"] == 0 and pa["steps"] == [0]
    assert pa["step_kinds"] == ["double_weft"]
    assert fa["steps"] == [2] and fa["step_kinds"] == ["missing_weft"]
    assert fa["score_breakdown"]["missing_weft"] == 1

    # 另一方向的复查：允许漏纬不允许重纬 -> 达标=漏纬，超限=重纬
    body_review2 = call(0, 1).json()
    assert body_review2["verdict"] == "review"
    assert body_review2["passing_alignment"]["steps"] == [2]
    assert body_review2["failing_alignment"]["steps"] == [0]

    body_rework = call(0, 0).json()
    assert body_rework["verdict"] == "rework"
    assert body_rework["passing_alignment"] is None
    assert body_rework["failing_alignment"] is not None


# --------------------------------------------------------------------------- #
# API 端到端
# --------------------------------------------------------------------------- #

def test_full_acceptance_flow_with_identifier_header(client):
    from app.main import _store
    grid = solid(A, w=4, h=4)
    draft_id = make_draft(_store, grid)

    # 正式纹版下载附带标识，放大下载同一标识
    h1 = client.get(f"/api/drafts/{draft_id}/pattern.png")
    h8 = client.get(f"/api/drafts/{draft_id}/pattern.png?scale=8")
    assert h1.status_code == h8.status_code == 200
    pid = h1.headers["X-Pattern-Id"]
    assert pid and pid == h8.headers["X-Pattern-Id"]
    # GET 草案同样给出标识
    assert client.get(f"/api/drafts/{draft_id}").json()["pattern_id"] == pid

    # 完美试织（正常行进 0 分）-> 放行
    resp = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": pid, "max_double_weft": 0, "max_missing_weft": 0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "pass"
    assert body["min_total_score"] == 0
    assert body["pattern_id"] == pid
    assert body["trial_image"] == {"width": 4, "height": 2, "visible_pixels": 8}


def test_acceptance_wrong_identifier_requires_repick(client):
    from app.main import _store
    draft_id = make_draft(_store, solid(A))
    resp = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": "sha256:deadbeef",
              "max_double_weft": 0, "max_missing_weft": 0})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "pattern_repick_required"
    assert detail["submitted_pattern_id"] == "sha256:deadbeef"
    assert detail["current_pattern_id"].startswith("sha256:")


def test_acceptance_blocked_when_pattern_not_claimable(client):
    from app.main import _store
    draft_id = make_draft(_store, solid(A))
    with _store._lock:
        _store._conn.execute(
            "UPDATE drafts SET status='feasible' WHERE id=?", (draft_id,))
        _store._conn.commit()
    pid = acc.pattern_identifier(4, 4, solid(A))
    resp = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": pid, "max_double_weft": 1, "max_missing_weft": 1})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "pattern_repick_required"


def test_acceptance_rechecks_identifier_before_result(client, monkeypatch):
    """验收期间纹版变化（如补录色点后重解）-> 出结果时标识不一致，要求重领。"""
    from app import main
    grid = solid(A)
    draft_id = make_draft(main._store, grid)
    pid = acc.pattern_identifier(4, 4, grid)
    original = main._store.get_draft
    calls = {"n": 0}

    def patched(row_id):
        row = original(row_id)
        calls["n"] += 1
        if calls["n"] == 2:  # 出结果前的第二次核对
            changed = dict(row)
            other = tuple(tuple(B if c == A else c for c in r) for r in grid)
            changed["grid"] = storage.DraftStore._encode_grid(other)
            return changed
        return row

    monkeypatch.setattr(main._store, "get_draft", patched)
    resp = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": pid, "max_double_weft": 0, "max_missing_weft": 0})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "pattern_repick_required"
    assert detail["current_pattern_id"] != pid


def test_acceptance_result_time_not_claimable(client, monkeypatch):
    """出结果时纹版变得不可领取（如状态退回 feasible）-> 同样要求重领。"""
    from app import main
    grid = solid(A)
    draft_id = make_draft(main._store, grid)
    pid = acc.pattern_identifier(4, 4, grid)
    original = main._store.get_draft
    calls = {"n": 0}

    def patched(row_id):
        row = original(row_id)
        calls["n"] += 1
        if calls["n"] == 2:  # 出结果前的第二次核对
            changed = dict(row)
            changed["status"] = "feasible"
            return changed
        return row

    monkeypatch.setattr(main._store, "get_draft", patched)
    resp = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": pid, "max_double_weft": 0, "max_missing_weft": 0})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "pattern_repick_required"


def test_acceptance_input_validation(client):
    from app.main import _store
    draft_id = make_draft(_store, solid(A))
    pid = acc.pattern_identifier(4, 4, solid(A))
    base = (f"/api/drafts/{draft_id}/acceptance")
    files = {"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")}

    # 负的次数上限
    bad = client.post(base, files=files, data={
        "pattern_id": pid, "max_double_weft": -1, "max_missing_weft": 0})
    assert bad.status_code == 400
    # 非整数上限（统一 400，而不是框架默认 422）
    bad_text = client.post(base, files=files, data={
        "pattern_id": pid, "max_double_weft": "abc", "max_missing_weft": 0})
    assert bad_text.status_code == 400
    assert bad_text.json()["error"] == "validation_error"
    # 全透明试织图
    transparent = trial_png([[None] * 4, [None] * 4])
    bad2 = client.post(base, files={"trial": ("t.png", transparent, "image/png")},
                       data={"pattern_id": pid,
                             "max_double_weft": 0, "max_missing_weft": 0})
    assert bad2.status_code == 400
    # 宽度不符
    narrow = trial_png([[A, A, A], [A, A, A]])
    bad3 = client.post(base, files={"trial": ("t.png", narrow, "image/png")},
                       data={"pattern_id": pid,
                             "max_double_weft": 0, "max_missing_weft": 0})
    assert bad3.status_code == 400
    # 高度越界（单行）
    short = trial_png([[A] * 4])
    bad4 = client.post(base, files={"trial": ("t.png", short, "image/png")},
                       data={"pattern_id": pid,
                             "max_double_weft": 0, "max_missing_weft": 0})
    assert bad4.status_code == 400


def test_acceptance_unknown_draft_404(client):
    resp = client.post(
        "/api/drafts/9999/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": "x", "max_double_weft": 0, "max_missing_weft": 0})
    assert resp.status_code == 404


def test_points_supplement_then_acceptance_still_works(client):
    """验收期间可补录色点：补录后用最新纹版标识即可继续验收；
    若纹版随之改变，旧标识会被要求重领。"""
    from app.main import _store
    # 用求解器真实产一张条纹草案，再补录一个同色点
    from tests.test_app import png_bytes, stripes_grid
    ref = stripes_grid(8)
    resp = client.post(
        "/api/drafts",
        files={
            "reference": ("r.png", png_bytes(ref), "image/png"),
            "fragment": ("f.png", png_bytes(
                ref, transparent_pred=lambda x, y, c: True), "image/png"),
        },
        data={"unit_w": 4, "unit_h": 4})
    draft_id = resp.json()["draft_id"]
    pid_before = client.get(
        f"/api/drafts/{draft_id}/pattern.png").headers["X-Pattern-Id"]
    added = client.post(
        f"/api/drafts/{draft_id}/points",
        data={"cell_x": 0, "cell_y": 0, "color": "220,60,60"})
    assert added.status_code == 200
    pid_after = client.get(
        f"/api/drafts/{draft_id}/pattern.png").headers["X-Pattern-Id"]

    # 旧标识在纹版变化后必须被拒绝（受理与出结果两次核对的第一道）
    stale = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png([[A] * 4, [A] * 4]), "image/png")},
        data={"pattern_id": pid_before,
              "max_double_weft": 0, "max_missing_weft": 0})
    if pid_before != pid_after:
        assert stale.status_code == 409
        assert stale.json()["detail"]["error"] == "pattern_repick_required"

    # 用最新标识继续验收：按当前纹版前两行构造完美试织 -> 放行
    grid = storage.DraftStore.decode_grid(_store.get_draft(draft_id)["grid"])
    trial = [[list(grid[y][x]) for x in range(4)] for y in range(2)]
    ok = client.post(
        f"/api/drafts/{draft_id}/acceptance",
        files={"trial": ("t.png", trial_png(trial), "image/png")},
        data={"pattern_id": pid_after,
              "max_double_weft": 0, "max_missing_weft": 0})
    assert ok.status_code == 200
    assert ok.json()["verdict"] == "pass"

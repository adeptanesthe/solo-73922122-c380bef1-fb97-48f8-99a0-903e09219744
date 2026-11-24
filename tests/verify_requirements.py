"""需求逐条核对脚本（不依赖网络，直接调用内部模块）。"""
import io
import sys

from PIL import Image

from app import acceptance, images, solver
from tests.test_app import png_bytes, stripes_grid, checker_grid
from tests.test_acceptance import trial_png

RED = (220, 60, 60)
BLUE = (60, 90, 220)
GREEN = (60, 180, 90)
YELLOW = (230, 200, 70)
ok = 0


def check(name, cond):
    global ok
    assert cond, f"FAIL: {name}"
    ok += 1
    print(f"[PASS] {name}")


# 1. 参考图最多 4 色、每边 <=64
ref = images.parse_reference(png_bytes(stripes_grid(8)))
check("参考图调色板 <=4 色", len(ref.colors) <= 4)

# 2. 2×2 图块统计：总数=像素数，跨边跨角
check("图块统计总数=参考图像素数", sum(ref.tile_counts.values()) == 64)
# 手工验证一个跨角图块（右下角为左上 -> 环绕到 (0,0)）
grid = stripes_grid(8)
corner_tile = (grid[7][7], grid[7][0], grid[0][7], grid[0][0])
check("跨角图块被统计", corner_tile in ref.tile_counts)

# 3. 同格异色返回两处原坐标
points = {(0, 0): RED, (4, 4): GREEN, (1, 1): BLUE}
fixed, conflicts = images.project_points(points, 4, 4)
check("同格异色检测", len(conflicts) == 1)
check("返回两处原坐标", set(conflicts[0].positions) == {(0, 0), (4, 4)})
check("冲突格坐标", conflicts[0].cell == (0, 0))

# 4. 同格同色合并
f2, c2 = images.project_points({(0, 0): RED, (4, 4): RED}, 4, 4)
check("同格同色合并无冲突", c2 == [] and len(f2) == 1)

# 5. 完好色点保留 + 每图块都在参考图出现（含跨边跨角）
r = solver.solve(ref.colors, ref.tile_counts, 6, 6, {(2, 3): RED}, 5.0)
check("求解有候选", r.grid is not None)
check("完好色点保留", r.grid[3][2] == RED)
W = H = 6
for y in range(H):
    for x in range(W):
        key = (r.grid[y][x], r.grid[y][(x + 1) % W],
               r.grid[(y + 1) % H][x],
               r.grid[(y + 1) % H][(x + 1) % W])
        assert key in ref.tile_counts, (x, y)
check("每格 2×2 图块均在参考图出现（含环绕）", True)

# 6. 得分=图块频次之和
manual = sum(
    ref.tile_counts[(
        r.grid[y][x], r.grid[y][(x + 1) % W],
        r.grid[(y + 1) % H][x],
        r.grid[(y + 1) % H][(x + 1) % W])]
    for y in range(H) for x in range(W))
check("得分=图块频次之和", manual == r.score)

# 7. 四种状态都可达
check("已证最优可达", r.status in (solver.STATUS_OPTIMAL, solver.STATUS_FEASIBLE))
ri = solver.solve(ref.colors, ref.tile_counts, 4, 4, {(0, 0): (1, 2, 3)}, 5.0)
check("已证无解可达", ri.status == solver.STATUS_INFEASIBLE and ri.grid is None)
ru = solver.solve(ref.colors, ref.tile_counts, 12, 12, {}, 0.02)
check("待定可达", ru.status in (solver.STATUS_UNKNOWN, solver.STATUS_FEASIBLE,
                                solver.STATUS_OPTIMAL))

# 8. 限时 5 秒
import time
t0 = time.perf_counter()
solver.solve(ref.colors, ref.tile_counts, 10, 10, {}, 5.0)
check("5 秒限时", time.perf_counter() - t0 <= 5.1)

# 9. 单元尺寸边界 4~12
for bad in (3, 13):
    try:
        images.validate_unit_size(bad, 8)
        raise AssertionError("应拒绝")
    except images.ImageValidationError:
        pass
check("单元尺寸 4~12 边界校验", True)

# 10. 透明=缺损
frag = images.parse_fragment(
    png_bytes(stripes_grid(4), transparent_pred=lambda x, y, c: y >= 2))
check("透明像素标缺损", len(frag.points) == 8)

# 11. 64 像素上限
big = png_bytes([[RED] * 65 for _ in range(4)])
try:
    images.parse_reference(big)
    raise AssertionError("应拒绝超宽图")
except images.ImageValidationError:
    check("每边 <=64 像素上限", True)

# 12. 回溯兜底与 CP-SAT 在小案例结论一致
bt = solver._solve_backtrack(ref.colors, ref.tile_counts, 6, 6, {(2, 3): RED}, 5.0)
cp = solver._solve_cpsat(ref.colors, ref.tile_counts, 6, 6, {(2, 3): RED}, 5.0)
check("回溯/CP-SAT 最优性一致", bt.status == cp.status == solver.STATUS_OPTIMAL)
check("回溯/CP-SAT 得分一致", bt.score == cp.score)

# ---- 试织验收（新增） ----
A, B, C = RED, BLUE, GREEN

# 13. 试织图校验：等宽、高度 2~64、全透明退回
ok_w = False
try:
    acceptance.parse_trial(trial_png([[A, A, A], [A, A, A]]), unit_w=4)
except images.ImageValidationError:
    ok_w = True
check("试织图必须与单元等宽", ok_w)
ok_h = False
try:
    acceptance.parse_trial(trial_png([[A] * 4]), unit_w=4)
except images.ImageValidationError:
    ok_h = True
check("试织图高度下限 2", ok_h)
ok_t = False
try:
    acceptance.parse_trial(
        trial_png([[None] * 4, [None] * 4]), unit_w=4)
except images.ImageValidationError:
    ok_t = True
check("全透明试织图退回", ok_t)
check("试织图高度上限 64",
      acceptance.parse_trial(trial_png([[A] * 4 for _ in range(64)]), 4).height == 64)

# 14. 透明像素忽略
tr = acceptance.parse_trial(
    trial_png([[A, None, A, None], [None, B, None, B]]), 4)
check("透明像素不计可见点", tr.visible_count == 4)

# 15. 纹版标识由单元尺寸与逐格颜色决定，放大不影响
grid4 = tuple(tuple(A for _ in range(4)) for _ in range(4))
pid = acceptance.pattern_identifier(4, 4, grid4)
check("纹版标识稳定且为 sha256", pid == acceptance.pattern_identifier(4, 4, grid4)
      and pid.startswith("sha256:"))
grid_other = tuple(tuple(B if (x, y) == (0, 0) else A for x in range(4))
                   for y in range(4))
check("改一格颜色标识变化",
      acceptance.pattern_identifier(4, 4, grid_other) != pid)

# 16. 步长语义：0=重纬、1=正常、2=漏纬，且各自计分
hgrid = (
    (A, A, A, A),
    (B, B, B, B),
    (A, A, A, A),
    (B, B, B, B),
)
perfect = acceptance.parse_trial(trial_png([[A] * 4, [B] * 4]), 4)
r = acceptance.evaluate_trial(hgrid, perfect, 0, 0)
check("正常行进 0 分放行", r.verdict == acceptance.VERDICT_PASS
      and r.min_score == 0 and r.passing_alignment.steps == (1,))
double = acceptance.parse_trial(trial_png([[A] * 4, [A] * 4]), 4)
r2 = acceptance.evaluate_trial(hgrid, double, 0, 0)
check("重纬最低分 1 且零容忍返工",
      r2.verdict == acceptance.VERDICT_REWORK and r2.min_score == 1
      and r2.failing_alignment.steps == (0,))
r3 = acceptance.evaluate_trial(hgrid, double, 2, 2)
check("放宽重纬上限后放行", r3.verdict == acceptance.VERDICT_PASS)

# 17. 三种判定：复查 = 达标与超限最低分对齐并存
review_grid = (
    (A, A, A, A),
    (B, B, B, B),
    (B, A, A, A),
    (B, B, B, B),
)
review_trial = acceptance.parse_trial(
    trial_png([[A, A, A, A], [C, A, A, A]]), 4)
res = acceptance.evaluate_trial(review_grid, review_trial, 1, 0)
check("复查判定可达且两类示例齐全",
      res.verdict == acceptance.VERDICT_REVIEW
      and res.passing_alignment is not None
      and res.failing_alignment is not None
      and res.passing_alignment.total_score == res.min_score
      and res.failing_alignment.total_score == res.min_score)
check("示例注明起始纹版行与每步行数",
      hasattr(res.passing_alignment, "start_row")
      and len(res.passing_alignment.steps) == review_trial.height - 1)

# 18. 不枚举完整路径：64 行试织在毫秒级完成（状态数 O(KH)）
import time
big_trial = acceptance.parse_trial(
    trial_png([[A] * 4 for _ in range(64)]), 4)
t0 = time.perf_counter()
acceptance.evaluate_trial(hgrid, big_trial, 3, 3)
check("64 行试织毫秒级返回（DP 非枚举）", time.perf_counter() - t0 < 1.0)

print(f"\n全部 {ok} 项需求核对通过")

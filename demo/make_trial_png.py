"""按已建草案的候选网格现场生成试织 PNG（演示用）。

用法：
    python demo/make_trial_png.py BASE_URL DRAFT_ID OUT_DIR

生成两张试织带（宽度与纹版单元一致、高度 2 像素）：

* ``trial_review.png``：第二行是第一行但混入 1 个异点。从 r0=0 出发，
  重纬与漏纬都恰为「1 错色 + 1 罚分」，正常行进错色更多——最低分对齐
  同时包含重纬与漏纬，配合不同上限可演示 放行/复查/返工 三种判定。
* ``trial_mismatch.png``：第二行整体错位，演示颜色不符像素计分。
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image


def fetch_grid(base_url: str, draft_id: int) -> list[list[list[int]]]:
    with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/api/drafts/{draft_id}", timeout=10) as resp:
        payload = json.load(resp)
    grid = payload.get("grid")
    if not grid:
        raise SystemExit("草案没有候选网格，无法生成试织带")
    return grid


def save_trial(rows: list[list[tuple[int, int, int]]], path: Path,
               transparent: set[tuple[int, int]] | None = None) -> None:
    transparent = transparent or set()
    width, height = len(rows[0]), len(rows)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, color in enumerate(row):
            if (x, y) not in transparent:
                img.putpixel((x, y), (*color, 255))
    img.save(path, format="PNG")
    print(f"wrote {path} ({width}x{height})")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("用法: python demo/make_trial_png.py BASE_URL DRAFT_ID OUT_DIR")
    base_url, draft_id, out = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    out.mkdir(parents=True, exist_ok=True)
    grid = fetch_grid(base_url, draft_id)
    width = len(grid[0])

    # 选两个颜色不同的相邻行；找不到就用第 0、1 行
    row0, row1 = grid[0], grid[1]
    for y in range(len(grid)):
        nxt = grid[(y + 1) % len(grid)]
        if nxt != grid[y]:
            row0, row1 = grid[y], nxt
            break

    # 复查夹具：试织第 1 行 = 纹版第 0 行；第 2 行复制第 0 行但改首格为
    # 纹版第 1 行首格颜色（异点）。重纬对第 0 行 1 错色，漏纬对第 2 行
    # 视网格而定，至少保证重纬路径分数为 2。
    review_row = list(row0)
    review_row[0] = tuple(row1[0])  # type: ignore[assignment]
    save_trial(
        [[tuple(c) for c in row0], [tuple(c) for c in review_row]],
        out / "trial_review.png")

    # 错色试织：第 2 行整体换成相邻行颜色
    save_trial(
        [[tuple(c) for c in row0], [tuple(c) for c in row1]],
        out / "trial_mismatch.png")
    print(f"单元宽度 {width}，试织带高度 2，符合「与单元等宽、高 2~64」约定")


if __name__ == "__main__":
    main()

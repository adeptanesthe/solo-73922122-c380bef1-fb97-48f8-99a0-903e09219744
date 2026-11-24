"""生成演示用的参考 PNG 与残片 PNG。

运行后在 demo/ 目录生成：

* ``reference_stripes.png``  8×8 双色竖条纹参考图（图块种类少、容易最优）
* ``fragment_stripes.png``   同尺寸残片，部分像素透明表示缺损
* ``reference_checker.png``  8×8 棋盘格参考图（含跨边/跨角图块）
* ``fragment_checker.png``   棋盘格残片
* ``reference_conflict.png`` 6×6 参考图，用于制造同格异色冲突
* ``fragment_conflict.png``  周期投影后同格异色的残片
* ``trial_perfect.png``      与竖条纹纹版前两行一致的完美试织带（4 宽 2 行）
* ``trial_double_weft.png``  含重纬缺陷的试织带（两行相同，正常行进会错色）
* ``reference_hstripes.png`` 8×8 横向条纹参考图（相邻行异色，验收演示用）
* ``fragment_hstripes.png``  横向条纹残片

直接执行：``python demo/make_demo_data.py``
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

OUT_DIR = Path(__file__).resolve().parent

RED = (220, 60, 60, 255)
BLUE = (60, 90, 220, 255)
GREEN = (60, 180, 90, 255)
YELLOW = (230, 200, 70, 255)
TRANSPARENT = (0, 0, 0, 0)


def save(img: Image.Image, name: str) -> Path:
    path = OUT_DIR / name
    img.save(path, format="PNG")
    return path


def stripes_reference(size: int = 8) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            img.putpixel((x, y), RED if x % 2 == 0 else BLUE)
    return img


def stripes_fragment(size: int = 8) -> Image.Image:
    img = stripes_reference(size)
    # 右下半区挖空为透明缺损
    for y in range(size // 2, size):
        for x in range(size // 2, size):
            img.putpixel((x, y), TRANSPARENT)
    # 再挖几个零散格
    for x, y in [(1, 1), (3, 2), (6, 1)]:
        img.putpixel((x, y), TRANSPARENT)
    return img


def checker_reference(size: int = 8) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            img.putpixel((x, y), GREEN if (x + y) % 2 == 0 else YELLOW)
    return img


def checker_fragment(size: int = 8) -> Image.Image:
    img = checker_reference(size)
    for y in range(size):
        for x in range(size):
            # 保留两条对角带，其余缺损
            if not (abs(x - y) <= 1 or abs(x + y - (size - 1)) <= 1):
                img.putpixel((x, y), TRANSPARENT)
    return img


def conflict_artifacts(size: int = 6) -> tuple[Image.Image, Image.Image]:
    """参考图为纯色竖条；残片在同一投影格放两种颜色制造冲突。"""
    ref = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            ref.putpixel((x, y), RED if x % 2 == 0 else BLUE)

    frag = Image.new("RGBA", (size, size), TRANSPARENT)
    # 单元宽 4：像素 (0,0) 与 (4,0) 投影到同一格 (0,0)，故意异色
    frag.putpixel((0, 0), RED)
    frag.putpixel((4, 0), GREEN)  # GREEN 也在参考调色板之外，先触发同格冲突
    # 其余给一些正常完好色点
    for y in range(1, size):
        frag.putpixel((0, y), RED)
        frag.putpixel((1, y), BLUE)
    return ref, frag


def trial_perfect(unit_w: int = 4) -> Image.Image:
    """完美试织带：两行即横向条纹纹版的前两行（红行、蓝行），正常行进 0 分。"""
    img = Image.new("RGBA", (unit_w, 2))
    for x in range(unit_w):
        img.putpixel((x, 0), RED)
        img.putpixel((x, 1), BLUE)
    return img


def trial_with_hole(unit_w: int = 4) -> Image.Image:
    """带透明缺损像素的试织带，演示透明像素被忽略。"""
    img = trial_perfect(unit_w)
    img.putpixel((1, 0), TRANSPARENT)
    img.putpixel((2, 1), TRANSPARENT)
    return img


def trial_double_weft(unit_w: int = 4) -> Image.Image:
    """两行完全相同（均为纯红行）的试织带：对横向条纹纹版，正常行进对准
    蓝行有 unit_w 个错色，重纬对准红行只罚 1 分 -> 最低分对齐是重纬，
    (0,0) 上限下返工；放宽重纬上限后放行。"""
    img = Image.new("RGBA", (unit_w, 2))
    for x in range(unit_w):
        img.putpixel((x, 0), RED)
        img.putpixel((x, 1), RED)
    return img


def horizontal_stripes_reference(size: int = 8) -> Image.Image:
    """横向条纹参考图：相邻行颜色不同（红行/蓝行交替），用于验收演示。

    这种纹版里相邻纹版行异色，试织两行相同才能体现重纬/漏纬罚分与
    颜色错色之间的权衡，从而演示放行/复查/返工三种判定。
    """
    img = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            img.putpixel((x, y), RED if y % 2 == 0 else BLUE)
    return img


def horizontal_stripes_fragment(size: int = 8) -> Image.Image:
    """横向条纹残片：挖掉右下四分之一与若干零散格。"""
    img = horizontal_stripes_reference(size)
    for y in range(size // 2, size):
        for x in range(size // 2, size):
            img.putpixel((x, y), TRANSPARENT)
    for x, y in [(1, 1), (3, 2)]:
        img.putpixel((x, y), TRANSPARENT)
    return img


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    artifacts = [
        (stripes_reference(), "reference_stripes.png"),
        (stripes_fragment(), "fragment_stripes.png"),
        (checker_reference(), "reference_checker.png"),
        (checker_fragment(), "fragment_checker.png"),
        (trial_perfect(), "trial_perfect.png"),
        (trial_with_hole(), "trial_with_hole.png"),
        (trial_double_weft(), "trial_double_weft.png"),
        (horizontal_stripes_reference(), "reference_hstripes.png"),
        (horizontal_stripes_fragment(), "fragment_hstripes.png"),
    ]
    ref_conflict, frag_conflict = conflict_artifacts()
    artifacts.append((ref_conflict, "reference_conflict.png"))
    artifacts.append((frag_conflict, "fragment_conflict.png"))

    for img, name in artifacts:
        path = save(img, name)
        print(f"wrote {path} ({img.size[0]}x{img.size[1]})")

    # 简单自检：确认参考图不透明、残片含透明像素
    for _, name in artifacts[:4]:
        im = Image.open(OUT_DIR / name)
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        print(f"{name}: alpha range=({lo},{hi})")


if __name__ == "__main__":
    main()

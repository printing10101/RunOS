"""生成 PWA 图标（一次性工具，产物入库、脚本保留供换品牌色时重跑）。

图案取自 App 左上角品牌 logo：青绿渐变圆角方 + 深色跑道椭圆 + 高亮圆点。
输出 web/public/icons/icon-{192,512}.png 与 maskable-{192,512}.png（后者
图案缩小留安全边距，供 Android 自适应图标裁切）。
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "web" / "public" / "icons"
JADE = (63, 208, 164)       # --jade
JADE_DEEP = (23, 130, 103)  # --jade-deep
INK = (8, 18, 16)           # --bg-page


def draw_icon(size: int, safe_ratio: float) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 背景：对角渐变 jade → jade-deep 的圆角方（maskable 版铺满不圆角，交给系统裁）
    radius = 0 if safe_ratio >= 1 else int(size * 0.22)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(JADE[i] + (JADE_DEEP[i] - JADE[i]) * t) for i in range(3)) + (255,)
        d.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # 图案：按安全区比例缩放的跑道椭圆 + 高亮圆点（与品牌 logo 同构）
    c = size / 2
    s = size * safe_ratio
    ew, eh = s * 0.38, s * 0.23          # 椭圆半轴（横向跑道）
    d.ellipse([c - ew, c - eh, c + ew, c + eh], outline=INK, width=max(2, int(s * 0.045)))
    dot_r = s * 0.052
    dx, dy = c + ew * 0.62, c - eh * 1.55
    d.ellipse([dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r], fill=INK)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        draw_icon(size, 0.78).save(OUT / f"icon-{size}.png")          # 普通图标（自带圆角）
        draw_icon(size, 0.60).save(OUT / f"maskable-{size}.png")      # 安全区版（铺满底色）
    print("icons written to", OUT)


if __name__ == "__main__":
    main()

"""Draw the integration icon (a snowflake: the start-up heap is frozen) into custom_components/gc_tuning/brand/.

Run from the repo root with the dev venv:  .venv/bin/python tools/make_icon.py
Output: icon.png (256x256) and icon@2x.png (512x512), transparent background, no Home Assistant artwork.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "custom_components" / "gc_tuning" / "brand"
COLOR = (41, 182, 246, 255)  # light blue, readable on light and dark themes
SCALE = 2048  # drawn large, then reduced for smooth edges


def draw() -> Image.Image:
    img = Image.new("RGBA", (SCALE, SCALE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = SCALE / 2
    arm = SCALE * 0.42
    width = int(SCALE * 0.055)

    def point(angle: float, dist: float) -> tuple[float, float]:
        return c + dist * math.cos(angle), c + dist * math.sin(angle)

    for k in range(6):
        a = math.radians(60 * k - 90)
        d.line([point(a, 0), point(a, arm)], fill=COLOR, width=width)
        for frac, branch in ((0.52, 0.26), (0.78, 0.17)):
            base = point(a, arm * frac)
            for side in (-1, 1):
                b = a + side * math.radians(55)
                tip = (base[0] + branch * SCALE * math.cos(b) * 0.9, base[1] + branch * SCALE * math.sin(b) * 0.9)
                d.line([base, tip], fill=COLOR, width=width)
        for p in (point(a, arm), ):
            r = width * 0.9
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=COLOR)
    r = width * 1.6
    d.ellipse([c - r, c - r, c + r, c + r], fill=COLOR)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(OUT / "icon@2x.png", optimize=True)
    big.resize((256, 256), Image.LANCZOS).save(OUT / "icon.png", optimize=True)


if __name__ == "__main__":
    main()

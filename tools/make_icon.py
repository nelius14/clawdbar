#!/usr/bin/env python3
"""
Generate the menu bar Clawd icon from the claudepix 20x20 sprite.
Produces:
  clawd_icon.png      (1x, 18px)
  clawd_icon@2x.png   (2x, 36px) for retina
Both coloured (#CD7F6A) on transparent background.
"""

from PIL import Image

# The claudepix CREATURE sprite (20x20). 0=empty, 1=body, 2=eye
CREATURE = [
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0],
    [0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0],
    [0,0,0,0,0,1,1,2,1,1,1,1,1,2,1,1,0,0,0,0],
    [0,0,0,1,1,1,1,2,1,1,1,1,1,2,1,1,1,1,0,0],
    [0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,0,0],
    [0,0,0,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,0,0],
    [0,0,0,1,0,1,1,1,1,1,1,1,1,1,1,1,0,1,0,0],
    [0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0],
    [0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0],
    [0,0,0,0,0,1,1,1,1,1,1,1,1,1,1,1,0,0,0,0],
    [0,0,0,0,0,1,0,0,1,0,0,0,1,0,0,1,0,0,0,0],
    [0,0,0,0,0,1,0,0,1,0,0,0,1,0,0,1,0,0,0,0],
    [0,0,0,0,0,1,0,0,1,0,0,0,1,0,0,1,0,0,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
    [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
]

BODY = (205, 127, 106, 255)   # #CD7F6A
EYE  = (17, 17, 17, 255)      # #111111
EMPTY = (0, 0, 0, 0)

# Crop to the non-empty bounding box so the icon fills the menu bar nicely
def bounding_box(grid):
    rows = [r for r, row in enumerate(grid) if any(row)]
    cols = [c for r, row in enumerate(grid) for c, v in enumerate(row) if v]
    return min(rows), max(rows), min(cols), max(cols)

r0, r1, c0, c1 = bounding_box(CREATURE)
H = r1 - r0 + 1
W = c1 - c0 + 1

def render(scale):
    img = Image.new("RGBA", (W * scale, H * scale), EMPTY)
    px = img.load()
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            v = CREATURE[r][c]
            color = BODY if v == 1 else EYE if v == 2 else EMPTY
            if color == EMPTY:
                continue
            for dy in range(scale):
                for dx in range(scale):
                    px[(c - c0) * scale + dx, (r - r0) * scale + dy] = color
    return img

# Menu bar usable height ~18px. Sprite is ~16 tall after crop.
# 1x: scale so height ~= 18; 2x: double.
scale_1x = max(1, round(18 / H))
render(scale_1x).save("clawd_icon.png")
render(scale_1x * 2).save("clawd_icon@2x.png")

print(f"Cropped sprite: {W}x{H}")
print(f"1x: {W*scale_1x}x{H*scale_1x}px  (scale {scale_1x})")
print(f"2x: {W*scale_1x*2}x{H*scale_1x*2}px")

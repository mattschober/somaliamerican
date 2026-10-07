#!/usr/bin/env python3
"""Cut the red and yellow tower panels out of the Somali towers photo.

The page lays the photo down as a background under a 70% wash of the page's
light blue, which leaves the photo reading at 30%. This script produces the
layer that sits on top of that wash: the red and yellow panels of the Riverside
Plaza towers at full opacity, everything else transparent, so those windows pop
out of the washed-out photo behind them.

Panels are picked out in HSV: the reds sit near hue 250 at high saturation, the
yellows near hue 33. The search stops at 70% of the frame height so the orange
traffic cones and the pedestrians in the foreground stay washed out with the
rest of the photo.

The output keeps the source's exact pixel dimensions. Both layers are drawn
with the same background-size and background-position, so the panels land back
on the windows they came from at any viewport size.

Usage: python3 tools/build-background.py
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "somali-towers-schober-tech-advisory.jpg"
OUT = ROOT / "somali-towers-panels.png"

photo = Image.open(SRC).convert("RGB")
hsv = np.asarray(photo.convert("HSV")).astype(int)
hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
height, width = hue.shape

red = ((hue >= 238) | (hue <= 8)) & (sat >= 90) & (val >= 70)
yellow = (hue >= 22) & (hue <= 46) & (sat >= 150) & (val >= 55)
towers = np.arange(height)[:, None] * np.ones((1, width)) < 0.70 * height

panels = (red | yellow) & towers
# Grow by a pixel so the JPEG's soft panel edges come along with the panel.
grown = Image.fromarray((panels * 255).astype(np.uint8), mode="L")
panels = (np.asarray(grown.filter(ImageFilter.MaxFilter(3))) > 0) & towers

rgba = np.dstack([np.asarray(photo), (panels * 255).astype(np.uint8)])
# Transparent pixels keep their RGB otherwise, which PNG cannot compress well.
rgba[~panels, :3] = 0
Image.fromarray(rgba, mode="RGBA").save(OUT, optimize=True)
print(f"{OUT.name}: {panels.sum()} panel pixels kept, {width}x{height}")

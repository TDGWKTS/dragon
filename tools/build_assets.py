#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dragon Alley -- starter asset pack builder.

Turns the three AI-generated raw sheets in assets/raw/ into a reusable,
transparent sprite atlas + backdrop plate, plus an inline data-URI pack that
keeps dragon-alley.html a single self-contained file.

Outputs
  assets/sprites/*.png       individual cut-out sprites (for reuse / re-skinning)
  assets/atlas.png            packed RGBA sprite atlas
  assets/bg-alley.jpg         AI backdrop plate (opaque, watermark cropped out)
  assets/atlas.json           frame manifest + role mapping
  assets/atlas-inline.js      the same pack as a JS data-URI blob
  assets/CREDITS.md           provenance / license note

Usage
  python tools/build_assets.py            # build the assets/ outputs
  python tools/build_assets.py --inject  # also refresh the marked block in dragon-alley.html

The generated sprite sheets are RGB with a *painted* checkerboard, so there is
no real alpha channel to read.  We key the two checker colours, then keep only
the background regions that are connected to the image border (plus enclosed
checker pockets).  That protects near-white foreground details such as the
hero's white trainers or the grey lid of the metal bin.
"""

import base64
import glob
import json
import os
import sys
from collections import defaultdict

from PIL import Image, ImageChops, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "assets", "raw")
OUT = os.path.join(ROOT, "assets")
SPRITES = os.path.join(OUT, "sprites")
HTML = os.path.join(ROOT, "dragon-alley.html")

START = b"/* DA_ASSET_PACK_START */"
END = b"/* DA_ASSET_PACK_END */"

CHAR_NAMES = ["hero_red", "hero_teal", "thug_green", "boss_denim"]
PROP_NAMES = ["crate", "metal_bin", "trash_can", "lantern", "neon_sign", "cone"]

# Atlas layout: 4 hero columns (128x256) + 6 prop columns (96x128).
ATLAS_W, ATLAS_H = 576, 384
CHAR_CELL = (128, 256)
PROP_CELL = (96, 128)


def find_raw(pattern, label):
    hits = sorted(glob.glob(os.path.join(RAW, pattern)))
    if not hits:
        raise SystemExit("missing raw sheet for %s: %s" % (label, pattern))
    return hits[0]


def luminance(c):
    return (c[0] * 299 + c[1] * 587 + c[2] * 114) // 1000


def estimate_checker(rgb):
    """Find the two checkerboard tile colours from the empty top strip."""
    w, h = rgb.size
    px = rgb.load()
    samples = [px[x, y] for y in range(0, min(64, h), 2) for x in range(0, w, 3)]
    vals = sorted(luminance(c) for c in samples)
    lo, hi = vals[len(vals) // 20], vals[-len(vals) // 20]
    mid = (lo + hi) // 2
    bright = [c for c in samples if luminance(c) >= mid]
    dark = [c for c in samples if luminance(c) < mid]

    def mean(cs):
        n = max(1, len(cs))
        return tuple(sum(c[i] for c in cs) // n for i in range(3))

    return mean(bright), mean(dark)


def d2(c, t):
    a, b, d = c[0] - t[0], c[1] - t[1], c[2] - t[2]
    return a * a + b * b + d * d


def remove_specks(binary, min_area=48):
    """Drop tiny disconnected blobs left behind by noisy checker pixels."""
    w, h = binary.size
    data = binary.tobytes()
    parent, rank, area = [], [], []

    def new_node(n):
        i = len(parent)
        parent.append(i)
        rank.append(0)
        area.append(n)
        return i

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    all_runs, prev = [], []
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        runs, p = [], 0
        while True:
            s = row.find(b"\xff", p)
            if s < 0:
                break
            e = row.find(b"\x00", s)
            if e < 0:
                e = w
            runs.append((s, e, new_node(e - s)))
            p = e
        i = j = 0
        while i < len(prev) and j < len(runs):
            a0, a1, ai = prev[i]
            b0, b1, bi = runs[j]
            if b0 < a1 and a0 < b1:
                union(ai, bi)
            if a1 <= b1:
                i += 1
            else:
                j += 1
        all_runs.append(runs)
        prev = runs

    totals = defaultdict(int)
    for idx, n in enumerate(area):
        totals[find(idx)] += n
    small = set(r for r, n in totals.items() if n < min_area)
    out = bytearray(data)
    for y, runs in enumerate(all_runs):
        base = y * w
        for s, e, idx in runs:
            if find(idx) in small:
                out[base + s:base + e] = b"\x00" * (e - s)
    if small:
        print("  removed %d speck components" % len(small))
    return Image.frombytes("L", (w, h), bytes(out))


def make_alpha(rgb, label):
    """Return (rgba_image, alpha_binary)."""
    w, h = rgb.size
    px = rgb.load()
    light, dark_tile = estimate_checker(rgb)
    print("  checker colours: light=%s dark=%s" % (light, dark_tile))

    cand = bytearray(w * h)
    darkish = bytearray(w * h)
    key_th = 38 * 38
    dark_th = 24 * 24
    for y in range(h):
        base = y * w
        for x in range(w):
            c = px[x, y]
            dl, dd = d2(c, light), d2(c, dark_tile)
            spread = max(c) - min(c)
            if spread <= 34 and min(dl, dd) <= key_th:
                cand[base + x] = 1
                if dd <= dark_th and dl > dark_th:
                    darkish[base + x] = 1

    # Closing fills single-pixel noise holes in the checker region.
    ci = Image.frombytes("L", (w, h), bytes(bytearray(255 if v else 0 for v in cand)))
    ci = ci.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    ci = ci.point(lambda v: 255 if v > 127 else 0)
    mask = ci.tobytes()

    # Union-find over horizontal white runs, linked between adjacent rows.
    parent, rank, node_dark = [], [], []

    def new_node():
        i = len(parent)
        parent.append(i)
        rank.append(0)
        node_dark.append(0)
        return i

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    all_runs, prev, border_nodes = [], [], set()
    for y in range(h):
        row = mask[y * w:(y + 1) * w]
        runs, p = [], 0
        while True:
            s = row.find(b"\xff", p)
            if s < 0:
                break
            e = row.find(b"\x00", s)
            if e < 0:
                e = w
            idx = new_node()
            runs.append((s, e, idx))
            node_dark[idx] = darkish[y * w + s:y * w + e].count(1)
            if s == 0 or e == w or y == 0 or y == h - 1:
                border_nodes.add(idx)
            p = e
        i = j = 0
        while i < len(prev) and j < len(runs):
            a0, a1, ai = prev[i]
            b0, b1, bi = runs[j]
            if b0 < a1 and a0 < b1:
                union(ai, bi)
            if a1 <= b1:
                i += 1
            else:
                j += 1
        all_runs.append(runs)
        prev = runs

    border_roots = set(find(i) for i in border_nodes)
    comp_dark = defaultdict(int)
    for idx, val in enumerate(node_dark):
        if val:
            comp_dark[find(idx)] += val
    bg_roots = set(border_roots)
    # Enclosed checker pockets (e.g. between an arm and the torso) contain
    # dark checker tiles; a white shoe / metal highlight does not.
    for root, val in comp_dark.items():
        if root not in border_roots and val > 12:
            bg_roots.add(root)

    fg = bytearray(b"\xff" * (w * h))
    for y, runs in enumerate(all_runs):
        base = y * w
        for s, e, idx in runs:
            if find(idx) in bg_roots:
                fg[base + s:base + e] = b"\x00" * (e - s)

    a = Image.frombytes("L", (w, h), bytes(fg))
    # Erode one pixel to drop the light checker fringe, then feather the edge.
    a_bin = remove_specks(a.filter(ImageFilter.MinFilter(3)))
    a_soft = a_bin.filter(ImageFilter.GaussianBlur(0.5))
    # Colour decontamination: semi-transparent edge pixels are blends with the
    # pale checker, so darken them in proportion to their alpha.
    factor = a_soft.point(lambda v: int(160 + 95 * v / 255))
    f3 = Image.merge("RGB", (factor, factor, factor))
    clean = ImageChops.multiply(rgb, f3).convert("RGBA")
    clean.putalpha(a_soft)
    kept = sum(1 for v in a_bin.tobytes() if v)
    print("  foreground pixels kept: %d (%.1f%%)" % (kept, 100.0 * kept / (w * h)))
    return clean, a_bin


def segments(alpha, expected, label, min_w=70, merge_gap=45):
    """Find horizontal sprite slots from the binary alpha."""
    w, h = alpha.size
    cols = [alpha.crop((x, 0, x + 1, h)).getbbox() is not None for x in range(w)]
    raw = []
    start = None
    for x, v in enumerate(cols):
        if v and start is None:
            start = x
        if not v and start is not None:
            raw.append([start, x])
            start = None
    if start is not None:
        raw.append([start, w])
    merged = []
    for s, e in raw:
        if merged and s - merged[-1][1] < merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    segs = [(s, e) for s, e in merged if e - s >= min_w]
    print("  %s: found %d segments %s" % (label, len(segs), segs))
    if len(segs) != expected:
        print("  ! expected %d, falling back to %d equal slots" % (expected, expected))
        pad = w * 0.02
        step = (w - 2 * pad) / expected
        segs = [(int(pad + i * step), int(pad + (i + 1) * step)) for i in range(expected)]
    return segs


def cut_sprites(sheet, alpha, segs, names, label):
    w, h = sheet.size
    out = []
    for i, (s, e) in enumerate(segs):
        bb = alpha.crop((s, 0, e, h)).getbbox()
        if not bb:
            raise SystemExit("empty sprite %s in %s" % (names[i], label))
        y0, y1 = bb[1], bb[3]
        box = (max(0, s - 2), max(0, y0 - 2), min(w, e + 2), min(h, y1 + 2))
        sp = sheet.crop(box)
        sp.save(os.path.join(SPRITES, names[i] + ".png"), optimize=True)
        out.append((names[i], sp))
        print("    %-12s %dx%d" % (names[i], sp.width, sp.height))
    return out


def fit_into(atlas, sp, cell_x, cell_y, cw, ch):
    pad = 3
    im = sp.copy()
    im.thumbnail((cw - pad * 2, ch - pad * 2), Image.LANCZOS)
    x = cell_x + (cw - im.width) // 2
    y = cell_y + ch - pad - im.height       # bottom aligned: feet on the ground line
    atlas.paste(im, (x, y))
    return {"x": x, "y": y, "w": im.width, "h": im.height}


def main():
    inject = "--inject" in sys.argv
    os.makedirs(SPRITES, exist_ok=True)

    char_file = find_raw("One_horizontal_sprite_sheet_fo_*.png", "characters")
    prop_file = find_raw("One_horizontal_sprite_sheet_of_*.png", "props")
    bg_file = find_raw("Wide_panoramic*.png", "backdrop")

    sheets = []
    for path, seg_count, names, label in (
        (char_file, 4, CHAR_NAMES, "characters"),
        (prop_file, 6, PROP_NAMES, "props"),
    ):
        print("== %s: %s" % (label, os.path.basename(path)))
        rgb = Image.open(path).convert("RGB")
        rgba, alpha = make_alpha(rgb, label)
        segs = segments(alpha, seg_count, label)
        sheets.append(cut_sprites(rgba, alpha, segs, names, label))

    atlas = Image.new("RGBA", (ATLAS_W, ATLAS_H), (0, 0, 0, 0))
    frames = {}
    for i, (name, sp) in enumerate(sheets[0]):
        frames[name] = fit_into(atlas, sp, i * CHAR_CELL[0], 0, *CHAR_CELL)
    for i, (name, sp) in enumerate(sheets[1]):
        frames[name] = fit_into(atlas, sp, i * PROP_CELL[0], 256, *PROP_CELL)
    atlas_path = os.path.join(OUT, "atlas.png")
    atlas.save(atlas_path, optimize=True)

    # Backdrop: keep only the skyline / shopfront band.  This also crops off
    # the "AI生成 WORKBUDDY" watermark in the bottom-right corner and the
    # waist-high wall that would otherwise sit across the play area.
    bg = Image.open(bg_file).convert("RGB")
    crop_h = int(bg.height * 0.715)
    bg = bg.crop((0, 0, bg.width, crop_h))
    bg = bg.resize((1152, 548), Image.LANCZOS)
    bg_path = os.path.join(OUT, "bg-alley.jpg")
    bg.save(bg_path, "JPEG", quality=84, optimize=True, progressive=True)

    manifest = {
        "version": 1,
        "generator": "tools/build_assets.py",
        "atlas": {"w": ATLAS_W, "h": ATLAS_H},
        "backdrop": {"w": bg.width, "h": bg.height},
        "frames": frames,
        "roles": {
            "hero": "hero_red",
            "hero2": "hero_teal",
            "brawler": "thug_green",
            "carrier": "thug_green",
            "brute": "boss_denim",
            "boss": "boss_denim",
        },
        "props": {"crate": "crate", "bin": "trash_can"},
        "decor": ["lantern", "neon_sign", "cone", "metal_bin"],
        "options": {
            "skinFighters": False,     # opt-in: AI idle sprite instead of procedural calm poses
            "backdropAlpha": 0.72,     # AI vista dimmed so the neon shopfronts read on top
            "hideCity": True,          # procedural far skyline is redundant under the AI plate
            "hideShops": False,        # keep the near shopfront parallax for depth
        },
    }
    with open(os.path.join(OUT, "atlas.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(atlas_path, "rb") as f:
        a_b64 = base64.b64encode(f.read()).decode("ascii")
    with open(bg_path, "rb") as f:
        b_b64 = base64.b64encode(f.read()).decode("ascii")
    pack = {
        "v": 1,
        "atlas": "data:image/png;base64," + a_b64,
        "backdrop": "data:image/jpeg;base64," + b_b64,
        "manifest": manifest,
    }
    js = "var DA_ASSET_PACK = " + json.dumps(pack, separators=(",", ":"), ensure_ascii=False) + ";\n"
    with open(os.path.join(OUT, "atlas-inline.js"), "w", encoding="utf-8", newline="\n") as f:
        f.write(js)

    credits = """# Asset credits / provenance

Generated starter sprite pack for Dragon Alley (2026-11-10).

| File | Source | Notes |
| --- | --- | --- |
| `raw/One_horizontal_sprite_sheet_fo_*.png` | AI image generation (WorkBuddy ImageGen) | 4 full-body fighters |
| `raw/One_horizontal_sprite_sheet_of_*.png` | AI image generation (WorkBuddy ImageGen) | 6 street props |
| `raw/Wide_panoramic*.png` | AI image generation (WorkBuddy ImageGen) | night alley backdrop plate |

All three raw sheets were generated for this project, then processed by
`tools/build_assets.py` (checkerboard removal, cut-out, downscale, packing).
The backdrop plate is cropped above the wall line, which also removes the
generator watermark.

**Licence note:** AI-generated output generally has no third-party attribution
requirement, but the terms of the generation service apply. If you later mix in
Kenney / OpenGameArt / itch.io packs, check each pack's licence (CC0, CC-BY,
GPL are all common there) and add the required attribution here. Keep this file
in the repository when redistributing.

Rebuild with:

```
python tools/build_assets.py --inject
```
"""
    with open(os.path.join(OUT, "CREDITS.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(credits)

    print("== sizes")
    for p in (atlas_path, bg_path, os.path.join(OUT, "atlas-inline.js")):
        print("  %-24s %8d bytes" % (os.path.basename(p), os.path.getsize(p)))

    if inject:
        with open(HTML, "rb") as f:
            data = f.read()
        a, b = data.find(START), data.find(END)
        if a < 0 or b < 0 or b <= a:
            raise SystemExit("asset pack markers not found in dragon-alley.html")
        data = data[:a + len(START)] + b"\n" + js.encode("utf-8") + b"  " + data[b:]
        with open(HTML, "wb") as f:
            f.write(data)
        print("  injected into dragon-alley.html (%d bytes)" % len(data))
    else:
        print("  (run with --inject to refresh the inline pack in the HTML)")


if __name__ == "__main__":
    main()

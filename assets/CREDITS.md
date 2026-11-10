# Asset credits / provenance

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

## Merged build (v7 + starter pack)

The deployed `index.html` is a merge of two visual layers:

* **Characters / weapons / food / crates:** the user's v7 sprite sheets, embedded
  directly in the HTML as `SPR_DATA` (7 `data:image/png;base64` sheets). These are
  authoritative for every fighter state. `drawFighterVec` / `drawPropVec` remain as
  the last-resort vector fallback.
* **Far city vista, neon decor, title rivals, select hero cards:** this starter
  pack (`DA_ASSET_PACK` -> `AssetStore`). The manifest keeps
  `skinFighters: false`, so the AI fighter sprites are opt-in for A/B comparison
  only and never replace the v7 characters by default.

`--inject` only rewrites the block between `DA_ASSET_PACK_START` and
`DA_ASSET_PACK_END`; it does not touch `SPR_DATA` or the v7 sprite renderer.

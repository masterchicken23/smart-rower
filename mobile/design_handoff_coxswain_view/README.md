# Handoff: Smart Rower — Coxswain Dashboard (v3)

## Overview
This replaces the **Coxswain Dashboard** screen in the existing Smart Rower React Native app. Landing and Developer screens are unchanged; do not modify them.

What changes from the current dashboard:
- **Standard crew is two rowers: Stroke (Rower 1, jetson-01) and Bow (Rower 2, jetson-02).** Build the layout as a list of seats so it also works for 3 or 4 rowers, but ship with 2 as the default.
- Each rower gets **their own slide lane** instead of two rails in one shared box.
- Every rower has a **HANDS** box. Every rower except Stroke has a **SYNC** box. Stroke's SYNC slot shows a muted "REFERENCE" box.
- The top bar shows **elapsed time** and **stroke rate** only. The STROKES counter is removed. PAUSE and RESET move into the top bar (landscape) or a bottom bar (portrait).
- The screen now supports **landscape and portrait**. The dashboard must stop forcing landscape.

## About the Design Files
`Coxswain Dashboard.dc.html` is a **design reference built in HTML**. It is not production code. Recreate it in the existing React Native project using its patterns (`View`/`Text`/`Pressable`, `StyleSheet`, `react-native-reanimated` for the moving dots). Open the HTML file in a browser to see both orientations animating live; the switcher at the top previews 2/3/4 rowers. `support.js` is only the prototype runtime.

## Fidelity
**High-fidelity.** Colors, type, sizes and spacing are final. All sizes are in points at 844 × 390 (landscape) and 390 × 844 (portrait). Respect safe areas.

## Global
- Font **Barlow** 600/700/800 (already in the project). Tabular figures for all numbers.
- Background `#FFFFFF`. Keep the screen awake.
- Orientation: allow both on this screen. Choose the layout with `useWindowDimensions()`: `width > height` → landscape, else portrait. Switch layouts instantly; the live animation must keep running across rotation.

## Seats model
Order top to bottom: Stroke first, then toward the bow.

| Rowers | Seats (name / dot letter / sub) |
|---|---|
| 2 (default) | STROKE / S / Rower 1 · BOW / B / Rower 2 |
| 3 | STROKE / S / Rower 1 · 2 SEAT / 2 / Rower 2 · BOW / B / Rower 3 |
| 4 | STROKE / S / Rower 1 · 3 SEAT / 3 / Rower 2 · 2 SEAT / 2 / Rower 3 · BOW / B / Rower 4 |

Rower N maps to jetson-0N. Rows divide the available height equally (`flex: 1` each).

---

## Landscape layout (844 × 390)
Screen padding: 16 top, 36 horizontal, 20 bottom. Vertical gap 10 between top bar and body.

### Top bar (height 58)
Three columns `1fr | auto | 1fr`, vertically centered.
- Left: "ELAPSED" (12pt 700, letterSpacing 1.4, `#5A5C57`), value `m:ss` below (32pt 700, lineHeight 32), gap 2.
- Center: stroke rate (64pt 800, lineHeight ~58, letterSpacing −1.3) and "SPM" (16pt 700, letterSpacing 1.6, `#5A5C57`) on one baseline, gap 8.
- Right, right-aligned, gap 8: **PAUSE** and **RESET** buttons, each 44 tall, paddingHorizontal 16, radius 12, 15pt 800, letterSpacing 0.9.
  - PAUSE: bg `#0E0F0C`, white. When paused it reads **RESUME**, bg `#0B5E30`.
  - RESET: bg `#F2F1ED`, text `#0E0F0C`, pressed `#E4E2DC`.

### Body grid
Columns `78 | flex | 146 | 146`, columnGap 10. Rows: header row 14 tall, then one `flex: 1` row per seat, rowGap 6.

**Header row:** empty | "CATCH" left and "FINISH" right (paddingHorizontal 4) | "HANDS" | "SYNC". All 11pt 700, letterSpacing 1.5, `#5A5C57`. CATCH/FINISH swap when catch side is Right.

**Column 0, seat label** (vertically centered): name 16pt 800, letterSpacing 0.6; sub "Rower N" 12pt 600 `#5A5C57`; gap 1.

**Column 1, slide lane** (one per seat): bg `#F7F6F3`, radius 12, clip children.
- End zones: left 8% and right 8% of the lane width, `#ECEAE4`, full height.
- Track area inset 18 horizontally (so dots don't clip). Rail: 6 tall, radius 3, `#DCDAD3`, vertically centered, full track width.
- **Stroke lane:** a 28 circle, `#0E0F0C`, white letter "S" (12pt 800), centered on the rail at the stroke's x.
- **Other lanes**, drawn in this order (bottom to top):
  1. Gap bar between stroke x and this rower's x: 6 tall, radius 3. `#0B5E30` when |stroke − rower| ≤ 0.05 (normalized), else `#C8410B`.
  2. Ghost marker at the **stroke's** x: 4 × 24, radius 2, `#B9B7B0`.
  3. Rower dot: 28 circle, white fill, 3pt `#0E0F0C` ring, black letter (12pt 800).
- x = pos × trackWidth when catch is Left; (1 − pos) × trackWidth when Right. pos is the normalized seat position, 0 = catch, 1 = finish.

**Columns 2 and 3, feedback boxes:** radius 14, paddingVertical 8, paddingHorizontal 12, content vertically centered, gap 3. Main text 18pt 800 lineHeight 18, single line. Sub text 11pt 600. No label inside the box (the column header labels it).

---

## Portrait layout (390 × 844)
Screen padding: 54 top, 22 horizontal, 26 bottom. Vertical gap 18 between the four blocks below.

### 1. Metrics (top)
Row, space-between, aligned to bottom.
- Left: "SPM" label (12pt 700, letterSpacing 1.4, `#5A5C57`), rate below (88pt 800, lineHeight ~76, letterSpacing −1.8), gap 2.
- Right, right-aligned: "ELAPSED" label, `m:ss` below (34pt 700).

### 2. Lanes
Column, gap 6.
- Label row: "CATCH" left, "FINISH" right, 11pt 700, letterSpacing 1.5, `#5A5C57`, paddingLeft 38, paddingRight 4.
- One row per seat: grid `30 | flex`, gap 8, vertically centered. Column 0 is the dot letter (S/B/2/3, 14pt 800). Column 1 is the lane: 36 tall, radius 10, same zones, rail, ghost and gap-bar rules as landscape, with track inset 14, dots 24 (no letter inside), ghost 4 × 22.

### 3. Feedback grid (`flex: 1`)
Columns `66 | flex | flex`, gap 8. Header row 14 tall: empty | "HANDS" | "SYNC" (11pt 700, letterSpacing 1.5, `#5A5C57`). Then one `flex: 1` row per seat.
- Seat label: name 15pt 800, letterSpacing 0.45; sub "Rower N" 12pt 600 `#5A5C57`.
- Boxes: radius 14, paddingVertical 8, paddingHorizontal 11; main 18pt 800; sub 11pt 600; gap 3; vertically centered.

### 4. Controls (bottom, height 60)
Two equal columns, gap 10. PAUSE/RESUME and RESET, radius 14, 17pt 800, letterSpacing 1. Same colors as landscape.

---

## Feedback states
| Box | State | Main | Sub | bg / text |
|---|---|---|---|---|
| HANDS | good | GOOD | Level at finish | `#E3F2E8` / `#0B5E30` |
| HANDS | too low | RAISE HANDS | Low at finish | `#C8410B` / `#FFFFFF` |
| HANDS | too high | LOWER HANDS | High at finish | `#C8410B` / `#FFFFFF` |
| SYNC (non-stroke) | \|offset\| < 25 ms | IN SYNC | ±N ms vs stroke | `#E3F2E8` / `#0B5E30` |
| SYNC (non-stroke) | offset > 0 | LATE CATCH / LATE FINISH | +N ms vs stroke | `#C8410B` / `#FFFFFF` |
| SYNC (non-stroke) | offset < 0 | EARLY CATCH / EARLY FINISH | −N ms vs stroke | `#C8410B` / `#FFFFFF` |
| SYNC (stroke) | always | REFERENCE | Crew syncs to stroke | `#F2F1ED` / `#5A5C57` |

Boxes update once per stroke so they stay readable. Keep 25 ms (sync) and 0.05 (gap bar) as named constants.

## Interactions & Behavior
- Dots, ghosts and gap bars animate at 60 fps from Reanimated shared values, interpolating between Jetson samples. No React re-render per frame.
- PAUSE freezes elapsed time and the display; RESUME continues. RESET zeros elapsed time and resets all boxes to GOOD / IN SYNC.
- Elapsed time ticks once per second. Rate (SPM) updates once per stroke, from Stroke's stroke events.
- Catch side setting (`Left` | `Right`) mirrors x positions and swaps the CATCH/FINISH labels in both orientations.

## State
- `rowerCount`: 2 (default) | 3 | 4. Derive the seats list from it.
- `session`: `{ running, startedAt, elapsedMs, rateSpm }`.
- `live[rowerN]`: normalized position, shared value.
- `feedback[rowerN]`: `{ hands: 'good'|'raise'|'lower', sync?: { kind: 'in'|'early'|'late', point: 'CATCH'|'FINISH', ms } }`. No `sync` for Rower 1.
- `catchSide`: `'Left' | 'Right'`.

Sync for each non-stroke rower is computed against Stroke: offset = rower catch_t − stroke catch_t (likewise finish). Positive = late. The data contract from the previous handoff still applies; add one unit per rower (j01…j04).

## Design tokens
- Ink `#0E0F0C` · Muted `#5A5C57`
- Surface `#FFFFFF` · Lane `#F7F6F3` · Neutral fill `#F2F1ED` · Pressed `#E4E2DC` · End zone `#ECEAE4` · Rail `#DCDAD3` · Ghost `#B9B7B0`
- Good bg `#E3F2E8` / fg `#0B5E30` (also RESUME bg, in-range gap bar) · Alert `#C8410B` with white text
- Radii: 14 (boxes, portrait buttons), 12 (landscape lanes and buttons), 10 (portrait lanes), 3 (rails, gap bars)
- Type (Barlow): 88 / 64 / 34 / 32 / 18 / 17 / 16 / 15 / 14 / 12 / 11

## Assets
None. No images or icons.

## Files
- `Coxswain Dashboard.dc.html` — live reference of both orientations (defaults to 2 rowers). The logic class at the bottom contains the simulated motion, sync math and state labels.
- `support.js` — prototype runtime, only needed to open the HTML.

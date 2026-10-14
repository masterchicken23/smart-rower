# Handoff: Smart Rower — Coxswain App

## Overview
React Native app (phone, **landscape only**) for a senior-design rowing system. Two Jetson units, one per rower (Stroke = Rower 1, Bow = Rower 2), each with an ultrasonic sensor (seat position) and a camera (hand height). The app shows the coxswain where each rower is on the slide, flags hand height, and tells the bow whether they are in sync with the stroke at the catch and finish. A Developer screen shows the raw Jetson stream and unit connection status.

Flow: **Landing → Coxswain Dashboard** and **Landing → Developer**.

## About the Design Files
`Smart Rower Flow v2.dc.html` is a **design reference built in HTML**. It shows the intended look and behavior. It is not production code to copy. Rebuild these screens in React Native (Expo recommended if no project exists yet) using normal RN patterns: `View`/`Text`/`Pressable`, `StyleSheet`, React Navigation for screens. Open the HTML file in a browser to see the live, animated prototype. `support.js` is only the prototype's runtime and is not needed in the app.

## Fidelity
**High-fidelity.** Colors, type, sizes, spacing and interactions are final. Recreate them closely. All sizes below are in points, for an 844 × 390 landscape screen (iPhone 14/15). Scale proportionally for other devices and respect safe areas (notch side).

## Global
- Orientation: lock to landscape (`expo-screen-orientation` or `app.json` `"orientation": "landscape"`). Keep the screen awake on the Dashboard (`expo-keep-awake`).
- Font: **Barlow** 500/600/700/800 (`@expo-google-fonts/barlow`). Terminal font: **JetBrains Mono** 400/600.
- All numbers use tabular figures (`fontVariant: ['tabular-nums']`).
- Screen background `#FFFFFF`. Screen padding: 18 top, 36 horizontal, 20 bottom. Vertical gap between the top bar and the body: 12.

---

## Screen 1: Landing
**Purpose:** Pick a mode.

**Layout:** padding 40/48, two-column row (`flex:1` | 340 wide), vertically centered, gap 40.
- Left: wordmark "SMART" / "ROWER" on two lines, Barlow 800, 92pt, lineHeight ~79 (0.86), letterSpacing −1.8, color `#0E0F0C`.
- Right: two stacked buttons, gap 14. Each is 112 tall, radius 22, paddingHorizontal 26, row with space-between: a title and subtitle on the left, "→" (30pt, 700) on the right.
  - **COXSWAIN VIEW**: bg `#0E0F0C`, text `#FFFFFF`; pressed bg `#2A2B28`. Title 26pt 800, letterSpacing 0.8. Subtitle "Live feedback on the water" 15pt 600.
  - **DEVELOPER**: bg `#F2F1ED`, text `#0E0F0C`; pressed bg `#E4E2DC`. Subtitle "Raw Jetson stream", color `#5A5C57`.

**Navigation:** Coxswain View → Dashboard. Developer → Developer.

---

## Screen 2: Coxswain Dashboard
**Purpose:** Live feedback during a piece.

### Top metrics row (height 60)
Three columns: `1fr | auto | 1fr`, vertically centered.
- Left: label "ELAPSED" (12pt 700, letterSpacing 1.4, `#5A5C57`), value `m:ss` (32pt 700).
- Center: stroke rate (66pt 800, letterSpacing −1.3), then "SPM" (16pt 700, letterSpacing 1.6, `#5A5C57`) on the same baseline, gap 8.
- Right, right-aligned: "STROKES" label and stroke count (same styles as the left column).

### Body grid
Columns: `88 | flex | 148 | 148`, columnGap 12. Rows: `16 | 1fr | 1fr`, rowGap 8.

**Row 0, rails column:** "CATCH" on the left and "FINISH" on the right (11pt 700, letterSpacing 1.5, `#5A5C57`). These swap when the catch-side setting is Right.

**Column 0 (rower labels):** vertically centered in each row. "STROKE" / "BOW" 18pt 800, letterSpacing 0.7, with "Rower 1" / "Rower 2" below (13pt 600, `#5A5C57`), gap 2.

**Column 1 (the rails), spanning both rower rows.** Container bg `#F7F6F3`, radius 16. Inner track area is inset 16 horizontally so the dots don't clip at the ends.
- End zones: the first 8% and last 8% (plus the 16 inset) are shaded `#ECEAE4`, to emphasize the catch and finish.
- Catch and finish guide lines: 2 wide, `#B9B7B0`, full height minus 10 top and bottom.
- Stroke rail at 25% height, bow rail at 75%. Each is 6 tall, radius 3, `#DCDAD3`.
- **Stroke dot:** 32 circle, `#0E0F0C`, white "S" (13pt 800), centered on the stroke rail at x = its normalized position.
- **Bow dot:** 32 circle, white fill, 3pt `#0E0F0C` ring, "B" in black, on the bow rail.
- **Ghost marker** on the bow rail at the *stroke's* x: 4 × 26, radius 2, `#B9B7B0`, drawn under the bow dot.
- **Gap bar** on the bow rail between stroke x and bow x: 6 tall. `#0B5E30` when |stroke − bow| ≤ 0.05 (normalized), otherwise `#C8410B`.
- x = pos × width when the catch is on the Left; (1 − pos) × width when it is on the Right.
- Animate at 60 fps without React re-renders. Use `react-native-reanimated` shared values driven by incoming samples, and interpolate between samples (the Jetsons will likely send at 20–50 Hz).

**Feedback boxes** (radius 16, padding 11/13, content spaced top to bottom): a label (11pt 700, letterSpacing 1.5), the main text (23pt 800, lineHeight ~23.5, wraps to 2 lines), and a sub line (12pt 600).
- Row 1 (Stroke), column 2: **HANDS**.
- Row 1 (Stroke), column 3: **Session controls**, two stacked buttons (gap 8, radius 14, 18pt 800, letterSpacing 1):
  - PAUSE: bg `#0E0F0C`, white text, with a pause icon (two bars, 5 × 16, gap 4). When paused it becomes **RESUME**: bg `#0B5E30`, with a play triangle.
  - RESET: bg `#F2F1ED`, text `#0E0F0C`.
- Row 2 (Bow), column 2: **HANDS**.
- Row 2 (Bow), column 3: **SYNC**. The stroke has no sync box because the stroke sets the rhythm; bow is measured against stroke.

### Feedback states
| Box | State | Main | Sub | bg / text |
|---|---|---|---|---|
| HANDS | good | GOOD | Level at finish | `#E3F2E8` / `#0B5E30` |
| HANDS | too low | RAISE HANDS | Low at finish | `#C8410B` / `#FFFFFF` |
| HANDS | too high | LOWER HANDS | High at finish | `#C8410B` / `#FFFFFF` |
| SYNC | \|offset\| < 25 ms | IN SYNC | ±N ms vs stroke | `#E3F2E8` / `#0B5E30` |
| SYNC | offset > 0 | LATE CATCH / LATE FINISH | +N ms vs stroke | `#C8410B` / `#FFFFFF` |
| SYNC | offset < 0 | EARLY CATCH / EARLY FINISH | −N ms vs stroke | `#C8410B` / `#FFFFFF` |

Boxes update once per stroke, not continuously, so they stay readable. The 25 ms threshold is a placeholder; make it a constant.

---

## Screen 3: Developer
**Purpose:** Debug the hardware: raw stream, connection status, position traces.

### Top bar (height 40, row, gap 12)
- "← Home" button: bg `#F2F1ED`, radius 12, paddingHorizontal 14, 15pt 700. Goes back to Landing.
- "DEVELOPER" title: 20pt 800, letterSpacing 0.8, flex 1.
- One status pill per unit: height 40, radius 12, paddingHorizontal 14, 13pt 700, with a 9pt status dot.
  - Connected: bg `#E3F2E8`, text/dot `#0B5E30`, text "STROKE · jetson-01 · 12 ms" (round-trip latency).
  - Disconnected: bg `#C8410B`, white text/dot, text "BOW · jetson-02 · NO SIGNAL".

### Body grid
Columns `1.15fr | 1fr`, rows `1fr | 1fr`, gap 12/14.

**Terminal** (left column, spans both rows): bg `#0E0F0C`, radius 16, JetBrains Mono.
- Header (34 tall, 1pt bottom border `#2A2B28`): connection URL (11pt, `#8A8C86`) and two small buttons, **pause/resume** and **clear** (24 tall, 1pt border `#3A3B37`, radius 6, 11pt, `#E7E5DF`).
- Log: newest line at the bottom, 11pt, lineHeight 14. Columns: timestamp `HH:MM:SS.mmm` (`#6E706A`), source tag `j01` / `j02` / `sync` / `sys` (24 wide), message.
- Message colors: normal `#C9C7C0`, sync events `#7FD1A0`, warnings `#F08A5D`.
- Keep a ring buffer (~500 lines) and render only the visible tail (use `FlatList` with `inverted` or a fixed tail slice).
- Example lines from the prototype:
  - `j01  seat=0.624  us=567mm  hand=+3mm  DRIVE`
  - `j01  STROKE #242  rate=28spm  period=2143ms`
  - `sync bow catch offset +40ms  → LATE`
  - `j02  heartbeat timeout (2000 ms), retrying…` (warning)

**Traces** (right column, Stroke on top, Bow below): each card is bg `#F7F6F3`, radius 16.
- Header top-left: "STROKE" / "BOW" (15pt 800) + "jetson-01" / "jetson-02" (13pt 600, `#5A5C57`). "FINISH" label top-right and "CATCH" bottom-right (10pt 700, `#5A5C57`).
- Plot area: inset 14 left, 70 right, 30 top, 22 bottom. Horizontal guide lines (2pt, `#DCDAD3`) at finish (top) and catch (bottom).
- Line: the last 3 s of normalized position (y: 1 = top/finish, 0 = bottom/catch), 3pt `#0E0F0C`, scrolling right to left. The newest sample sits at the right edge, marked by a 24 dot (S filled / B ringed, same style as the rails).
- The bow card also draws the stroke's trace as a dashed line (2pt, `#B9B7B0`, dash 5/5) underneath.
- Use `react-native-svg` for the paths (or `@shopify/react-native-skia` for smoother 60 fps).
- When a unit is disconnected, cover its card with `rgba(247,246,243,0.82)` and a centered "NO SIGNAL" badge (bg `#C8410B`, white, radius 10, 14pt 800).

---

## Data contract (suggested — confirm with the Jetson code)
The phone connects to each Jetson over WebSocket (prototype shows `ws://192.168.55.1:8765`) or to a single hub that merges both units. Suggested JSON per sample:
```json
{ "unit": "j01", "t": 1727712271218, "seat_mm": 567, "seat_norm": 0.624, "hand_mm": 3, "phase": "DRIVE" }
```
Per stroke event:
```json
{ "type": "stroke", "unit": "j01", "n": 242, "rate_spm": 28, "catch_t": 1727712271000, "finish_t": 1727712271780 }
```
- **Normalization:** each rower's slide length differs. Map raw ultrasonic distance to 0 (catch) … 1 (finish) per rower, using that rower's min/max from calibration or a rolling window of the last N strokes. This is what the rails and traces draw.
- **Sync:** bow offset = bow catch_t − stroke catch_t (and likewise for the finish). Positive means late. Compute it on the Jetson or in the app, but the two units' clocks must be synced (NTP/PTP, or timestamp everything at a single hub).
- **Hands:** the camera gives hand height relative to a reference line at the finish → good / raise / lower, with a tolerance constant.
- **Connection:** heartbeat every 500 ms. Mark a unit disconnected after 2000 ms of silence and log a warning.

## State
- `session`: `{ running: boolean, startedAt, elapsedMs, strokeCount, rateSpm }`. Pause stops the elapsed time and freezes the display. Reset zeros elapsed time and strokes.
- `units[j01|j02]`: `{ connected, latencyMs, lastSeenAt }`.
- `live[j01|j02]`: current normalized position as a Reanimated shared value (not React state).
- `history[j01|j02]`: ring buffer of `(t, pos)` covering 3 s, for the traces.
- `feedback`: `{ strokeHands, bowHands, bowSync: { kind: 'in'|'early'|'late', point: 'CATCH'|'FINISH', ms } }`, updated per stroke.
- `log`: ring buffer of raw lines, plus a `streamPaused` flag.
- Settings: `catchSide: 'Left' | 'Right'`.

## Design tokens
- Ink `#0E0F0C` · Muted `#5A5C57` · Pressed ink `#2A2B28`
- Surface `#FFFFFF` · Subtle `#F7F6F3` · Neutral fill `#F2F1ED` · Zone `#ECEAE4` · Track `#DCDAD3` · Border `#E4E2DC` · Guide `#B9B7B0`
- Good bg `#E3F2E8` / Good fg `#0B5E30` · Alert `#C8410B` on `#FFFFFF`
- Terminal: bg `#0E0F0C`, border `#2A2B28`/`#3A3B37`, text `#E7E5DF`/`#C9C7C0`, dim `#6E706A`/`#8A8C86`, ok `#7FD1A0`, warn `#F08A5D`
- Radii: 42 (screen), 22 (landing buttons), 16 (cards/boxes), 14 (control buttons), 12 (pills), 6 (terminal buttons)
- Type: 92 / 66 / 32 / 26 / 23 / 20 / 18 / 16 / 15 / 13 / 12 / 11 / 10, all Barlow except the terminal

## Assets
No images or icons. The pause and play icons are simple shapes (bars and a triangle); the arrows are the text glyph "→".

## Files
- `Smart Rower Flow v2.dc.html` — the full prototype: Landing, Dashboard, Developer, and a sheet of box states. Open it in a browser. The logic class at the bottom shows the simulated motion model, sync math and log format.
- `support.js` — the prototype's runtime (only needed to open the HTML).

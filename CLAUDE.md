# Airborne Survey Flight Planner — working notes

Tkinter GUI that generates airborne survey flight patterns over a fire/target area and
exports waypoints for ForeFlight and a Honeywell FMS.

## Reference docs in this repo

- **[foreflight.md](foreflight.md)** — how ForeFlight ingests waypoints and overlays.
  **Read this before touching any export format, filename, or waypoint-naming code.**
  Covers the User Waypoints vs User Map Layers distinction, the `user_waypoints.csv`
  format rules, KML/KMZ support, content pack structure, and subscription gating. Claims
  are confidence-marked (⭐ verified on a ForeFlight page / ⚠ search summary only /
  ❓ unknown) — preserve those markings when editing, and don't promote a ⚠ to a fact
  without re-checking the source.
  - Note: `support.foreflight.com` returns 403 to automated fetching; use the
    `foreflight.com/support/...` pages instead.
  - It documents what the app now exports for ForeFlight (KML, KMZ, content pack) and the
    KML gotchas: coordinates are `lon,lat`, colours are `aabbggrr`, only `StyleMap`'s
    `normal` style is read. It also lists the mismatches deliberately **not** fixed —
    check that list before "fixing" an export.

## Export invariants

Everything for a run lands in `./<AREA>/`, including `<AREA>_plan.json` so the folder is
self-describing and reloadable.

- Both waypoint CSVs are built to match **sample flight plans the pilot supplied** — that
  is the authority, above ForeFlight's web docs. See the ✈ section of foreflight.md. Tests
  assert each rule; do not "tidy" these formats:
  - Waypoints are named `<prefix>L<n>S` / `<prefix>L<n>F` (Start/Finish of line n), two per
    line, **no waypoint on the turns**. This replaced sequential `CLM01…` naming.
  - Honeywell latitude degrees are 2 digits, **longitude degrees 3 and zero-padded**, and
    there is **no trailing whitespace** (a trailing space used to be emitted).
  - ForeFlight coordinates carry 8 decimals, and `Description` is `NA` — his working file
    uses `NA`, so ForeFlight's `""` suggestion is not binding.
- `navdata/user_waypoints.csv` inside the content pack **must keep that exact filename**;
  ForeFlight will not recognise it otherwise, and it fails silently rather than erroring.
- `build_survey_kml` must emit only elements from ForeFlight's documented subset. A test
  asserts this — check any new KML element against the list in foreflight.md first.
- The waypoint CSVs were byte-frozen against the original pre-refactor output up until the
  margin fix, which **deliberately moved every waypoint** (see below). The old coordinates
  are no longer the reference; regenerate a baseline before the next geometry change.

## Pattern shape — "mow the lawn"

**Survey flying means equal-length parallel lines.** The `Rectangular Box` checkbox is
**on by default** and clips passes to the smallest rectangle enclosing the padded target at
the requested heading: every line runs the full width, all the same length, spread 1.00× at
every heading.

Unchecking it clips to the padded target *outline* instead. That is minimum-area and covers
less ground, but it produced lines of 2.84 / 20.95 / 2.84 km on the default area — a 7.4×
spread, an hourglass with two useless stub lines. It is kept as an option, **not a default**.
Do not make outline clipping the default again.

`Repeats` (1–4, default 1) flies the whole box again from the top, so line directions and
sensor geometry are identical each cycle. Line numbering runs straight through — 3 lines
twice gives `1L1S`…`1L6F` — so names stay unique and in flight order. The transit back to
line 1 is implicit in the polyline and carries no waypoint.

Both controls are saved in `<AREA>_plan.json` and round-trip; `repeats` is clamped to 1–4
on load in case the file was hand-edited.

## Coverage geometry — the margin fix

`perimeter_margin_km` used to be unreliable and heading-dependent. Two causes, both fixed:

1. **Pivot mismatch.** The polygon was rotated about *its own* centroid but the bounding
   rectangle was rotated back about *the rectangle's* centroid — points up to ~0.6 km apart
   on the default area. That translated the whole coverage region, so the effective margin
   varied with heading (measured 3.94 km min against a 5.00 km request; heading 090 was the
   only value that looked correct, because there the rotation angle is zero). All rotations
   in `build_rectangular_pattern` now share one explicit `pivot`. **Do not reintroduce
   `origin='centroid'` there** — it re-evaluates per geometry, which is the bug.
2. **What gets padded.** The margin is applied by buffering the *target*, and the clip
   region is derived from that buffered shape — so the padding is baked in before the
   rectangle is taken, and the box therefore clears the target by at least the margin on
   every side. `buffer()` uses `quad_segs=64` because the default 8 inscribes the arc and
   lands ~10 m *inside* the requested margin.

Consequences to keep in mind:

- The returned first value is the **region actually flown** — a rectangle in box mode, the
  buffered outline (hundreds of vertices) otherwise. Display code must not assume either.
- A concave boundary can split a pass row into several segments, so rows are grouped and
  sequenced per row rather than by sorting a flat segment list.
- `measure_clearance()` samples the target outline and reports the padding actually
  achieved; the summary panel and status line show it and flag a shortfall. Trust that
  number over the requested value.
- Lat/lon offsets shift the coverage region and therefore eat margin on the trailing side.
  The lat offset default was changed from `0.025` to `0.0` so the request holds by default;
  a nonzero offset is reported as SHORT rather than silently accepted.

## Environment

Mamba env is **`survey`** (not the `flightplan` name in the README):

```
C:\Users\Cory\.local\share\mamba\envs\survey\python.exe
```

`matplotlib` and `Pillow` are **not** installed. The preview pane is drawn with plain
Tkinter canvas primitives specifically to avoid adding them — don't reach for matplotlib.

## Architecture notes

- `airborne_survey_gui.py` is the whole app: geometry helpers at module level, then
  `FlightPlannerGUI`.
- Geometry works in **UTM metres**, projected per-run from the boundary centroid. The
  preview draws in those metres so the aspect ratio stays true — never scale raw lat/lon
  to pixels.
- In box mode (the default) boundary *shape* is discarded — only extent along and across
  the heading matters. Verified: a 40-vertex perimeter and its 12-point convex hull give
  survey areas within 0.02%. So a high-fidelity perimeter buys almost nothing here; the
  convex hull is enough. Do **not** shortcut to 4 lat/lon corners though — an axis-aligned
  lat/lon box is not the heading-rotated box, and it inflated area 57% and added a line.
- The boundary grid is capped at **10 rows**, which is already too few for a real
  perimeter's convex hull (needed 12 in testing).
- Waypoint naming is driven by `flown_segments`, which `build_rectangular_pattern` returns
  **oriented along the direction each pass is flown** so the exporter can label the ends
  Start/Finish without re-deriving the sequence. Keep that orientation if you touch it.
- `_export_csv_files` returns the `(name, lat, lon)` list it writes, and the preview labels
  come from that same list — keep it that way so the pane can't disagree with the CSVs.
- `__init__` calls `calculate_and_render()` once at startup, so output exists before the
  user clicks anything. The status line under the button carries a timestamp and run
  counter so a repeat click is visibly registered.
- The label checkboxes are view-only: they call `_draw_preview` off cached geometry and
  must not trigger a recalculation or rewrite the CSVs.
- `ToolTip` gives hover help (Tkinter has none built in). It binds with `add="+"` so it
  cannot displace a widget's existing `command`/handler — keep that if you extend it. All
  three checkboxes and the Repeats dropdown carry one; the text explains the *trade-off*,
  not the label. `_show` re-checks `winfo_exists()` because a pending `after` can fire
  after the widget is gone.

## Verification habits that have paid off here

- The exported CSVs are the deliverable — after any change to the geometry or export path,
  diff them against a known-good baseline. Several changes so far were confirmed
  byte-identical this way.
- The GUI can be driven headlessly: instantiate `FlightPlannerGUI`, call `app.update()`,
  then inspect `app.stats_text`, `app._preview`, and `app.preview_canvas.find_all()`.
  Canvas items can be dumped to SVG to eyeball the drawing without a screenshot.
- `tkinterweb` was removed because its Tkhtml engine cannot execute Leaflet's JavaScript,
  so the embedded Folium map only ever rendered a blank pane. Don't reintroduce it; the
  Folium map opens in the system browser instead.

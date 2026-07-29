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

`calculate_and_render` writes six files. Two are load-bearing contracts:

- The Honeywell CSV and the legacy `<AREA>_waypoints_foreflight.csv` are **byte-frozen**.
  They have been verified byte-identical to the original pre-refactor output through every
  change so far — diff them against a baseline after touching geometry or export code.
- `navdata/user_waypoints.csv` inside the content pack **must keep that exact filename**;
  ForeFlight will not recognise it otherwise, and it fails silently rather than erroring.

`build_survey_kml` must emit only elements from ForeFlight's documented subset. A test
asserts this — if you add a KML element, check it against the list in foreflight.md first.

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
- `build_rectangular_pattern` reduces the boundary to a **rotated bounding rectangle**
  (`rotated_poly.bounds`), so boundary *shape* is discarded and only extent along/across
  the heading survives. Verified: a 40-vertex perimeter and its 12-point convex hull give
  survey areas within 0.02%, and the box came out 4.06× the actual polygon area. Passes are
  clipped against `rotated_rect`, not the true `rotated_poly` — pointing that intersection
  at the polygon instead is what would make accurate perimeter data worth having.
- The boundary grid is capped at **10 rows**, which is already too few for a real
  perimeter's convex hull (needed 12 in testing).
- `_export_csv_files` returns the `(name, lat, lon)` list it writes, and the preview labels
  come from that same list — keep it that way so the pane can't disagree with the CSVs.
- `__init__` calls `calculate_and_render()` once at startup, so output exists before the
  user clicks anything. The status line under the button carries a timestamp and run
  counter so a repeat click is visibly registered.
- The label checkboxes are view-only: they call `_draw_preview` off cached geometry and
  must not trigger a recalculation or rewrite the CSVs.

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

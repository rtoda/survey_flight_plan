# ForeFlight Import Reference

Notes gathered from ForeFlight's own documentation on 2026-07-29, for getting survey
flight lines and fire/custom points onto an iPad.

**Confidence marking:** ✈ = matched against a working flight-plan file the pilot supplied —
**the highest authority here, since it is what actually gets loaded and flown.**
⭐ = read directly off a ForeFlight page (see Sources). ⚠ = seen only in a search-engine
summary of a ForeFlight page, not confirmed on the page itself. ❓ = not found; unknown.

`support.foreflight.com/hc/...` returns **HTTP 403** to automated fetching. The
`foreflight.com/support/...` pages are readable and cover the same ground — start there.

---

## ✈ The formats the pilot actually flies

Taken from two sample flight plans he supplied, both sitting in the project root:
`GIII FireSense sample Honeywell FP.csv` and `GIII FireSense sample ForeFlight FP.csv`.
His words: *"coordinate formats I need each time we go flying"* — so these override anything
inferred from the web docs. The exporter is built to match them, and a test reads the real
files and compares our rows against them structurally.

The two samples are unrelated missions (one off the Carolina coast, one in Utah); they
demonstrate format, not content.

### Waypoint naming — `1L1S` / `1L1F`

`<block>L<line><S|F>`: **S**tart and **F**inish of each survey line. Two waypoints per
line, and **no waypoint on the turns** between lines. That is what makes the plan readable
from the operator seat — you know you are on line 3 running to its finish, not staring at
`CLM06`.

We emit `<Line ID Prefix>L<n>S` / `...F`, prefix defaulting to `1`. A concave area that
splits one pass row into two straight runs numbers them as separate lines, so a name always
brackets exactly one straight run.

### Honeywell FMS CSV

```
E,WPT,FIX,LAT,LON
X,1L1S,NA,N 36 44.53,W 076 38.02
X,1L1F,NA,N 35 26.62,W 076 29.63
```

- Column 1 is a literal `X` and the FMS **ignores it**.
- `FIX` is `NA`.
- Latitude: sign, space, **2-digit** degrees, space, minutes to 2 dp, zero padded.
- Longitude: sign, space, **3-digit zero-padded** degrees (`076`, not `76`), same minutes.
- **No trailing whitespace.** We used to emit `N 43 36.77 ` with a trailing space.
- Precision ceiling is inherent: 0.01 minute ≈ **18.5 m**. Nothing to fix — it is the
  format the box takes.

### ForeFlight CSV

```
Waypoint,Description,LAT,LONG
1L1S,NA,38.41664435,-112.5471749
```

- Header matches what we already wrote.
- `Description` is **`NA`** in his working file — so ForeFlight's own suggestion of `""`
  for an empty description is not a requirement, and our `NA` was right all along.
- Coordinates carry **7–8 decimal places**. We were writing 4 (≈11 m of needless
  rounding); now 8.
- Filename in his workflow is descriptive, not `user_waypoints.csv` — he is presumably
  importing by a route that tolerates it, or renaming. We still put the exactly-named copy
  in the content pack.

He also asked directly for a KML of the pattern, which the layer export covers.

## The distinction that matters

ForeFlight treats imported geographic data as one of two different things:

| | **User Waypoints** | **User Map Layers** |
|---|---|---|
| What it is | Named points entered into your waypoint database | A display overlay of shapes/lines/points |
| Navigable? | **Yes** — enterable in the FPL, sequences as legs, gives distance/time to next | **No** — passive picture |
| Persists? | Yes, until you delete them | Toggleable layer, removable |
| Formats | CSV, KML ⭐ | KML, KMZ ⭐ (GeoJSON in content packs ⭐) |

**How ForeFlight decides, on KML import:** files containing shapes/lines/polygons become
**User Map Layers**; files containing *only points* become **User Waypoints**. You are
prompted to choose at import time. ⭐

That prompt is the direct answer to "can I show points without them becoming waypoints":
a KML holding only Points will be offered as waypoints, so to get a pure overlay the file
needs to carry shape geometry (or be imported as a layer explicitly).

### For an instrument operator watching the lines

- **Route from user waypoints** is the only option giving *active* navigation state —
  current leg, distance/time to the next turn. Cost: every vertex lands permanently in the
  waypoint database (at 0.75 km swath this project generates 56 of them).
- **KML/KMZ map layer** draws the whole pattern at once, toggles off, leaves the waypoint
  database clean. Cost: no sequencing, no distance-to-next.
- A **mix** — overlay for the full pattern, real waypoints only for line entry points —
  is the obvious middle ground but is *inference, not documented*.

---

## User waypoint CSV format

- Filename **must be exactly `user_waypoints.csv`** ⭐⚠ — corroborated by two sources but
  both trace to the same support article. This is the single most likely reason an import
  silently does nothing.
- Column **order** matters; column **names do not** ⚠ — so a header row is fine:
  1. Name/ID  2. Description  3. Latitude  4. Longitude  5. Elevation (ft, optional)
- Name rules ⭐ (confirmed on two separate pages):
  - **All capital letters**
  - At least 3 characters, including at least one letter
  - Single word, no spaces (`THE_CABIN` ✓, `The Cabin` ✗)
- Coordinates: **decimal degrees only**, numeric, negative = West/South. No other
  coordinate format is supported. ⭐
- Description: optional; use `""` when empty. Only the first ~30–40 characters display. ⭐
- Elevation: optional, feet, no unit symbol. Improves flight-plan accuracy and lets
  Profile View show climbs/descents from user waypoints. ⚠

Documented example rows ⭐:

```
MY_HOUSE,My house,38.1,-79.2
ANOTHER,"",39.22,-81.966
PROPERTY_ALPHA,First house to observe,40.588,-80.1295
```

**Import path is restricted:** CSV can be imported **only** via a Content Pack, or
iTunes/Finder. ⭐ CSV is *not* AirDrop-able. KML *is*. If you want to AirDrop straight to
the iPad, KML is the format to use.

---

## KML / KMZ

Supported KML subset ⭐:

- Geometry: `Point`, `LineString`, `LinearRing`, `Polygon`, `MultiGeometry`
- Styling: `Style`, `StyleMap`, `LineStyle`, `PolyStyle`, `IconStyle`
- Google extension namespace, incl. `gx:LabelVisibility`

Import methods for KML ⭐: **AirDrop**, email/hyperlink → "Copy to ForeFlight", or
iTunes/Finder file sharing.

**KMZ** (from ForeFlight 11.0, Jan 2019) ⭐ adds over plain KML:
- custom icons not natively supported by ForeFlight
- bundling several KML layers in one file
- rich text formatting in a placemark description

KMZ works both as a Custom Map Layer and inside a Content Pack. ⭐

Size limits: ⚠ one search summary gave **< 200 MB** for v11.0+ and **< 4 MB** for older
versions. Not confirmed on any page fetched directly; the map-shapes page mentions only
that large files render slowly. Treat as unverified.

---

## Content Packs

A ZIP containing a parent folder with up to three subfolders (at least one required) ⭐:

| Folder | Holds |
|---|---|
| `navdata` | custom waypoints (KML or CSV) plus linked PDF/TXT |
| `layers` | georeferenced charts and vector data — MBTiles, FBTiles, geospatial PDF, KML, GeoJSON |
| `byop` | procedure plates and airport diagrams (PDF) |

Optional `manifest.json` alongside them; **all fields optional** ⭐:

```json
{
  "name": "string",
  "abbreviation": "string",
  "version": 1,
  "expirationDate": "YYYYMMDDThh:mm:ss",
  "effectiveDate": "YYYYMMDDThh:mm:ss",
  "organizationName": "string"
}
```

Dates are ISO with a required `T`; append `Z` for Zulu. ⭐

Install via AirDrop, email, Safari download, custom URL scheme hyperlink, Finder
(macOS 10.15+), iTunes (older macOS), or Cloud Documents (Essential tier and above). ⭐
Installed packs appear under **More > Custom Content**. ⭐

A content pack is therefore the way to ship survey lines as an overlay *and* the waypoint
CSV together in one AirDrop-able file.

---

## Subscription gating

- User waypoints, custom map layers, custom charts, content packs: **individual Plus plans
  and above; Business/MFB Performance and above.** ⭐
- Content packs specifically: "available to all individual subscribers and Performance
  level or above Business and MFB subscribers." ⭐ — note this wording is *looser* than the
  user-content page's "Plus plans" and the two may conflict. Worth confirming against your
  own plan before relying on it.

---

## What this repo now exports

Each run of `airborne_survey_gui.py` writes three ForeFlight-targeted files, one per
transfer path. Built by `build_survey_kml()` and `_export_foreflight_bundle()`.

| File | Import path | Becomes |
|---|---|---|
| `<AREA>_survey.kml` | AirDrop, email, "Copy to ForeFlight" | User Map Layer |
| `<AREA>_survey.kmz` | same, zipped — survives mail clients that mangle raw XML | User Map Layer |
| `<AREA>_foreflight_pack.zip` | AirDrop/email → **More > Custom Content** | layer **+** waypoints |

The KML carries a `Polygon` and a `LineString` as well as `Point`s, so ForeFlight should
classify it as a **map layer** rather than offering the points as user waypoints. That
classification rule is documented ⭐; that our specific file triggers the layer branch is
**inferred, not observed in the app.**

Content pack layout produced:

```
<AREA>_survey/
├── manifest.json                       name, abbreviation (= waypoint prefix), version, effectiveDate
├── layers/<AREA>_survey.kml            the overlay
└── navdata/user_waypoints.csv          correctly-named waypoint CSV
```

A test asserts the KML uses **only** elements from ForeFlight's documented subset, that
coordinates are emitted `lon,lat`, and that every waypoint name is all-caps / no spaces /
≥3 chars / contains a letter.

### Still not addressed

1. **The `<AREA>_waypoints_foreflight.csv` keeps its descriptive name**, matching the
   pilot's own workflow; the exactly-named `user_waypoints.csv` copy lives inside the
   content pack for the documented import route.
2. **Elevation column is unused** in both CSVs. Adding it would improve flight-plan accuracy
   and enable Profile View — relevant for a survey flown at fixed altitude. The pilot's
   samples do not use it either.
3. `SendToForeFlight.ipynb` still has its own separate `foreflightmobile://maps/search?q=`
   route URL path, untouched by any of this.
4. ❓ Whether the pilot's FMS cares that a survey line's Start/Finish pair are consecutive
   waypoints with no turn fix between them over a long transit. His samples do it, so
   presumably not.

## KML gotchas worth remembering

- **Coordinates are `lon,lat`** — longitude first, the opposite of this app's internal
  ordering. Easy to invert silently.
- **Colours are `aabbggrr`**, not `rrggbb` — channel order is reversed from CSS and the
  alpha comes first. `kml_colour()` does the conversion.
- **`StyleMap`: only the `normal` style is read** ⭐, so plain `<Style>` is safer.
- **`gx:labelVisibility`** is a boolean child of `<LineStyle>` that draws the Placemark
  `<name>` along a LineString. Google's canonical spelling is lower-case `l`; ForeFlight's
  docs write "gx:LabelVisibility". We emit Google's spelling — ❓ if the track label never
  appears in the app, that capitalisation is the first thing to try flipping.

## Open questions

- ❓ Whether a header row is *actually* skipped, or parsed as a waypoint. (The pilot's
  samples both carry one, so it is evidently tolerated on his import path.)
- ❓ Real KML/KMZ size limits.
- ❓ Max user waypoint count.
- ❓ Which plan tier statement governs, given the two pages differ.
- ❓ Whether Watch Duty exposes any public perimeter data (not investigated).

## Sources beyond the web docs

The pilot's two sample flight plans, in the **project root**:

- `GIII FireSense sample Honeywell FP.csv`
- `GIII FireSense sample ForeFlight FP.csv`

Read these directly rather than trusting the transcription above. The Honeywell row
skeleton was verified identical to ours, digit for digit:

```
pilot: X,#L#S,NA,N ## ##.##,W ### ##.##
ours:  X,#L#S,NA,N ## ##.##,W ### ##.##
```

## Sources

- [ForeFlight — Importing User Waypoints](https://foreflight.com/support/user-waypoints/)
- [ForeFlight — User Map Shapes](https://foreflight.com/support/user-map-shapes/)
- [ForeFlight — User Content](https://foreflight.com/support/user-content/)
- [ForeFlight — Content Packs Support](https://foreflight.com/support/content-packs/)
- [ForeFlight — KMZ File Support](https://foreflight.com/enhancements/kmz-file-support)
- [ForeFlight Support — User Waypoints in bulk via CSV](https://support.foreflight.com/hc/en-us/articles/206356598-How-can-User-Waypoints-be-added-in-bulk-using-a-CSV-file) (403 to automated fetch)
- [ForeFlight Support — 1.3 Creating a User Waypoint File](https://support.foreflight.com/hc/en-us/articles/23405941254679-1-3-Creating-a-User-Waypoint-File-to-Import-Waypoints-in-Bulk) (403 to automated fetch)

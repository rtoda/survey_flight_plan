# Airborne Survey Flight Planner

A small GUI toolset to generate airborne survey flight paths and export waypoints for ForeFlight and Honeywell FMS. The repo includes two Tkinter-based GUIs: `airborne_survey_gui.py` and `airborne_survey_gui2.py`.

## Features
- Interactive Tkinter GUI to define survey boundary points and flight parameters
- Generates an optimized rectangular survey pattern based on swath width, overlap, heading, and margins
- Exports two CSV formats: ForeFlight-friendly and Honeywell-friendly
- Draws a scaled flight-path preview directly in the window (native Tk canvas — no browser engine needed)
- Saves an interactive Folium HTML map, opened on demand via **Open Interactive Map in Browser**
- `airborne_survey_gui2.py` additionally generates a ForeFlight QR link for iPad transfer

## Files of interest
- `airborne_survey_gui.py` — main GUI (scaled path preview drawn in-window; Folium map opens in a browser)
- `airborne_survey_gui2.py` — GUI with QR code and separate HTML map backup
- `requirements.txt` — pinned dependencies used by notebooks and scripts

## Dependencies
The primary Python dependencies (also listed in `requirements.txt`) include:

- numpy
- shapely
- folium
- pandas
- qrcode

Additional packages required by the GUIs but not always present in `requirements.txt`:

- pyproj
- Pillow (PIL) — only needed by the QR-code variant

On macOS, `tkinter` is usually provided by the system Python. If using Conda, install `tk` via Conda or ensure the selected Python includes Tk support.

## Quick install
1. (Recommended) Create and activate a Python environment:

```bash
conda create -n flightplan python=3.11 -y
conda activate flightplan
```

2. Install pinned deps:

```bash
pip install -r requirements.txt
# install missing extras
pip install pyproj Pillow
```

If you prefer `pipenv` or `venv`, create an environment and use the same `pip install` commands.

## Usage

Run either GUI script and interactively enter boundary waypoints (minimum 3), set parameters, then click the generate button.

```bash
python airborne_survey_gui.py
# or the QR-enabled variant
python airborne_survey_gui2.py
```

Outputs created in the current working directory:

- `<AREA>_waypoints_foreflight.csv` — waypoint list in ForeFlight's column order
- `<AREA>_waypoints_honeywell.csv` — Honeywell FMS formatted CSV
- `<AREA>_flight_path.html` — Folium map (open with the button, or double-click the file)
- `<AREA>_survey.kml` / `.kmz` — survey pattern as a ForeFlight **map layer**: AirDrop or
  email it and the lines, buffer and named points display as a toggleable overlay without
  entering the waypoint database
- `<AREA>_foreflight_pack.zip` — ForeFlight **content pack** bundling the overlay *and* a
  correctly-named `user_waypoints.csv`. This is the one to send the pilot: he gets the same
  overlay plus real waypoints he can build a route from and override.

Use **Show Export Files** in the app to open the folder for AirDropping.

See [foreflight.md](foreflight.md) for the import rules these files are built against,
including which claims are verified against ForeFlight's docs and which are not.

## Notes & Tips
- Ensure the waypoint prefix is exactly 3 characters (the GUI enforces this).
- Provide at least 3 boundary points to define the survey polygon.
- The GUI runs one calculation at startup, so the preview and CSVs already exist before you
  click anything. The status line under the button stamps each run so repeat clicks are visible.
- Tile-backed maps need real JavaScript; use **Open Interactive Map in Browser** for that view.
- The QR workflow embeds a `foreflightmobile://` link which opens ForeFlight on an iPad when scanned.

## Example
Open `airborne_survey_gui2.py`, fill coordinates and parameters, then click `Generate Flight Plan`. The right panel will show a QR code and the file registry will list the produced CSV and HTML files.

## License
Add your preferred license here.

---
Generated from analysis of the repository Python scripts.

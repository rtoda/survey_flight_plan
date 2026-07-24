# Airborne Survey Flight Planner

A small GUI toolset to generate airborne survey flight paths and export waypoints for ForeFlight and Honeywell FMS. The repo includes two Tkinter-based GUIs: `airborne_survey_gui.py` and `airborne_survey_gui2.py`.

## Features
- Interactive Tkinter GUI to define survey boundary points and flight parameters
- Generates an optimized rectangular survey pattern based on swath width, overlap, heading, and margins
- Exports two CSV formats: ForeFlight-friendly and Honeywell-friendly
- Saves an interactive Folium HTML map of the flight path
- `airborne_survey_gui2.py` additionally generates a ForeFlight QR link for iPad transfer

## Files of interest
- `airborne_survey_gui.py` — main GUI (map embedded in window, uses `tkinterweb`)
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
- Pillow (PIL)
- tkinterweb (for embedded HTML rendering in `airborne_survey_gui.py`)

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
pip install pyproj Pillow tkinterweb
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

- `<AREA>_waypoints_foreflight.csv` — ForeFlight importable CSV
- `<AREA>_waypoints_honeywell.csv` — Honeywell FMS formatted CSV
- `<AREA>_flight_path.html` or `temp_survey_map.html` — Folium map backup / preview

## Notes & Tips
- Ensure the waypoint prefix is exactly 3 characters (the GUI enforces this).
- Provide at least 3 boundary points to define the survey polygon.
- If `tkinterweb` fails to render the embedded map, open the generated HTML file in a browser.
- The QR workflow embeds a `foreflightmobile://` link which opens ForeFlight on an iPad when scanned.

## Example
Open `airborne_survey_gui2.py`, fill coordinates and parameters, then click `Generate Flight Plan`. The right panel will show a QR code and the file registry will list the produced CSV and HTML files.

## License
Add your preferred license here.

---
Generated from analysis of the repository Python scripts.

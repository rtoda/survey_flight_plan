"""Check that web/survey_engine.py produces the same files as the desktop app.

The web engine is a GUI-free copy of the desktop app's logic, so the two can drift. This
drives the real desktop app (a Tk window appears briefly) and the engine over the same
plans and compares the waypoint CSVs, the KML, the route link and the summary's figures. Timestamps are masked;
everything else must match byte for byte.

Run from the repository root:  python tests/parity_check.py
Works in a temporary directory, so plans/ is not touched.
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web"))

BOUNDARY = [
    {"lat": "43.703", "lon": "-116.089", "label": "Boise_Peak"},
    {"lat": "43.640", "lon": "-116.062", "label": "Aldape_Summit"},
    {"lat": "43.5949", "lon": "-116.1429", "label": "Table_Rock"},
    {"lat": "43.635", "lon": "-116.176", "label": "Crestline_Trailhead"},
]
TRANSIT = {
    "before": [
        {"ident": "BOI", "lat": "", "lon": "", "label": "", "make_line": False},
        {"ident": "", "lat": "43.95", "lon": "-116.30", "label": "entry gate", "make_line": False},
        {"ident": "", "lat": "43.85", "lon": "-116.20", "label": "GATE", "make_line": True},
    ],
    "after": [
        {"ident": "", "lat": "43.40", "lon": "-115.90", "label": "", "make_line": True},
        {"ident": "", "lat": "43.30", "lon": "-116.00", "label": "OUT", "make_line": False},
    ],
}


def plan(name, params=None, **flags):
    base = {
        "schema": "airborne_survey_plan/1", "area_name": name, "waypoint_prefix": "",
        "parameters": {
            "groundspeed_kt": "200", "transit_groundspeed_kt": "200", "bank_angle_deg": "25", "swath_width_km": "4.0", "swath_overlap": "0.1",
            "perimeter_margin_km": "5.0", "initial_heading_deg": "0", "lat_offset": "0.0",
            "lon_offset": "0.0", "lead_in_km": "0.0", "origin_airport": "KBOI",
            "destination_airport": "KBOI", "survey_msl_ft": "20000", "center_lat": "43.6475",
            "center_lon": "-116.1195", "center_width_km": "12.0", "center_length_km": "18.0",
            "make_line_km": "20.0",
        },
        "rectangular_box": True, "survey_area_mode": "Boundary GPS (Max 10)", "repeats": "1",
        "retrace_lines": False, "survey_only": False, "skip_edges": "0", "skip_box": False,
        "make_line_bearing": "Along track", "make_line_keep_centre": False,
        "boundary": BOUNDARY, "transit": {"before": [], "after": []},
    }
    base["parameters"].update(params or {})
    base.update(flags)
    return base


PLANS = [
    plan("basic"),
    plan("heading_leadin", {"initial_heading_deg": "37", "lead_in_km": "8"}),
    plan("clipped_retrace", {"initial_heading_deg": "110"}, rectangular_box=False,
         retrace_lines=True, repeats="2"),
    plan("prefix_skip", {"swath_width_km": "2.0"}, waypoint_prefix="a", skip_edges="1"),
    plan("offsets", {"lat_offset": "0.02", "lon_offset": "-0.03", "initial_heading_deg": "270"}),
    plan("centerpoint", {"initial_heading_deg": "346.5"}, survey_area_mode="CenterPoint"),
    plan("transit", {"lead_in_km": "5"}, transit=TRANSIT),
    plan("transit_heading_lines", {"initial_heading_deg": "20"}, transit=TRANSIT,
         make_line_bearing="Survey heading", make_line_keep_centre=True),
    plan("survey_only", {"lead_in_km": "5"}, transit=TRANSIT, survey_only=True),
    plan("skip_box", transit=TRANSIT, skip_box=True),
    plan("no_turn_model", {"bank_angle_deg": "0", "lead_in_km": "5"}, retrace_lines=True),
    plan("wide_turns", {"swath_width_km": "10.0", "bank_angle_deg": "30"}, repeats="2"),
    plan("airports", {"origin_airport": "KLAX", "destination_airport": "KSLC"}),
    plan("two_speeds", {"origin_airport": "KLAX", "destination_airport": "KSLC",
                        "groundspeed_kt": "250", "transit_groundspeed_kt": "450",
                        "swath_width_km": "1.0"}, transit=TRANSIT),
    plan("airports_transit", {"origin_airport": "KSLC", "destination_airport": "ZZZZ"},
         transit=TRANSIT),
    plan("diagonal_45", {"initial_heading_deg": "45", "swath_width_km": "2.0"}),
    plan("diagonal_135", {"initial_heading_deg": "135", "swath_width_km": "2.0"},
         transit=TRANSIT, make_line_bearing="Survey heading"),
    plan("first_east", {"swath_width_km": "2.0"}, first_line="E"),
    plan("first_west_transit", {"lead_in_km": "5"}, transit=TRANSIT, first_line="W"),
    plan("first_nw_45", {"initial_heading_deg": "45", "swath_width_km": "2.0"}, first_line="NW",
         retrace_lines=True),
    plan("over_limit", {"swath_width_km": "0.75"}, retrace_lines=True, repeats="2"),
]

STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\dZ")
FILES = ("{}_waypoints_foreflight.csv", "{}_waypoints_honeywell.csv", "{}_survey.kml",
         "{}_foreflight_route.txt")
KEYS = ("foreflight_csv", "honeywell_csv", "kml", "route")


def main():
    work = tempfile.mkdtemp(prefix="parity_")
    os.chdir(work)                      # the desktop app writes plans/ under the cwd

    import airborne_survey_gui as gui
    import survey_engine as engine

    # A dialog would block the run; turn any the desktop app raises into a failure instead.
    raised = []
    gui.messagebox.showerror = lambda title, message: raised.append(f"{title}: {message}")

    app = gui.FlightPlannerGUI()
    app.update()
    failures = 0
    for p in PLANS:
        name = p["area_name"]
        raised.clear()
        # Loading a plan leaves the previous prefix in place when the new one is empty, so
        # clear it by hand or one plan's prefix leaks into the next.
        app.prefix_entry.delete(0, "end")
        app._apply_plan_dict(p)
        app.calculate_and_render()
        web = json.loads(engine.generate_json(json.dumps(p)))
        if raised or not web["ok"]:
            failures += 1
            print(f"FAIL {name}: desktop={raised or 'ok'} web={web.get('message', 'ok')}")
            continue
        bad = []
        for pattern, key in zip(FILES, KEYS):
            path = os.path.join(work, "plans", name, pattern.format(name))
            with open(path, encoding="utf-8", newline="") as f:
                desktop = f.read().replace("\r\n", "\n")
            if STAMP.sub("T", desktop) != STAMP.sub("T", web["files"][key]["text"].replace("\r\n", "\n")):
                bad.append(key)
        # The distance, time and turn figures are computed twice too; compare those lines.
        figures = ("Turn Radius:", "Turns:", "Survey Path:", "In-Pattern Time:", "Segment ",
                   "Transit Path:", "Outbound Transit:", "Inbound Transit:", "Transit Time:", "Total Flight Time:", "Actual Padding:",
                   "Waypoints in CSV:", "First Line:",
                   "Origin:", "Destination:")
        desktop_stats = [line for line in app.stats_text.get("1.0", "end").splitlines()
                         if line.startswith(figures)]
        web_stats = [line for line in web["summary"].splitlines() if line.startswith(figures)]
        if desktop_stats != web_stats:
            bad.append("summary figures")
        rows = web["files"]["foreflight_csv"]["text"].count("\n") - 1
        failures += bool(bad)
        print(f"{'FAIL' if bad else 'ok  '} {name}: {rows} waypoints"
              + (f" -- differs: {', '.join(bad)}" if bad else ""))
    app.destroy()
    print(f"\n{len(PLANS) - failures}/{len(PLANS)} plans match (outputs in {work})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

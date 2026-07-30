import os
import csv
import io
import json
import math
import subprocess
import sys
import time
import webbrowser
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog
import numpy as np
from shapely.geometry import LineString, Polygon, MultiLineString, GeometryCollection
from shapely import affinity
from pyproj import CRS, Transformer
import folium

# Generated output lives under here, one directory per named plan, so it never litters
# the project root. Git-ignored wholesale.
PLANS_DIR = "plans"

# --- SURVEY ENGINE CONFIGURATION & HELPERS ---

def dd_to_honeywell_format(value, positive_indicator, negative_indicator, degree_digits=2):
    """Degrees + decimal minutes, matching the FMS sample the pilot supplied exactly.

    Sample rows are `N 36 44.53` and `W 076 38.02`: latitude degrees are 2 digits,
    longitude degrees are 3 and zero padded, minutes are 2 decimals zero padded, and there
    is NO trailing space (this used to emit one, e.g. `N 43 36.77 `).
    """
    sign = positive_indicator if value >= 0 else negative_indicator
    abs_value = abs(value)
    degrees = int(abs_value)
    minutes = (abs_value - degrees) * 60.0
    # Without this, minutes of 59.996+ would format as "60.00" rather than rolling over.
    if round(minutes, 2) >= 60.0:
        degrees += 1
        minutes = 0.0
    return f"{sign} {degrees:0{degree_digits}d} {minutes:05.2f}"

def summarize_segment_travel(flight_pattern, groundspeed_kt=200.0):
    coords = list(flight_pattern.coords)
    if len(coords) < 2:
        return [], 0.0, 0.0, 0.0

    segment_summaries = []
    total_distance_m = 0.0
    for idx in range(1, len(coords)):
        x1, y1 = coords[idx - 1]
        x2, y2 = coords[idx]
        dist_m = math.hypot(x2 - x1, y2 - y1)
        dist_nm = dist_m / 1852.0
        travel_min = (dist_nm / groundspeed_kt) * 60.0 if groundspeed_kt > 0 else float('nan')
        segment_summaries.append((idx, dist_m, dist_nm, travel_min))
        total_distance_m += dist_m

    total_distance_nm = total_distance_m / 1852.0
    total_time_min = (total_distance_nm / groundspeed_kt) * 60.0 if groundspeed_kt > 0 else float('nan')
    return segment_summaries, total_distance_m, total_distance_nm, total_time_min

def flatten_linestrings(geom):
    """Every non-degenerate LineString inside an arbitrary intersection result."""
    if isinstance(geom, LineString):
        return [geom] if geom.length > 0 else []
    if isinstance(geom, (MultiLineString, GeometryCollection)):
        found = []
        for part in geom.geoms:
            found.extend(flatten_linestrings(part))
        return found
    return []


def measure_clearance(target_poly, coverage_poly, samples=400):
    """Smallest padding between the target outline and the coverage edge, in metres.

    Negative means part of the target is not covered at all. Sampled around the target
    exterior rather than taken vertex-to-vertex, so a long edge bowing outside the
    coverage is still caught.
    """
    exterior = target_poly.exterior
    worst = None
    for i in range(samples):
        point = exterior.interpolate(i / samples, normalized=True)
        gap = coverage_poly.exterior.distance(point)
        if not coverage_poly.contains(point):
            gap = -gap
        worst = gap if worst is None else min(worst, gap)
    return 0.0 if worst is None else worst


def build_rectangular_pattern(latlon_coords, swath_km, overlap, perimeter_margin_km, initial_heading_deg, lat_offset, lon_offset, transformer_to_m, center_lat, rectangular=True, repeats=1):
    if len(latlon_coords) < 3:
        raise ValueError('At least three coordinates are required to define a survey area.')

    # Convert coordinates to UTM (metric) projection
    input_xy = [transformer_to_m.transform(lon, lat) for lat, lon, _ in latlon_coords]
    target_poly = Polygon(input_xy).buffer(0)

    if target_poly.is_empty or target_poly.area == 0:
        raise ValueError('Survey polygon area is zero. Check input coordinates.')

    # Pad the target itself and fly to that shape. Buffering grows the outline by the
    # margin in every direction, so clipping the passes against it guarantees the
    # requested padding on all sides at any heading. Clipping against the bounding box
    # instead could not: the box overshoots wildly on some sides while the rotate-back
    # step below used to shave the margin on others.
    coverage = target_poly
    if perimeter_margin_km > 0:
        # quad_segs well above the default 8: buffer() approximates the rounded corners
        # with straight segments and the result is inscribed, so a coarse approximation
        # lands slightly INSIDE the requested margin (~10 m short at 5 km with the
        # default). Padding must never come out under what was asked for.
        coverage = coverage.buffer(perimeter_margin_km * 1000.0, quad_segs=64)

    if lat_offset != 0.0 or lon_offset != 0.0:
        lat_offset_m = lat_offset * 111320.0
        lon_offset_m = lon_offset * 111320.0 * math.cos(math.radians(center_lat))
        coverage = affinity.translate(coverage, xoff=lon_offset_m, yoff=lat_offset_m)

    # ONE pivot for every rotation in this function. The previous code rotated the
    # polygon about the polygon's centroid but rotated the result back about the
    # bounding rectangle's own centroid -- a different point, up to ~0.6 km away on the
    # default area -- which translated the whole coverage region and made the effective
    # margin depend on heading. Heading 090 was the only value that appeared to work,
    # because that is the one case where the rotation angle is zero.
    heading_angle_deg = (90.0 - initial_heading_deg) % 360.0
    pivot = coverage.centroid
    rotated_coverage = affinity.rotate(coverage, -heading_angle_deg, origin=pivot, use_radians=False)
    minx, miny, maxx, maxy = rotated_coverage.bounds

    if rectangular:
        # "Mow the lawn": clip to the smallest rectangle enclosing the padded target at
        # this heading, so every line runs the full width and they are all the same
        # length. Clipping to the padded outline itself instead produces lines of wildly
        # differing length -- 2.8 km / 21 km / 2.8 km on the default area, an hourglass
        # that is minimal-area but not how a survey is actually flown.
        clip_region = Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)])
    else:
        clip_region = rotated_coverage

    center_y = (miny + maxy) / 2.0
    line_spacing = swath_km * 1000 * (1 - overlap)
    if line_spacing <= 0:
        raise ValueError('Swath width and overlap give a zero or negative line spacing.')

    height = maxy - miny
    if height <= line_spacing:
        candidate_ys = [center_y]
    else:
        num_lines = int(math.floor(height / line_spacing)) + 1
        start_y = center_y - ((num_lines - 1) * line_spacing / 2.0)
        candidate_ys = [start_y + i * line_spacing for i in range(num_lines)]

    # One row per pass line. A concave coverage shape can split a row into several
    # segments, so rows are kept grouped and ordered along the row before sequencing.
    pass_rows = []
    for current_y in candidate_ys:
        pass_line = LineString([(minx - 10000, current_y), (maxx + 10000, current_y)])
        segments = flatten_linestrings(pass_line.intersection(clip_region))
        if segments:
            pass_rows.append(sorted(segments, key=lambda s: s.centroid.x))

    if not pass_rows:
        raise ValueError('No pass segments could be generated. Adjust coordinates or swath width.')

    cycle_segments = []
    for row_idx, row_segments in enumerate(pass_rows):
        reverse = row_idx % 2 == 1
        ordered = list(reversed(row_segments)) if reverse else row_segments
        for segment in ordered:
            # Orient each pass along the direction it is actually flown, so the exporter
            # can name its ends Start and Finish without re-deriving the sequence.
            oriented = sorted(segment.coords, key=lambda c: c[0])
            if reverse:
                oriented = oriented[::-1]
            cycle_segments.append(LineString(oriented))

    # Repeats fly the whole box again from the top, so the line directions and sensor
    # geometry of every cycle are identical. Line numbering runs straight on through, which
    # keeps every waypoint name unique and in flight order.
    pass_segments = cycle_segments * max(1, int(repeats))

    pattern_points = []
    for segment in pass_segments:
        coords = list(segment.coords)
        # The turn onto the next pass -- and the transit back to line 1 for a repeat -- is
        # implicit in the polyline; only guard against emitting a duplicate point, which
        # would create a zero-length segment.
        if pattern_points and pattern_points[-1] == coords[0]:
            coords = coords[1:]
        pattern_points.extend(coords)

    if len(pattern_points) < 2:
        raise ValueError('Survey area is too small for the configured swath width.')

    pattern_line = LineString(pattern_points)
    rotated_pattern = affinity.rotate(pattern_line, heading_angle_deg, origin=pivot, use_radians=False)
    # Segments come back in the un-rotated frame too, so their coordinates are usable.
    flown_segments = [affinity.rotate(seg, heading_angle_deg, origin=pivot, use_radians=False)
                      for seg in pass_segments]
    # Report the region actually flown, so the preview, KML and clearance check all agree.
    flown_region = affinity.rotate(clip_region, heading_angle_deg, origin=pivot,
                                   use_radians=False) if rectangular else coverage
    return flown_region, rotated_pattern, flown_segments

# --- FOREFLIGHT KML / CONTENT PACK EXPORT ---
#
# ForeFlight renders only a subset of KML: Point, LineString, LinearRing, Polygon,
# MultiGeometry, Style, StyleMap (normal style only), LineStyle, PolyStyle, IconStyle and
# the gx: namespace. Nothing outside that list is used here. See foreflight.md.
#
# Because this document carries shape geometry (a Polygon and a LineString) ForeFlight
# imports it as a User Map Layer, not as user waypoints -- which is what makes the named
# points show up as overlay labels without entering the waypoint database.

def kml_colour(hex_rgb, alpha="ff"):
    """#rrggbb -> KML aabbggrr (KML orders the channels backwards from CSS)."""
    rgb = hex_rgb.lstrip('#')
    return f"{alpha}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def build_survey_kml(area_name, envelope_latlon, track_latlon, waypoints, boundary, meta,
                     generated_utc):
    """Return a ForeFlight-compatible KML overlay of the survey pattern."""

    def coords(points):
        # KML is lon,lat -- longitude first, opposite of this app's internal ordering.
        return " ".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)

    summary = (f"{meta['lines']} survey lines @ {meta['heading']:.0f}°T — "
               f"{meta['dist_nm']:.1f} nm / {meta['time_min']:.0f} min")
    out = io.StringIO()
    w = out.write
    w('<?xml version="1.0" encoding="UTF-8"?>\n')
    w('<kml xmlns="http://www.opengis.net/kml/2.2" '
      'xmlns:gx="http://www.google.com/kml/ext/2.2">\n')
    w('  <Document>\n')
    w(f'    <name>{xml_escape(area_name)} survey</name>\n')
    w(f'    <description>{xml_escape(summary)} — generated {generated_utc}</description>\n')

    w('    <Style id="envelope">\n'
      f'      <LineStyle><color>{kml_colour("1f6fd0")}</color><width>2</width></LineStyle>\n'
      f'      <PolyStyle><color>{kml_colour("1f6fd0", "26")}</color></PolyStyle>\n'
      '    </Style>\n')
    # gx:labelVisibility puts the Placemark <name> onto the line itself. Google spells the
    # element with a lower-case l; ForeFlight's docs write "gx:LabelVisibility". If the
    # track label does not appear in the app, that capitalisation is the thing to flip.
    w('    <Style id="track">\n'
      f'      <LineStyle><color>{kml_colour("d81b1b")}</color><width>4</width>'
      '<gx:labelVisibility>1</gx:labelVisibility></LineStyle>\n'
      '    </Style>\n')
    w('    <Style id="waypoint">\n'
      f'      <IconStyle><color>{kml_colour("d81b1b")}</color><scale>0.7</scale></IconStyle>\n'
      '    </Style>\n')
    w('    <Style id="boundary">\n'
      f'      <IconStyle><color>{kml_colour("7b2fbe")}</color><scale>0.9</scale></IconStyle>\n'
      '    </Style>\n')

    w('    <Folder>\n      <name>Survey area</name>\n')
    w('      <Placemark>\n'
      f'        <name>{xml_escape(area_name)} buffer envelope</name>\n'
      '        <styleUrl>#envelope</styleUrl>\n'
      '        <Polygon><altitudeMode>clampToGround</altitudeMode><outerBoundaryIs>'
      f'<LinearRing><coordinates>{coords(envelope_latlon)}</coordinates></LinearRing>'
      '</outerBoundaryIs></Polygon>\n'
      '      </Placemark>\n')
    w('    </Folder>\n')

    w('    <Folder>\n      <name>Flight track</name>\n')
    w('      <Placemark>\n'
      f'        <name>{xml_escape(area_name)} — {xml_escape(summary)}</name>\n'
      '        <styleUrl>#track</styleUrl>\n'
      '        <LineString><tessellate>1</tessellate>'
      '<altitudeMode>clampToGround</altitudeMode>'
      f'<coordinates>{coords(track_latlon)}</coordinates></LineString>\n'
      '      </Placemark>\n')
    w('    </Folder>\n')

    w('    <Folder>\n      <name>Survey waypoints</name>\n')
    for idx, (name, lat, lon) in enumerate(waypoints, start=1):
        w('      <Placemark>\n'
          f'        <name>{xml_escape(name)}</name>\n'
          f'        <description>waypoint {idx} of {len(waypoints)} — '
          f'{lat:.4f}, {lon:.4f}</description>\n'
          '        <styleUrl>#waypoint</styleUrl>\n'
          f'        <Point><coordinates>{lon:.6f},{lat:.6f}</coordinates></Point>\n'
          '      </Placemark>\n')
    w('    </Folder>\n')

    if boundary:
        w('    <Folder>\n      <name>Target boundary points</name>\n')
        for lat, lon, label in boundary:
            w('      <Placemark>\n'
              f'        <name>{xml_escape(label)}</name>\n'
              f'        <description>{lat:.5f}, {lon:.5f}</description>\n'
              '        <styleUrl>#boundary</styleUrl>\n'
              f'        <Point><coordinates>{lon:.6f},{lat:.6f}</coordinates></Point>\n'
              '      </Placemark>\n')
        w('    </Folder>\n')

    w('  </Document>\n</kml>\n')
    return out.getvalue()


# --- HOVER HELP ---

class ToolTip:
    """Hover help for a widget; Tkinter has no built-in tooltip.

    Binds with add="+" so it never displaces a handler the widget already has.
    """

    DELAY_MS = 450

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._after_id = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        # The widget can be gone by the time a pending callback fires.
        if self._window is not None or not self.widget.winfo_exists():
            return

        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        for setup in (lambda: window.wm_attributes("-topmost", True),
                      # Borderless windows need this on macOS or they steal focus.
                      lambda: window.tk.call("::tk::unsupported::MacWindowStyle", "style",
                                             window._w, "help", "noActivates")):
            try:
                setup()
            except tk.TclError:
                pass

        tk.Label(window, text=self.text, justify=tk.LEFT, background="#ffffe0",
                 foreground="#111111", relief=tk.SOLID, borderwidth=1, wraplength=340,
                 font=("Helvetica", 9), padx=7, pady=5).pack()
        window.wm_geometry(f"+{self.widget.winfo_rootx() + 16}"
                           f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 6}")
        self._window = window

    def _hide(self, _event=None):
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None


# --- GUI APPLICATION CLASS ---

class FlightPlannerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Airborne Survey Flight Path Planner")
        self.geometry("1300x850")
        
        # Default Survey Coordinates
        self.survey_boundary = [
            (43.703, -116.089, 'Boise_Peak'),
            (43.640, -116.062, 'Aldape_Summit'),
            (43.6125, -116.151, '2967_Shaw_Mtn_Rd'),
            (43.635, -116.176, 'Crestline_Trailhead')
        ]
        
        # Geometry cached for the preview canvas so a window resize can redraw it,
        # plus the path of the last saved Folium map for the "open in browser" button.
        self._preview = None
        self._map_path = None
        self._output_dir = None
        self._run_count = 0

        # Measured when labelling so a white backing box can be sized to the text.
        self._label_font = tkfont.Font(family="Helvetica", size=8)
        self._label_font_bold = tkfont.Font(family="Helvetica", size=8, weight="bold")

        # Label visibility: on by default, but a dense survey crowds the pane so both
        # sets can be switched off without regenerating the plan.
        self.show_waypoint_labels = tk.BooleanVar(value=True)
        self.show_boundary_labels = tk.BooleanVar(value=True)

        # Rectangular box on by default: survey flying is "mow the lawn", equal-length
        # parallel lines. Unchecked clips the passes to the padded target outline instead,
        # which covers less ground but gives lines of very uneven length.
        self.rectangular_box = tk.BooleanVar(value=True)
        self.repeats = tk.StringVar(value="1")

        self._setup_layout()
        self._load_defaults()
        self.calculate_and_render()

    def _setup_layout(self):
        # Main split: Left controls, Right visualization map
        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True)

        # Left panel (Controls Container)
        left_frame = ttk.Frame(main_pane, padding=10)
        main_pane.add(left_frame, weight=1)

        # Right panel (Map Container)
        right_frame = ttk.Frame(main_pane)
        main_pane.add(right_frame, weight=3)

        # --- TABBED NOTEBOOK FOR INPUTS ---
        notebook = ttk.Notebook(left_frame)
        notebook.grid(row=0, column=0, columnspan=2, sticky="nsew", pady=(0, 10))

        # TAB 1: PARAMETERS & FILE EXPORTS
        param_tab = ttk.Frame(notebook, padding=10)
        notebook.add(param_tab, text="Flight & Export Settings")

        self.inputs = {}
        
        # File & Prefix Inputs
        ttk.Label(param_tab, text="Export Settings", font=("Helvetica", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        
        ttk.Label(param_tab, text="Filename/Area Name:").grid(row=1, column=0, sticky="w", pady=5)
        self.area_name_entry = ttk.Entry(param_tab, width=18)
        self.area_name_entry.insert(0, "Clairmont_Fire")
        self.area_name_entry.grid(row=1, column=1, sticky="w", pady=5, padx=10)

        # Waypoints come out as <prefix>L<n>S / <prefix>L<n>F, so "1" gives the pilot's
        # 1L1S / 1L1F convention. Not a 3-character prefix any more.
        ttk.Label(param_tab, text="Line ID Prefix:").grid(row=2, column=0, sticky="w", pady=5)
        self.prefix_entry = ttk.Entry(param_tab, width=10)
        self.prefix_entry.insert(0, "1")
        self.prefix_entry.grid(row=2, column=1, sticky="w", pady=5, padx=10)

        ttk.Separator(param_tab, orient='horizontal').grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)

        # Flight settings fields
        ttk.Label(param_tab, text="Flight Parameters", font=("Helvetica", 10, "bold")).grid(row=4, column=0, sticky="w", pady=(0, 5))
        rect_check = ttk.Checkbutton(param_tab, text="Rectangular Box",
                                     variable=self.rectangular_box,
                                     command=self.calculate_and_render)
        rect_check.grid(row=4, column=1, sticky="w", padx=10)
        ToolTip(rect_check,
                "ON (normal): fly the smallest rectangle that covers the target plus its "
                "perimeter margin, at the chosen heading. Every line is the same length — "
                "mow the lawn.\n\n"
                "OFF: clip the lines to the padded target outline instead. Covers less "
                "ground, but line lengths vary badly — 2.8 / 21 / 2.8 km on the default "
                "area.\n\n"
                "Either way the perimeter margin still clears the boundary on all sides.")

        repeats_label = ttk.Label(param_tab, text="Repeats (fly box N times):")
        repeats_label.grid(row=5, column=0, sticky="w", pady=5)
        repeats_box = ttk.Combobox(param_tab, width=12, state="readonly",
                                   values=("1", "2", "3", "4"), textvariable=self.repeats)
        repeats_box.grid(row=5, column=1, sticky="w", pady=5, padx=10)
        repeats_box.bind("<<ComboboxSelected>>", lambda _e: self.calculate_and_render())
        repeats_tip = ("How many times to fly the whole box.\n\n"
                       "Each cycle repeats the same lines in the same directions, so sensor "
                       "geometry matches between cycles. Line numbering continues across "
                       "them — 3 lines flown twice gives 1L1S through 1L6F — so every "
                       "waypoint name stays unique and in flight order.\n\n"
                       "The transit back to line 1 carries no waypoint.")
        ToolTip(repeats_box, repeats_tip)
        ToolTip(repeats_label, repeats_tip)

        fields = [
            ("Groundspeed (knots):", "groundspeed_kt", "200"),
            ("Swath Width (km):", "swath_width_km", "10.0"),
            ("Swath Overlap (0.0-1.0):", "swath_overlap", "0.1"),
            ("Perimeter Margin (km):", "perimeter_margin_km", "5.0"),
            ("Initial Heading (deg True):", "initial_heading_deg", "0"),
            # Offsets shift the whole coverage area, which eats into the perimeter margin
            # on the trailing side. Default to no shift so the requested padding actually
            # holds on all sides; the summary panel reports the padding achieved.
            ("Latitude Offset (deg):", "lat_offset", "0.0"),
            ("Longitude Offset (deg):", "lon_offset", "0.0")
        ]

        for i, (label_text, dict_key, default_val) in enumerate(fields, start=6):
            ttk.Label(param_tab, text=label_text).grid(row=i, column=0, sticky="w", pady=5)
            entry = ttk.Entry(param_tab, width=15)
            entry.insert(0, default_val)
            entry.grid(row=i, column=1, sticky="w", pady=5, padx=10)
            self.inputs[dict_key] = entry

        # TAB 2: COORDINATES INPUT (MAX 10)
        coords_tab = ttk.Frame(notebook, padding=10)
        notebook.add(coords_tab, text="Boundary GPS (Max 10)")

        # Grid Header Labels
        ttk.Label(coords_tab, text="#", font=("Helvetica", 9, "bold")).grid(row=0, column=0, padx=2, pady=2)
        ttk.Label(coords_tab, text="Latitude", font=("Helvetica", 9, "bold")).grid(row=0, column=1, padx=2, pady=2)
        ttk.Label(coords_tab, text="Longitude", font=("Helvetica", 9, "bold")).grid(row=0, column=2, padx=2, pady=2)
        ttk.Label(coords_tab, text="Waypoint Label", font=("Helvetica", 9, "bold")).grid(row=0, column=3, padx=2, pady=2)

        self.coord_rows = []
        for i in range(10):
            ttk.Label(coords_tab, text=f"{i+1}").grid(row=i+1, column=0, padx=5, pady=2)
            lat_entry = ttk.Entry(coords_tab, width=11)
            lon_entry = ttk.Entry(coords_tab, width=11)
            lbl_entry = ttk.Entry(coords_tab, width=14)
            
            lat_entry.grid(row=i+1, column=1, padx=3, pady=2)
            lon_entry.grid(row=i+1, column=2, padx=3, pady=2)
            lbl_entry.grid(row=i+1, column=3, padx=3, pady=2)
            
            self.coord_rows.append((lat_entry, lon_entry, lbl_entry))

        # Clear Coordinates Button
        clear_btn = ttk.Button(coords_tab, text="Clear All GPS Fields", command=self._clear_gps_fields)
        clear_btn.grid(row=12, column=1, columnspan=3, pady=5, sticky="ew")

        # --- AREA SAVE / LOAD ---
        # On the common pane rather than inside a tab: these act on the whole area
        # definition (points AND parameters), not just the coordinate grid.
        area_btns = ttk.Frame(left_frame)
        area_btns.grid(row=1, column=0, columnspan=2, sticky="ew")
        save_btn = ttk.Button(area_btns, text="Save Plan…", command=self._save_plan)
        save_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        load_btn = ttk.Button(area_btns, text="Load Plan…", command=self._load_plan)
        load_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0))
        ToolTip(save_btn,
                "Write the whole plan to a .json file — boundary points, waypoint labels, "
                "and every flight parameter including the Rectangular Box and Repeats "
                "settings.\n\n"
                "Each run also drops a copy beside its outputs, so any past output folder "
                "can be reloaded as-is.")
        ToolTip(load_btn,
                "Read a saved plan back in, so a set of points never has to be retyped, "
                "then regenerate immediately.\n\n"
                "A malformed file is rejected whole rather than half-applied.")

        # --- RUN CONTROL BUTTON ---
        calculate_btn = ttk.Button(left_frame, text="Generate Flight Plan & Update Map", command=self.calculate_and_render)
        calculate_btn.grid(row=2, column=0, columnspan=2, pady=10, sticky="ew")

        # Visible confirmation that a click was handled, even when the inputs are unchanged.
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(left_frame, textvariable=self.status_var, foreground="#0b6b3a", wraplength=330,
                  justify=tk.LEFT).grid(row=3, column=0, columnspan=2, sticky="w")

        # --- FLIGHT STATISTICS WINDOW ---
        ttk.Label(left_frame, text="Flight Path Summary Output", font=("Helvetica", 10, "bold")).grid(row=4, column=0, columnspan=2, pady=(10, 2), sticky="w")
        self.stats_text = tk.Text(left_frame, width=45, height=15, wrap=tk.WORD, font=("Courier", 11))
        self.stats_text.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=5)

        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(5, weight=1)

        # --- FLIGHT PATH PREVIEW ---
        # Drawn on a native Tk canvas: the Folium map is Leaflet/JavaScript, which the
        # embedded HTML widget cannot execute, so it only ever rendered a blank page.
        preview_header = ttk.Frame(right_frame, padding=(8, 6))
        preview_header.pack(fill=tk.X)
        ttk.Label(preview_header, text="Flight Path Preview", font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(preview_header, text="Open Interactive Map in Browser",
                   command=self._open_map_in_browser).pack(side=tk.RIGHT)
        ttk.Button(preview_header, text="Show Export Files",
                   command=self._open_export_folder).pack(side=tk.RIGHT, padx=(0, 8))
        # View-only toggles: they redraw from cached geometry, they do not recalculate.
        boundary_check = ttk.Checkbutton(preview_header, text="Boundary labels",
                                         variable=self.show_boundary_labels,
                                         command=self._draw_preview)
        boundary_check.pack(side=tk.RIGHT, padx=(0, 14))
        ToolTip(boundary_check,
                "Show the names of your own boundary points — the purple markers.\n\n"
                "These are usually the longest labels, so they crowd the pane first. The "
                "markers stay visible either way.\n\n"
                "Display only: never changes the exported files.")

        waypoint_check = ttk.Checkbutton(preview_header, text="Waypoint labels",
                                         variable=self.show_waypoint_labels,
                                         command=self._draw_preview)
        waypoint_check.pack(side=tk.RIGHT, padx=(0, 14))
        ToolTip(waypoint_check,
                "Show the exported waypoint names (1L1S, 1L1F, …) at each line end, so the "
                "preview can be read against the CSVs.\n\n"
                "Turn off when a dense survey crowds the pane — at 0.75 km swath there are "
                "56 waypoints and only ~37 labels fit. The red dots stay either way.\n\n"
                "Display only: never changes the exported files.")

        self.preview_canvas = tk.Canvas(right_frame, bg="white", highlightthickness=0)
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", lambda _event: self._draw_preview())

    def _open_map_in_browser(self):
        if not self._map_path or not os.path.exists(self._map_path):
            messagebox.showinfo("No Map Yet", "Generate a flight plan first.")
            return
        webbrowser.open(Path(self._map_path).absolute().as_uri())

    def _label(self, x, y, text, anchor="center", fill="#333333", bold=False, avoid=None):
        """Draw canvas text over a white backing box so it stays legible over the pattern.

        Pass a list as `avoid` to suppress labels that would overlap one already drawn;
        the box of each label that does get drawn is appended to it. Returns True if the
        label was drawn, False if it was suppressed.
        """
        font = self._label_font_bold if bold else self._label_font
        text_w = font.measure(text)
        text_h = font.metrics("linespace")
        if anchor == "w":
            x0, x1 = x - 2, x + text_w + 2
        elif anchor == "e":
            x0, x1 = x - text_w - 2, x + 2
        else:
            x0, x1 = x - text_w / 2 - 2, x + text_w / 2 + 2
        y0, y1 = y - text_h / 2, y + text_h / 2

        if avoid is not None:
            if any(x0 < bx1 and bx0 < x1 and y0 < by1 and by0 < y1 for bx0, by0, bx1, by1 in avoid):
                return False
            avoid.append((x0, y0, x1, y1))

        self.preview_canvas.create_rectangle(x0, y0, x1, y1, fill="white", outline="")
        self.preview_canvas.create_text(x, y, text=text, anchor=anchor, fill=fill, font=font)
        return True

    def _draw_preview(self):
        """Render the cached survey geometry (UTM metres, so already equal-aspect)."""
        canvas = self.preview_canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 60 or height < 60:
            return

        if not self._preview:
            canvas.create_text(width / 2, height / 2, fill="#888888", font=("Helvetica", 11),
                               text="Click 'Generate Flight Plan & Update Map' to draw the survey pattern.")
            return

        rect = self._preview['rect']
        track = self._preview['track']
        marks = self._preview['marks']

        points = list(rect) + list(track) + [(x, y) for x, y, _ in marks]
        min_x = min(p[0] for p in points)
        max_x = max(p[0] for p in points)
        min_y = min(p[1] for p in points)
        max_y = max(p[1] for p in points)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)

        # Asymmetric padding: the top band is reserved for the header and legend and the
        # bottom for the scale bar, so the pattern is never drawn underneath either.
        pad, pad_top, pad_bottom = 60, 108, 52
        scale = min((width - 2 * pad) / span_x, (height - pad_top - pad_bottom) / span_y)
        off_x = (width - span_x * scale) / 2.0
        band = height - pad_top - pad_bottom
        base_y = pad_top + (band + span_y * scale) / 2.0   # screen y of min_y

        def to_px(point):
            return (off_x + (point[0] - min_x) * scale,
                    base_y - (point[1] - min_y) * scale)

        # Target buffer envelope
        rect_px = [coord for point in rect for coord in to_px(point)]
        canvas.create_polygon(rect_px, fill="#eaf1fb", outline="#1f6fd0", width=2)

        # Flight track, with a direction arrow on each leg long enough to show one
        track_px = [to_px(point) for point in track]
        for start, end in zip(track_px, track_px[1:]):
            long_enough = math.hypot(end[0] - start[0], end[1] - start[1]) > 45
            canvas.create_line(start[0], start[1], end[0], end[1], fill="#d81b1b", width=2,
                               dash=(6, 4), arrow=tk.LAST if long_enough else None,
                               arrowshape=(12, 14, 5))

        if track_px:
            sx, sy = track_px[0]
            ex, ey = track_px[-1]
            canvas.create_oval(sx - 7, sy - 7, sx + 7, sy + 7, fill="", outline="#12a150", width=3)
            canvas.create_rectangle(ex - 6, ey - 6, ex + 6, ey + 6, fill="", outline="#333333", width=3)

        # Exported flight-plan waypoints, labelled with the names written to the CSVs.
        # Labels sit outboard of the pattern centre so they clear the track lines.
        # Seed the collision list with the chrome drawn further down (header, legend,
        # scale bar, north arrow) so a waypoint label never ends up hidden underneath it.
        placed, hidden = [(pad - 6, 8, pad + 380, 100),
                          (pad - 6, height - 44, pad + 300, height - 10),
                          (width - 46, height - 74, width - 22, height - 22)], 0
        centre_x = sum(p[0] for p in track_px) / len(track_px) if track_px else 0.0
        show_wp = self.show_waypoint_labels.get()
        for x, y, name in self._preview['waypoints']:
            px, py = to_px((x, y))
            canvas.create_oval(px - 3, py - 3, px + 3, py + 3, fill="#d81b1b", outline="white", width=1)
            if not show_wp:
                continue
            out, back = ((px + 11, "w"), (px - 11, "e")) if px >= centre_x else ((px - 11, "e"), (px + 11, "w"))
            # Prefer outboard of the pattern, then the far side, then above, then below.
            spots = [(out[0], py, out[1]), (back[0], py, back[1]),
                     (px, py - 14, "center"), (px, py + 14, "center")]
            if not any(self._label(lx, ly, name, anchor=la, fill="#a01010", bold=True, avoid=placed)
                       for lx, ly, la in spots):
                hidden += 1

        # Boundary waypoints supplied by the operator
        show_bnd = self.show_boundary_labels.get()
        for x, y, label in marks:
            px, py = to_px((x, y))
            canvas.create_oval(px - 5, py - 5, px + 5, py + 5, fill="#7b2fbe", outline="white", width=1)
            if not show_bnd:
                continue
            spots = [(px, py - 14, "center"), (px, py + 14, "center"),
                     (px + 9, py, "w"), (px - 9, py, "e")]
            if not any(self._label(lx, ly, label, anchor=la, fill="#4a1a75", avoid=placed)
                       for lx, ly, la in spots):
                hidden += 1

        # North arrow (UTM grid north is up); kept low-right, clear of the header text
        nx, ny = width - 34, height - 58
        canvas.create_line(nx, ny + 16, nx, ny - 12, fill="#333333", width=2,
                           arrow=tk.LAST, arrowshape=(10, 12, 4))
        canvas.create_text(nx, ny + 27, text="N", fill="#333333", font=("Helvetica", 9, "bold"))

        # Scale bar
        bar_km = next((km for km in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500)
                       if km * 1000 * scale >= (width - 2 * pad) * 0.25), 500)
        bar_px = bar_km * 1000 * scale
        bx, by = pad, height - 24
        canvas.create_line(bx, by, bx + bar_px, by, fill="#333333", width=3)
        for end_x in (bx, bx + bar_px):
            canvas.create_line(end_x, by - 5, end_x, by + 5, fill="#333333", width=2)
        canvas.create_text(bx + bar_px / 2, by - 13, text=f"{bar_km} km", fill="#333333",
                           font=("Helvetica", 9))

        # Header / legend
        meta = self._preview['meta']
        canvas.create_text(pad, 20, anchor="w", fill="#222222", font=("Helvetica", 10, "bold"),
                           text=f"{meta['area_name']}  —  {meta['lines']} survey lines @ "
                                f"{meta['heading']:.0f}°T  —  {meta['dist_nm']:.1f} nm / "
                                f"{meta['time_min']:.0f} min")
        if hidden:
            canvas.create_text(pad, 34, anchor="w", fill="#a06000", font=("Helvetica", 8, "italic"),
                               text=f"{hidden} label(s) hidden to avoid overlap — enlarge the window to see them")
        legend_top = 50 if hidden else 40
        legend = [("#1f6fd0", "Target buffer envelope"),
                  ("#d81b1b", "Flight track & exported waypoints (ring = first, square = last)"),
                  ("#7b2fbe", "Boundary waypoints")]
        for row, (color, text) in enumerate(legend):
            ly = legend_top + row * 16
            canvas.create_line(pad, ly, pad + 18, ly, fill=color, width=3)
            canvas.create_text(pad + 24, ly, anchor="w", text=text, fill="#555555",
                               font=("Helvetica", 8))

    def _load_defaults(self):
        # Pre-populate fields with default Clairmont Fire Coordinates
        for idx, (lat, lon, label) in enumerate(self.survey_boundary):
            self.coord_rows[idx][0].insert(0, str(lat))
            self.coord_rows[idx][1].insert(0, str(lon))
            self.coord_rows[idx][2].insert(0, label)

    def _clear_gps_fields(self):
        for lat, lon, lbl in self.coord_rows:
            lat.delete(0, tk.END)
            lon.delete(0, tk.END)
            lbl.delete(0, tk.END)

    # --- FLIGHT PLAN SAVE / LOAD -----------------------------------------------------

    PLAN_SCHEMA = "airborne_survey_plan/1"

    def _plan_as_dict(self):
        boundary = []
        for lat_entry, lon_entry, lbl_entry in self.coord_rows:
            lat, lon = lat_entry.get().strip(), lon_entry.get().strip()
            if lat or lon:
                boundary.append({"lat": lat, "lon": lon, "label": lbl_entry.get().strip()})
        return {
            "schema": self.PLAN_SCHEMA,
            "area_name": self.area_name_entry.get().strip(),
            "waypoint_prefix": self.prefix_entry.get().strip(),
            "saved_utc": time.strftime('%Y-%m-%d %H:%MZ', time.gmtime()),
            # Stored as typed so a round-trip does not silently reformat "10.0" to "10".
            "parameters": {key: entry.get().strip() for key, entry in self.inputs.items()},
            "rectangular_box": self.rectangular_box.get(),
            "repeats": self.repeats.get(),
            "boundary": boundary,
        }

    def _apply_plan_dict(self, data):
        if not isinstance(data, dict) or "boundary" not in data:
            raise ValueError("Not a flight plan file: no 'boundary' key.")
        schema = data.get("schema")
        if schema and schema != self.PLAN_SCHEMA:
            raise ValueError(f"Unsupported plan schema {schema!r}; expected {self.PLAN_SCHEMA!r}.")

        boundary = data["boundary"]
        if len(boundary) > len(self.coord_rows):
            raise ValueError(f"File has {len(boundary)} boundary points; "
                             f"only {len(self.coord_rows)} rows are available.")

        for key, value in (data.get("parameters") or {}).items():
            if key in self.inputs:
                self.inputs[key].delete(0, tk.END)
                self.inputs[key].insert(0, str(value))

        if "rectangular_box" in data:
            self.rectangular_box.set(bool(data["rectangular_box"]))
        if "repeats" in data:
            try:
                self.repeats.set(str(min(4, max(1, int(float(data["repeats"]))))))
            except (TypeError, ValueError):
                self.repeats.set("1")

        if data.get("area_name"):
            self.area_name_entry.delete(0, tk.END)
            self.area_name_entry.insert(0, str(data["area_name"]))
        if data.get("waypoint_prefix"):
            self.prefix_entry.delete(0, tk.END)
            self.prefix_entry.insert(0, str(data["waypoint_prefix"]))

        self._clear_gps_fields()
        for row, point in zip(self.coord_rows, boundary):
            row[0].insert(0, str(point.get("lat", "")))
            row[1].insert(0, str(point.get("lon", "")))
            row[2].insert(0, str(point.get("label", "")))

    def _plan_dialog_dir(self):
        """Open the dialogs on this plan's own folder, falling back to plans/."""
        area_name = self.area_name_entry.get().strip().replace(' ', '_')
        for candidate in (os.path.join(os.getcwd(), PLANS_DIR, area_name) if area_name else None,
                          os.path.join(os.getcwd(), PLANS_DIR)):
            if candidate and os.path.isdir(candidate):
                return candidate
        return os.getcwd()

    def _save_plan(self):
        area_name = self.area_name_entry.get().strip().replace(' ', '_') or "survey_area"
        path = filedialog.asksaveasfilename(
            title="Save Flight Plan", defaultextension=".json",
            initialdir=self._plan_dialog_dir(),
            initialfile=f"{area_name}_plan.json",
            filetypes=[("Flight plan", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._plan_as_dict(), f, indent=2)
        except OSError as err:
            messagebox.showerror("Save Failed", str(err))
            return
        self.status_var.set(f"Saved plan to {path}")

    def _load_plan(self):
        path = filedialog.askopenfilename(
            title="Load Flight Plan",
            initialdir=self._plan_dialog_dir(),
            filetypes=[("Flight plan", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            self._apply_plan_dict(data)
        except (OSError, ValueError, json.JSONDecodeError) as err:
            messagebox.showerror("Load Failed", f"{path}\n\n{err}")
            return
        self.status_var.set(f"Loaded plan {os.path.basename(path)} — regenerating.")
        self.calculate_and_render()

    def _get_active_coordinates(self):
        active_coords = []
        for idx, (lat_entry, lon_entry, lbl_entry) in enumerate(self.coord_rows):
            lat_val = lat_entry.get().strip()
            lon_val = lon_entry.get().strip()
            lbl_val = lbl_entry.get().strip()

            if lat_val or lon_val:
                try:
                    lat = float(lat_val)
                    lon = float(lon_val)
                    lbl = lbl_val if lbl_val else f"WP{idx+1:02d}"
                    active_coords.append((lat, lon, lbl))
                except ValueError:
                    raise ValueError(f"Invalid decimal format in Row {idx+1}.")
        return active_coords

    def calculate_and_render(self):
        # 1. Harvest Export Configs and Validate Prefix
        area_name = self.area_name_entry.get().strip().replace(' ', '_')
        waypoint_prefix = self.prefix_entry.get().strip().upper()

        if not area_name:
            messagebox.showerror("Input Error", "Filename/Area Name cannot be empty.")
            return
        # Names are built as <prefix>L<n>S, so the L and S/F already satisfy ForeFlight's
        # "at least 3 characters including a letter, no spaces" rule for any short prefix.
        if not waypoint_prefix or not waypoint_prefix.isalnum() or len(waypoint_prefix) > 4:
            messagebox.showerror("Input Error",
                                 "Line ID Prefix must be 1-4 letters or digits (e.g. '1').")
            return

        # 2. Harvest & Validate Coordinates from GPS list
        try:
            survey_boundary = self._get_active_coordinates()
        except ValueError as err:
            messagebox.showerror("Input Error", str(err))
            return

        if len(survey_boundary) < 3:
            messagebox.showerror("Input Error", "At least 3 valid GPS coordinate rows must be filled to create a closed survey boundary.")
            return

        # 3. Re-calculate projection parameters dynamically based on current centroid
        center_lat = np.mean([lat for lat, lon, *_ in survey_boundary])
        center_lon = np.mean([lon for lat, lon, *_ in survey_boundary])
        
        crs_geo = CRS.from_epsg(4326)
        utm_zone = int((center_lon + 180.0) // 6.0) + 1
        epsg_code = 32600 + utm_zone if center_lat >= 0 else 32700 + utm_zone
        crs_local = CRS.from_epsg(epsg_code)

        to_m = Transformer.from_crs(crs_geo, crs_local, always_xy=True)
        to_geo = Transformer.from_crs(crs_local, crs_geo, always_xy=True)

        def xy_to_latlon(xy_points):
            return [(to_geo.transform(x, y)[1], to_geo.transform(x, y)[0]) for x, y in xy_points]

        # 4. Parse Flight Settings Safely
        try:
            gs = float(self.inputs["groundspeed_kt"].get())
            swath = float(self.inputs["swath_width_km"].get())
            overlap = float(self.inputs["swath_overlap"].get())
            margin = float(self.inputs["perimeter_margin_km"].get())
            heading = float(self.inputs["initial_heading_deg"].get())
            lat_off = float(self.inputs["lat_offset"].get())
            lon_off = float(self.inputs["lon_offset"].get())
        except ValueError:
            messagebox.showerror("Input Error", "Ensure all Flight settings are valid numbers.")
            return

        # Combobox is read-only so this only guards a hand-edited area file.
        try:
            repeats = min(4, max(1, int(float(self.repeats.get()))))
        except ValueError:
            repeats = 1
        self.repeats.set(str(repeats))

        # 5. Generate the flight pattern path
        try:
            survey_poly, survey_pattern, segments = build_rectangular_pattern(
                survey_boundary, swath, overlap, margin, heading, lat_off, lon_off, to_m, center_lat,
                rectangular=self.rectangular_box.get(), repeats=repeats
            )
        except Exception as e:
            messagebox.showerror("Execution Error", str(e))
            return

        # 6. Every artefact for this plan lands in plans/<name>/, and that folder carries
        #    the plan itself so it can be reloaded without retyping the points.
        out_dir = os.path.join(os.getcwd(), PLANS_DIR, area_name)
        try:
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, f"{area_name}_plan.json"), 'w', encoding='utf-8') as f:
                json.dump(self._plan_as_dict(), f, indent=2)
        except OSError as err:
            messagebox.showerror("Output Folder Error", f"{out_dir}\n\n{err}")
            return
        self._output_dir = out_dir

        ff_file, hw_file, waypoints = self._export_csv_files(
            segments, xy_to_latlon, area_name, waypoint_prefix, out_dir)

        # 7. Parse and Print Diagnostics Panel
        self.stats_text.delete("1.0", tk.END)
        segment_summaries, dist_m, dist_nm, time_min = summarize_segment_travel(survey_pattern, gs)

        generated_utc = time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())
        manifest_stamp = time.strftime('%Y%m%dT%H:%M:%SZ', time.gmtime())
        meta = {'lines': len(segments), 'heading': heading,
                'dist_nm': dist_nm, 'time_min': time_min}

        kml_text = build_survey_kml(
            area_name,
            xy_to_latlon(list(survey_poly.exterior.coords)),
            xy_to_latlon(list(survey_pattern.coords)),
            waypoints, survey_boundary, meta, generated_utc,
        )
        kml_file, kmz_file, pack_file = self._export_foreflight_bundle(
            area_name, waypoint_prefix, kml_text, waypoints, generated_utc, manifest_stamp, out_dir)

        # Verify the padding actually achieved rather than assuming the request was met.
        target_poly = Polygon([to_m.transform(lon, lat) for lat, lon, _ in survey_boundary]).buffer(0)
        clearance_m = measure_clearance(target_poly, survey_poly)
        if clearance_m < 0:
            margin_note = (f"Actual Padding: TARGET NOT FULLY COVERED "
                           f"({abs(clearance_m)/1000:.2f} km outside)")
        elif clearance_m < margin * 1000.0 - 1.0:
            margin_note = (f"Actual Padding: {clearance_m/1000:.2f} km "
                           f"— SHORT of the {margin:.2f} km requested")
        else:
            margin_note = f"Actual Padding: {clearance_m/1000:.2f} km on all sides"

        stats_output = [
            f"Survey Identifier: {area_name}",
            f"Prefix Configured: {waypoint_prefix}",
            f"Generated (UTC): {generated_utc}",
            f"Output Folder: {PLANS_DIR}{os.sep}{area_name}{os.sep}",
            f"Active Vertices parsed: {len(survey_boundary)}",
            f"Pattern: {'Rectangular box' if self.rectangular_box.get() else 'Clipped to target outline'}",
            f"Repeats: {repeats}x  ({len(segments) // repeats} lines per cycle)",
            f"Generated Survey Lines: {len(segments)}",
            f"Ground Heading: {heading:.1f}° True",
            f"Requested Margin: {margin:.2f} km",
            margin_note,
            f"Path Metrics: {dist_m/1000:.2f} km ({dist_nm:.2f} nm)",
            f"Est. Flight Time: {time_min:.1f} min",
            f"Wrote Output: {os.path.basename(ff_file)}",
            f"Wrote Output: {os.path.basename(hw_file)}",
            f"ForeFlight layer: {os.path.basename(kml_file)}",
            f"ForeFlight layer: {os.path.basename(kmz_file)}",
            f"Share with pilot: {os.path.basename(pack_file)}",
            "-" * 41
        ]
        for idx, dm, dnm, tmin in segment_summaries:
            stats_output.append(f"Segment {idx:02d}: {dm/1000:.1f} km | {tmin:.1f} min")

        self.stats_text.insert(tk.END, "\n".join(stats_output))

        # 8. Refresh the in-window preview (UTM metres, matching the geometry engine)
        self._preview = {
            'rect': list(survey_poly.exterior.coords),
            'track': list(survey_pattern.coords),
            'marks': [(*to_m.transform(lon, lat), label) for lat, lon, label in survey_boundary],
            'waypoints': [(*to_m.transform(lon, lat), name) for name, lat, lon in waypoints],
            'meta': {
                'area_name': area_name,
                'lines': len(segments),
                'heading': heading,
                'dist_nm': dist_nm,
                'time_min': time_min,
            },
        }
        self._draw_preview()

        # 9. Render dynamic map using Folium
        survey_map = folium.Map(location=[center_lat, center_lon], zoom_start=12)

        # Hull Perimeter Boundaries (Blue Envelope)
        hull_latlon = xy_to_latlon(list(survey_poly.exterior.coords))
        folium.PolyLine(hull_latlon, color='blue', weight=3, opacity=0.7, tooltip='Target Buffer Envelope').add_to(survey_map)

        # Input GPS Waypoint Marks (Purple Icons)
        for idx, point in enumerate(survey_boundary, start=1):
            lat, lon = point[0], point[1]
            wp_name = point[2]
            folium.Marker(
                location=[lat, lon],
                popup=f"{wp_name}: ({lat:.5f}, {lon:.5f})",
                tooltip=wp_name,
                icon=folium.Icon(color='purple', icon='info-sign')
            ).add_to(survey_map)

        # Track Flight Path Overlay (Red Strip)
        pattern_latlon = xy_to_latlon(list(survey_pattern.coords))
        folium.PolyLine(pattern_latlon, color='red', weight=4, opacity=0.9, dash_array='5, 6', tooltip='Flight Track').add_to(survey_map)

        # 10. Save the interactive map for the browser button (Leaflet needs a real browser)
        self._map_path = os.path.join(out_dir, f"{area_name}_flight_path.html")
        survey_map.save(self._map_path)

        self._run_count += 1
        short = clearance_m < margin * 1000.0 - 1.0
        self.status_var.set(
            f"Generated {generated_utc} (run #{self._run_count}) into "
            f"{PLANS_DIR}{os.sep}{area_name}{os.sep}: "
            f"{len(segments)} lines, {dist_nm:.1f} nm, "
            f"{clearance_m/1000:.2f} km padding{' (SHORT — check offsets)' if short else ''}. "
            f"Send {os.path.basename(pack_file)} to the pilot."
        )

    def _export_csv_files(self, flown_segments, conversion_func, area_name, line_prefix, out_dir):
        """Write both flight-plan CSVs in the exact formats the pilot supplied samples for.

        Waypoints are named per survey line as <prefix>L<n>S / <prefix>L<n>F -- Start and
        Finish of line n -- matching the pilot's `1L1S` / `1L1F` convention. Turns between
        lines carry no waypoint, exactly as in his samples. Each named pair brackets one
        straight run, so a concave area that splits a row into two runs numbers them as
        separate lines rather than producing an ambiguous name.
        """
        # Single source of truth for waypoint names: whatever gets written here is what
        # the preview labels and the KML use, so they cannot disagree with these files.
        waypoints = []
        for idx, segment in enumerate(flown_segments, start=1):
            ends = conversion_func([segment.coords[0], segment.coords[-1]])
            (start_lat, start_lon), (end_lat, end_lon) = ends
            waypoints.append((f"{line_prefix}L{idx}S", start_lat, start_lon))
            waypoints.append((f"{line_prefix}L{idx}F", end_lat, end_lon))

        # Pilot's sample carries 7-8 decimals; 4 was ~11 m of avoidable rounding.
        ff_file = os.path.join(out_dir, f"{area_name}_waypoints_foreflight.csv")
        with open(ff_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Waypoint', 'Description', 'LAT', 'LONG'])
            for name, lat, lon in waypoints:
                writer.writerow([name, 'NA', f'{lat:.8f}', f'{lon:.8f}'])

        hw_file = os.path.join(out_dir, f"{area_name}_waypoints_honeywell.csv")
        with open(hw_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['E', 'WPT', 'FIX', 'LAT', 'LON'])
            for name, lat, lon in waypoints:
                lat_fmt = dd_to_honeywell_format(lat, 'N', 'S', degree_digits=2)
                lon_fmt = dd_to_honeywell_format(lon, 'E', 'W', degree_digits=3)
                writer.writerow(['X', name, 'NA', lat_fmt, lon_fmt])

        return ff_file, hw_file, waypoints

    def _export_foreflight_bundle(self, area_name, waypoint_prefix, kml_text, waypoints,
                                  generated_utc, manifest_stamp, out_dir):
        """Write the KML overlay, a KMZ copy, and a content pack bundling both with the CSV.

        Three transfer paths, because ForeFlight accepts them differently (foreflight.md):
          .kml  -- AirDrop / email / "Copy to ForeFlight"; imports as a User Map Layer
          .kmz  -- same, zipped; survives mail clients that mangle raw XML attachments
          .zip  -- content pack: the only route that gets a waypoint CSV onto the iPad
                   without iTunes/Finder, so this is the file to share with the pilot
        """
        kml_file = os.path.join(out_dir, f"{area_name}_survey.kml")
        with open(kml_file, 'w', encoding='utf-8', newline='\n') as f:
            f.write(kml_text)

        kmz_file = os.path.join(out_dir, f"{area_name}_survey.kmz")
        with zipfile.ZipFile(kmz_file, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('doc.kml', kml_text)

        # ForeFlight requires the waypoint CSV to be named exactly user_waypoints.csv, and
        # it must sit in the pack's navdata/ folder. Column order matters, names do not.
        wp_csv = io.StringIO()
        writer = csv.writer(wp_csv, lineterminator='\n')
        writer.writerow(['Name', 'Description', 'Latitude', 'Longitude'])
        for name, lat, lon in waypoints:
            writer.writerow([name, f'{area_name} {generated_utc}', f'{lat:.4f}', f'{lon:.4f}'])

        # ForeFlight uses the manifest to identify pack versions, so a hardcoded version
        # meant every regeneration claimed to be the same pack -- a re-import could be
        # treated as already-installed and silently ignored, leaving yesterday's lines
        # loaded. Derive it from the generation time: YYYYMMDDHHMM, so it always increases.
        # The name carries the stamp too, so two generations are distinguishable in the
        # Custom Content list rather than both reading "<AREA> Survey".
        pack_version = int(''.join(ch for ch in manifest_stamp if ch.isdigit())[:12])
        manifest = {
            "name": f"{area_name} Survey {generated_utc}",
            "abbreviation": waypoint_prefix,
            "version": pack_version,
            "effectiveDate": manifest_stamp,
        }

        pack_file = os.path.join(out_dir, f"{area_name}_foreflight_pack.zip")
        root = f"{area_name}_survey"
        with zipfile.ZipFile(pack_file, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(f'{root}/manifest.json', json.dumps(manifest, indent=2))
            z.writestr(f'{root}/layers/{area_name}_survey.kml', kml_text)
            z.writestr(f'{root}/navdata/user_waypoints.csv', wp_csv.getvalue())

        return kml_file, kmz_file, pack_file

    def _open_export_folder(self):
        """Reveal the output directory so the files can be AirDropped or attached."""
        folder = self._output_dir or os.getcwd()
        try:
            if sys.platform == 'win32':
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.run(['open', folder], check=False)
            else:
                subprocess.run(['xdg-open', folder], check=False)
        except Exception as err:
            messagebox.showerror("Could Not Open Folder", f"{folder}\n\n{err}")

if __name__ == "__main__":
    app = FlightPlannerGUI()
    app.mainloop()
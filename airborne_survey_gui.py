import os
import csv
import io
import json
import math
import re
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
from shapely.geometry import LineString, Polygon, MultiLineString, GeometryCollection, Point
from shapely import affinity
from pyproj import CRS, Transformer

# folium is imported lazily inside calculate_and_render's render_map(). It costs ~1.1 s of
# import on its own -- it pulls in the whole of pandas -- and only the HTML map needs it, so
# paying for it at module scope delayed every launch. Startup time matters here because the
# app ships to the pilot as a PyInstaller build. Keep it out of the module header.

# Generated output lives under here, one directory per named plan, so it never litters
# the project root. Git-ignored wholesale.
PLANS_DIR = "plans"

# Records which plan was last worked on, so a relaunch comes back to it rather than the
# built-in demo area. Kept inside plans/ on purpose: it is regenerable state, not
# configuration, so clearing plans/ correctly forgets it and the project root stays clean.
LAST_PLAN_POINTER = ".last_plan"

# Rows offered for transit waypoints, in each of the before/after groups.
TRANSIT_ROWS = 6

# "Waypoint names entered into the navigation data base are limited to a maximum of five
# characters" -- Jeppesen NavData name conventions, following ARINC 424. Anything longer is
# not a style question: the box will not take it.
MAX_WAYPOINT_NAME = 5

# The pilot's box will not take a longer list than this -- his number, and he said "for now",
# so treat it as a setting that may move rather than a law like the five-character ceiling.
# Warned about rather than enforced: the file is still written, because the limit is his
# equipment's and the plan may be for someone else's.
MAX_FMS_WAYPOINTS = 123

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


def build_rectangular_pattern(latlon_coords, swath_km, overlap, perimeter_margin_km, initial_heading_deg, lat_offset, lon_offset, transformer_to_m, center_lat, rectangular=True, repeats=1, retrace=False, entry_xy=None, exit_xy=None, skip_edges=0):
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
    row_ys = []
    for current_y in candidate_ys:
        pass_line = LineString([(minx - 10000, current_y), (maxx + 10000, current_y)])
        segments = flatten_linestrings(pass_line.intersection(clip_region))
        if segments:
            pass_rows.append(sorted(segments, key=lambda s: s.centroid.x))
            row_ys.append(current_y)

    if not pass_rows:
        raise ValueError('No pass segments could be generated. Adjust coordinates or swath width.')

    # Dropping the outermost rows on both sides. The remaining rows renumber from 1 by
    # falling out of assemble()'s enumerate, which is what should happen -- the plan that
    # gets flown is the plan, and its lines read 1..n.
    #
    # This is real coverage given up, so skip_band trims the region reported as flown.
    # Without it measure_clearance would keep measuring against the full box and the summary
    # would go on claiming a perimeter margin that is no longer being flown -- the one way
    # this feature could actually hurt someone. Left as None when nothing is skipped, so
    # existing plans report exactly what they did before.
    skip_band = None
    skip_edges = max(0, int(skip_edges))
    if skip_edges > 0:
        keep = len(pass_rows) - 2 * skip_edges
        if keep < 1:
            raise ValueError(
                f'Skipping {skip_edges} line(s) at each end leaves nothing to fly: this '
                f'area only makes {len(pass_rows)} line(s). Reduce the skip, narrow the '
                f'swath, or widen the perimeter margin.')
        pass_rows = pass_rows[skip_edges:skip_edges + keep]
        row_ys = row_ys[skip_edges:skip_edges + keep]
        # The retained lines cover their own band plus half a swath beyond the outermost
        # on each side. Built in the rotated frame like everything else here.
        half_swath = swath_km * 1000.0 / 2.0
        lo, hi = row_ys[0] - half_swath, row_ys[-1] + half_swath
        skip_band = Polygon([(minx, lo), (maxx, lo), (maxx, hi), (minx, hi)])

    def assemble(flip_rows, flip_direction):
        """One candidate pattern: which row to begin at, and which end of it.

        Returns (segment, line_number) pairs. The line number is the row's position in
        flight order, and a retrace keeps its row's number rather than taking a new one --
        the pilot's scheme names the physical ends of a line, so flying it out and back
        revisits the same two names.
        """
        segments = []
        rows = list(reversed(pass_rows)) if flip_rows else pass_rows
        for row_idx, row_segments in enumerate(rows):
            # Retracing returns the aircraft to the end it started from, so the usual
            # boustrophedon alternation is dropped: every row is entered from the same
            # side, which keeps the turn onto the next row down to the line spacing.
            # Alternating as well would mean crossing the full width of the box.
            alternate = (row_idx % 2 == 1) and not retrace
            reverse = alternate != flip_direction        # xor
            ordered = list(reversed(row_segments)) if reverse else row_segments
            for segment in ordered:
                # Orient each pass along the direction it is actually flown, so the
                # exporter can name its ends Start and Finish without re-deriving order.
                oriented = sorted(segment.coords, key=lambda c: c[0])
                if reverse:
                    oriented = oriented[::-1]
                segments.append((LineString(oriented), row_idx + 1))
                if retrace:
                    # Same ground, opposite direction, SAME line number.
                    segments.append((LineString(oriented[::-1]), row_idx + 1))
        return segments

    def unrotated(point):
        return affinity.rotate(Point(point), heading_angle_deg, origin=pivot,
                               use_radians=False).coords[0]

    # The pattern can start at either end of either extreme row -- four corners, all
    # covering the same ground. Pick the one that makes the run in from the last transit
    # waypoint (and the run out to the next) shortest, instead of always starting at the
    # bottom-left and crossing the box to get there.
    best = None
    for flip_rows in (False, True):
        for flip_direction in (False, True):
            candidate = assemble(flip_rows, flip_direction)
            cost = 0.0
            if entry_xy is not None:
                cost += math.dist(entry_xy, unrotated(candidate[0][0].coords[0]))
            if exit_xy is not None:
                cost += math.dist(unrotated(candidate[-1][0].coords[-1]), exit_xy)
            if best is None or cost < best[0]:
                best = (cost, candidate)
    cycle_segments = best[1]

    # Repeats fly the whole box again from the top, so the line directions and sensor
    # geometry of every cycle are identical. Line numbers restart each cycle, because a
    # name identifies a physical end of a line -- flying it again revisits that name.
    pass_pairs = cycle_segments * max(1, int(repeats))
    pass_segments = [seg for seg, _line in pass_pairs]
    line_numbers = [line for _seg, line in pass_pairs]

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
    # Applied after the mode choice so it trims either shape, and rotated with the same
    # pivot as everything else -- the margin bug this file already carries a section about
    # was exactly a second pivot creeping in.
    if skip_band is not None:
        flown_region = flown_region.intersection(
            affinity.rotate(skip_band, heading_angle_deg, origin=pivot, use_radians=False))
    return flown_region, rotated_pattern, flown_segments, line_numbers

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


def line_end_labels(segment):
    """Which compass end of its line each end of this segment sits at.

    Returns (start_letter, finish_letter). The pair is N/S for lines running predominantly
    north-south and E/W for lines running predominantly east-west, decided by which axis
    the line is closer to. Flying north means starting at the South end, hence ('S', 'N').
    """
    (x0, y0), (x1, y1) = segment.coords[0], segment.coords[-1]
    dx, dy = x1 - x0, y1 - y0
    bearing = math.degrees(math.atan2(dx, dy)) % 180.0
    if bearing < 45.0 or bearing >= 135.0:
        return ('S', 'N') if dy > 0 else ('N', 'S')
    return ('W', 'E') if dx > 0 else ('E', 'W')


def lead_in_point(first_segment, lead_km):
    """A point `lead_km` back from a line's start, along that line's own bearing.

    Extending backwards rather than sideways is the whole point: the aircraft flies the
    lead-in on the survey heading, so it is wings-level and established on track by the
    time the line -- and the sensor run -- actually begins.
    """
    if lead_km <= 0:
        return None
    (x0, y0), (x1, y1) = first_segment.coords[0], first_segment.coords[-1]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        return None
    return (x0 - dx / length * lead_km * 1000.0,
            y0 - dy / length * lead_km * 1000.0)


def foreflight_waypoint_name(raw, fallback):
    """Coerce a user-typed label into something ForeFlight will accept as a waypoint name.

    Its rules: all capitals, at least 3 characters, at least one letter, no spaces. Anything
    that cannot be salvaged falls back to the generated name rather than silently producing
    a row ForeFlight would reject on import.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", (raw or "").strip()).upper().strip("_")
    if len(cleaned) >= 3 and any(ch.isalpha() for ch in cleaned):
        return cleaned
    return fallback


def build_foreflight_route(waypoints, origin, destination, flight_level,
                           before=(), after=(), cruise_kt=None):
    """ForeFlight route text and its foreflightmobile:// URL.

    `APT@<origin>` then one `<lat>N/<lon>W` token per waypoint, the destination, and finally
    the trailing performance tokens ForeFlight documents as
    `ORIGIN+...+DESTINATION+SPEED+FUEL_BURN+ALTITUDE`.

    The altitude and speed are stated **once, at the end**, not on every waypoint. The
    per-waypoint `/F<level>` form exists to mark a *change* partway along a route; a survey
    flies one level throughout, so repeating it cost 5 characters on every point -- 400 on an
    80-waypoint plan -- and bought nothing. Trailing cruise altitude is documented as plain
    feet, so FL200 goes out as 20000; the F-prefixed form is only for mid-route changes.

    Without a speed token ForeFlight loads the route but complains "Cruise TAS required for
    performance calculations", so the groundspeed is sent as the cruise speed. That is an
    approximation -- ForeFlight reads it as true airspeed and applies its own winds on top --
    but it is the only speed this app knows, and it is what the time estimates already use.

    Coordinates go out at 4 decimals, about 11 m: far finer than a survey line needs, and it
    keeps the URL short enough to stay a scannable QR code.
    """
    def coord_token(lat, lon):
        return (f"{abs(lat):.4f}{'N' if lat >= 0 else 'S'}/"
                f"{abs(lon):.4f}{'E' if lon >= 0 else 'W'}")

    def transit_tokens(points):
        # An identifier goes in as-is -- ForeFlight resolves it, and it costs ~6 characters
        # against ~19 for a coordinate pair, which keeps the QR code sparser.
        return [p["ident"] if p["ident"] else coord_token(p["lat"], p["lon"]) for p in points]

    parts = (transit_tokens(before)
             + [coord_token(lat, lon) for _name, lat, lon in waypoints]
             + transit_tokens(after))
    route_text = f"APT@{origin}+" + "+".join(parts) + f"+{destination}"

    # Both are best-effort: a junk entry drops its token rather than emitting something
    # ForeFlight would choke on. The route itself is still perfectly usable without them.
    try:
        if cruise_kt is not None and float(cruise_kt) > 0:
            route_text += f"+{int(float(cruise_kt))}kts"
    except (TypeError, ValueError):
        pass
    try:
        route_text += f"+{int(float(flight_level)) * 100}"
    except (TypeError, ValueError):
        pass

    return route_text, f"foreflightmobile://maps/search?q={route_text}"


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
        self.geometry("1500x900")
        
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
        # Closure that writes the Folium map, outstanding until the idle queue runs it,
        # plus the id of the scheduled callback so it can be cancelled.
        self._pending_map = None
        self._map_after_id = None
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
        self.retrace_lines = tk.BooleanVar(value=False)
        # Strips the lead-in and the transit legs out of everything that gets drawn or
        # written, leaving the survey box alone. Deliberately NOT a different calculation:
        # see calculate_and_render for why the transit points still steer the geometry.
        self.survey_only = tk.BooleanVar(value=False)
        # Rows dropped from each end of the box. Trades coverage at the edges for flight
        # time; the summary reports the padding that survives, which may go negative.
        self.skip_edges = tk.StringVar(value="0")

        # QR view replaces the flight path in the same pane, so the code gets the full
        # width -- a dense survey needs every pixel per module to stay scannable.
        self.showing_qr = False
        self.qr_button_text = tk.StringVar(value="Show QR")
        self._route = None

        # Preview zoom. _zoom is the map region (metres) to fill the pane with, or None for
        # the full extent; _view caches the last draw's transform so a pixel rectangle can
        # be turned back into map coordinates.
        self._zoom = None
        self._view = None
        self._drag_origin = None

        self._setup_layout()
        self._load_defaults()
        # Defaults first, then last session's plan over the top, so a failure to restore
        # leaves a usable app rather than empty fields.
        restored = self._restore_last_plan()
        self.calculate_and_render()
        if restored:
            self.status_var.set(f"Reopened {os.path.basename(restored)} from your last "
                                f"session. {self.status_var.get()}")

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

        # Waypoints come out as <prefix>L<nn>S / <prefix>L<nn>F. Empty by default: a
        # navigation database allows five characters, and L999F already uses all five.
        prefix_label = ttk.Label(param_tab, text="Line ID Prefix (optional):")
        prefix_label.grid(row=2, column=0, sticky="w", pady=5)
        self.prefix_entry = ttk.Entry(param_tab, width=10)
        ToolTip(prefix_label,
                "Optional block letter in front of every waypoint name, for telling two "
                "survey areas apart in the same database.\n\n"
                "Names are L01S, L01F, L02S … — Line 01 Start, Line 01 Finish. A navigation "
                "database caps identifiers at five characters, so a prefix only fits while "
                "the line count stays under 100. Leave it empty unless you need it.")
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
                       "geometry matches between cycles. Each cycle reuses the same names, "
                       "because a name identifies an end of a line rather than a step in "
                       "the sequence — 3 lines flown twice is SL01..NL03 over again.\n\n"
                       "The transit back to line 1 carries no waypoint.")
        ToolTip(repeats_box, repeats_tip)
        ToolTip(repeats_label, repeats_tip)

        retrace_check = ttk.Checkbutton(param_tab, text="Retrace line",
                                        variable=self.retrace_lines,
                                        command=self.calculate_and_render)
        retrace_check.grid(row=6, column=1, sticky="w", padx=10)
        survey_only_check = ttk.Checkbutton(param_tab, text="Survey box only",
                                            variable=self.survey_only,
                                            command=self.calculate_and_render)
        survey_only_check.grid(row=7, column=1, sticky="w", padx=10)
        ToolTip(survey_only_check,
                "Show and export the survey lines alone — no lead-in waypoint, no transit "
                "legs. For working on the box without the run-in cluttering the view.\n\n"
                "The survey itself does not change. The transit points are still read and "
                "still decide which corner the box starts at, so the lines, their names and "
                "their order are identical with this on or off — only what gets drawn and "
                "written is filtered.\n\n"
                "Distances drop to the survey alone, so the total no longer reflects the "
                "whole sortie. Turn it off before sending anything to the pilot.")
        ToolTip(retrace_check,
                "Fly each line out and back before moving to the next one, instead of "
                "once through.\n\n"
                "The return run keeps its line's number and revisits its two names, so "
                "three rows read SL01, NL01, SL01, SL02, NL02, SL02, ... — the turnaround "
                "listed once.\n\n"
                "Distance on the lines doubles; the turns between rows do not, so the "
                "total grows by less than 2x — 78 km becomes 139 km on the default area.\n\n"
                "Rows are no longer alternated when this is on — retracing puts you back "
                "at the end you started from, so every row is entered from the same side "
                "and the turn between rows stays short.")

        skip_label = ttk.Label(param_tab, text="Skip Edge Lines (each end):")
        skip_label.grid(row=8, column=0, sticky="w", pady=5)
        skip_box = ttk.Combobox(param_tab, width=12, state="readonly",
                                values=("0", "1", "2", "3"), textvariable=self.skip_edges)
        skip_box.grid(row=8, column=1, sticky="w", pady=5, padx=10)
        skip_box.bind("<<ComboboxSelected>>", lambda _e: self.calculate_and_render())
        skip_tip = ("Drop this many lines from EACH end of the box — 1 turns 7 lines into "
                    "5, removing the outermost pass on both sides.\n\n"
                    "The outer lines are the ones mostly over the perimeter margin rather "
                    "than the target, so this buys back flight time where the least is "
                    "burning. The remaining lines renumber from 1.\n\n"
                    "It gives up real coverage: Actual Padding in the summary drops by the "
                    "line spacing for each one skipped, and will read TARGET NOT FULLY "
                    "COVERED once the target itself is being clipped. Trust that figure — "
                    "it is measured, not assumed.")
        ToolTip(skip_box, skip_tip)
        ToolTip(skip_label, skip_tip)

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
            ("Longitude Offset (deg):", "lon_offset", "0.0"),
            # 0 disables it. Anything else puts a waypoint that far back along the first
            # line's own bearing, so the aircraft is wings-level on track before the line.
            ("Lead-in Distance (km):", "lead_in_km", "0.0"),
            # Route endpoints and cruise level for the ForeFlight route link and its QR
            # code. Not used by the survey geometry at all.
            ("Origin Airport:", "origin_airport", "KBOI"),
            ("Destination Airport:", "destination_airport", "KBOI"),
            ("Survey Flight Level:", "survey_altitude", "200"),
        ]

        # Starts below the rows above: 4 rectangular box, 5 repeats, 6 retrace,
        # 7 survey-box-only, 8 skip edge lines. Adding a control means moving this, or the
        # first field lands in the same cell and Tk silently overlaps the two.
        for i, (label_text, dict_key, default_val) in enumerate(fields, start=9):
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
        move_header = ttk.Label(coords_tab, text="Move", font=("Helvetica", 9, "bold"))
        move_header.grid(row=0, column=4, columnspan=2, padx=2, pady=2)
        ToolTip(move_header,
                "Swap a row with the one above or below, so a new point can be slotted "
                "between two filled rows.\n\n"
                "These rows are the polygon's vertices in order, so reordering them "
                "changes the shape of the survey area, not just the list. Press Generate "
                "to see the result.")

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
            self._add_row_movers(coords_tab, self.coord_rows, i, i + 1, 4)

        # Clear Coordinates Button
        clear_btn = ttk.Button(coords_tab, text="Clear All GPS Fields", command=self._clear_gps_fields)
        clear_btn.grid(row=12, column=1, columnspan=3, pady=5, sticky="ew")

        # TAB 3: TRANSIT WAYPOINTS, BEFORE AND AFTER THE SURVEY BOX
        transit_tab = ttk.Frame(notebook, padding=10)
        notebook.add(transit_tab, text="Waypoints")

        ttk.Label(transit_tab, wraplength=520, justify=tk.LEFT, foreground="#555555",
                  text="Legs flown before reaching the survey box and after leaving it. "
                       "Fill EITHER an identifier (KBOI, BOI, DANDD) OR a lat/lon pair. "
                       "Use Move to reorder — rows are flown top to bottom.").grid(
            row=0, column=0, columnspan=7, sticky="w", pady=(0, 8))

        self.transit_rows = {}
        row_cursor = 1
        for group, heading in (("before", "Before the survey box"),
                               ("after", "After the survey box")):
            ttk.Label(transit_tab, text=heading, font=("Helvetica", 10, "bold")).grid(
                row=row_cursor, column=0, columnspan=5, sticky="w", pady=(8, 2))
            row_cursor += 1
            for col, title in enumerate(("#", "Identifier", "Latitude", "Longitude", "Label")):
                ttk.Label(transit_tab, text=title, font=("Helvetica", 9, "bold")).grid(
                    row=row_cursor, column=col, padx=2, pady=2)
            transit_move_header = ttk.Label(transit_tab, text="Move",
                                            font=("Helvetica", 9, "bold"))
            transit_move_header.grid(row=row_cursor, column=5, columnspan=2, padx=2, pady=2)
            ToolTip(transit_move_header,
                    "Swap a row with the one above or below, so a new waypoint can be "
                    "slotted between two filled rows.\n\n"
                    "Rows are flown top to bottom, so this sets the order of the transit "
                    "legs. Press Generate to rebuild the route.")
            row_cursor += 1

            rows = []
            for i in range(TRANSIT_ROWS):
                ttk.Label(transit_tab, text=f"{i+1}").grid(row=row_cursor, column=0, padx=5, pady=2)
                ident = ttk.Entry(transit_tab, width=10)
                lat = ttk.Entry(transit_tab, width=11)
                lon = ttk.Entry(transit_tab, width=11)
                label = ttk.Entry(transit_tab, width=12)
                for col, widget in enumerate((ident, lat, lon, label), start=1):
                    widget.grid(row=row_cursor, column=col, padx=3, pady=2)
                rows.append((ident, lat, lon, label))
                self._add_row_movers(transit_tab, rows, i, row_cursor, 5)
                row_cursor += 1
            self.transit_rows[group] = rows

        ttk.Button(transit_tab, text="Clear All Waypoints",
                   command=self._clear_transit_fields).grid(
            row=row_cursor, column=1, columnspan=4, pady=8, sticky="ew")

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
        # Used for the over-limit banner. The panel is a wall of Courier, so a warning set
        # in the same weight and colour as everything else reads as just another row.
        self.stats_text.tag_configure("warn", foreground="#b00020",
                                      font=("Courier", 11, "bold"))

        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(5, weight=1)

        # --- FLIGHT PATH PREVIEW ---
        # Drawn on a native Tk canvas: the Folium map is Leaflet/JavaScript, which the
        # embedded HTML widget cannot execute, so it only ever rendered a blank page.
        preview_header = ttk.Frame(right_frame, padding=(8, 6))
        preview_header.pack(fill=tk.X)
        ttk.Label(preview_header, text="Flight Path Preview", font=("Helvetica", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(preview_header, text="Open Map in Browser",
                   command=self._open_map_in_browser).pack(side=tk.RIGHT)
        ttk.Button(preview_header, text="Show Export Files",
                   command=self._open_export_folder).pack(side=tk.RIGHT, padx=(0, 8))
        self.zoom_button = ttk.Button(preview_header, text="Reset Zoom", width=11,
                                      command=self._reset_zoom)
        self.zoom_button.state(["disabled"])
        self.zoom_button.pack(side=tk.RIGHT, padx=(0, 8))
        ToolTip(self.zoom_button,
                "Drag a box on the preview to zoom into it. Right-click the preview, or "
                "press this, to go back to the full extent.\n\n"
                "The selection is grown to the pane's shape before zooming, so a metre "
                "stays a metre in both directions and the pattern is never stretched.")

        qr_button = ttk.Button(preview_header, textvariable=self.qr_button_text,
                               width=10, command=self._toggle_qr)
        qr_button.pack(side=tk.RIGHT, padx=(0, 8))
        ToolTip(qr_button,
                "Swap this pane between the flight path and a QR code of the ForeFlight "
                "route link.\n\n"
                "Scanning it on the iPad opens ForeFlight with the whole survey as a "
                "route, origin and destination included — no file transfer needed.\n\n"
                "Route endpoints and the flight level come from the Origin Airport, "
                "Destination Airport and Survey Flight Level fields.")
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
        self.preview_canvas.bind("<ButtonPress-1>", self._on_zoom_press)
        self.preview_canvas.bind("<B1-Motion>", self._on_zoom_drag)
        self.preview_canvas.bind("<ButtonRelease-1>", self._on_zoom_release)
        # Button-2 as well as 3: right-click is the middle button number on some macOS setups.
        for sequence in ("<Button-3>", "<Button-2>"):
            self.preview_canvas.bind(sequence, self._reset_zoom)

    def _cancel_map_callback(self):
        """Drop any queued _flush_map, without touching the pending closure itself.

        A callback left in Tk's queue when the window goes away fires against a command
        that no longer exists, which Tcl reports on stderr as an invalid command name.
        """
        if self._map_after_id is not None:
            try:
                self.after_cancel(self._map_after_id)
            except Exception:
                pass
            self._map_after_id = None

    def _flush_map(self):
        """Write the deferred Folium map if one is outstanding.

        Runs off the idle queue after a calculation, and synchronously from anything that
        needs the file to exist. The pending closure is cleared *before* it runs, so a
        failure reports once rather than being retried by the next caller.
        """
        self._cancel_map_callback()
        render, self._pending_map = self._pending_map, None
        if render is None:
            return
        try:
            render()
        except Exception as err:
            messagebox.showerror("Map Not Written", f"{self._map_path}\n\n{err}")

    def destroy(self):
        """Settle the deferred map before the window goes away.

        The idle callback is dropped on exit, so quitting straight after a run would leave
        the plan folder one file short of what it advertises. Costs nothing in the normal
        case, where the queue has long since drained.
        """
        try:
            self._flush_map()
        except Exception:
            pass                          # never let a map failure block the window closing
        super().destroy()

    def _open_map_in_browser(self):
        # The map may still be queued; the click is exactly the moment to pay for it.
        self._flush_map()
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

    # --- PREVIEW ZOOM ----------------------------------------------------------------

    def _update_zoom_controls(self):
        self.zoom_button.state(["!disabled"] if self._zoom else ["disabled"])

    def _reset_zoom(self, _event=None):
        if self._zoom is None:
            return
        self._zoom = None
        self._update_zoom_controls()
        self._draw_preview()

    def _on_zoom_press(self, event):
        # No zooming in the QR view -- it is not a map.
        self._drag_origin = None if (self.showing_qr or not self._view) else (event.x, event.y)

    def _on_zoom_drag(self, event):
        if not self._drag_origin:
            return
        x0, y0 = self._drag_origin
        self.preview_canvas.delete("zoombox")
        self.preview_canvas.create_rectangle(x0, y0, event.x, event.y, outline="#1f6fd0",
                                             width=1, dash=(4, 3), tags="zoombox")

    def _on_zoom_release(self, event):
        if not self._drag_origin:
            return
        x0, y0 = self._drag_origin
        self._drag_origin = None
        self.preview_canvas.delete("zoombox")
        # A click, or a twitch while clicking, is not a zoom request.
        if abs(event.x - x0) < 12 or abs(event.y - y0) < 12:
            return
        self._zoom = self._pixels_to_region(x0, y0, event.x, event.y)
        self._update_zoom_controls()
        self._draw_preview()

    def _pixels_to_region(self, x0, y0, x1, y1):
        """Turn a dragged pixel rectangle into a map region, in metres."""
        view = self._view

        def to_map(px, py):
            return (view['min_x'] + (px - view['off_x']) / view['scale'],
                    view['min_y'] + (view['base_y'] - py) / view['scale'])

        (ax, ay), (bx, by) = to_map(x0, y0), to_map(x1, y1)
        min_x, max_x = sorted((ax, bx))
        min_y, max_y = sorted((ay, by))

        # Grow the selection to the drawing area's aspect ratio. Without this a wide, flat
        # drag would stretch the pattern, and this preview's whole point is that a metre
        # across reads the same as a metre up.
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)
        pane_aspect = max(view['avail_w'], 1.0) / max(view['avail_h'], 1.0)
        if span_x / span_y < pane_aspect:
            grow = (span_y * pane_aspect - span_x) / 2.0
            min_x, max_x = min_x - grow, max_x + grow
        else:
            grow = (span_x / pane_aspect - span_y) / 2.0
            min_y, max_y = min_y - grow, max_y + grow
        return (min_x, min_y, max_x, max_y)

    def _toggle_qr(self):
        """Flip the pane between the flight path and the route QR code."""
        self.showing_qr = not self.showing_qr
        self.qr_button_text.set("Hide QR" if self.showing_qr else "Show QR")
        self._draw_preview()

    def _draw_qr(self):
        """Draw the route QR straight onto the canvas as rectangles.

        qrcode's make_image() needs Pillow, which this project deliberately does without
        (see the preview pane). get_matrix() gives the module grid with no image library
        at all, and the canvas can paint it directly.
        """
        canvas = self.preview_canvas
        width, height = canvas.winfo_width(), canvas.winfo_height()

        if not self._route:
            canvas.create_text(width / 2, height / 2, fill="#888888", font=("Helvetica", 11),
                               text="Generate a flight plan first.")
            return

        route_text, url = self._route
        try:
            import qrcode
        except ImportError:
            canvas.create_text(width / 2, height / 2, fill="#a01010", font=("Helvetica", 11),
                               width=width - 80, justify=tk.CENTER,
                               text="The qrcode package is not installed.\n\n"
                                    "pip install qrcode\n\n"
                                    "(Pillow is NOT required — the code is drawn directly.)")
            return

        code = qrcode.QRCode(border=4)   # 4 modules of quiet zone, per the QR spec
        code.add_data(url)
        try:
            code.make(fit=True)
            matrix = code.get_matrix()
        except Exception as err:
            canvas.create_text(width / 2, height / 2, fill="#a01010", font=("Helvetica", 11),
                               width=width - 80, justify=tk.CENTER,
                               text=f"Route will not fit in a QR code.\n\n{err}\n\n"
                                    f"{len(url)} characters. Reduce the number of lines.")
            return

        top = 84                      # room for the header lines
        bottom = 76                   # room for the wrapped link and the caption
        modules = len(matrix)
        scale = int(min((width - 40) / modules, (height - top - bottom) / modules))
        if scale < 1:
            canvas.create_text(width / 2, height / 2, fill="#a01010", font=("Helvetica", 11),
                               text="Pane too small to draw the QR code — enlarge the window.")
            return

        side = modules * scale
        off_x = (width - side) / 2.0
        off_y = top + (height - top - bottom - side) / 2.0

        # White backing under the whole symbol, quiet zone included.
        canvas.create_rectangle(off_x, off_y, off_x + side, off_y + side,
                                fill="white", outline="")
        for r, row in enumerate(matrix):
            run_start = None
            for c in range(modules + 1):
                dark = c < modules and row[c]
                if dark and run_start is None:
                    run_start = c
                elif not dark and run_start is not None:
                    # Draw each horizontal run as one rectangle rather than per module:
                    # a version-25 code is 14,641 modules and Tk slows to a crawl at that
                    # many canvas items.
                    canvas.create_rectangle(off_x + run_start * scale, off_y + r * scale,
                                            off_x + c * scale, off_y + (r + 1) * scale,
                                            fill="#000000", outline="")
                    run_start = None

        meta = self._preview['meta'] if self._preview else {}
        canvas.create_text(40, 24, anchor="w", fill="#222222", font=("Helvetica", 11, "bold"),
                           text=f"{meta.get('area_name', 'Survey')} — ForeFlight route link")
        canvas.create_text(40, 46, anchor="w", fill="#555555", font=("Helvetica", 9),
                           text=f"Scan with the iPad camera to open this survey as a route "
                                f"in ForeFlight. QR version {code.version}, {modules} modules.")
        canvas.create_text(40, 64, anchor="w", fill="#555555", font=("Helvetica", 9),
                           text=f"{route_text.count('+') - 1} waypoints between "
                                f"{route_text.split('@')[1].split('+')[0]} and "
                                f"{route_text.rsplit('+', 1)[1]}   ·   {len(url)} characters")
        canvas.create_text(width / 2, height - 44, fill="#777777", font=("Helvetica", 8),
                           width=width - 80, justify=tk.CENTER,
                           text=url if len(url) <= 110 else url[:107] + "...")
        canvas.create_text(width / 2, height - 16, fill="#777777", font=("Helvetica", 8),
                           text=f"Full link saved next to the exports as "
                                f"{meta.get('area_name', '<AREA>')}_foreflight_route.txt")

    def _draw_preview(self):
        """Render the cached survey geometry (UTM metres, so already equal-aspect)."""
        canvas = self.preview_canvas
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 60 or height < 60:
            return

        if self.showing_qr:
            self._draw_qr()
            return

        if not self._preview:
            canvas.create_text(width / 2, height / 2, fill="#888888", font=("Helvetica", 11),
                               text="Click 'Generate Flight Plan & Update Map' to draw the survey pattern.")
            return

        rect = self._preview['rect']
        track = self._preview['track']
        marks = self._preview['marks']

        transit_before = self._preview.get('transit_before', [])
        transit_after = self._preview.get('transit_after', [])
        points = (list(rect) + list(track) + [(x, y) for x, y, _ in marks]
                  + [(x, y) for x, y, _ in transit_before + transit_after])
        if self._zoom:
            min_x, min_y, max_x, max_y = self._zoom
        else:
            min_x = min(p[0] for p in points)
            max_x = max(p[0] for p in points)
            min_y = min(p[1] for p in points)
            max_y = max(p[1] for p in points)
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)

        # Asymmetric padding: the top band is reserved for the header and legend and the
        # bottom for the scale bar, so the pattern is never drawn underneath either.
        pad, pad_top, pad_bottom = 60, 108, 52
        avail_w = width - 2 * pad
        avail_h = height - pad_top - pad_bottom
        scale = min(avail_w / span_x, avail_h / span_y)
        off_x = (width - span_x * scale) / 2.0
        base_y = pad_top + (avail_h + span_y * scale) / 2.0   # screen y of min_y

        # Cached so a dragged pixel rectangle can be converted back into map metres.
        self._view = {'min_x': min_x, 'min_y': min_y, 'scale': scale, 'off_x': off_x,
                      'base_y': base_y, 'avail_w': avail_w, 'avail_h': avail_h}

        def to_px(point):
            return (off_x + (point[0] - min_x) * scale,
                    base_y - (point[1] - min_y) * scale)

        # Target buffer envelope
        rect_px = [coord for point in rect for coord in to_px(point)]
        canvas.create_polygon(rect_px, fill="#eaf1fb", outline="#1f6fd0", width=2)

        # Transit legs, drawn under the survey track so the survey stays dominant
        if track:
            for chain, joins_at in ((transit_before, track[0]), (transit_after, track[-1])):
                if not chain:
                    continue
                leg = [to_px((x, y)) for x, y, _ in chain]
                leg = leg + [to_px(joins_at)] if chain is transit_before else [to_px(joins_at)] + leg
                for start, end in zip(leg, leg[1:]):
                    canvas.create_line(start[0], start[1], end[0], end[1], fill="#8a8a8a",
                                       width=2, dash=(2, 5), arrow=tk.LAST,
                                       arrowshape=(10, 12, 4))

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

        # Transit waypoints: grey, so they read as "getting there" not "surveying"
        for x, y, name in transit_before + transit_after:
            px, py = to_px((x, y))
            canvas.create_rectangle(px - 4, py - 4, px + 4, py + 4,
                                    fill="#666666", outline="white", width=1)
            if show_wp:
                spots = [(px, py - 14, "center"), (px, py + 14, "center"),
                         (px + 10, py, "w"), (px - 10, py, "e")]
                if not any(self._label(lx, ly, name, anchor=la, fill="#444444", avoid=placed)
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
        if transit_before or transit_after:
            legend.append(("#8a8a8a", "Transit legs to and from the box"))
        canvas.create_text(width - pad, 20, anchor="e",
                           fill="#a06000" if self._zoom else "#999999",
                           font=("Helvetica", 8, "bold" if self._zoom else "normal"),
                           text="ZOOMED — right-click to reset" if self._zoom
                                else "drag a box to zoom")
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

    def _move_row(self, rows, index, delta):
        """Swap one grid row's contents with its neighbour.

        Swapping the text rather than re-gridding the widgets keeps the layout untouched,
        and every consumer reads these grids top to bottom anyway, so order is all that
        matters. Does not recalculate -- it is an edit like typing, so press Generate.
        """
        target = index + delta
        if not 0 <= target < len(rows):
            return
        for here, there in zip(rows[index], rows[target]):
            mine, theirs = here.get(), there.get()
            here.delete(0, tk.END)
            here.insert(0, theirs)
            there.delete(0, tk.END)
            there.insert(0, mine)

    def _add_row_movers(self, parent, rows, index, grid_row, first_col):
        """Up/down buttons for one row of a coordinate grid."""
        for offset, (glyph, delta) in enumerate((("▲", -1), ("▼", 1))):
            ttk.Button(parent, text=glyph, width=2,
                       command=lambda i=index, d=delta: self._move_row(rows, i, d)).grid(
                row=grid_row, column=first_col + offset, padx=(4 if offset == 0 else 1, 1))

    def _clear_transit_fields(self):
        for rows in self.transit_rows.values():
            for widgets in rows:
                for widget in widgets:
                    widget.delete(0, tk.END)

    def _get_transit_points(self, group, line_prefix):
        """Transit waypoints for one group, in the order flown.

        A row needs either an identifier or a complete lat/lon pair. Identifier-only rows
        reach the route link but cannot be drawn or written to the waypoint CSVs, because
        their position is unknown here -- ForeFlight resolves them, this app cannot.
        """
        tag = 'B' if group == 'before' else 'A'
        points = []
        for idx, (ident_e, lat_e, lon_e, label_e) in enumerate(self.transit_rows[group], start=1):
            ident = ident_e.get().strip().upper()
            lat_raw, lon_raw = lat_e.get().strip(), lon_e.get().strip()
            if not (ident or lat_raw or lon_raw):
                continue

            lat = lon = None
            if lat_raw or lon_raw:
                try:
                    lat, lon = float(lat_raw), float(lon_raw)
                except ValueError:
                    raise ValueError(f"{group.capitalize()} waypoint row {idx}: latitude and "
                                     f"longitude must both be decimal numbers, or leave both "
                                     f"blank and give an identifier.")

            # Padded to two digits so an empty prefix still clears ForeFlight's
            # three-character minimum: B01, not B1.
            fallback = f"{line_prefix}{tag}{idx:02d}"
            points.append({
                "ident": ident or None,
                "lat": lat,
                "lon": lon,
                "name": foreflight_waypoint_name(label_e.get() or ident, fallback),
            })
        return points

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
            "retrace_lines": self.retrace_lines.get(),
            "survey_only": self.survey_only.get(),
            "skip_edges": self.skip_edges.get(),
            "boundary": boundary,
            "transit": {
                group: [{"ident": i.get().strip(), "lat": la.get().strip(),
                         "lon": lo.get().strip(), "label": lb.get().strip()}
                        for i, la, lo, lb in rows
                        if any(w.get().strip() for w in (i, la, lo, lb))]
                for group, rows in self.transit_rows.items()
            },
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
        if "retrace_lines" in data:
            self.retrace_lines.set(bool(data["retrace_lines"]))
        if "survey_only" in data:
            self.survey_only.set(bool(data["survey_only"]))
        if "skip_edges" in data:
            # Clamped to the dropdown's range in case the file was hand-edited.
            try:
                self.skip_edges.set(str(min(3, max(0, int(float(data["skip_edges"]))))))
            except (TypeError, ValueError):
                self.skip_edges.set("0")
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

        transit = data.get("transit") or {}
        for group, rows in self.transit_rows.items():
            if len(transit.get(group, [])) > len(rows):
                raise ValueError(f"File has {len(transit[group])} '{group}' waypoints; "
                                 f"only {len(rows)} rows are available.")

        self._clear_gps_fields()
        for row, point in zip(self.coord_rows, boundary):
            row[0].insert(0, str(point.get("lat", "")))
            row[1].insert(0, str(point.get("lon", "")))
            row[2].insert(0, str(point.get("label", "")))

        # Absent for plans saved before transit waypoints existed, which simply means none.
        self._clear_transit_fields()
        for group, rows in self.transit_rows.items():
            for widgets, point in zip(rows, transit.get(group, [])):
                for widget, key in zip(widgets, ("ident", "lat", "lon", "label")):
                    widget.insert(0, str(point.get(key, "")))

    def _plans_root(self):
        root = os.path.join(os.getcwd(), PLANS_DIR)
        return root if os.path.isdir(root) else os.getcwd()

    def _save_dialog_dir(self):
        """Saving defaults into this plan's own folder, beside its other outputs."""
        area_name = self.area_name_entry.get().strip().replace(' ', '_')
        own = os.path.join(os.getcwd(), PLANS_DIR, area_name) if area_name else None
        return own if own and os.path.isdir(own) else self._plans_root()

    def _pointer_path(self):
        return os.path.join(os.getcwd(), PLANS_DIR, LAST_PLAN_POINTER)

    def _remember_plan(self, path):
        """Note which plan to reopen next launch.

        Best-effort by design: failing to record where you were is not worth interrupting
        a run that otherwise succeeded, so this never raises and never reports.
        """
        try:
            os.makedirs(os.path.join(os.getcwd(), PLANS_DIR), exist_ok=True)
            with open(self._pointer_path(), 'w', encoding='utf-8') as f:
                f.write(os.path.abspath(path))
        except OSError:
            pass

    def _restore_last_plan(self):
        """Reopen the plan from the last session over the top of the built-in defaults.

        Deliberately forgiving, and the reason it catches broadly: this runs before the
        window is usable, so a pointer to a deleted, moved or hand-broken plan must leave
        the user with a working app on the defaults rather than a startup crash or a modal
        they cannot get past. Returns the path restored, or None if the defaults stand.
        """
        try:
            with open(self._pointer_path(), encoding='utf-8') as f:
                path = f.read().strip()
            if not path or not os.path.isfile(path):
                return None
            with open(path, encoding='utf-8') as f:
                self._apply_plan_dict(json.load(f))
            return path
        except Exception:
            return None

    def _save_plan(self):
        area_name = self.area_name_entry.get().strip().replace(' ', '_') or "survey_area"
        path = filedialog.asksaveasfilename(
            title="Save Flight Plan", defaultextension=".json",
            initialdir=self._save_dialog_dir(),
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
        self._remember_plan(path)
        self.status_var.set(f"Saved plan to {path}")

    def _load_plan(self):
        path = filedialog.askopenfilename(
            title="Load Flight Plan",
            # plans/ rather than one plan's folder: loading is for picking
            # between plans, not for rummaging inside the current one.
            initialdir=self._plans_root(),
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
        # A zoom from the previous plan means nothing over a different area.
        self._zoom = None
        self._update_zoom_controls()
        self._remember_plan(path)
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
        # Optional. The L and the S/F already satisfy ForeFlight's "at least 3 characters
        # including a letter" rule, and _export_csv_files enforces the 5-character ceiling
        # on the finished name, which is what actually matters.
        if waypoint_prefix and (not waypoint_prefix.isalnum() or len(waypoint_prefix) > 2):
            messagebox.showerror("Input Error",
                                 "Line ID Prefix must be empty, or 1-2 letters or digits.")
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
            lead_in_km = max(0.0, float(self.inputs["lead_in_km"].get()))
        except ValueError:
            messagebox.showerror("Input Error", "Ensure all Flight settings are valid numbers.")
            return

        # Combobox is read-only so this only guards a hand-edited area file.
        try:
            repeats = min(4, max(1, int(float(self.repeats.get()))))
        except ValueError:
            repeats = 1
        self.repeats.set(str(repeats))

        # Same guard as repeats: read-only combobox, so this only catches a hand-edited plan.
        try:
            skip_edges = max(0, int(float(self.skip_edges.get())))
        except ValueError:
            skip_edges = 0
        self.skip_edges.set(str(skip_edges))

        # 5. Transit waypoints first: the pattern needs to know which side the aircraft
        #    arrives from before it can pick which corner of the box to start at.
        try:
            before = self._get_transit_points("before", waypoint_prefix)
            after = self._get_transit_points("after", waypoint_prefix)
        except ValueError as err:
            messagebox.showerror("Input Error", str(err))
            return

        mapped_before = [p for p in before if p["lat"] is not None]
        mapped_after = [p for p in after if p["lat"] is not None]
        entry_xy = to_m.transform(mapped_before[-1]["lon"], mapped_before[-1]["lat"]) \
            if mapped_before else None
        exit_xy = to_m.transform(mapped_after[0]["lon"], mapped_after[0]["lat"]) \
            if mapped_after else None

        # "Survey box only" filters the output; it does not plan a different flight. The
        # entry/exit hints above are taken from the real transit points first, so the corner
        # the box starts at -- and therefore the lines, their names and their order -- comes
        # out identical either way. Dropping the lists here means nothing downstream (CSV,
        # KML, content pack, route link, preview, Folium map) sees them, with no per-consumer
        # flag to keep in step. If this ran before the hints, ticking a view checkbox would
        # silently move the survey, which is exactly the trap worth avoiding.
        survey_only = self.survey_only.get()
        if survey_only:
            before = after = []
            mapped_before = mapped_after = []

        try:
            survey_poly, survey_pattern, segments, line_numbers = build_rectangular_pattern(
                survey_boundary, swath, overlap, margin, heading, lat_off, lon_off, to_m, center_lat,
                rectangular=self.rectangular_box.get(), repeats=repeats,
                retrace=self.retrace_lines.get(), entry_xy=entry_xy, exit_xy=exit_xy,
                skip_edges=skip_edges
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
        # Generating into an area is what "working on it" means, so the pointer follows the
        # run rather than only an explicit Save. It points at this canonical copy even if
        # the plan was loaded from somewhere else -- the contents are the same, and this one
        # is guaranteed to sit beside the outputs it produced.
        self._remember_plan(os.path.join(out_dir, f"{area_name}_plan.json"))

        # The lead-in is flown, so it belongs on the path as well as in the waypoint list.
        # It sits outside the box, so "survey box only" suppresses it along with the transit.
        lead_xy = lead_in_point(segments[0], lead_in_km) \
            if segments and not survey_only else None
        if lead_xy is not None:
            survey_pattern = LineString([lead_xy] + list(survey_pattern.coords))

        try:
            ff_file, hw_file, waypoints, survey_waypoints = self._export_csv_files(
                segments, xy_to_latlon, area_name, waypoint_prefix, out_dir, before, after,
                lead_xy, line_numbers)
        except ValueError as err:
            messagebox.showerror("Waypoint Naming Error", str(err))
            return

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

        # Route link for the QR view and for pasting into an email. Nothing here feeds the
        # survey geometry, so bad input degrades the link rather than the flight plan.
        route_text, route_url = build_foreflight_route(
            survey_waypoints,
            self.inputs["origin_airport"].get().strip().upper() or "KBOI",
            self.inputs["destination_airport"].get().strip().upper() or "KBOI",
            self.inputs["survey_altitude"].get().strip() or "200",
            before=before, after=after,
            cruise_kt=self.inputs["groundspeed_kt"].get().strip(),
        )
        self._route = (route_text, route_url)
        route_file = os.path.join(out_dir, f"{area_name}_foreflight_route.txt")
        with open(route_file, 'w', encoding='utf-8') as f:
            f.write(route_text + "\n\n" + route_url + "\n")

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
            # With retrace on, half the "lines" are return runs over ground already flown,
            # so spell out how many distinct tracks that actually is.
            f"Retrace: on — {max(line_numbers)} lines flown out and back"
            if self.retrace_lines.get() else "Retrace: off",
            # Distinct lines, then how many passes are flown over them.
            f"Survey Lines: {max(line_numbers)}   ({len(segments)} passes)",
            f"Skipped Edges: {skip_edges} per end ({2 * skip_edges} lines dropped)"
            if skip_edges else "Skipped Edges: none",
            # Distinguish "you set it to 0" from "it is set but this view suppresses it",
            # or the panel reads as though the lead-in had been lost.
            f"Lead-in: {lead_in_km:.2f} km on the line bearing" if lead_xy is not None
            else f"Lead-in: {lead_in_km:.2f} km, hidden by Survey box only"
            if survey_only and lead_in_km > 0 else "Lead-in: off",
            "Scope: SURVEY BOX ONLY — lead-in and transit excluded" if survey_only
            else "Scope: full plan",
            f"Ground Heading: {heading:.1f}° True",
            f"Requested Margin: {margin:.2f} km",
            margin_note,
            f"Survey Path: {dist_m/1000:.2f} km ({dist_nm:.2f} nm)",
            f"Est. Survey Time: {time_min:.1f} min",
            # Row count of both CSVs, after consecutive duplicate names are collapsed --
            # what the pilot actually loads, not the number of line ends the geometry has.
            f"Waypoints in CSV: {len(waypoints)}"
            + (f" / {MAX_FMS_WAYPOINTS} max" if len(waypoints) > MAX_FMS_WAYPOINTS else "")
            + (f"   ({len(survey_waypoints)} survey + "
               f"{len(waypoints) - len(survey_waypoints)} transit)"
               if len(waypoints) != len(survey_waypoints) else ""),
            f"Wrote Output: {os.path.basename(ff_file)}",
            f"Wrote Output: {os.path.basename(hw_file)}",
            f"ForeFlight layer: {os.path.basename(kml_file)}",
            f"ForeFlight layer: {os.path.basename(kmz_file)}",
            f"Share with pilot: {os.path.basename(pack_file)}",
            f"Route link: {os.path.basename(route_file)}",
        ]

        # Transit distance covers only the rows that carry coordinates: an identifier's
        # position is unknown here, so including it would be a guess.
        if before or after:
            transit_m = 0.0
            for chain, anchor in ((mapped_before, segments[0].coords[0]),
                                  (mapped_after, segments[-1].coords[-1])):
                if not chain:
                    continue
                legs = [to_m.transform(p["lon"], p["lat"]) for p in chain]
                legs = legs + [anchor] if chain is mapped_before else [anchor] + legs
                transit_m += sum(math.dist(a, b) for a, b in zip(legs, legs[1:]))
            transit_nm = transit_m / 1852.0
            unmapped = (len(before) - len(mapped_before)) + (len(after) - len(mapped_after))
            stats_output.append(
                f"Transit Legs: {len(before)} before / {len(after)} after")
            stats_output.append(
                f"Transit Path: {transit_m/1000:.2f} km ({transit_nm:.2f} nm)"
                + (f"  [{unmapped} by identifier, not counted]" if unmapped else ""))
            if gs > 0:
                total_nm = dist_nm + transit_nm
                stats_output.append(
                    f"Total w/ Transit: {total_nm:.2f} nm, {(total_nm / gs) * 60.0:.1f} min")

        stats_output.append("-" * 41)
        for idx, dm, dnm, tmin in segment_summaries:
            stats_output.append(f"Segment {idx:02d}: {dm/1000:.1f} km | {tmin:.1f} min")

        self.stats_text.insert(tk.END, "\n".join(stats_output))

        # A file the pilot's box will not load is the one thing here worth interrupting for,
        # so it goes to the top of the panel in red rather than taking its turn in the list.
        # Not a modal: the count is exceeded by ordinary settings (0.75 km swath, or retrace
        # with repeats), and a dialog on every recalculation would train him to dismiss it.
        over_limit = len(waypoints) > MAX_FMS_WAYPOINTS
        if over_limit:
            self.stats_text.insert(
                "1.0",
                f"!! {len(waypoints)} waypoints — the pilot's software takes "
                f"{MAX_FMS_WAYPOINTS}.\n"
                f"   Widen the swath, cut repeats, or turn retrace off.\n\n",
                "warn")

        # 8. Refresh the in-window preview (UTM metres, matching the geometry engine)
        self._preview = {
            'rect': list(survey_poly.exterior.coords),
            'track': list(survey_pattern.coords),
            'marks': [(*to_m.transform(lon, lat), label) for lat, lon, label in survey_boundary],
            # Survey line ends only; transit points get their own grey markers below so the
            # two are never confused in the air.
            'waypoints': [(*to_m.transform(lon, lat), name) for name, lat, lon in survey_waypoints],
            # Transit legs run from the last mapped "before" point into the first survey
            # waypoint, and out of the last one into the "after" points. Identifier-only
            # rows are absent, so a leg may simply be missing rather than wrong.
            'transit_before': [(*to_m.transform(p["lon"], p["lat"]), p["name"])
                               for p in before if p["lat"] is not None],
            'transit_after': [(*to_m.transform(p["lon"], p["lat"]), p["name"])
                              for p in after if p["lat"] is not None],
            'meta': {
                'area_name': area_name,
                'lines': len(segments),
                'heading': heading,
                'dist_nm': dist_nm,
                'time_min': time_min,
            },
        }
        self._draw_preview()

        # 9. The Folium map is the slowest thing in a run: folium alone is ~1.1 s of
        # import because it drags in pandas, and the map is only ever read through the
        # browser button. Import and build are both deferred to the idle queue so the
        # window paints first. Anything needing the file on disk calls _flush_map().
        self._map_path = os.path.join(out_dir, f"{area_name}_flight_path.html")

        def render_map():
            import folium

            survey_map = folium.Map(location=[center_lat, center_lon], zoom_start=12)

            # Colours match the canvas preview so the two views read the same way. Each group
            # is its own FeatureGroup, so LayerControl can switch it off -- the only sane way
            # to cope with a dense survey putting 50+ markers on the map.
            hull_latlon = xy_to_latlon(list(survey_poly.exterior.coords))
            folium.PolyLine(hull_latlon, color='#1f6fd0', weight=3, opacity=0.7,
                            tooltip='Target buffer envelope').add_to(survey_map)

            pattern_latlon = xy_to_latlon(list(survey_pattern.coords))
            folium.PolyLine(pattern_latlon, color='#d81b1b', weight=4, opacity=0.9,
                            dash_array='5, 6', tooltip='Survey flight track').add_to(survey_map)

            boundary_layer = folium.FeatureGroup(name='Boundary points')
            for point in survey_boundary:
                lat, lon, wp_name = point[0], point[1], point[2]
                folium.Marker(
                    location=[lat, lon],
                    popup=f"{wp_name}: ({lat:.5f}, {lon:.5f})",
                    tooltip=wp_name,
                    icon=folium.Icon(color='purple', icon='info-sign')
                ).add_to(boundary_layer)
            boundary_layer.add_to(survey_map)

            # Labels ride along permanently while there are few enough to read; past that they
            # would be an unreadable mat, so they fall back to hover.
            label_always = len(survey_waypoints) <= 24
            waypoint_layer = folium.FeatureGroup(name='Survey waypoints')
            for name, lat, lon in survey_waypoints:
                folium.CircleMarker(
                    location=[lat, lon], radius=4, color='#d81b1b', weight=1,
                    fill=True, fill_color='#d81b1b', fill_opacity=0.9,
                    tooltip=folium.Tooltip(name, permanent=label_always, direction='right'),
                    popup=f"{name}<br>{lat:.6f}, {lon:.6f}",
                ).add_to(waypoint_layer)
            waypoint_layer.add_to(survey_map)

            if mapped_before or mapped_after:
                transit_layer = folium.FeatureGroup(name='Transit legs')
                for chain, anchor, leads in ((mapped_before, pattern_latlon[0], True),
                                             (mapped_after, pattern_latlon[-1], False)):
                    if not chain:
                        continue
                    points = [(p["lat"], p["lon"]) for p in chain]
                    leg = points + [anchor] if leads else [anchor] + points
                    folium.PolyLine(leg, color='#8a8a8a', weight=3, opacity=0.9,
                                    dash_array='2, 8',
                                    tooltip='Transit to the box' if leads
                                            else 'Transit from the box').add_to(transit_layer)
                    for p in chain:
                        folium.CircleMarker(
                            location=[p["lat"], p["lon"]], radius=5, color='#444444', weight=2,
                            fill=True, fill_color='#8a8a8a', fill_opacity=0.9,
                            tooltip=folium.Tooltip(p["name"], permanent=True, direction='right'),
                            popup=f"{p['name']}<br>{p['lat']:.6f}, {p['lon']:.6f}",
                        ).add_to(transit_layer)
                transit_layer.add_to(survey_map)

            folium.LayerControl(collapsed=False).add_to(survey_map)

            # 10. Save the interactive map for the browser button (Leaflet needs a real browser)
            survey_map.save(self._map_path)

        # Drop the previous run's file before queuing this one. Everything else here is
        # written synchronously, so a leftover HTML would sit next to fresh CSVs looking
        # current while describing the last run -- silently wrong beats loudly absent, so
        # a reader that gets in before the idle callback finds nothing rather than a lie.
        if os.path.exists(self._map_path):
            try:
                os.remove(self._map_path)
            except OSError:
                pass                      # still open in a browser; render_map overwrites it
        self._pending_map = render_map
        self._cancel_map_callback()          # supersede the previous run's, if it is queued
        self._map_after_id = self.after_idle(self._flush_map)

        self._run_count += 1
        short = clearance_m < margin * 1000.0 - 1.0
        self.status_var.set(
            f"Generated {generated_utc} (run #{self._run_count}) into "
            f"{PLANS_DIR}{os.sep}{area_name}{os.sep}: "
            f"{len(segments)} lines, {len(waypoints)} waypoints"
            f"{f' (OVER {MAX_FMS_WAYPOINTS} LIMIT)' if over_limit else ''}"
            f"{' [survey box only]' if survey_only else ''}, {dist_nm:.1f} nm, "
            f"{clearance_m/1000:.2f} km padding{' (SHORT — check offsets)' if short else ''}. "
            f"Send {os.path.basename(pack_file)} to the pilot."
        )

    def _export_csv_files(self, flown_segments, conversion_func, area_name, line_prefix, out_dir,
                          before=(), after=(), lead_xy=None, line_numbers=None):
        """Write both flight-plan CSVs in the exact formats the pilot supplied samples for.

        Waypoints are named per survey line as <prefix>L<n>S / <prefix>L<n>F -- Start and
        Finish of line n -- matching the pilot's `1L1S` / `1L1F` convention. Turns between
        lines carry no waypoint, exactly as in his samples. Each named pair brackets one
        straight run, so a concave area that splits a row into two runs numbers them as
        separate lines rather than producing an ambiguous name.
        """
        # Single source of truth for waypoint names: whatever gets written here is what
        # the preview labels and the KML use, so they cannot disagree with these files.
        # Names are <compass end><L><line number>: SL01 is the south end of line 1. Zero-pad
        # to the width the line count needs, minimum two digits, so names sort correctly and
        # still fit the five-character ARINC 424 ceiling.
        line_numbers = list(line_numbers or range(1, len(flown_segments) + 1))
        digits = max(2, len(str(max(line_numbers, default=1))))

        survey = []
        if lead_xy is not None:
            # I for intercept, keeping the <letter>L<nn> shape: IL01 then SL01, NL01.
            (lead_lat, lead_lon), = conversion_func([lead_xy])
            survey.append((f"{line_prefix}IL{line_numbers[0]:0{digits}d}", lead_lat, lead_lon))
        for segment, line in zip(flown_segments, line_numbers):
            ends = conversion_func([segment.coords[0], segment.coords[-1]])
            (start_lat, start_lon), (end_lat, end_lon) = ends
            start_end, finish_end = line_end_labels(segment)
            survey.append((f"{line_prefix}{start_end}L{line:0{digits}d}", start_lat, start_lon))
            survey.append((f"{line_prefix}{finish_end}L{line:0{digits}d}", end_lat, end_lon))

        # A retrace finishes where the next pass begins, so the turnaround would otherwise
        # be listed twice. Collapsing consecutive repeats gives the pilot's sequence
        # exactly: SL01, NL01, SL01 -- three waypoints for an out-and-back, not four.
        collapsed = []
        for entry in survey:
            if not collapsed or collapsed[-1][0] != entry[0]:
                collapsed.append(entry)
        survey = collapsed

        too_long = [name for name, _lat, _lon in survey if len(name) > MAX_WAYPOINT_NAME]
        if too_long:
            raise ValueError(
                f"{len(too_long)} waypoint names would exceed the {MAX_WAYPOINT_NAME}-character "
                f"limit a navigation database allows (e.g. {too_long[0]!r}). Shorten the Line "
                f"ID Prefix, or reduce the line count.")

        # Transit waypoints bracket the survey in flight order. Identifier-only rows are
        # skipped: these files need coordinates, and ForeFlight/the FMS already know where
        # a published identifier is.
        def mapped(points):
            return [(p["name"], p["lat"], p["lon"]) for p in points if p["lat"] is not None]

        waypoints = mapped(before) + survey + mapped(after)

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

        # `waypoints` is everything written (transit included) for the KML and the preview;
        # `survey` is the line ends alone, which is what the route link brackets.
        return ff_file, hw_file, waypoints, survey

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
        # Finish the deferred map first, or the folder opens a file short of what it lists.
        self._flush_map()
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
"""GUI-free survey engine: a plan dictionary in, every export out.

This is what the web app runs (in the browser, under Pyodide), so it must not import
tkinter or touch the disk. The plan dictionary is exactly the `<AREA>_plan.json` the desktop
app saves, so plans move between the two freely.

The section between the PORTED markers is copied verbatim from airborne_survey_gui.py, and
generate() follows that file's calculate_and_render() step for step. The desktop app does
not import this module yet, so a geometry or export fix made there has to be repeated here
-- tests/parity_check.py runs both on the same plans and fails on any difference.
"""
import base64
import csv
import io
import json
import math
import os
import re
import time
import zipfile
from xml.sax.saxutils import escape as xml_escape


def _load_geometry():
    """Import the geometry stack into module scope. Idempotent and cheap after the first."""
    global np, LineString, Polygon, MultiLineString, GeometryCollection, Point
    global affinity, CRS, Transformer
    if "LineString" in globals():
        return
    import numpy as np
    from shapely.geometry import LineString, Polygon, MultiLineString, GeometryCollection, Point
    from shapely import affinity
    from pyproj import CRS, Transformer


PLAN_SCHEMA = "airborne_survey_plan/1"
MAX_WAYPOINT_NAME = 5      # ARINC 424 / Jeppesen NavData ceiling; see the desktop app
MAX_FMS_WAYPOINTS = 123    # the pilot's software; warned about, never enforced
AREA_MODES = ("Boundary GPS (Max 10)", "CenterPoint")

# The ICAO airport table: beside this file, which is where the web page writes it in the
# browser and where it already sits in the repository.
AIRPORT_FILES = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "airports.csv")]

# The desktop form's defaults, used for any parameter a plan file leaves out.
DEFAULT_PARAMETERS = {
    "groundspeed_kt": "200", "transit_groundspeed_kt": "200", "bank_angle_deg": "25", "swath_width_km": "10.0", "swath_overlap": "0.1",
    "perimeter_margin_km": "5.0", "initial_heading_deg": "0", "lat_offset": "0.0",
    "lon_offset": "0.0", "lead_in_km": "0.0", "origin_airport": "KBOI",
    "destination_airport": "KBOI", "survey_altitude": "200", "center_lat": "43.6475",
    "center_lon": "-116.1195", "center_width_km": "12.0", "center_length_km": "18.0",
    "make_line_km": "20.0",
}


class PlanError(ValueError):
    """Bad input, reported to the user with the same title the desktop dialog uses."""

    def __init__(self, title, message):
        super().__init__(message)
        self.title = title


# ---- PORTED verbatim from airborne_survey_gui.py: begin ---------------------------------

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

def flatten_linestrings(geom):
    """Every non-degenerate LineString inside an arbitrary intersection result."""
    _load_geometry()   # safe to call the engine without building the GUI
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


def build_rectangular_pattern(latlon_coords, swath_km, overlap, perimeter_margin_km, initial_heading_deg, lat_offset, lon_offset, transformer_to_m, center_lat, rectangular=True, repeats=1, retrace=False, entry_xy=None, exit_xy=None, skip_edges=0, first_rows=None):
    _load_geometry()   # safe to call the engine without building the GUI
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
    # first_rows pins which outer line is flown first (False: the right-hand edge looking
    # along the heading, True: the left); the entry and exit still pick which end of it.
    for flip_rows in ((False, True) if first_rows is None else (bool(first_rows),)):
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

    # Empty when the survey box is skipped. A Polygon with no coordinates is not valid KML,
    # so the folder goes away rather than being emitted hollow.
    if envelope_latlon:
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


def naming_axis(bearing_deg):
    """"NS" or "EW": which pair of letters names the ends of a line on this bearing.

    N/S for lines running predominantly north-south, E/W for east-west. The diagonals --
    045, 135, 225, 315 -- are N/S. A tolerance of a millionth of a degree absorbs rounding,
    so a heading of exactly 45 is not split between the two by floating-point noise.
    """
    bearing = bearing_deg % 180.0
    return "NS" if bearing <= 45.0 + 1e-6 or bearing >= 135.0 - 1e-6 else "EW"


def line_end_labels(segment, axis=None):
    """Which compass end of its line each end of this segment sits at.

    Returns (start_letter, finish_letter). Flying north means starting at the South end,
    hence ('S', 'N'). `axis` ("NS" or "EW", from naming_axis) names every line of a plan on
    the same pair; give the survey heading's axis for survey lines. Left out, it is worked
    out from this segment's own bearing -- fine for a lone line, but on a survey it let
    rounding name some lines N/S and others E/W at a heading of exactly 45.
    """
    (x0, y0), (x1, y1) = segment.coords[0], segment.coords[-1]
    dx, dy = x1 - x0, y1 - y0
    if axis is None:
        axis = naming_axis(math.degrees(math.atan2(dx, dy)))
    if axis == "NS":
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


# How far the aircraft angles away from the next line before turning back onto it -- the
# outbound angle of a teardrop course reversal, used when the lines are too close together
# for a U-turn. 45 degrees is the conventional figure, and it is always used in full.
DEPARTURE_ANGLE_DEG = 45.0


def turn_radius_m(groundspeed_kt, bank_deg):
    """Radius of a level, coordinated turn: v^2 / (g tan(bank)). Zero means "not modelled".

    At 200 kt and 25 degrees of bank that is 2.3 km, so a turn takes 4.6 km of room -- wider
    than the line spacing of any swath under about 5 km. The speed is the groundspeed field,
    the only speed this app knows; a real turn follows true airspeed and drifts with the
    wind, so treat the result as a planning figure rather than a prediction.
    """
    if groundspeed_kt <= 0 or bank_deg <= 0:
        return 0.0
    speed_ms = groundspeed_kt * 1852.0 / 3600.0
    return speed_ms * speed_ms / (9.80665 * math.tan(math.radians(bank_deg)))


def _fly_pieces(start, theta, pieces, radius):
    """Fly [("L" | "R" | "S", amount), ...] from a pose; arcs in radians, straights in metres.

    An arc may carry its own radius as a third element, ("R", amount, radius); otherwise it
    is flown at `radius`.

    Returns (points, (x, y, theta)): the points exclude `start`, arcs are 10-degree chords.
    Angles are maths angles -- radians, anticlockwise from east -- so "L" adds to theta.
    """
    step = math.radians(10.0)
    x, y = start
    points = []
    default_radius = radius
    for kind, amount, *own in pieces:
        radius = own[0] if own else default_radius
        if amount <= 1e-9:
            continue
        if kind == "S":
            x += amount * math.cos(theta)
            y += amount * math.sin(theta)
            points.append((x, y))
            continue
        side = 1.0 if kind == "L" else -1.0
        centre_x = x - side * radius * math.sin(theta)
        centre_y = y + side * radius * math.cos(theta)
        chords = max(1, int(math.ceil(amount / step)))
        for i in range(1, chords + 1):
            heading = theta + side * amount * i / chords
            points.append((centre_x + side * radius * math.sin(heading),
                           centre_y - side * radius * math.cos(heading)))
        theta += side * amount
        x, y = points[-1]
    return points, (x, y, theta)


def _arrives(pose, end, end_theta):
    off_heading = abs((pose[2] - end_theta + math.pi) % (2.0 * math.pi) - math.pi)
    return math.hypot(pose[0] - end[0], pose[1] - end[1]) <= 1.0 and off_heading <= 1e-3


def dubins_turn(start, start_theta, end, end_theta, radius):
    """Shortest flyable path from one pose to another at a fixed turn radius.

    Poses are a UTM point and the direction of travel as a maths angle. The answer is a
    Dubins path: an arc, then a straight or a third arc, then an arc.

    Returns (points, length_m). The points exclude `start` and end exactly on `end`, so the
    caller can append them to a track. Every candidate is flown numerically and kept only if
    it really arrives at the end pose, so a formula slip can cost optimality but never draws
    a path that does not connect. If nothing arrives, the straight leg is returned.
    """
    two_pi = 2.0 * math.pi
    dx, dy = end[0] - start[0], end[1] - start[1]
    gap = math.hypot(dx, dy)
    d = gap / radius
    phi = math.atan2(dy, dx) if gap > 0 else 0.0
    a, b = (start_theta - phi) % two_pi, (end_theta - phi) % two_pi
    sa, sb, ca, cb, cab = math.sin(a), math.sin(b), math.cos(a), math.cos(b), math.cos(a - b)

    def root(value):                  # rounding leaves exact tangencies a hair negative
        return math.sqrt(max(value, 0.0)) if value > -1e-9 else None

    def arc(value):
        return math.acos(max(-1.0, min(1.0, value))) if abs(value) <= 1.0 + 1e-9 else None

    candidates = []
    p = root(2 + d * d - 2 * cab + 2 * d * (sa - sb))
    if p is not None:
        th = math.atan2(cb - ca, d + sa - sb)
        candidates.append(("LSL", (-a + th) % two_pi, p, (b - th) % two_pi))
    p = root(2 + d * d - 2 * cab + 2 * d * (sb - sa))
    if p is not None:
        th = math.atan2(ca - cb, d - sa + sb)
        candidates.append(("RSR", (a - th) % two_pi, p, (-b + th) % two_pi))
    p = root(-2 + d * d + 2 * cab + 2 * d * (sa + sb))
    if p is not None:
        th = math.atan2(-ca - cb, d + sa + sb) - math.atan2(-2.0, p)
        candidates.append(("LSR", (-a + th) % two_pi, p, (-b + th) % two_pi))
    p = root(d * d - 2 + 2 * cab - 2 * d * (sa + sb))
    if p is not None:
        th = math.atan2(ca + cb, d - sa - sb) - math.atan2(2.0, p)
        candidates.append(("RSL", (a - th) % two_pi, p, (b - th) % two_pi))
    p = arc((6 - d * d + 2 * cab + 2 * d * (sa - sb)) / 8.0)
    if p is not None:
        p = (two_pi - p) % two_pi
        t = (a - math.atan2(ca - cb, d - sa + sb) + p / 2.0) % two_pi
        candidates.append(("RLR", t, p, (a - b - t + p) % two_pi))
    p = arc((6 - d * d + 2 * cab + 2 * d * (sb - sa)) / 8.0)
    if p is not None:
        p = (two_pi - p) % two_pi
        t = (-a - math.atan2(ca - cb, d + sa - sb) + p / 2.0) % two_pi
        candidates.append(("LRL", t, p, (b - a - t + p) % two_pi))

    for kinds, *amounts in sorted(candidates, key=lambda c: c[1] + c[2] + c[3]):
        # The formulas work in turn radii; a straight is the only piece that needs scaling.
        pieces = [(kind, amount * radius if kind == "S" else amount)
                  for kind, amount in zip(kinds, amounts)]
        points, pose = _fly_pieces(start, start_theta, pieces, radius)
        if _arrives(pose, end, end_theta):
            return (points[:-1] if points else []) + [tuple(end)], sum(amounts) * radius
    return [tuple(end)], gap


def bank_for_radius_deg(groundspeed_kt, radius_m):
    """The bank angle that flies a given radius; the inverse of turn_radius_m."""
    if groundspeed_kt <= 0 or radius_m <= 0:
        return 0.0
    speed_ms = groundspeed_kt * 1852.0 / 3600.0
    return math.degrees(math.atan(speed_ms * speed_ms / (9.80665 * radius_m)))


def reversal_turn(start, start_theta, end, end_theta, radius, single_arc=True):
    """Turn round from one line onto a parallel one flown the other way.

    `radius` is the tightest turn allowed. What gets flown depends on how far apart the
    two lines are:

    A turn diameter or more -- ONE arc, half a circle whose diameter is the gap between the
    lines. The pilot rolls in once, holds a single bank, and rolls out on the next line.
    That bank is shallower than the limit whenever the gap is wider than the tightest turn
    needs. The alternative, a tight quarter turn, a straight, and another quarter turn, is
    a little shorter but means banking twice, which pilots would rather not. `single_arc`
    False declines this case, for a join that is not between neighbouring lines: a
    half-circle spanning the whole box is a long way round.

    Under a turn diameter -- the teardrop course reversal, which is how a fast aircraft
    flying a narrow swath gets from one line to the next. One bank cannot do it without
    exceeding the limit, so the aircraft turns *away* from the next line onto an outbound
    leg DEPARTURE_ANGLE_DEG off its heading, flies it for as long as the geometry needs,
    then makes one continuous turn back and rolls out on the next line's extension with a
    straight run in to its start. Lines only a little under a diameter apart would need a
    negative outbound leg at the limiting radius; there the outbound leg is dropped and the
    turn back is flown a little wider instead, so the departure is still the full 45 degrees
    and the bank never exceeds the limit.

    Where the two line ends are not level, the aircraft carries straight on before the turn
    or runs straight in after it; either way wings level, outside the turn itself.

    Returns (points, length_m) like dubins_turn, or None when this is not a reversal or the
    case is declined, and the caller falls back to dubins_turn.
    """
    reversed_heading = abs((start_theta - end_theta) % (2.0 * math.pi) - math.pi) <= 1e-6
    if not reversed_heading:
        return None
    hx, hy = math.cos(start_theta), math.sin(start_theta)
    dx, dy = end[0] - start[0], end[1] - start[1]
    ahead = dx * hx + dy * hy               # how far past this line's end the next one starts
    left = hx * dy - hy * dx                # the next line's offset to the left of this one
    offset = abs(left)

    if offset >= 2.0 * radius - 1e-6:
        if not single_arc:
            return None
        # A half circle comes back level with where it began, so begin it level with
        # whichever line end is further along.
        carry_on, run_in = max(0.0, ahead), max(0.0, -ahead)
        pieces = [("S", carry_on), ("L" if left > 0 else "R", math.pi), ("S", run_in)]
        points, pose = _fly_pieces(start, start_theta, pieces, offset / 2.0)
        if not _arrives(pose, end, end_theta):
            return None
        return points[:-1] + [tuple(end)], carry_on + run_in + math.pi * offset / 2.0

    angle = math.radians(DEPARTURE_ANGLE_DEG)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    outbound = (2.0 * radius * cos_a - offset) / sin_a
    if outbound >= 0.0:
        back_radius = radius
    else:
        # Too wide for an outbound leg: widen the turn back until it fits. Equals `radius`
        # at the hand-over point and reaches about 1.34x it at a full diameter.
        outbound, back_radius = 0.0, (offset + radius * (1.0 - cos_a)) / (1.0 + cos_a)
    away, back = ("L", "R") if left <= 0 else ("R", "L")
    reversal = [(away, angle, radius), ("S", outbound), (back, math.pi + angle, back_radius)]

    # Where the reversal rolls out, measured along this line from its end. Short of the next
    # line's start means carrying straight on first; beyond it, a straight run back in.
    _points, pose = _fly_pieces(start, start_theta, reversal, radius)
    rollout = (pose[0] - start[0]) * hx + (pose[1] - start[1]) * hy
    carry_on, run_in = max(0.0, ahead - rollout), max(0.0, rollout - ahead)
    pieces = [("S", carry_on)] + reversal + [("S", run_in)]
    points, pose = _fly_pieces(start, start_theta, pieces, radius)
    if not points or not _arrives(pose, end, end_theta):
        return None
    length = (carry_on + outbound + run_in + radius * angle
              + back_radius * (math.pi + angle))
    return points[:-1] + [tuple(end)], length


def fly_segments(flown_segments, lead_xy, radius_m, line_numbers=None):
    """The path actually flown over the survey lines, turns included.

    The waypoint list only names line ends; between the end of one line and the start of the
    next the aircraft has to turn round, and that turn is neither free nor a straight leg.
    With a radius, each gap is filled with a turn that leaves one line on its heading and
    rolls out on the next -- see reversal_turn for the shapes. Anything that is not a
    reversal between neighbouring lines (the run back to line 1 for a repeat, say) takes
    the shortest path instead. With radius 0 the gaps are straight legs.

    `line_numbers` says which line each segment is, so neighbours can be told from a join
    across the box; without it every reversal is treated as one between neighbours.

    Returns (track_points, legs), where legs is [(length_m, is_turn), ...] in flight order.
    """
    track, legs = [], []
    if lead_xy is not None:
        track.append(tuple(lead_xy))
    numbers = list(line_numbers) if line_numbers else [1] * len(flown_segments)
    last_end = last_theta = last_number = None
    for segment, number in zip(flown_segments, numbers):
        start, end = tuple(segment.coords[0]), tuple(segment.coords[-1])
        theta = math.atan2(end[1] - start[1], end[0] - start[0])
        if last_end is None:
            if track:
                legs.append((math.dist(track[-1], start), False))     # the lead-in
            track.append(start)
        elif radius_m > 0:
            neighbours = abs(number - last_number) <= 1
            points, length = (
                reversal_turn(last_end, last_theta, start, theta, radius_m, neighbours)
                or dubins_turn(last_end, last_theta, start, theta, radius_m))
            track.extend(points)
            legs.append((length, True))
        elif last_end != start:           # a retrace turns on the spot: no leg to count
            track.append(start)
            legs.append((math.dist(last_end, start), True))
        track.append(end)
        legs.append((segment.length, False))
        last_end, last_theta, last_number = end, theta, number
    return track, legs


def summarize_legs(legs, groundspeed_kt=200.0):
    """Per-leg and total distance and time for [(length_m, is_turn), ...]."""
    def minutes(dist_nm):
        return (dist_nm / groundspeed_kt) * 60.0 if groundspeed_kt > 0 else float('nan')

    summaries = [(idx, dist_m, dist_m / 1852.0, minutes(dist_m / 1852.0), is_turn)
                 for idx, (dist_m, is_turn) in enumerate(legs, start=1)]
    total_m = sum(dist_m for dist_m, _is_turn in legs)
    return summaries, total_m, total_m / 1852.0, minutes(total_m / 1852.0)


def expand_transit_lines(points, distance_km, to_m, to_latlon, heading_deg=None,
                         keep_centre=False):
    """Turn each flagged transit waypoint into a short line flown through it.

    A flagged point is *replaced* by two waypoints, half of `distance_km` either side of it,
    so `distance_km` is the whole line end to end. `keep_centre` puts the original back
    between them. The line is the same either way; the centre is a waypoint the aircraft
    would overfly regardless, so it costs an FMS slot to say nothing extra.

    Two ways to orient that line:

    `heading_deg is None` -- along track, on the course from the previous waypoint to the
    next, so the aircraft flies straight through and the line costs no extra turns. Where
    only one neighbour carries coordinates the single leg's course is used instead, which is
    what lets a row against the survey box or an airport still make a line. A point with no
    placed neighbour at all has no course to follow and is skipped.

    `heading_deg` set -- every line runs on that bearing regardless of neighbours, so it is
    parallel to the survey lines and the sensor sees the same geometry. Magnetic north-south
    is this with Initial Heading set to 000 minus the local variation; the app deliberately
    carries no geomagnetic model, so the pilot supplies that correction natively.

    The two new ends are named by the compass end they sit at, like the survey lines are:
    a line through GATE becomes NGATE / GATE / SGATE. The stem is cut to four characters so
    the name still clears the five-character ceiling.

    Returns (expanded_points, made, skipped) -- skipped counts flagged points that did not
    qualify, so the caller can say so rather than silently ignoring the request.
    """
    _load_geometry()   # safe to call the engine without building the GUI
    if distance_km <= 0:
        return list(points), 0, sum(1 for p in points if p.get("make_line"))

    fixed = None
    if heading_deg is not None:
        # UTM x runs east and y north, and a bearing is clockwise from north.
        radians = math.radians(heading_deg)
        fixed = (math.sin(radians), math.cos(radians))

    placed = [p["lat"] is not None for p in points]
    out, made, skipped = [], 0, 0
    for i, point in enumerate(points):
        if not point.get("make_line"):
            out.append(point)
            continue
        if not placed[i]:                  # an identifier has no position to build from
            skipped += 1
            out.append(point)
            continue

        px, py = to_m(point["lon"], point["lat"])

        # The course actually being flown through this point. Needed by both modes: along
        # track it *is* the line, and on a fixed heading it decides which end to fly first.
        course = None
        back = i - 1 if i > 0 and placed[i - 1] else None
        fwd = i + 1 if i + 1 < len(points) and placed[i + 1] else None
        if back is not None or fwd is not None:
            # Missing either side falls back to this point, turning the through course into
            # the single leg's own course.
            ax, ay = to_m(points[back]["lon"], points[back]["lat"]) if back is not None \
                else (px, py)
            bx, by = to_m(points[fwd]["lon"], points[fwd]["lat"]) if fwd is not None \
                else (px, py)
            span = math.hypot(bx - ax, by - ay)
            if span > 0:                   # zero means neighbours on top of each other
                course = ((bx - ax) / span, (by - ay) / span)

        direction = fixed if fixed is not None else course
        if direction is None:
            skipped += 1
            out.append(point)
            continue

        # A fixed heading points the same absolute way on every leg, so on a leg flown
        # against it the far end would be sequenced first and the aircraft would overshoot,
        # double back, and go forward again -- visible as a zigzag on the return leg only.
        # Flying the line the other way round puts it on the same ground in the same
        # direction of travel, which is what the survey box's own alternating rows already
        # do. Orientation is preserved; only the order the two ends are reached changes.
        if fixed is not None and course is not None and \
                fixed[0] * course[0] + fixed[1] * course[1] < 0:
            direction = (-fixed[0], -fixed[1])

        ux, uy = direction
        # distance_km is the whole line, so each end sits half of it from the waypoint.
        reach = distance_km * 1000.0 / 2.0
        start_xy = (px - ux * reach, py - uy * reach)
        end_xy = (px + ux * reach, py + uy * reach)
        head, tail = line_end_labels(
            LineString([start_xy, end_xy]),
            naming_axis(heading_deg) if heading_deg is not None else None)
        stem = point["name"][:4]
        (start_lat, start_lon), (end_lat, end_lon) = to_latlon([start_xy, end_xy])

        out.append({"ident": None, "lat": start_lat, "lon": start_lon,
                    "name": f"{head}{stem}", "make_line": False})
        # The centre is dropped by default: the two ends already define the line, and the
        # aircraft overflies the middle whether or not a waypoint sits there. Keeping it
        # spends a waypoint out of the FMS budget to say nothing extra. Tick Keep centre
        # point when the waypoint is itself the target and the box should sequence over it.
        if keep_centre:
            out.append(point)
        out.append({"ident": None, "lat": end_lat, "lon": end_lon,
                    "name": f"{tail}{stem}", "make_line": False})
        made += 1
    return out, made, skipped


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


# --- AIRPORTS ---
#
# The Origin and Destination Airport fields take ICAO codes (KLAX, KSLC). A code found in the
# airport table becomes a placed point: drawn, counted in the flight distance, and used to
# pick which corner of the box to enter from. It is never written to the waypoint files --
# the FMS knows its airports, its waypoint memory is limited, and the route link already
# names both ends. The table is web/airports.csv, built from OurAirports by
# tools/build_airports.py; AIRPORT_FILES says where each app looks for it.

_AIRPORTS = {}


def load_airports():
    """ICAO code -> (lat, lon, name), read once from the first AIRPORT_FILES entry found.

    Empty if no table can be read, in which case every code reports as not found and the
    airports simply go undrawn -- the route link still carries them.
    """
    if not _AIRPORTS:
        for path in AIRPORT_FILES:
            try:
                with open(path, encoding='utf-8') as f:
                    lines = [line for line in f if not line.startswith('#')]
            except OSError:
                continue
            for row in csv.DictReader(lines):
                try:
                    _AIRPORTS[row['icao'].strip().upper()] = (
                        float(row['lat']), float(row['lon']), row['name'].strip())
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
            if _AIRPORTS:
                break
    return _AIRPORTS


def airport_point(code, role):
    """A route endpoint as a placed point, or None, plus a summary line saying which.

    `role` is "Origin" or "Destination". The point has the same shape as a transit
    waypoint, so the drawing and distance code needs nothing special for it.
    """
    code = (code or '').strip().upper()
    found = load_airports().get(code)
    if found is None:
        why = ("not in the airport table" if load_airports()
               else "the airport table could not be loaded")
        return None, (f"{role}: {code} — {why}; kept in the route link, not drawn or "
                      f"counted")
    lat, lon, name = found
    return ({"ident": code, "lat": lat, "lon": lon, "name": code, "make_line": False},
            f"{role}: {code} {name}")


# --- FLIGHT TIME ---

def format_minutes(minutes):
    """Whole minutes, because the time is an estimate: 90.4 -> "1 h 30 min", 42.3 -> "42 min".

    Anything above zero but under half a minute reads "under 1 min" rather than "0 min", and
    NaN (no speed to divide by) reads "n/a".
    """
    if minutes != minutes:
        return "n/a"
    if 0 < minutes < 0.5:
        return "under 1 min"
    hours, mins = divmod(int(round(minutes)), 60)
    return f"{hours} h {mins:02d} min" if hours else f"{mins} min"


def flight_time_lines(pattern_nm, pattern_min, survey_kt, outbound_nm, inbound_nm,
                      transit_kt):
    """Summary lines for the time in the pattern, in transit each way, and in total.

    The pattern -- lead-in, survey lines and the turns between them -- is flown at the survey
    groundspeed. Transit is flown at the transit groundspeed, which for most aircraft is a
    good deal faster, and is reported each way: outbound is from the origin airport through
    the "before" waypoints to the pattern, inbound from the pattern through the "after"
    waypoints to the destination.

    Returns (lines, transit_min, total_min). With nothing to fly in transit the total is just
    the pattern time.
    """
    def minutes(dist_nm):
        if dist_nm <= 0:
            return 0.0
        return (dist_nm / transit_kt) * 60.0 if transit_kt > 0 else float('nan')

    outbound_min, inbound_min = minutes(outbound_nm), minutes(inbound_nm)
    transit_nm, transit_min = outbound_nm + inbound_nm, outbound_min + inbound_min
    total_min = pattern_min + transit_min
    lines = [f"In-Pattern Time: {format_minutes(pattern_min)} — "
             f"{pattern_nm:.2f} nm at {survey_kt:g} kt"]
    if transit_nm > 0:
        for label, dist_nm, mins in (("Outbound Transit", outbound_nm, outbound_min),
                                     ("Inbound Transit", inbound_nm, inbound_min)):
            lines.append(f"{label}: {format_minutes(mins)} — "
                         f"{dist_nm:.2f} nm at {transit_kt:g} kt")
        lines.append(f"Transit Time: {format_minutes(transit_min)} — "
                     f"{transit_nm:.2f} nm")
    lines.append(f"Total Flight Time: {format_minutes(total_min)} — "
                 f"{pattern_nm + transit_nm:.2f} nm")
    return lines, transit_min, total_min


# --- FIRST LINE ---
#
# The survey lines are parallel, so the line flown first is one of the two outermost --
# one on each side of the box, across the heading. Those two sides are named by compass
# point for the heading in use: West/East edge at 000, Northwest/Southeast at 045,
# North/South at 090. Offering only those two names is what keeps the choice unambiguous:
# "north" means nothing at 000, where both outer lines are equally far north, and the
# drop-down simply does not offer it.

COMPASS_POINTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
COMPASS_NAMES = {"N": "North", "NE": "Northeast", "E": "East", "SE": "Southeast",
                 "S": "South", "SW": "Southwest", "W": "West", "NW": "Northwest"}


def compass_point(bearing_deg):
    """Nearest of the eight compass points to a bearing. Opposite bearings always get
    opposite points, so the two edges of a box can never share a name."""
    return COMPASS_POINTS[int(((bearing_deg % 360.0) + 22.5) // 45.0) % 8]


def edge_choices(heading_deg):
    """(right edge, left edge): the compass points of the two outermost lines, looking
    along the heading. The right-hand edge is the one build_rectangular_pattern reaches
    with first_rows=False."""
    return compass_point(heading_deg + 90.0), compass_point(heading_deg - 90.0)


def first_line_rows(heading_deg, choice):
    """Turn a First Line choice into build_rectangular_pattern's first_rows.

    Returns (first_rows, note): False for the right-hand edge, True for the left, None for
    Auto -- the corner nearest the entry, as before. A compass point that is not one of this
    heading's two edges (the heading changed after it was picked, or a hand-edited plan)
    goes to whichever edge it is nearer; one exactly between the two is ambiguous, and
    falls back to Auto with a note saying so.
    """
    choice = (choice or "Auto").strip()
    if choice == "Auto":
        return None, ""
    right, left = edge_choices(heading_deg)
    if choice == right:
        return False, ""
    if choice == left:
        return True, ""
    edges = f"{COMPASS_NAMES[right]} or {COMPASS_NAMES[left]}"
    if choice not in COMPASS_POINTS:
        return None, f"First Line: {choice!r} not understood — use Auto, {edges}"
    target = COMPASS_POINTS.index(choice) * 45.0

    def off(bearing):
        return abs((bearing - target + 180.0) % 360.0 - 180.0)

    to_right, to_left = off(heading_deg + 90.0), off(heading_deg - 90.0)
    if abs(to_right - to_left) < 1e-6:
        return None, (f"First Line: {COMPASS_NAMES[choice]} is ambiguous at heading "
                      f"{heading_deg:g}° — the outer lines are the {edges} edge; "
                      f"nearest the entry used")
    return to_right > to_left, ""


def first_line_edge(segments, heading_deg):
    """Compass point of the edge the first survey line actually sits on, for the summary.

    None when there is no box or only one row, so no edge to speak of.
    """
    if not segments:
        return None
    mids = [segment.interpolate(0.5, normalized=True) for segment in segments]
    cx = sum(p.x for p in mids) / len(mids)
    cy = sum(p.y for p in mids) / len(mids)
    rx, ry = math.sin(math.radians(heading_deg + 90.0)), math.cos(math.radians(heading_deg + 90.0))
    side = (mids[0].x - cx) * rx + (mids[0].y - cy) * ry
    if abs(side) < 1.0:
        return None
    return compass_point(heading_deg + 90.0 if side > 0 else heading_deg - 90.0)


def first_line_summary(segments, heading_deg, first_rows, note):
    """Summary lines for the First Line choice."""
    lines = [note] if note else []
    if segments:
        edge = first_line_edge(segments, heading_deg)
        how = "chosen" if first_rows is not None else "Auto, nearest the entry"
        lines.append(f"First Line: {COMPASS_NAMES[edge]} edge ({how})" if edge
                     else "First Line: single row of lines")
    return lines

# ---- PORTED verbatim from airborne_survey_gui.py: end -----------------------------------


# --- PLAN IN, EXPORTS OUT ---
#
# Everything below mirrors FlightPlannerGUI in airborne_survey_gui.py with the widgets taken
# out: the same steps in the same order, reading a plan dictionary instead of entry boxes and
# returning file contents instead of writing them. Step numbers match calculate_and_render().

def _text(value):
    return "" if value is None else str(value).strip()


def _boundary_points(rows):
    points = []
    for idx, row in enumerate(rows):
        lat_val, lon_val = _text(row.get("lat")), _text(row.get("lon"))
        if lat_val or lon_val:
            try:
                points.append((float(lat_val), float(lon_val),
                               _text(row.get("label")) or f"WP{idx + 1:02d}"))
            except ValueError:
                raise ValueError(f"Invalid decimal format in Row {idx + 1}.")
    return points


def _centerpoint_corners(params, initial_heading_deg):
    """Four rectangle corners: length along the heading, width perpendicular to it."""
    try:
        center_lat = float(params["center_lat"])
        center_lon = float(params["center_lon"])
        width_km = float(params["center_width_km"])
        length_km = float(params["center_length_km"])
    except ValueError:
        raise ValueError("CenterPoint fields must all be valid decimal numbers.")
    if width_km <= 0.0 or length_km <= 0.0:
        raise ValueError("CenterPoint width and length must both be greater than zero.")

    crs_geo = CRS.from_epsg(4326)
    utm_zone = int((center_lon + 180.0) // 6.0) + 1
    crs_local = CRS.from_epsg(32600 + utm_zone if center_lat >= 0 else 32700 + utm_zone)
    to_m = Transformer.from_crs(crs_geo, crs_local, always_xy=True)
    to_geo = Transformer.from_crs(crs_local, crs_geo, always_xy=True)

    cx, cy = to_m.transform(center_lon, center_lat)
    along = np.array([math.sin(math.radians(initial_heading_deg)),
                      math.cos(math.radians(initial_heading_deg))])
    across = np.array([along[1], -along[0]])
    half_len_m, half_wid_m = (length_km * 1000.0) / 2.0, (width_km * 1000.0) / 2.0
    corners_xy = [
        np.array([cx, cy]) + along * half_len_m + across * half_wid_m,
        np.array([cx, cy]) + along * half_len_m - across * half_wid_m,
        np.array([cx, cy]) - along * half_len_m - across * half_wid_m,
        np.array([cx, cy]) - along * half_len_m + across * half_wid_m,
    ]
    corners = []
    for idx, (x, y) in enumerate(corners_xy, start=1):
        lon, lat = to_geo.transform(float(x), float(y))
        corners.append((lat, lon, f"CP{idx:02d}"))
    return corners


def _transit_points(rows, group, line_prefix):
    """Transit waypoints for one group, in the order flown. See the desktop app for the rules."""
    tag = 'B' if group == 'before' else 'A'
    points = []
    for idx, row in enumerate(rows, start=1):
        ident = _text(row.get("ident")).upper()
        lat_raw, lon_raw = _text(row.get("lat")), _text(row.get("lon"))
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
        points.append({
            "ident": ident or None, "lat": lat, "lon": lon,
            "name": foreflight_waypoint_name(_text(row.get("label")) or ident,
                                             f"{line_prefix}{tag}{idx:02d}"),
            "make_line": bool(row.get("make_line")),
        })
    return points


def _named_waypoints(flown_segments, conversion_func, line_prefix, before, after, lead_xy,
                     line_numbers, axis=None):
    """Waypoint names and positions; the naming half of the desktop's _export_csv_files."""
    line_numbers = list(line_numbers or range(1, len(flown_segments) + 1))
    digits = max(2, len(str(max(line_numbers, default=1))))

    survey = []
    if lead_xy is not None:
        (lead_lat, lead_lon), = conversion_func([lead_xy])
        survey.append((f"{line_prefix}IL{line_numbers[0]:0{digits}d}", lead_lat, lead_lon))
    for segment, line in zip(flown_segments, line_numbers):
        (start_lat, start_lon), (end_lat, end_lon) = conversion_func(
            [segment.coords[0], segment.coords[-1]])
        start_end, finish_end = line_end_labels(segment, axis)
        survey.append((f"{line_prefix}{start_end}L{line:0{digits}d}", start_lat, start_lon))
        survey.append((f"{line_prefix}{finish_end}L{line:0{digits}d}", end_lat, end_lon))

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

    def mapped(points):
        return [(p["name"], p["lat"], p["lon"]) for p in points if p["lat"] is not None]

    return mapped(before) + survey + mapped(after), survey


def _csv_text(header, rows, lineterminator='\r\n'):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator=lineterminator)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def _zip_b64(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in members:
            z.writestr(name, text)
    return base64.b64encode(buffer.getvalue()).decode('ascii')


def generate(plan):
    """Run one plan. Returns a JSON-serialisable dict; raises PlanError on bad input."""
    _load_geometry()
    if not isinstance(plan, dict):
        raise PlanError("Input Error", "Not a flight plan.")
    schema = plan.get("schema")
    if schema and schema != PLAN_SCHEMA:
        raise PlanError("Input Error",
                        f"Unsupported plan schema {schema!r}; expected {PLAN_SCHEMA!r}.")

    params = dict(DEFAULT_PARAMETERS)
    params.update({k: _text(v) for k, v in (plan.get("parameters") or {}).items()})
    mode = plan.get("survey_area_mode")
    mode = mode if mode in AREA_MODES else AREA_MODES[0]
    bearing_mode = plan.get("make_line_bearing")
    bearing_mode = bearing_mode if bearing_mode in ("Along track", "Survey heading") \
        else "Along track"
    rectangular = bool(plan.get("rectangular_box", True))
    retrace = bool(plan.get("retrace_lines", False))
    survey_only = bool(plan.get("survey_only", False))
    skip_box = bool(plan.get("skip_box", False))
    keep_centre = bool(plan.get("make_line_keep_centre", False))

    # 1. Export settings
    area_name = _text(plan.get("area_name")).replace(' ', '_')
    waypoint_prefix = _text(plan.get("waypoint_prefix")).upper()
    if not area_name:
        raise PlanError("Input Error", "Filename/Area Name cannot be empty.")
    if re.search(r'[\\/:*?"<>|]', area_name):
        raise PlanError("Input Error",
                        'Filename/Area Name cannot contain any of \\ / : * ? " < > |')
    if waypoint_prefix and (not waypoint_prefix.isalnum() or len(waypoint_prefix) > 2):
        raise PlanError("Input Error", "Line ID Prefix must be empty, or 1-2 letters or digits.")

    # 2. Heading, used by both area modes
    try:
        heading = float(params["initial_heading_deg"])
    except ValueError:
        raise PlanError("Input Error", "Ensure all Flight settings are valid numbers.")

    # 3. Survey boundary from the selected mode
    try:
        if mode == "CenterPoint":
            survey_boundary = _centerpoint_corners(params, heading)
        else:
            survey_boundary = _boundary_points(plan.get("boundary") or [])
    except ValueError as err:
        raise PlanError("Input Error", str(err))
    if len(survey_boundary) < 3:
        raise PlanError("Input Error", "At least 3 valid GPS coordinate rows must be filled "
                                       "to create a closed survey boundary.")

    # 4. Local UTM projection around the area's centroid
    center_lat = np.mean([lat for lat, lon, *_ in survey_boundary])
    center_lon = np.mean([lon for lat, lon, *_ in survey_boundary])
    if not (-80.0 <= center_lat <= 84.0 and -180.0 <= center_lon <= 180.0):
        raise PlanError("Input Error", "Coordinates are outside the range a survey can be "
                                       "projected in. Check latitude and longitude signs.")
    crs_geo = CRS.from_epsg(4326)
    utm_zone = int((center_lon + 180.0) // 6.0) + 1
    crs_local = CRS.from_epsg(32600 + utm_zone if center_lat >= 0 else 32700 + utm_zone)
    to_m = Transformer.from_crs(crs_geo, crs_local, always_xy=True)
    to_geo = Transformer.from_crs(crs_local, crs_geo, always_xy=True)

    def xy_to_latlon(xy_points):
        return [(to_geo.transform(x, y)[1], to_geo.transform(x, y)[0]) for x, y in xy_points]

    # 5. Flight settings
    try:
        gs = float(params["groundspeed_kt"])
        transit_gs = float(params["transit_groundspeed_kt"])
        swath = float(params["swath_width_km"])
        overlap = float(params["swath_overlap"])
        margin = float(params["perimeter_margin_km"])
        lat_off = float(params["lat_offset"])
        lon_off = float(params["lon_offset"])
        lead_in_km = max(0.0, float(params["lead_in_km"]))
        bank_deg = float(params["bank_angle_deg"])
    except ValueError:
        raise PlanError("Input Error", "Ensure all Flight settings are valid numbers.")
    if not 0.0 <= bank_deg <= 60.0:
        raise PlanError("Input Error",
                        "Bank Angle must be between 0 and 60 degrees. 0 ignores the turn radius.")
    turn_radius = turn_radius_m(gs, bank_deg)
    try:
        repeats = min(4, max(1, int(float(plan.get("repeats", 1)))))
    except (TypeError, ValueError):
        repeats = 1
    try:
        skip_edges = min(3, max(0, int(float(plan.get("skip_edges", 0)))))
    except (TypeError, ValueError):
        skip_edges = 0

    # 6. Transit waypoints first: they decide which corner of the box is entered at.
    transit = plan.get("transit") or {}
    try:
        before = _transit_points(transit.get("before") or [], "before", waypoint_prefix)
        after = _transit_points(transit.get("after") or [], "after", waypoint_prefix)
    except ValueError as err:
        raise PlanError("Input Error", str(err))
    try:
        make_line_km = max(0.0, float(params["make_line_km"]))
    except ValueError:
        make_line_km = 0.0
    line_heading = heading if bearing_mode == "Survey heading" else None
    before, made_b, skipped_b = expand_transit_lines(
        before, make_line_km, to_m.transform, xy_to_latlon, line_heading, keep_centre)
    after, made_a, skipped_a = expand_transit_lines(
        after, make_line_km, to_m.transform, xy_to_latlon, line_heading, keep_centre)
    made_lines, skipped_lines = made_b + made_a, skipped_b + skipped_a

    mapped_before = [p for p in before if p["lat"] is not None]
    mapped_after = [p for p in after if p["lat"] is not None]
    origin_code = params["origin_airport"].upper() or "KBOI"
    destination_code = params["destination_airport"].upper() or "KBOI"
    origin_pt, origin_note = airport_point(origin_code, "Origin")
    destination_pt, destination_note = airport_point(destination_code, "Destination")
    # The box is entered from whatever the aircraft arrives from: the last placed transit
    # waypoint, or failing that the origin airport. Likewise for leaving.
    arrives_from = mapped_before[-1] if mapped_before else origin_pt
    leaves_to = mapped_after[0] if mapped_after else destination_pt
    entry_xy = to_m.transform(arrives_from["lon"], arrives_from["lat"]) \
        if arrives_from else None
    exit_xy = to_m.transform(leaves_to["lon"], leaves_to["lat"]) \
        if leaves_to else None

    # "Survey box only" filters the output after the entry/exit hints are taken, so the
    # survey itself is identical with it on or off. Do not move this above the hints.
    first_rows, first_note = first_line_rows(heading, _text(plan.get("first_line")) or "Auto")
    if survey_only and skip_box:
        raise PlanError(
            "Nothing To Fly",
            "Survey box only and Skip survey box are both ticked. One keeps just the box, "
            "the other drops it — between them they exclude everything.\n\n"
            "Untick whichever one you did not mean.")
    if survey_only:
        before = after = []
        mapped_before = mapped_after = []
        origin_pt = destination_pt = None
        origin_note = destination_note = ""

    if skip_box:
        survey_poly, segments, line_numbers = None, [], []
        chain = [to_m.transform(p["lon"], p["lat"]) for p in mapped_before + mapped_after]
        if len(chain) < 2:
            raise PlanError(
                "Nothing To Fly",
                "Skip survey box is ticked, so the plan is the transit waypoints alone — "
                "and fewer than two of them carry coordinates.\n\n"
                "Add waypoints on the Waypoints tab, or untick Skip survey box.")
        survey_pattern = LineString(chain)
    else:
        try:
            survey_poly, survey_pattern, segments, line_numbers = build_rectangular_pattern(
                survey_boundary, swath, overlap, margin, heading, lat_off, lon_off, to_m,
                center_lat, rectangular=rectangular, repeats=repeats, retrace=retrace,
                entry_xy=entry_xy, exit_xy=exit_xy, skip_edges=skip_edges,
                first_rows=first_rows)
        except Exception as err:
            raise PlanError("Execution Error", str(err))

    # 7. Lead-in, then names
    lead_xy = lead_in_point(segments[0], lead_in_km) if segments and not survey_only else None
    if lead_xy is not None:
        survey_pattern = LineString([lead_xy] + list(survey_pattern.coords))
    try:
        waypoints, survey_waypoints = _named_waypoints(
            segments, xy_to_latlon, waypoint_prefix, before, after, lead_xy, line_numbers,
            naming_axis(heading))
    except ValueError as err:
        raise PlanError("Waypoint Naming Error", str(err))

    # 8. Exports
    # Distance and time follow the path with its turns flown at the turn radius. The KML and
    # the waypoint files keep the corner-to-corner polyline: the FMS flies its own turns.
    if segments:
        flown_track, legs = fly_segments(segments, lead_xy, turn_radius, line_numbers)
    else:
        flown_track = list(survey_pattern.coords)
        legs = [(math.dist(a, b), False) for a, b in zip(flown_track, flown_track[1:])]
    segment_summaries, dist_m, dist_nm, time_min = summarize_legs(legs, gs)
    turn_m = sum(length for length, is_turn in legs if is_turn)
    # Airport legs: from the origin to the first placed point of the flight, and from the
    # last one to the destination. Straight lines -- a planning figure, like the rest.
    first_xy = (to_m.transform(mapped_before[0]["lon"], mapped_before[0]["lat"])
                if mapped_before else flown_track[0])
    last_xy = (to_m.transform(mapped_after[-1]["lon"], mapped_after[-1]["lat"])
               if mapped_after else flown_track[-1])
    airport_m = airport_out_m = airport_in_m = 0.0
    airport_lines = []
    for point, note, joins, leaving in ((origin_pt, origin_note, first_xy, True),
                                        (destination_pt, destination_note, last_xy, False)):
        if point is None:
            if note:
                airport_lines.append(note)
            continue
        leg_m = math.dist(to_m.transform(point["lon"], point["lat"]), joins)
        airport_m += leg_m
        if leaving:
            airport_out_m += leg_m
        else:
            airport_in_m += leg_m
        airport_lines.append(f"{note} — {leg_m / 1000:.1f} km ({leg_m / 1852.0:.1f} nm) "
                             + ("to the first waypoint" if leaving else "from the last"))
    drawn_before = ([origin_pt] if origin_pt else []) + mapped_before
    drawn_after = mapped_after + ([destination_pt] if destination_pt else [])
    line_spacing_m = swath * 1000.0 * (1.0 - overlap)
    if turn_radius <= 0:
        turn_note = "Turn Radius: not modelled (bank angle 0) — turns counted as straight legs"
    else:
        turn_note = (f"Turn Radius: {turn_radius / 1000:.2f} km "
                     f"({bank_deg:g}° bank at {gs:g} kt)")
        # Neighbouring lines a turn diameter or more apart are joined by one half circle at
        # whatever bank fits the gap -- a single roll in and out. Closer than that, one bank
        # cannot do it within the limit, and the turn becomes a teardrop with two.
        if segments and not retrace:
            if line_spacing_m >= 2 * turn_radius:
                turn_note += (f" — U-turns are one turn at "
                              f"{bank_for_radius_deg(gs, line_spacing_m / 2):.1f}° bank "
                              f"({line_spacing_m / 2000:.2f} km radius), filling the "
                              f"{line_spacing_m / 1000:.2f} km line spacing")
            else:
                turn_note += (f" — line spacing {line_spacing_m / 1000:.2f} km is under the "
                              f"{2 * turn_radius / 1000:.2f} km turn diameter, so each turn "
                              f"is a teardrop: out at {DEPARTURE_ANGLE_DEG:g}°, then back")
    generated_utc = time.strftime('%Y-%m-%d %H:%MZ', time.gmtime())
    manifest_stamp = time.strftime('%Y%m%dT%H:%M:%SZ', time.gmtime())
    meta = {'lines': len(segments), 'heading': heading, 'dist_nm': dist_nm, 'time_min': time_min}
    boundary_for_export = survey_boundary if mode != "CenterPoint" else []
    envelope_latlon = xy_to_latlon(list(survey_poly.exterior.coords)) if survey_poly else []
    track_latlon = xy_to_latlon(list(survey_pattern.coords))

    kml_text = build_survey_kml(area_name, envelope_latlon, track_latlon, waypoints,
                                boundary_for_export, meta, generated_utc)
    ff_csv = _csv_text(['Waypoint', 'Description', 'LAT', 'LONG'],
                       [[name, 'NA', f'{lat:.8f}', f'{lon:.8f}'] for name, lat, lon in waypoints])
    hw_csv = _csv_text(['E', 'WPT', 'FIX', 'LAT', 'LON'],
                       [['X', name, 'NA', dd_to_honeywell_format(lat, 'N', 'S', degree_digits=2),
                         dd_to_honeywell_format(lon, 'E', 'W', degree_digits=3)]
                        for name, lat, lon in waypoints])
    # ForeFlight needs this exact filename inside the pack's navdata/ folder.
    pack_csv = _csv_text(['Name', 'Description', 'Latitude', 'Longitude'],
                         [[name, f'{area_name} {generated_utc}', f'{lat:.4f}', f'{lon:.4f}']
                          for name, lat, lon in waypoints], lineterminator='\n')
    # Version from the generation time, so a re-import is never mistaken for the same pack.
    manifest = {
        "name": f"{area_name} Survey {generated_utc}",
        "abbreviation": waypoint_prefix,
        "version": int(''.join(ch for ch in manifest_stamp if ch.isdigit())[:12]),
        "effectiveDate": manifest_stamp,
    }
    root = f"{area_name}_survey"
    pack_b64 = _zip_b64([(f'{root}/manifest.json', json.dumps(manifest, indent=2)),
                         (f'{root}/layers/{area_name}_survey.kml', kml_text),
                         (f'{root}/navdata/user_waypoints.csv', pack_csv)])

    route_text, route_url = build_foreflight_route(
        survey_waypoints,
        origin_code,
        destination_code,
        params["survey_altitude"] or "200",
        before=before, after=after, cruise_kt=params["transit_groundspeed_kt"])

    saved_plan = dict(plan)
    saved_plan.update({"schema": PLAN_SCHEMA, "area_name": area_name,
                       "waypoint_prefix": waypoint_prefix, "saved_utc": generated_utc})

    names = {
        "foreflight_csv": f"{area_name}_waypoints_foreflight.csv",
        "honeywell_csv": f"{area_name}_waypoints_honeywell.csv",
        "kml": f"{area_name}_survey.kml",
        "kmz": f"{area_name}_survey.kmz",
        "pack": f"{area_name}_foreflight_pack.zip",
        "route": f"{area_name}_foreflight_route.txt",
        "plan": f"{area_name}_plan.json",
    }
    files = {
        "foreflight_csv": {"name": names["foreflight_csv"], "mime": "text/csv", "text": ff_csv},
        "honeywell_csv": {"name": names["honeywell_csv"], "mime": "text/csv", "text": hw_csv},
        "kml": {"name": names["kml"], "mime": "application/vnd.google-earth.kml+xml",
                "text": kml_text},
        "kmz": {"name": names["kmz"], "mime": "application/vnd.google-earth.kmz",
                "b64": _zip_b64([('doc.kml', kml_text)])},
        "pack": {"name": names["pack"], "mime": "application/zip", "b64": pack_b64},
        "route": {"name": names["route"], "mime": "text/plain",
                  "text": route_text + "\n\n" + route_url + "\n"},
        "plan": {"name": names["plan"], "mime": "application/json",
                 "text": json.dumps(saved_plan, indent=2)},
    }

    # Padding actually achieved, measured rather than assumed.
    target_poly = Polygon([to_m.transform(lon, lat) for lat, lon, _ in survey_boundary]).buffer(0)
    clearance_m = 0.0 if survey_poly is None else measure_clearance(target_poly, survey_poly)
    if survey_poly is None:
        margin_note = "Actual Padding: n/a — survey box skipped"
    elif clearance_m < 0:
        margin_note = (f"Actual Padding: TARGET NOT FULLY COVERED "
                       f"({abs(clearance_m) / 1000:.2f} km outside)")
    elif clearance_m < margin * 1000.0 - 1.0:
        margin_note = (f"Actual Padding: {clearance_m / 1000:.2f} km "
                       f"— SHORT of the {margin:.2f} km requested")
    else:
        margin_note = f"Actual Padding: {clearance_m / 1000:.2f} km on all sides"

    stats = [
        f"Survey Identifier: {area_name}",
        f"Prefix Configured: {waypoint_prefix}",
        f"Generated (UTC): {generated_utc}",
        f"Survey Area Mode: {mode}",
        f"Active Vertices parsed: {len(survey_boundary)}",
        f"Pattern: {'Rectangular box' if rectangular else 'Clipped to target outline'}"
        if segments else "Pattern: none — survey box skipped",
        f"Repeats: {repeats}x  ({len(segments) // repeats} lines per cycle)"
        if segments else "Repeats: n/a",
        f"Retrace: on — {max(line_numbers)} lines flown out and back"
        if retrace and segments else "Retrace: off",
        f"Survey Lines: {max(line_numbers)}   ({len(segments)} passes)"
        if segments else "Survey Lines: none — box skipped",
        f"Skipped Edges: {skip_edges} per end ({2 * skip_edges} lines dropped)"
        if skip_edges and segments else "Skipped Edges: none",
        *first_line_summary(segments, heading, first_rows, first_note),
        f"Lead-in: {lead_in_km:.2f} km on the line bearing" if lead_xy is not None
        else f"Lead-in: {lead_in_km:.2f} km, hidden by Survey box only"
        if survey_only and lead_in_km > 0 else "Lead-in: off",
        "Scope: SURVEY BOX ONLY — lead-in and transit excluded" if survey_only
        else "Scope: BOX SKIPPED — transit and make-lines only" if skip_box
        else "Scope: full plan",
        f"Ground Heading: {heading:.1f}° True",
        turn_note,
        f"Turns: {sum(1 for _l, is_turn in legs if is_turn)}, "
        f"{turn_m / 1000:.2f} km ({turn_m / 1852.0:.2f} nm) of the survey path",
        f"Requested Margin: {margin:.2f} km",
        margin_note,
        f"Survey Path: {dist_m / 1000:.2f} km ({dist_nm:.2f} nm)",
        f"Waypoints in CSV: {len(waypoints)}"
        + (f" / {MAX_FMS_WAYPOINTS} max" if len(waypoints) > MAX_FMS_WAYPOINTS else "")
        + (f"   ({len(survey_waypoints)} survey + "
           f"{len(waypoints) - len(survey_waypoints)} transit)"
           if len(waypoints) != len(survey_waypoints) else ""),
    ]
    if before or after:
        stats.append(f"Transit Legs: {len(before)} before / {len(after)} after")
    if made_lines or skipped_lines:
        note = (f"Make-Line: {made_lines} line(s), {make_line_km:.2f} km long, "
                f"{bearing_mode.lower()}, {'3 pts' if keep_centre else '2 pts'} each")
        if skipped_lines:
            note += (f"; {skipped_lines} skipped (no course — needs a placed waypoint "
                     f"above or below)")
        stats.append(note)
    transit_nm = transit_out_m = transit_in_m = 0.0
    if (before or after) and segments:
        transit_m = 0.0
        for chain, anchor in ((mapped_before, segments[0].coords[0]),
                              (mapped_after, segments[-1].coords[-1])):
            if not chain:
                continue
            legs = [to_m.transform(p["lon"], p["lat"]) for p in chain]
            legs = legs + [anchor] if chain is mapped_before else [anchor] + legs
            side_m = sum(math.dist(a, b) for a, b in zip(legs, legs[1:]))
            transit_m += side_m
            if chain is mapped_before:
                transit_out_m += side_m
            else:
                transit_in_m += side_m
        transit_nm = transit_m / 1852.0
        unmapped = (len(before) - len(mapped_before)) + (len(after) - len(mapped_after))
        stats.append(f"Transit Path: {transit_m / 1000:.2f} km ({transit_nm:.2f} nm)"
                     + (f"  [{unmapped} by identifier, not counted]" if unmapped else ""))
    stats.extend(airport_lines)
    # Pattern at the survey speed; transit waypoints and airport legs at the transit speed.
    time_lines, transit_min, total_min = flight_time_lines(
        dist_nm, time_min, gs, (transit_out_m + airport_out_m) / 1852.0,
        (transit_in_m + airport_in_m) / 1852.0, transit_gs)
    stats.extend(time_lines)
    stats.append("-" * 41)
    for idx, dm, _dnm, tmin, is_turn in segment_summaries:
        stats.append(f"Segment {idx:02d}: {dm / 1000:.1f} km | {tmin:.1f} min"
                     + ("  (turn)" if is_turn else ""))

    over_limit = len(waypoints) > MAX_FMS_WAYPOINTS
    short = clearance_m < margin * 1000.0 - 1.0
    status = (
        f"Generated {generated_utc}: {len(segments)} lines"
        f"{' (box skipped)' if skip_box else ''}, {len(waypoints)} waypoints"
        f"{f' (OVER {MAX_FMS_WAYPOINTS} LIMIT)' if over_limit else ''}"
        f"{' [survey box only]' if survey_only else ''}, {dist_nm:.1f} nm, "
        f"pattern {format_minutes(time_min)}, total {format_minutes(total_min)}, "
        f"{clearance_m / 1000:.2f} km padding{' (SHORT — check offsets)' if short else ''}. "
        f"Send {names['pack']} to the pilot.")

    def placed(points):
        return [[p["lat"], p["lon"], p["name"]] for p in points if p["lat"] is not None]

    return {
        "ok": True,
        "area_name": area_name,
        "status": status,
        "summary": "\n".join(stats),
        "warning": (f"{len(waypoints)} waypoints — the pilot's software takes "
                    f"{MAX_FMS_WAYPOINTS}. Widen the swath, cut repeats, or turn retrace off.")
        if over_limit else "",
        "title": (f"{area_name} — {len(segments)} survey lines @ {heading:.0f}°T — "
                  f"{dist_nm:.1f} nm / {time_min:.0f} min"),
        "route_text": route_text,
        "route_url": route_url,
        "files": files,
        "preview": {
            "envelope": [list(p) for p in envelope_latlon],
            "track": [list(p) for p in xy_to_latlon(flown_track)],
            "boundary": [[lat, lon, label] for lat, lon, label in boundary_for_export],
            "waypoints": [[lat, lon, name] for name, lat, lon in survey_waypoints],
            "transit_before": placed(drawn_before),
            "transit_after": placed(drawn_after),
        },
    }


def generate_json(plan_json):
    """String in, string out -- the one call the web page makes."""
    try:
        return json.dumps(generate(json.loads(plan_json)))
    except PlanError as err:
        return json.dumps({"ok": False, "title": err.title, "message": str(err)})
    except Exception as err:      # never let a traceback be the user interface
        return json.dumps({"ok": False, "title": "Execution Error",
                           "message": f"{type(err).__name__}: {err}"})

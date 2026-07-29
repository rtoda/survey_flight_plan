import os
import csv
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
from shapely.geometry import LineString, Polygon, MultiLineString, GeometryCollection
from shapely import affinity
from pyproj import CRS, Transformer
import folium
from tkinterweb import HtmlFrame 

# --- SURVEY ENGINE CONFIGURATION & HELPERS ---

def dd_to_honeywell_format(value, positive_indicator, negative_indicator):
    sign = positive_indicator if value >= 0 else negative_indicator
    abs_value = abs(value)
    degrees = int(abs_value)
    minutes = (abs_value - degrees) * 60.0
    return f"{sign} {degrees:02d} {minutes:05.2f} "

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

def build_rectangular_pattern(latlon_coords, swath_km, overlap, perimeter_margin_km, initial_heading_deg, lat_offset, lon_offset, transformer_to_m, center_lat):
    if len(latlon_coords) < 3:
        raise ValueError('At least three coordinates are required to define a survey area.')

    # Convert coordinates to UTM (metric) projection
    input_xy = [transformer_to_m.transform(lon, lat) for lat, lon, _ in latlon_coords]
    survey_poly = Polygon(input_xy).buffer(0)

    if survey_poly.is_empty or survey_poly.area == 0:
        raise ValueError('Survey polygon area is zero. Check input coordinates.')

    if perimeter_margin_km > 0:
        survey_poly = survey_poly.buffer(perimeter_margin_km * 1000.0)

    # Rotate survey polygon so requested heading becomes horizontal
    heading_angle_deg = (90.0 - initial_heading_deg) % 360.0
    rotated_poly = affinity.rotate(survey_poly, -heading_angle_deg, origin='centroid', use_radians=False)
    minx, miny, maxx, maxy = rotated_poly.bounds
    rect_coords = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]
    survey_rect = Polygon(rect_coords)
    survey_rect = affinity.rotate(survey_rect, heading_angle_deg, origin='centroid', use_radians=False)

    if lat_offset != 0.0 or lon_offset != 0.0:
        lat_offset_m = lat_offset * 111320.0
        lon_offset_m = lon_offset * 111320.0 * math.cos(math.radians(center_lat))
        survey_rect = affinity.translate(survey_rect, xoff=lon_offset_m, yoff=lat_offset_m)

    rotated_rect = affinity.rotate(survey_rect, -heading_angle_deg, origin='centroid', use_radians=False)
    minx, miny, maxx, maxy = rotated_rect.bounds
    center_y = (miny + maxy) / 2.0
    line_spacing = swath_km * 1000 * (1 - overlap)

    pass_segments = []
    height = maxy - miny
    if height <= line_spacing:
        candidate_ys = [center_y]
    else:
        num_lines = int(math.floor(height / line_spacing)) + 1
        start_y = center_y - ((num_lines - 1) * line_spacing / 2.0)
        candidate_ys = [start_y + i * line_spacing for i in range(num_lines)]

    for current_y in candidate_ys:
        pass_line = LineString([(minx - 10000, current_y), (maxx + 10000, current_y)])
        clipped = pass_line.intersection(rotated_rect)

        if clipped.is_empty or getattr(clipped, 'geom_type', None) not in {'LineString', 'MultiLineString', 'GeometryCollection'}:
            continue

        if isinstance(clipped, LineString):
            if clipped.length > 0:
                pass_segments.append(clipped)
        elif isinstance(clipped, MultiLineString):
            pass_segments.extend([seg for seg in clipped.geoms if seg.length > 0])
        elif isinstance(clipped, GeometryCollection):
            for part in clipped.geoms:
                if isinstance(part, LineString) and part.length > 0:
                    pass_segments.append(part)

    if not pass_segments:
        center_y = (miny + maxy) / 2.0
        pass_line = LineString([(minx - 10000, center_y), (maxx + 10000, center_y)])
        clipped = pass_line.intersection(rotated_rect)
        if isinstance(clipped, LineString) and clipped.length > 0:
            pass_segments.append(clipped)
        elif isinstance(clipped, MultiLineString):
            pass_segments.extend([seg for seg in clipped.geoms if seg.length > 0])

    if not pass_segments:
        raise ValueError('No pass segments could be generated. Adjust coordinates or swath width.')

    pass_segments = sorted(pass_segments, key=lambda s: s.centroid.y)
    pattern_points = []
    for idx, segment in enumerate(pass_segments):
        coords = list(segment.coords)
        if idx % 2 == 1:
            coords = coords[::-1]
        # The turn onto the next pass is implicit in the polyline; only guard against
        # emitting a duplicate point (which would create a zero-length segment).
        if pattern_points and pattern_points[-1] == coords[0]:
            coords = coords[1:]
        pattern_points.extend(coords)

    pattern_line = LineString(pattern_points)
    rotated_pattern = affinity.rotate(pattern_line, heading_angle_deg, origin='centroid', use_radians=False)
    return survey_rect, rotated_pattern, pass_segments

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

        ttk.Label(param_tab, text="Waypoint Prefix (3-Char):").grid(row=2, column=0, sticky="w", pady=5)
        self.prefix_entry = ttk.Entry(param_tab, width=10)
        self.prefix_entry.insert(0, "CLM")
        self.prefix_entry.grid(row=2, column=1, sticky="w", pady=5, padx=10)

        ttk.Separator(param_tab, orient='horizontal').grid(row=3, column=0, columnspan=2, sticky="ew", pady=10)

        # Flight settings fields
        ttk.Label(param_tab, text="Flight Parameters", font=("Helvetica", 10, "bold")).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 5))
        fields = [
            ("Groundspeed (knots):", "groundspeed_kt", "200"),
            ("Swath Width (km):", "swath_width_km", "10.0"),
            ("Swath Overlap (0.0-1.0):", "swath_overlap", "0.1"),
            ("Perimeter Margin (km):", "perimeter_margin_km", "5.0"),
            ("Initial Heading (deg True):", "initial_heading_deg", "20"),
            ("Latitude Offset (deg):", "lat_offset", "0.025"),
            ("Longitude Offset (deg):", "lon_offset", "0.0")
        ]

        for i, (label_text, dict_key, default_val) in enumerate(fields, start=5):
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

        # --- RUN CONTROL BUTTON ---
        calculate_btn = ttk.Button(left_frame, text="Generate Flight Plan & Update Map", command=self.calculate_and_render)
        calculate_btn.grid(row=1, column=0, columnspan=2, pady=10, sticky="ew")

        # --- FLIGHT STATISTICS WINDOW ---
        ttk.Label(left_frame, text="Flight Path Summary Output", font=("Helvetica", 10, "bold")).grid(row=2, column=0, columnspan=2, pady=(10, 2), sticky="w")
        self.stats_text = tk.Text(left_frame, width=45, height=15, wrap=tk.WORD, font=("Courier", 11))
        self.stats_text.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=5)
        
        left_frame.rowconfigure(3, weight=1)

        # --- INTERACTIVE MAP DISPLAY ---
        self.map_view = HtmlFrame(right_frame)
        self.map_view.pack(fill=tk.BOTH, expand=True)

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
        if len(waypoint_prefix) != 3:
            messagebox.showerror("Input Error", "Waypoint Prefix must be exactly 3 characters.")
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

        # 5. Generate the flight pattern path
        try:
            survey_poly, survey_pattern, segments = build_rectangular_pattern(
                survey_boundary, swath, overlap, margin, heading, lat_off, lon_off, to_m, center_lat
            )
        except Exception as e:
            messagebox.showerror("Execution Error", str(e))
            return

        # 6. Export waypoint CSV sheets
        ff_file, hw_file = self._export_csv_files(survey_pattern, xy_to_latlon, area_name, waypoint_prefix)

        # 7. Parse and Print Diagnostics Panel
        self.stats_text.delete("1.0", tk.END)
        segment_summaries, dist_m, dist_nm, time_min = summarize_segment_travel(survey_pattern, gs)

        stats_output = [
            f"Survey Identifier: {area_name}",
            f"Prefix Configured: {waypoint_prefix}",
            f"Active Vertices parsed: {len(survey_boundary)}",
            f"Generated Survey Lines: {len(segments)}",
            f"Ground Heading: {heading:.1f}° True",
            f"Path Metrics: {dist_m/1000:.2f} km ({dist_nm:.2f} nm)",
            f"Est. Flight Time: {time_min:.1f} min",
            f"Wrote Output: {ff_file}",
            f"Wrote Output: {hw_file}",
            "-" * 41
        ]
        for idx, dm, dnm, tmin in segment_summaries:
            stats_output.append(f"Segment {idx:02d}: {dm/1000:.1f} km | {tmin:.1f} min")
        
        self.stats_text.insert(tk.END, "\n".join(stats_output))

        # 8. Render dynamic map using Folium
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

        # 9. Save and Display Map dynamically INSIDE the Tkinter GUI Window
        temp_map_path = os.path.join(os.getcwd(), "temp_survey_map.html")
        survey_map.save(temp_map_path)
        
        # Convert local file path to an absolute URI for flawless rendering inside tkinterweb
        file_uri = Path(temp_map_path).absolute().as_uri()
        self.map_view.load_url(file_uri)

    def _export_csv_files(self, flight_pattern, conversion_func, area_name, waypoint_prefix):
        latlon_points = conversion_func(list(flight_pattern.coords))
        deduped = []
        for point in latlon_points:
            if not deduped or deduped[-1] != point:
                deduped.append(point)

        ff_file = f"{area_name}_waypoints_foreflight.csv"
        with open(ff_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Waypoint', 'Description', 'LAT', 'LONG'])
            for idx, (lat, lon) in enumerate(deduped, start=1):
                writer.writerow([waypoint_prefix + f'{idx:02d}', 'NA', f'{lat:.4f}', f'{lon:.4f}'])

        hw_file = f"{area_name}_waypoints_honeywell.csv"
        with open(hw_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['E', 'WPT', 'FIX', 'LAT', 'LON'])
            for idx, (lat, lon) in enumerate(deduped, start=1):
                lat_fmt = dd_to_honeywell_format(lat, 'N', 'S')
                lon_fmt = dd_to_honeywell_format(lon, 'E', 'W')
                writer.writerow(['X', waypoint_prefix + f'{idx:02d}', 'NA', lat_fmt, lon_fmt])

        return ff_file, hw_file

if __name__ == "__main__":
    app = FlightPlannerGUI()
    app.mainloop()
// Airborne Survey Flight Planner -- web front end.
//
// The planning itself is survey_engine.py, run in the browser by Pyodide. This file only
// collects the form into a plan dictionary (the same JSON the desktop app saves), hands it
// to the engine, and shows what comes back. Nothing typed here is sent to a server.

const PYODIDE_INDEX = "https://cdn.jsdelivr.net/pyodide/v0.27.7/full/";
const STORAGE_KEY = "flightplan.plan";
const BOUNDARY_ROWS = 10;
const TRANSIT_ROWS = 10;
const AREA_MODES = ["Boundary GPS (Max 10)", "CenterPoint"];

// kind: "param" lives under plan.parameters, "top" is a top-level string, "flag" a boolean,
// "choice" a top-level dropdown. Tips are the desktop app's hover help, shortened.
const FIELDS = {
  "export-fields": [
    { kind: "top", key: "area_name", label: "Filename / Area Name", value: "Clairmont_Fire",
      tip: "Names the plan and every file it produces. Spaces become underscores." },
    { kind: "top", key: "waypoint_prefix", label: "Line ID Prefix (optional)", value: "",
      tip: "One or two letters or digits put in front of every waypoint name, to tell two areas apart in the same database. Names are limited to five characters, so a prefix only fits while there are fewer than 100 lines." },
  ],
  "area-fields": [
    { kind: "choice", key: "survey_area_mode", label: "Survey Area Defined By", value: AREA_MODES[0],
      options: AREA_MODES,
      tip: "Boundary GPS uses the corner points on the Boundary GPS tab. CenterPoint ignores them and builds a rectangle from the CenterPoint tab." },
    { kind: "flag", key: "rectangular_box", label: "Rectangular Box", value: true,
      tip: "On: fly the smallest rectangle enclosing the target plus its margin, with equal-length parallel lines. Off: clip the lines to the padded target outline, which covers less ground but gives lines of very uneven length." },
    { kind: "flag", key: "survey_only", label: "Survey box only", value: false,
      tip: "Leaves the lead-in and the transit legs out of everything drawn and downloaded. The survey lines themselves do not change. Turn it off before sending anything to the pilot." },
    { kind: "flag", key: "skip_box", label: "Skip survey box", value: false,
      tip: "Drops the box and flies only the transit waypoints and their make-lines. Needs at least two transit waypoints with coordinates." },
  ],
  "flight-fields": [
    { kind: "choice", key: "repeats", label: "Repeats (fly box N times)", value: "1",
      options: ["1", "2", "3", "4"],
      tip: "Flies the whole box that many times, with the same line directions each time." },
    { kind: "flag", key: "retrace_lines", label: "Retrace line", value: false,
      tip: "Flies each line out and back before moving to the next." },
    { kind: "choice", key: "skip_edges", label: "Skip Edge Lines (each end)", value: "0",
      options: ["0", "1", "2", "3"],
      tip: "Drops that many lines from both sides of the box. This gives up real coverage; the Summary tab reports the padding actually achieved." },
    { kind: "choice", key: "first_line", label: "First Line", value: "Auto", options: ["Auto"],
      dynamic: true,
      tip: "Which of the two outermost lines is flown first. The choices are named for the current Initial Heading: West or East edge at 000, Northwest or Southeast at 045, North or South at 090 -- only the two edges that exist, so the choice is never ambiguous. They update as the heading changes. Auto starts at the corner nearest where the aircraft arrives from. The chosen line becomes line 1." },
    { kind: "param", key: "groundspeed_kt", label: "Survey Groundspeed (knots)", value: "200",
      tip: "Speed flown in the pattern: the lead-in, the survey lines and the turns between them. Sets the in-pattern time and, with the bank angle, the turn radius." },
    { kind: "param", key: "transit_groundspeed_kt", label: "Transit Groundspeed (knots)", value: "200",
      tip: "Speed flown to and from the pattern: the transit waypoints and the legs from the origin airport and to the destination. Sets the transit time, and is sent to ForeFlight as the cruise speed." },
    { kind: "param", key: "bank_angle_deg", label: "Max Bank Angle (deg)", value: "25",
      tip: "The steepest bank the turns may use. Sets the turn radius between survey lines, together with the groundspeed. 25 is the usual autopilot bank limit. Distance, time and the drawn track include the turns; the waypoint files do not change. 0 counts turns as straight legs." },
    { kind: "param", key: "swath_width_km", label: "Swath Width (km)", value: "10.0",
      tip: "Width of ground the sensor covers on one pass." },
    { kind: "param", key: "swath_overlap", label: "Swath Overlap (0.0-1.0)", value: "0.1",
      tip: "Fraction of each swath that overlaps its neighbour. 0.1 is 10%." },
    { kind: "param", key: "perimeter_margin_km", label: "Perimeter Margin (km)", value: "5.0",
      tip: "Padding flown around the target on all sides." },
    { kind: "param", key: "initial_heading_deg", label: "Initial Heading (deg True)", value: "0",
      tip: "Direction the survey lines run, in degrees True. For magnetic north-south, enter 000 minus the local variation, e.g. 346.5 for 13.5°E." },
    { kind: "param", key: "lat_offset", label: "Latitude Offset (deg)", value: "0.0",
      tip: "Shifts the whole coverage area north (+) or south (-). A shift eats into the margin on the trailing side." },
    { kind: "param", key: "lon_offset", label: "Longitude Offset (deg)", value: "0.0",
      tip: "Shifts the whole coverage area east (+) or west (-)." },
    { kind: "param", key: "lead_in_km", label: "Lead-in Distance (km)", value: "0.0",
      tip: "Adds a waypoint this far back from the first line's start, on the line's own bearing, so the aircraft is established on track before the line begins. 0 turns it off." },
  ],
  "route-fields": [
    { kind: "param", key: "origin_airport", label: "Origin Airport (optional)", value: "KBOI",
      tip: "ICAO code, e.g. KLAX, or leave blank. Starts the ForeFlight route link, and when the code is in the built-in airport table it is drawn, counted in the flight distance and time, and used to pick the corner the box is entered from. Never added to the waypoint files." },
    { kind: "param", key: "destination_airport", label: "Destination Airport (optional)", value: "KBOI",
      tip: "ICAO code, e.g. KSLC, or leave blank. Ends the ForeFlight route link, and when the code is in the built-in airport table it is drawn and counted like the origin. Never added to the waypoint files." },
    { kind: "param", key: "survey_msl_ft", label: "Survey MSL (ft)", value: "20000",
      tip: "Survey altitude in feet above mean sea level, e.g. 20000. Sent to ForeFlight as the cruise altitude." },
  ],
  "center-fields": [
    { kind: "param", key: "center_lat", label: "Center Latitude", value: "43.6475", tip: "Decimal degrees; south is negative." },
    { kind: "param", key: "center_lon", label: "Center Longitude", value: "-116.1195", tip: "Decimal degrees; west is negative." },
    { kind: "param", key: "center_width_km", label: "Rectangle Width (km)", value: "12.0", tip: "Across the Initial Heading." },
    { kind: "param", key: "center_length_km", label: "Rectangle Length (km)", value: "18.0", tip: "Along the Initial Heading." },
  ],
  "line-fields": [
    { kind: "param", key: "make_line_km", label: "Make-Line Length (km)", value: "20.0",
      tip: "The whole length of a make-line, end to end. 20 gives a 20 km line centred on the waypoint." },
    { kind: "choice", key: "make_line_bearing", label: "Make-Line Bearing", value: "Along track",
      options: ["Along track", "Survey heading"],
      tip: "Along track: on the course from the waypoint above to the one below. Survey heading: parallel to the survey lines." },
    { kind: "flag", key: "make_line_keep_centre", label: "Keep centre point", value: false,
      tip: "Keeps the original waypoint between the two ends of its make-line. Costs one extra waypoint per line." },
  ],
};

const DEFAULT_BOUNDARY = [
  { lat: "43.703", lon: "-116.089", label: "Boise_Peak" },
  { lat: "43.640", lon: "-116.062", label: "Aldape_Summit" },
  { lat: "43.5949", lon: "-116.1429", label: "Table_Rock" },
  { lat: "43.635", lon: "-116.176", label: "Crestline_Trailhead" },
];

const $ = (id) => document.getElementById(id);
const controls = {};          // key -> {field, element}
let engine = null;            // survey_engine.generate_json, once Pyodide is up
let result = null;            // the last successful run
let map, layers, baseLayer = null;

// ---------- form ----------

function buildFields() {
  for (const [containerId, fields] of Object.entries(FIELDS)) {
    const box = $(containerId);
    for (const field of fields) {
      const id = "f-" + field.key;
      let element;
      if (field.kind === "flag") {
        const label = document.createElement("label");
        label.className = "check";
        label.title = field.tip;
        element = Object.assign(document.createElement("input"), { type: "checkbox", id });
        label.append(element, field.label);
        box.append(label);
      } else {
        const label = Object.assign(document.createElement("label"), { htmlFor: id, textContent: field.label });
        label.title = field.tip;
        if (field.kind === "choice") {
          element = Object.assign(document.createElement("select"), { id });
          for (const option of field.options) element.add(new Option(option, option));
        } else {
          element = Object.assign(document.createElement("input"), { type: "text", id, autocomplete: "off", spellcheck: false });
        }
        element.title = field.tip;
        box.append(label, element);
      }
      controls[field.key] = { field, element };
    }
  }
}

function textInput(className, placeholder) {
  const input = Object.assign(document.createElement("input"), { type: "text", autocomplete: "off", spellcheck: false });
  if (className) input.className = className;
  if (placeholder) input.placeholder = placeholder;
  return input;
}

// One grid row. `columns` names the cells in plan-file order; "make_line" is a checkbox.
// `pick`, when given, adds a pick-on-map button: {what: row number -> description,
// mode: the Survey Area mode the pick implies, if any}.
function buildRows(tableId, count, columns, pick) {
  const body = document.querySelector(`#${tableId} tbody`);
  for (let i = 0; i < count; i++) {
    const row = body.insertRow();
    row.insertCell().textContent = i + 1;
    for (const column of columns) {
      const cell = row.insertCell();
      const input = column === "make_line"
        ? Object.assign(document.createElement("input"), { type: "checkbox" })
        : textInput(column === "ident" ? "ident" : "", "");
      input.dataset.column = column;
      cell.append(input);
    }
    const move = row.insertCell();
    move.className = "move";
    for (const [glyph, delta, title] of [["▲", -1, "Move this row up"], ["▼", 1, "Move this row down"]]) {
      const button = Object.assign(document.createElement("button"), { type: "button", textContent: glyph, title });
      button.addEventListener("click", () => swapRows(body, i, i + delta));
      move.append(button);
    }
    if (pick) {
      const button = Object.assign(document.createElement("button"), {
        type: "button", textContent: "⊕", className: "pick",
        title: "Then click the map to place this point. Turns the basemap on. Esc cancels." });
      button.addEventListener("click", () => beginPick(pick.what(i + 1), (lat, lon) => {
        // A waypoint row is an identifier OR a position; the click means the position.
        setRowValues(row, { ...rowValues(row), ident: "", lat, lon });
        if (pick.mode) controls.survey_area_mode.element.value = pick.mode;
      }));
      row.insertCell().append(button);
    }
    const remove = Object.assign(document.createElement("button"), {
      type: "button", textContent: "✕", className: "pick",
      title: "Delete this row. The rows below it move up." });
    remove.addEventListener("click", () => deleteRow(tableId, body, i));
    row.insertCell().append(remove);
  }
}

function rowValues(row) {
  const values = {};
  for (const input of row.querySelectorAll("[data-column]")) {
    values[input.dataset.column] = input.type === "checkbox" ? input.checked : input.value.trim();
  }
  return values;
}

function setRowValues(row, values) {
  for (const input of row.querySelectorAll("[data-column]")) {
    const value = values ? values[input.dataset.column] : undefined;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value === undefined || value === null ? "" : String(value);
  }
}

function swapRows(body, a, b) {
  if (b < 0 || b >= body.rows.length) return;
  const first = rowValues(body.rows[a]), second = rowValues(body.rows[b]);
  setRowValues(body.rows[a], second);
  setRowValues(body.rows[b], first);
  rememberPlan();
}

// Remove one row and close the gap. Regenerates, except when that would only raise the
// "need 3 points" error: fewer than 3 boundary corners is not an area yet.
function deleteRow(tableId, body, index) {
  const all = [...body.rows].map(rowValues);
  const { make_line: _flag, ...texts } = all[index];
  if (!Object.values(texts).some(Boolean)) return;     // already empty
  all.splice(index, 1);
  [...body.rows].forEach((tr, i) => setRowValues(tr, all[i]));
  rememberPlan();
  const corners = all.filter((r) => r.lat || r.lon).length;
  if (tableId === "boundary-table" && corners < 3 && controls.survey_area_mode.element.value !== "CenterPoint") {
    setStatus(`Boundary point deleted. ${corners} left — at least 3 are needed to generate.`);
    return;
  }
  generate();
}

function tableRows(tableId, keepIf) {
  return [...document.querySelectorAll(`#${tableId} tbody tr`)].map(rowValues).filter(keepIf);
}

function fillTable(tableId, rows) {
  const trs = document.querySelectorAll(`#${tableId} tbody tr`);
  trs.forEach((tr, i) => setRowValues(tr, rows[i]));
}

// The form as a plan dictionary: the desktop app's <AREA>_plan.json, key for key.
function planFromForm() {
  const plan = { schema: "airborne_survey_plan/1", parameters: {} };
  for (const { field, element } of Object.values(controls)) {
    if (field.kind === "flag") plan[field.key] = element.checked;
    else if (field.kind === "param") plan.parameters[field.key] = element.value.trim();
    else plan[field.key] = element.value.trim();
  }
  plan.boundary = tableRows("boundary-table", (r) => r.lat || r.lon);
  const filled = (r) => r.ident || r.lat || r.lon || r.label;
  plan.transit = { before: tableRows("before-table", filled), after: tableRows("after-table", filled) };
  return plan;
}

function defaultPlan() {
  const plan = { schema: "airborne_survey_plan/1", parameters: {}, boundary: DEFAULT_BOUNDARY,
                 transit: { before: [], after: [] } };
  for (const fields of Object.values(FIELDS)) {
    for (const field of fields) {
      if (field.kind === "param") plan.parameters[field.key] = field.value;
      else plan[field.key] = field.value;
    }
  }
  return plan;
}

// Throws on a file that is not a plan, before anything on the form is touched.
function applyPlan(data) {
  if (!data || typeof data !== "object" || !Array.isArray(data.boundary)) {
    throw new Error("Not a flight plan file: no 'boundary' list.");
  }
  if (data.schema && data.schema !== "airborne_survey_plan/1") {
    throw new Error(`Unsupported plan schema "${data.schema}".`);
  }
  const transit = data.transit || {};
  if (data.boundary.length > BOUNDARY_ROWS) {
    throw new Error(`File has ${data.boundary.length} boundary points; only ${BOUNDARY_ROWS} rows are available.`);
  }
  for (const group of ["before", "after"]) {
    if ((transit[group] || []).length > TRANSIT_ROWS) {
      throw new Error(`File has ${transit[group].length} '${group}' waypoints; only ${TRANSIT_ROWS} rows are available.`);
    }
  }
  // Plans saved before Survey MSL held a flight level (200 = FL200) under survey_altitude.
  const params = { ...(data.parameters || {}) };
  if (params.survey_altitude != null && params.survey_msl_ft == null) {
    const level = parseFloat(params.survey_altitude);
    if (Number.isFinite(level)) params.survey_msl_ft = String(Math.round(level * 100));
  }
  delete params.survey_altitude;
  data = { ...data, parameters: params };
  const base = defaultPlan();
  for (const { field, element } of Object.values(controls)) {
    let value = field.kind === "param" ? (data.parameters || {})[field.key] : data[field.key];
    if (value === undefined || value === null) {
      value = field.kind === "param" ? base.parameters[field.key] : base[field.key];
    }
    if (field.kind === "flag") element.checked = Boolean(value);
    else if (field.dynamic) continue;   // set below, once the heading it depends on is in
    else if (field.kind === "choice") element.value = field.options.includes(String(value)) ? String(value) : field.value;
    else element.value = String(value);
  }
  refreshFirstLine(data.first_line || "Auto");
  fillTable("boundary-table", data.boundary);
  fillTable("before-table", transit.before || []);
  fillTable("after-table", transit.after || []);
}

function rememberPlan() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(planFromForm())); } catch (_) { /* private window */ }
}

function restorePlan() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) { applyPlan(JSON.parse(saved)); return; }
  } catch (_) { /* unreadable or stale: fall through to the demo area */ }
  applyPlan(defaultPlan());
}

// ---------- first line ----------
// The two outermost lines sit on opposite sides of the box, across the heading. They are
// offered by compass point for the heading in use -- West/East at 000, Northwest/Southeast
// at 045, North/South at 090 -- so the drop-down never holds a choice that means nothing.
// Mirrors compass_point / edge_choices / first_line_rows in survey_engine.py.

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const COMPASS_NAMES = { N: "North", NE: "Northeast", E: "East", SE: "Southeast",
                        S: "South", SW: "Southwest", W: "West", NW: "Northwest" };

function compassPoint(bearing) {
  return COMPASS[Math.floor((((bearing % 360) + 360) % 360 + 22.5) / 45) % 8];
}

// Rebuild the First Line options for the heading typed now. `wanted` is the code to keep
// (default: the current pick); one from another heading moves to the nearer edge, or to
// Auto when it sits exactly between the two.
function refreshFirstLine(wanted) {
  const select = controls.first_line.element;
  const current = wanted !== undefined ? String(wanted) : (select.value || "Auto");
  const heading = parseFloat(controls.initial_heading_deg.element.value);
  const edges = Number.isFinite(heading) ? [compassPoint(heading + 90), compassPoint(heading - 90)] : [];
  select.length = 0;
  select.add(new Option("Auto (nearest entry)", "Auto"));
  for (const code of edges) select.add(new Option(`${COMPASS_NAMES[code]} edge`, code));
  let pick = "Auto";
  if (edges.includes(current)) {
    pick = current;
  } else if (edges.length && COMPASS.includes(current)) {
    const target = COMPASS.indexOf(current) * 45;
    const off = (b) => Math.abs(((((b - target + 180) % 360) + 360) % 360) - 180);
    const toRight = off(heading + 90), toLeft = off(heading - 90);
    if (Math.abs(toRight - toLeft) >= 1e-6) pick = toRight < toLeft ? edges[0] : edges[1];
  }
  select.value = pick;
}

// ---------- status ----------

function setStatus(text) { $("status").textContent = text; }

function showError(title, message) {
  const box = $("error");
  box.hidden = !message;
  box.textContent = message ? `${title}: ${message}` : "";
}

function showWarning(message) {
  $("warning").hidden = !message;
  $("warning").textContent = message || "";
}

// ---------- engine ----------

async function startEngine() {
  setStatus("Loading the planning engine. The first visit takes 10–20 seconds…");
  const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX });
  await pyodide.loadPackage(["numpy", "shapely", "pyproj"]);
  const response = await fetch("survey_engine.py", { cache: "no-cache" });
  if (!response.ok) throw new Error(`survey_engine.py: HTTP ${response.status}`);
  pyodide.FS.writeFile("survey_engine.py", await response.text());
  // The airport table is optional: without it airports go undrawn and the summary says so.
  try {
    const airports = await fetch("airports.csv");
    if (airports.ok) pyodide.FS.writeFile("airports.csv", await airports.text());
  } catch (_) { /* offline or blocked: carry on without it */ }
  engine = pyodide.runPython("import survey_engine\nsurvey_engine.generate_json");
}

function generate() {
  if (!engine) return;
  const plan = planFromForm();
  rememberPlan();
  let out;
  try {
    out = JSON.parse(engine(JSON.stringify(plan)));
  } catch (err) {
    out = { ok: false, title: "Execution Error", message: String(err) };
  }
  if (!out.ok) {
    showError(out.title, out.message);
    setStatus("Not generated — fix the problem above and try again. The preview and downloads below are from the last successful run.");
    return;
  }
  result = out;
  showError("", "");
  showWarning(out.warning);
  setStatus(out.status);
  $("summary").textContent = out.summary;
  $("plan-title").textContent = out.title;
  for (const button of document.querySelectorAll("[data-file]")) button.disabled = false;
  $("save-plan").disabled = false;
  $("toggle-qr").disabled = false;
  $("open-foreflight").disabled = false;
  drawMap();
  if (!$("qr").hidden) drawQr();
  refreshTerrain();
}

// ---------- downloads ----------

function download(file) {
  let blob;
  if (file.b64 !== undefined) {
    const bytes = Uint8Array.from(atob(file.b64), (c) => c.charCodeAt(0));
    blob = new Blob([bytes], { type: file.mime });
  } else {
    blob = new Blob([file.text], { type: file.mime + ";charset=utf-8" });
  }
  const link = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: file.name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
}

// Saves what is on the form now, not the last generated plan, so nothing typed is lost.
function savePlan() {
  const plan = planFromForm();
  plan.area_name = plan.area_name.replace(/ /g, "_");
  plan.saved_utc = new Date().toISOString().slice(0, 16).replace("T", " ") + "Z";
  download({ name: `${plan.area_name || "survey_area"}_plan.json`, mime: "application/json",
             text: JSON.stringify(plan, null, 2) });
}

async function loadPlanFile(file) {
  try {
    applyPlan(JSON.parse(await file.text()));
  } catch (err) {
    showError("Load Failed", err.message);
    return;
  }
  setStatus(`Loaded plan ${file.name}.`);
  generate();
}

// ---------- map ----------

const BASEMAPS = {
  topo: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
  satellite: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
};

function startMap() {
  map = L.map("map", { zoomSnap: 0.25, attributionControl: true }).setView([43.65, -116.12], 10);
  map.attributionControl.setPrefix("");
  L.control.scale({ imperial: false }).addTo(map);
  layers = L.layerGroup().addTo(map);
  map.on("click", onMapClick);
  startOverlays();
}

function setBasemap(name) {
  if (baseLayer) { map.removeLayer(baseLayer); baseLayer = null; }
  if (BASEMAPS[name]) {
    baseLayer = L.tileLayer(BASEMAPS[name], { maxZoom: 19, attribution: "Tiles © Esri" }).addTo(map);
    baseLayer.bringToBack();
  }
}

function dot(lat, lon, name, options, labelClass, permanent) {
  const marker = L.circleMarker([lat, lon], options);
  marker.bindTooltip(name, { permanent, direction: "right", offset: [6, 0], className: "wp-label " + labelClass });
  marker.bindPopup(`${name}<br>${lat.toFixed(6)}, ${lon.toFixed(6)}`);
  return marker.addTo(layers);
}

function drawMap() {
  const p = result.preview;
  const labels = $("show-labels").checked;
  layers.clearLayers();
  const everything = [];

  if (p.envelope.length) {
    L.polygon(p.envelope, { color: "#1f6fd0", weight: 2, fillOpacity: 0.08 }).addTo(layers);
    everything.push(...p.envelope);
  }
  L.polyline(p.track, { color: "#d81b1b", weight: 3, dashArray: "5 6" }).addTo(layers);
  everything.push(...p.track);

  // Transit legs join the chain to the end of the track they lead into or out of.
  const legs = [[p.transit_before, true], [p.transit_after, false]];
  for (const [chain, leads] of legs) {
    if (!chain.length) continue;
    const points = chain.map(([lat, lon]) => [lat, lon]);
    const anchor = leads ? p.track[0] : p.track[p.track.length - 1];
    L.polyline(leads ? [...points, anchor] : [anchor, ...points],
               { color: "#8a8a8a", weight: 3, dashArray: "2 8" }).addTo(layers);
    for (const [lat, lon, name] of chain) {
      dot(lat, lon, name, { radius: 5, color: "#444444", weight: 2, fillColor: "#8a8a8a", fillOpacity: 0.9 }, "transit", labels);
    }
    everything.push(...points);
  }

  for (const [lat, lon, label] of p.boundary) {
    dot(lat, lon, label, { radius: 5, color: "#7b2fbe", weight: 1, fillColor: "#7b2fbe", fillOpacity: 0.9 }, "boundary", labels);
  }
  // Past a couple of dozen, permanent labels are an unreadable mat; fall back to hover.
  const waypointLabels = labels && p.waypoints.length <= 24;
  p.waypoints.forEach(([lat, lon, name], i) => {
    const first = i === 0;
    dot(lat, lon, name, { radius: first ? 7 : 4, color: first ? "#0a8a3a" : "#d81b1b", weight: first ? 3 : 1,
                          fillColor: "#d81b1b", fillOpacity: 0.9 }, "", waypointLabels);
  });

  map.invalidateSize();
  if (everything.length) map.fitBounds(L.latLngBounds(everything).pad(0.08));
}

// ---------- terrain ----------
// Highest and lowest ground under the survey box, and the height above ground that leaves
// at Survey MSL. Mirrors terrain_extremes / terrain_lines in the desktop app: the open
// "terrarium" terrain tiles (elevation packed into each pixel's red, green and blue), every
// pixel whose centre is inside the box.

const TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/";
const TERRAIN_MAX_ZOOM = 12, TERRAIN_MAX_TILES = 12, LOW_AGL_FT = 1000, M_TO_FT = 3.280839895;
let terrainLayer, terrainSeq = 0, terrainCache = null;    // {key, data}

function terrainPixel(lat, lon, zoom) {
  const scale = 256 * 2 ** zoom, clamped = Math.max(-85, Math.min(85, lat));
  return [(lon + 180) / 360 * scale,
          (1 - Math.asinh(Math.tan(clamped * Math.PI / 180)) / Math.PI) / 2 * scale];
}

function terrainLatLon(px, py, zoom) {
  const scale = 256 * 2 ** zoom;
  return [Math.atan(Math.sinh(Math.PI * (1 - 2 * py / scale))) * 180 / Math.PI, px / scale * 360 - 180];
}

function loadTerrainTile(zoom, x, y) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.crossOrigin = "anonymous";
    image.referrerPolicy = "no-referrer";
    image.onload = () => {
      const canvas = Object.assign(document.createElement("canvas"), { width: 256, height: 256 });
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(image, 0, 0);
      resolve(ctx.getImageData(0, 0, 256, 256).data);
    };
    image.onerror = () => reject(new Error("terrain tiles could not be loaded"));
    image.src = `${TERRAIN_URL}${zoom}/${x}/${y}.png`;
  });
}

async function terrainExtremes(ringLatLon) {
  let zoom, ring, tx0, tx1, ty0, ty1;
  for (zoom = TERRAIN_MAX_ZOOM; zoom >= 0; zoom--) {
    ring = ringLatLon.map(([lat, lon]) => terrainPixel(lat, lon, zoom));
    const xs = ring.map((p) => p[0]), ys = ring.map((p) => p[1]);
    tx0 = Math.floor(Math.min(...xs) / 256); tx1 = Math.floor(Math.max(...xs) / 256);
    ty0 = Math.floor(Math.min(...ys) / 256); ty1 = Math.floor(Math.max(...ys) / 256);
    if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= TERRAIN_MAX_TILES) break;
  }
  const edges = ring.map((p, i) => [p, ring[(i + 1) % ring.length]]);
  let low = null, high = null;            // [metres, global px, global py]
  for (let ty = ty0; ty <= ty1; ty++) {
    for (let tx = tx0; tx <= tx1; tx++) {
      const data = await loadTerrainTile(zoom, tx, ty);
      for (let r = 0; r < 256; r++) {
        const gy = ty * 256 + r + 0.5;
        const crossings = [];
        for (const [[x0, y0], [x1, y1]] of edges) {
          if ((y0 <= gy) !== (y1 <= gy)) crossings.push(x0 + (gy - y0) * (x1 - x0) / (y1 - y0));
        }
        crossings.sort((a, b) => a - b);
        for (let k = 0; k + 1 < crossings.length; k += 2) {
          const c0 = Math.max(Math.ceil(crossings[k] - 0.5) - tx * 256, 0);
          const c1 = Math.min(Math.floor(crossings[k + 1] - 0.5) - tx * 256, 255);
          for (let c = c0; c <= c1; c++) {
            const i = (r * 256 + c) * 4;
            const metres = data[i] * 256 + data[i + 1] + data[i + 2] / 256 - 32768;
            if (!high || metres > high[0]) high = [metres, tx * 256 + c + 0.5, gy];
            if (!low || metres < low[0]) low = [metres, tx * 256 + c + 0.5, gy];
          }
        }
      }
    }
  }
  if (!high) return null;
  const midLat = ringLatLon.reduce((sum, p) => sum + p[0], 0) / ringLatLon.length;
  return { min_m: low[0], max_m: high[0], min_at: terrainLatLon(low[1], low[2], zoom),
           max_at: terrainLatLon(high[1], high[2], zoom),
           res_m: 156543.03392 * Math.cos(midLat * Math.PI / 180) / 2 ** zoom };
}

const feet = (value) => Math.round(value).toLocaleString("en-US");

function terrainLines(terrain, mslText) {
  const lowFt = terrain.min_m * M_TO_FT, highFt = terrain.max_m * M_TO_FT;
  const lines = [`Terrain in Survey Box: ${feet(lowFt)} to ${feet(highFt)} ft MSL ` +
                 `(${feet(terrain.min_m)} to ${feet(terrain.max_m)} m)`];
  const msl = parseFloat(mslText);
  let warn = "";
  if (!Number.isFinite(msl) || msl <= 0) {
    lines.push("AGL: enter Survey MSL (ft) to see the height above ground");
  } else {
    const minAgl = msl - highFt, maxAgl = msl - lowFt;
    lines.push(`AGL at ${feet(msl)} ft MSL: minimum ${feet(minAgl)} ft, maximum ${feet(maxAgl)} ft`);
    if (minAgl <= 0) warn = "WARNING: TERRAIN IN THE BOX IS ABOVE THE SURVEY ALTITUDE";
    else if (minAgl < LOW_AGL_FT) warn = `WARNING: less than ${feet(LOW_AGL_FT)} ft above the highest terrain`;
    if (warn) lines.push(warn);
  }
  lines.push(`Highest ground: ${terrain.max_at[0].toFixed(4)}, ${terrain.max_at[1].toFixed(4)}   ` +
             `Lowest: ${terrain.min_at[0].toFixed(4)}, ${terrain.min_at[1].toFixed(4)}`);
  lines.push(`(terrain tiles, about ${Math.round(terrain.res_m)} m a pixel; obstacles such as towers and trees are not included)`);
  return { lines, warn };
}

function showTerrain(run, note, terrain) {
  if (run !== result) return;             // a newer plan has been generated meanwhile
  terrainLayer.clearLayers();
  let lines = [note], warn = "";
  if (terrain) {
    const msl = controls.survey_msl_ft.element.value;
    ({ lines, warn } = terrainLines(terrain, msl));
    for (const [at, metres, word] of [[terrain.max_at, terrain.max_m, "High"], [terrain.min_at, terrain.min_m, "Low"]]) {
      L.circleMarker(at, { radius: 6, color: "#ffffff", weight: 1, fillColor: "#5d4037", fillOpacity: 1 })
        .bindTooltip(`${word} ${feet(metres * M_TO_FT)} ft`, { permanent: true, direction: "bottom", offset: [0, 6], className: "wp-label terrain" })
        .addTo(terrainLayer);
    }
    const mslFt = parseFloat(msl);
    if (Number.isFinite(mslFt)) {
      setStatus(`${run.status} AGL ${feet(mslFt - terrain.max_m * M_TO_FT)} to ${feet(mslFt - terrain.min_m * M_TO_FT)} ft.`);
    }
  }
  $("summary").textContent = lines.join("\n") + "\n\n" + run.summary;
  if (warn) showWarning([run.warning, warn].filter(Boolean).join("\n"));
}

// Called after every successful generate, for the box just drawn.
async function refreshTerrain() {
  const run = result, ring = run.preview.envelope, seq = ++terrainSeq;
  const key = JSON.stringify(ring);
  if (!ring.length) return showTerrain(run, "Terrain: n/a (no survey box)", null);
  // Basemap None is the keep-it-private setting; terrain tiles would give the area away
  // just as map tiles do.
  if (!BASEMAPS[$("basemap").value]) {
    return showTerrain(run, "Terrain and AGL: not looked up while Basemap is None", null);
  }
  if (terrainCache && terrainCache.key === key) return showTerrain(run, "", terrainCache.data);
  showTerrain(run, "Terrain and AGL: looking up…", null);
  let terrain;
  try {
    // The envelope closes on itself; the repeated last corner is not a separate edge.
    terrain = await terrainExtremes(ring.slice(0, -1));
  } catch (err) {
    if (seq === terrainSeq) showTerrain(run, `Terrain and AGL: not available (${err.message})`, null);
    return;
  }
  if (seq !== terrainSeq) return;
  if (!terrain) return showTerrain(run, "Terrain and AGL: the survey box is too small to measure", null);
  terrainCache = { key, data: terrain };
  showTerrain(run, "", terrain);
}

// ---------- live overlays ----------
// Fire hotspots (NASA FIRMS VIIRS, via the keyless copy Esri hosts) and the FAA's active
// TFRs. Both off until ticked, both display only. Mirrors the desktop app's overlays.

const FIRE_URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/" +
  "Satellite_VIIRS_Thermal_Hotspots_and_Fire_Activity/FeatureServer/0/query" +
  "?where=1%3D1&geometryType=esriGeometryEnvelope&inSR=4326" +
  "&outFields=hours_old,frp,satellite&outSR=4326&geometryPrecision=5&f=geojson&geometry=";
const TFR_WMS = "https://tfr.faa.gov/geoserver/TFR/wms";
const OVERLAY_MAX_AGE_MS = 600000;
const TFR_COLOUR = "#b0006a";
let fireLayer, tfrLayer, fireRenderer;
let fireBox = null, fireAt = 0, fireSeq = 0, tfrSeq = 0, tfrTimer = null;

function startOverlays() {
  // Their own panes, under the plan itself (Leaflet's overlay pane is 400).
  map.createPane("tfr").style.zIndex = 350;
  map.createPane("fire").style.zIndex = 360;
  fireRenderer = L.canvas({ pane: "fire" });
  tfrLayer = L.layerGroup().addTo(map);
  fireLayer = L.layerGroup().addTo(map);
  terrainLayer = L.layerGroup().addTo(map);
  let timer = null;
  map.on("moveend", () => { clearTimeout(timer); timer = setTimeout(refreshFires, 400); });
}

// Grow the view by a degree and out to a 2-degree grid: one download serves a lot of
// panning, and the server sees a coarse block rather than the survey's outline.
function snapRegion([west, south, east, north]) {
  const block = 2;
  return [Math.max(-180, Math.floor((west - 1) / block) * block),
          Math.max(-90, Math.floor((south - 1) / block) * block),
          Math.min(180, Math.ceil((east + 1) / block) * block),
          Math.min(90, Math.ceil((north + 1) / block) * block)];
}

async function refreshFires() {
  if (!$("show-fires").checked) {
    fireSeq++; fireBox = null; fireLayer.clearLayers();
    return;
  }
  const b = map.getBounds();
  const view = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
  const covers = fireBox && fireBox[0] <= view[0] && fireBox[1] <= view[1] &&
                 fireBox[2] >= view[2] && fireBox[3] >= view[3];
  if (covers && Date.now() - fireAt < OVERLAY_MAX_AGE_MS) return;
  const want = snapRegion(view), seq = ++fireSeq;
  // Marked as in hand straight away, so a failure is not retried on every pan.
  fireBox = want; fireAt = Date.now();
  let features;
  try {
    const response = await fetch(FIRE_URL + want.join(","), { referrerPolicy: "no-referrer" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    features = (await response.json()).features;
    if (!Array.isArray(features)) throw new Error("unexpected answer");
  } catch (err) {
    if (seq === fireSeq) setStatus(`Fire hotspots could not be loaded (${err.message}). Check the internet connection.`);
    return;
  }
  if (seq !== fireSeq) return;            // unticked, or the view moved on, meanwhile
  fireLayer.clearLayers();
  const hours = (f) => Number((f.properties || {}).hours_old) || 0;
  features.sort((a, b2) => hours(b2) - hours(a));     // newest last, so it lands on top
  let recent = 0;
  for (const feature of features) {
    const [lon, lat] = feature.geometry.coordinates, age = hours(feature);
    if (age <= 24) recent++;
    const marker = L.circleMarker([lat, lon], {
      renderer: fireRenderer, pane: "fire", radius: 3, stroke: false, fillOpacity: 0.85,
      fillColor: age <= 24 ? "#d50000" : age <= 72 ? "#ff6d00" : "#f2b600" });
    const p = feature.properties || {};
    marker.bindPopup(`Fire hotspot (VIIRS ${p.satellite || ""})<br>${Math.round(age)} h old` +
                     (p.frp != null ? ` · ${p.frp} MW` : "") + `<br>${lat.toFixed(4)}, ${lon.toFixed(4)}`);
    marker.addTo(fireLayer);
  }
  setStatus(`Fire hotspots: ${features.length} in the last 7 days across this region, ${recent} in the last 24 h (NASA FIRMS, VIIRS).`);
}

function tfrPopup(feature) {
  const p = feature.properties || {}, box = document.createElement("div");
  const head = document.createElement("b");
  head.textContent = `TFR ${String(p.NOTAM_KEY || "").split("-")[0]} — ${p.LEGAL || ""}`;
  const title = document.createElement("div");
  title.textContent = p.TITLE || "";
  const note = document.createElement("div");
  note.textContent = "Outline only. Check NOTAMs for altitudes and times.";
  box.append(head, title, note);
  return box;
}

// The national list, through this site's /tfr relay (the FAA server sends no CORS header,
// so the page cannot read it directly). Where the relay is missing -- the page served from
// a plain file server -- the FAA's own map image is laid over instead: same outlines, but
// no names, and the FAA then sees which map tiles were asked for.
async function refreshTfrs() {
  const seq = ++tfrSeq;
  clearTimeout(tfrTimer);
  tfrLayer.clearLayers();
  if (!$("show-tfrs").checked) return;
  tfrTimer = setTimeout(refreshTfrs, OVERLAY_MAX_AGE_MS);
  try {
    const response = await fetch("tfr", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!Array.isArray(data.features)) throw new Error("unexpected answer");
    if (seq !== tfrSeq) return;
    L.geoJSON(data, {
      pane: "tfr",
      style: { color: TFR_COLOUR, weight: 2, dashArray: "8 5", fillColor: TFR_COLOUR, fillOpacity: 0.1 },
      onEachFeature: (feature, layer) => {
        layer.bindPopup(() => tfrPopup(feature));
        layer.bindTooltip(`TFR ${String((feature.properties || {}).NOTAM_KEY || "").split("-")[0]} ${(feature.properties || {}).LEGAL || ""}`,
                          { sticky: true, className: "wp-label tfr" });
      },
    }).addTo(tfrLayer);
    const hazards = data.features.filter((f) => (f.properties || {}).LEGAL === "HAZARDS").length;
    setStatus(`TFRs: ${data.features.length} active nationwide, ${hazards} of them HAZARDS (FAA). Not a substitute for a briefing.`);
  } catch (_) {
    if (seq !== tfrSeq) return;
    L.tileLayer.wms(TFR_WMS, { layers: "TFR:V_TFR_LOC", format: "image/png", transparent: true,
                               pane: "tfr", attribution: "TFRs © FAA" }).addTo(tfrLayer);
    setStatus("TFRs shown as the FAA's map image (outlines without names). Not a substitute for a briefing.");
  }
}

// ---------- pick on map ----------
// A Pick button arms the map: the next click hands its position to `apply(lat, lon)`, as
// text, and the plan is regenerated. Nothing leaves the browser.

let picking = null;

function beginPick(description, apply) {
  if (!$("qr").hidden) toggleQr();
  picking = apply;
  $("map").classList.add("picking");
  let note = "";
  if (!BASEMAPS[$("basemap").value]) {
    // A click on a blank map can only be aimed against the pattern itself.
    $("basemap").value = "topo";
    setBasemap("topo");
    note = " Basemap turned on (its tiles come from Esri).";
  }
  setStatus(`Click the map to place ${description}. Esc cancels.${note}`);
  // The same prompt on the map itself, with a Cancel button: an iPad has no Esc key, and
  // on a narrow screen the status line is scrolled out of sight above the map.
  $("pick-text").textContent = `Tap the map to place ${description}`;
  $("pick-banner").hidden = false;
  // On a phone the map sits below the form.
  $("view").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function endPick(message) {
  if (!picking) return;
  picking = null;
  $("map").classList.remove("picking");
  $("pick-banner").hidden = true;
  if (message) setStatus(message);
}

function onMapClick(event) {
  if (!picking) return;
  const apply = picking, at = event.latlng.wrap();
  endPick();
  map.closePopup();
  // 5 decimals is about a metre, far finer than a click can aim.
  apply(at.lat.toFixed(5), at.lng.toFixed(5));
  rememberPlan();
  generate();
}

function pickCentre() {
  beginPick("the centre point", (lat, lon) => {
    controls.center_lat.element.value = lat;
    controls.center_lon.element.value = lon;
    controls.survey_area_mode.element.value = "CenterPoint";
  });
}

// ---------- QR ----------

function drawQr() {
  const canvas = $("qr-canvas"), note = $("qr-note");
  let code;
  try {
    // Lowest error correction: the route is long, and density is what stops a scan.
    code = qrcode(0, "L");
    code.addData(result.route_url);
    code.make();
  } catch (_) {
    canvas.width = canvas.height = 10;
    note.textContent = "This route is too long to fit in a QR code. Use the content pack or the route link file instead.";
    return;
  }
  const count = code.getModuleCount(), quiet = 4;
  const scale = Math.max(2, Math.floor(640 / (count + 2 * quiet)));
  canvas.width = canvas.height = (count + 2 * quiet) * scale;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000000";
  for (let r = 0; r < count; r++) {
    for (let c = 0; c < count; c++) {
      if (code.isDark(r, c)) ctx.fillRect((c + quiet) * scale, (r + quiet) * scale, scale, scale);
    }
  }
  note.textContent = "Scan with the iPad camera to open this survey in ForeFlight as a route " +
    `(${result.route_url.length} characters).`;
}

// The browser hands foreflightmobile:// to ForeFlight when it is installed on this device.
// When it is not, most browsers do nothing at all, so say what to do instead.
function openInForeFlight() {
  if (!result) return;
  window.location.href = result.route_url;
  setStatus("Opening ForeFlight… If nothing happens, ForeFlight is not installed on this " +
            "device: use Show QR and scan it with the iPad, or download the content pack.");
}

function toggleQr() {
  const showing = !$("qr").hidden;
  $("qr").hidden = showing;
  $("toggle-qr").textContent = showing ? "Show QR" : "Show Map";
  if (showing) map.invalidateSize(); else drawQr();
}

// ---------- wiring ----------

function selectTab(name) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.tab === name));
  }
  for (const tab of document.querySelectorAll(".tab")) tab.hidden = tab.id !== "tab-" + name;
}

async function main() {
  buildFields();
  buildRows("boundary-table", BOUNDARY_ROWS, ["lat", "lon", "label"],
            { what: (n) => `boundary point ${n}`, mode: AREA_MODES[0] });
  buildRows("before-table", TRANSIT_ROWS, ["ident", "lat", "lon", "label", "make_line"],
            { what: (n) => `waypoint ${n} before the survey box` });
  buildRows("after-table", TRANSIT_ROWS, ["ident", "lat", "lon", "label", "make_line"],
            { what: (n) => `waypoint ${n} after the survey box` });
  restorePlan();
  controls.initial_heading_deg.element.addEventListener("input", () => refreshFirstLine());
  startMap();
  setBasemap($("basemap").value);     // terrain unless the visitor chose otherwise

  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
  }
  $("generate").addEventListener("click", generate);
  $("inputs").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.tagName === "INPUT") generate();
  });
  $("inputs").addEventListener("change", rememberPlan);
  $("save-plan").addEventListener("click", savePlan);
  $("load-plan").addEventListener("click", () => $("plan-file").click());
  $("plan-file").addEventListener("change", (event) => {
    if (event.target.files[0]) loadPlanFile(event.target.files[0]);
    event.target.value = "";
  });
  $("reset-plan").addEventListener("click", () => { applyPlan(defaultPlan()); generate(); });
  $("clear-boundary").addEventListener("click", () => { fillTable("boundary-table", []); rememberPlan(); });
  $("clear-transit").addEventListener("click", () => {
    fillTable("before-table", []); fillTable("after-table", []); rememberPlan();
  });
  $("show-labels").addEventListener("change", () => { if (result) drawMap(); });
  $("basemap").addEventListener("change", (event) => {
    setBasemap(event.target.value);
    if (result) refreshTerrain();       // None stops the terrain lookup too; a map starts it
  });
  $("toggle-qr").addEventListener("click", toggleQr);
  $("open-foreflight").addEventListener("click", openInForeFlight);
  $("show-fires").addEventListener("change", refreshFires);
  $("show-tfrs").addEventListener("change", refreshTfrs);
  // A reload restores these checkboxes ticked without firing "change", so honour the state
  // they are in now rather than waiting for a click.
  refreshFires();
  refreshTfrs();
  $("pick-center").addEventListener("click", pickCentre);
  $("pick-cancel").addEventListener("click", () => endPick("Pick cancelled."));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") endPick("Pick cancelled.");
  });
  for (const button of document.querySelectorAll("[data-file]")) {
    button.addEventListener("click", () => download(result.files[button.dataset.file]));
  }

  try {
    await startEngine();
  } catch (err) {
    showError("Could not start", `The planning engine failed to load (${err.message || err}). Check the internet connection and reload the page.`);
    setStatus("Not ready.");
    return;
  }
  $("generate").disabled = false;
  generate();
}

main();

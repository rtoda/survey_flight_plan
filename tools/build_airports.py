"""Rebuild web/airports.csv, the ICAO airport table, from OurAirports.

Downloads the OurAirports airport list (public domain, about 12 MB) and keeps every airport,
small airport and seaplane base that carries a four-character ICAO code -- about 10,000 rows,
460 KB. Heliports and closed fields are left out. Both the desktop app and the web app read
the result.

Run from the repository root:  python tools/build_airports.py
"""
import csv
import io
import os
import sys
import urllib.request

SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"
KEEP_TYPES = ("large_airport", "medium_airport", "small_airport", "seaplane_base")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "airports.csv")


def main():
    with urllib.request.urlopen(SOURCE, timeout=60) as response:
        text = response.read().decode("utf-8")
    keep = []
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("icao_code") or "").strip().upper()
        if len(code) != 4 or not code.isalnum() or row["type"] not in KEEP_TYPES:
            continue
        keep.append((code, round(float(row["latitude_deg"]), 5),
                     round(float(row["longitude_deg"]), 5), row["name"].strip()))
    keep.sort()

    out = io.StringIO()
    out.write("# ICAO airport locations for the Origin/Destination Airport fields.\n"
              "# Source: OurAirports (https://ourairports.com/data/), public domain. Airports, small\n"
              "# airports and seaplane bases that carry an ICAO code; heliports and closed fields left out.\n"
              "# Regenerate with tools/build_airports.py.\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["icao", "lat", "lon", "name"])
    writer.writerows(keep)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(out.getvalue())
    print(f"{len(keep)} airports -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

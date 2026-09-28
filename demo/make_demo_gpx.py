#!/usr/bin/env python3
"""Generates demo/ride.gpx: a synthetic bike ride used by the "demo" tracking id.

Route from the OSM bike router (routing.openstreetmap.de), elevation from Open-Meteo,
timestamps from a simple gradient-based speed model with two stops. No real ride data.

    python3 demo/make_demo_gpx.py
"""
import json
import math
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

WAYPOINTS = [  # lng, lat
    (14.4213, 50.0875),  # Prague, Old Town Square
    (14.3925, 50.0180),  # along the Vltava, Braník
    (14.2860, 49.9690),  # Černošice
    (14.1882, 49.9394),  # Karlštejn castle
]
NAME = "Prague → Karlštejn"
START = datetime(2025, 6, 14, 8, 30, tzinfo=timezone.utc)
STOPS = {0.28: 4 * 60, 0.62: 25 * 60}  # fraction of distance -> seconds stopped
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ride.gpx")


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "karoo-live-tracker demo generator"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def haversine(a, b):
    rad = math.pi / 180
    dlat, dlng = (b[0] - a[0]) * rad, (b[1] - a[1]) * rad
    h = math.sin(dlat / 2) ** 2 + math.cos(a[0] * rad) * math.cos(b[0] * rad) * math.sin(dlng / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


# 1. Route geometry, densified to ~10 m steps
coords = ";".join(f"{lng},{lat}" for lng, lat in WAYPOINTS)
route = get_json(f"https://routing.openstreetmap.de/routed-bike/route/v1/driving/{coords}"
                 "?overview=full&geometries=geojson")["routes"][0]
raw = [[lat, lng] for lng, lat in route["geometry"]["coordinates"]]
pts = [raw[0]]
for a, b in zip(raw, raw[1:]):
    n = max(1, int(haversine(a, b) // 10))
    pts += [[a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n] for k in range(1, n + 1)]
cum = [0.0]
for a, b in zip(pts, pts[1:]):
    cum.append(cum[-1] + haversine(a, b))
total = cum[-1]

# 2. Elevation every ~100 m (Open-Meteo takes 100 coordinates per request), interpolated in between
step = max(1, len(pts) // max(2, int(total / 100)))
sample_idx = list(range(0, len(pts), step)) + ([len(pts) - 1] if (len(pts) - 1) % step else [])
sample_ele = []
for i in range(0, len(sample_idx), 100):
    chunk = [pts[j] for j in sample_idx[i:i + 100]]
    lat = ",".join(f"{p[0]:.5f}" for p in chunk)
    lng = ",".join(f"{p[1]:.5f}" for p in chunk)
    sample_ele += get_json(f"https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lng}")["elevation"]
    time.sleep(0.5)
ele = []
for k in range(len(sample_idx) - 1):
    i0, i1 = sample_idx[k], sample_idx[k + 1]
    for j in range(i0, i1):
        ele.append(sample_ele[k] + (sample_ele[k + 1] - sample_ele[k]) * (cum[j] - cum[i0]) / ((cum[i1] - cum[i0]) or 1))
ele.append(sample_ele[-1])

# 3. Timestamps: ~24 km/h on the flat, slower uphill, faster downhill, plus the stops
t, times, pending = 0.0, [0.0], sorted(STOPS.items())
for i in range(1, len(pts)):
    seg = cum[i] - cum[i - 1]
    grade = (ele[i] - ele[i - 1]) / seg if seg else 0
    kmh = min(45, max(9, 24 * (1 - grade * 7) + 1.5 * math.sin(i / 40)))
    t += seg / (kmh / 3.6)
    if pending and cum[i] >= pending[0][0] * total:
        t += pending.pop(0)[1]
    times.append(t)

# 4. Write GPX
with open(OUT, "w") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<gpx version="1.1" creator="karoo-live-tracker demo generator" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'  <metadata><name>{NAME}</name></metadata>\n  <trk>\n    <name>{NAME}</name>\n    <trkseg>\n')
    for p, e, s in zip(pts, ele, times):
        ts = (START + timedelta(seconds=round(s))).strftime("%Y-%m-%dT%H:%M:%SZ")
        f.write(f'      <trkpt lat="{p[0]:.6f}" lon="{p[1]:.6f}"><ele>{e:.1f}</ele><time>{ts}</time></trkpt>\n')
    f.write("    </trkseg>\n  </trk>\n</gpx>\n")

print(f"{NAME}: {total / 1000:.1f} km, {len(pts)} points, {times[-1] / 3600:.2f} h -> {OUT}")

"""Simulated ride for demos, served by server.py for tracking id "demo".

Replays demo/ride.gpx (or DEMO_GPX) using its own timestamps, so real stops show up as
"paused". Without a GPX it falls back to the route in response/live.json with modelled
speeds and one coffee stop. By default it is a frozen snapshot halfway through the ride;
with DEMO_SPEED=N it starts there and plays at N x real time, finishes and loops. The response mimics the Hammerhead API, using
live.json as the template.
"""
import bisect
import json
import math
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "response", "live.json")
GPX = os.environ.get("DEMO_GPX", os.path.join(HERE, "demo", "ride.gpx"))
SPEED_FACTOR = float(os.environ.get("DEMO_SPEED", 0))  # 0 = frozen snapshot (for screenshots)
START_AT = 0.5          # fraction of the ride time where the demo starts
STOP_GAP = 60           # a gap between GPX points longer than this (s) counts as a stop
BASE_KMH = 25           # fallback model only
STOP_AT = 0.6           # fallback model: coffee stop at this fraction of the route
STOP_SECONDS = 20 * 60  # fallback model: coffee stop length
FINISHED_SECONDS = 90   # ride time the "finished" state is shown before looping


def decode(s, dim=2, factor=1e5):
    out, prev, i = [], [0] * dim, 0
    while i < len(s):
        pt = []
        for k in range(dim):
            r = sh = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                r |= (b & 0x1F) << sh
                sh += 5
                if b < 0x20:
                    break
            prev[k] += ~(r >> 1) if r & 1 else r >> 1
            pt.append(prev[k] / factor)
        out.append(pt if dim > 1 else pt[0])
    return out


def encode(points):
    out, plat, plng = [], 0, 0
    for lat, lng in points:
        ilat, ilng = round(lat * 1e5), round(lng * 1e5)
        for v in (ilat - plat, ilng - plng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plng = ilat, ilng
    return "".join(out)


def haversine(a, b):
    rad = math.pi / 180
    dlat, dlng = (b[0] - a[0]) * rad, (b[1] - a[1]) * rad
    h = math.sin(dlat / 2) ** 2 + math.cos(a[0] * rad) * math.cos(b[0] * rad) * math.sin(dlng / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def bearing(a, b):
    rad = math.pi / 180
    y = math.sin((b[1] - a[1]) * rad) * math.cos(b[0] * rad)
    x = math.cos(a[0] * rad) * math.sin(b[0] * rad) - math.sin(a[0] * rad) * math.cos(b[0] * rad) * math.cos((b[1] - a[1]) * rad)
    return (math.atan2(y, x) / rad + 360) % 360


def encode_values(values, factor=1e5):
    """1-D variant of encode(), as used by route.elevation.polyline."""
    out, prev = [], 0
    for x in values:
        iv = round(x * factor)
        v = iv - prev
        v = ~(v << 1) if v < 0 else v << 1
        while v >= 0x20:
            out.append(chr((0x20 | (v & 0x1F)) + 63))
            v >>= 5
        out.append(chr(v + 63))
        prev = iv
    return "".join(out)


def cumulative(points):
    cum = [0.0]
    for a, b in zip(points, points[1:]):
        cum.append(cum[-1] + haversine(a, b))
    return cum


def load_gpx(path):
    """Returns (name, points, elevations, seconds-from-start or None) from a GPX track or route."""
    root = ET.parse(path).getroot()
    for el in root.iter():  # drop XML namespaces so tags can be matched by plain name
        el.tag = el.tag.rsplit("}", 1)[-1]
    nodes = root.findall(".//trkpt") or root.findall(".//rtept")
    name = root.findtext(".//name")
    pts = [[float(n.get("lat")), float(n.get("lon"))] for n in nodes]
    ele = [float(n.findtext("ele", "0")) for n in nodes]
    times = [n.findtext("time") for n in nodes]
    if all(times):
        ts = [datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() for t in times]
        secs = [t - ts[0] for t in ts]
        if all(b >= a for a, b in zip(secs, secs[1:])) and secs[-1] > 0:
            return name, pts, ele, secs
    return name, pts, ele, None


# ---------- build the demo ride: points, ride time per point, stops, elevation profile ----------
template = json.load(open(TEMPLATE))
source = "template"
if os.path.exists(GPX):
    name, raw_pts, raw_ele, raw_t = load_gpx(GPX)
    # Thin to ~10 m spacing (keeps the trace light) but keep both ends of every stop.
    keep = [0]
    for i in range(1, len(raw_pts)):
        big_gap_next = raw_t and i + 1 < len(raw_t) and raw_t[i + 1] - raw_t[i] > STOP_GAP
        big_gap_prev = raw_t and raw_t[i] - raw_t[i - 1] > STOP_GAP
        if big_gap_next or big_gap_prev or i == len(raw_pts) - 1 or haversine(raw_pts[keep[-1]], raw_pts[i]) >= 10:
            keep.append(i)
    pts = [raw_pts[i] for i in keep]
    pt_ele = [raw_ele[i] for i in keep]
    t_at = [raw_t[i] for i in keep] if raw_t else None
    route_name = name or os.path.splitext(os.path.basename(GPX))[0]
    source = "gpx"
else:
    route_name = template["route"]["name"]
    pts = decode(template["route"]["routePolyline"])
    pt_ele = None
    t_at = None

cum = cumulative(pts)
total = cum[-1]

# Elevation profile sampled every ~25 m along the route (what the page's chart expects).
if pt_ele:
    n = max(2, int(total / 25))
    elev, j = [], 0
    for k in range(n):
        d = k / (n - 1) * total
        while j < len(cum) - 2 and cum[j + 1] < d:
            j += 1
        span = cum[j + 1] - cum[j]
        f = (d - cum[j]) / span if span else 0
        elev.append(pt_ele[j] + (pt_ele[j + 1] - pt_ele[j]) * f)
else:
    elev = decode(template["route"]["elevation"]["polyline"], dim=1)
gain_cum = [0.0]
for a, b in zip(elev, elev[1:]):
    gain_cum.append(gain_cum[-1] + max(0, b - a))


def elev_at(d):
    return elev[round(min(d, total) / total * (len(elev) - 1))]


def gain_at(d):
    return gain_cum[round(min(d, total) / total * (len(elev) - 1))]


# stops: {point index: seconds stood still at that point before moving on}
if t_at:
    stops = {i: t_at[i + 1] - t_at[i] for i in range(len(t_at) - 1) if t_at[i + 1] - t_at[i] > STOP_GAP}
else:
    # No timestamps: slower uphill, faster downhill, plus one coffee stop.
    t_at, stops = [0.0], {}
    for i in range(1, len(pts)):
        seg = cum[i] - cum[i - 1]
        grade = (elev_at(cum[i]) - elev_at(cum[i - 1])) / seg if seg else 0
        kmh = min(50, max(8, BASE_KMH * (1 - grade * 8)))
        if not stops and cum[i] >= STOP_AT * total:
            stops[i - 1] = STOP_SECONDS
        t_at.append(t_at[-1] + stops.get(i - 1, 0) + seg / (kmh / 3.6))
ride_total = t_at[-1]
stationary_before = [0.0]  # stationary seconds accumulated before each point
for i in range(1, len(pts)):
    stationary_before.append(stationary_before[-1] + stops.get(i - 1, 0))

e = template["route"]["elevation"]
route = {
    **template["route"],
    "name": route_name,
    "distance": total,
    "routePolyline": encode(pts),
    "elevation": {**e, "gain": gain_cum[-1], "loss": sum(max(0, a - b) for a, b in zip(elev, elev[1:])),
                  "min": min(elev), "max": max(elev), "source": source, "polyline": encode_values(elev)},
    "waypoints": [{"lat": pts[0][0], "lng": pts[0][1], "waypointType": "BREAK", "polylineIndex": 0},
                  {"lat": pts[-1][0], "lng": pts[-1][1], "waypointType": "BREAK", "polylineIndex": len(pts) - 1}],
}

CYCLE = ride_total + FINISHED_SECONDS

# Start mid-ride so the demo opens with a trace behind the rider; skip ahead if that's mid-stop.
start_t = ride_total * START_AT
for i, s in stops.items():
    if t_at[i] <= start_t < t_at[i] + s:
        start_t = t_at[i] + s
started = time.time()


def ride_time(now):
    return (start_t + (now - started) * SPEED_FACTOR) % CYCLE


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def response():
    now = time.time()
    speed = SPEED_FACTOR or 1  # frozen: timestamps and ETA as if riding in real time
    t = ride_time(now)  # ride seconds since this loop's start
    loop_start = now - t / speed

    if t >= ride_total:
        state, idx, frac = "finished", len(pts) - 1, 0.0
        t = ride_total
    else:
        idx = min(bisect.bisect_right(t_at, t) - 1, len(pts) - 2)
        stop = stops.get(idx, 0)  # standing at pts[idx] for the first `stop` seconds of this segment
        into = t - t_at[idx]
        moving_span = t_at[idx + 1] - t_at[idx] - stop
        state = "paused" if into < stop else "riding"
        frac = 0.0 if into < stop or moving_span <= 0 else min(1.0, (into - stop) / moving_span)

    nxt = min(idx + 1, len(pts) - 1)
    a, b = pts[idx], pts[nxt]
    loc = [a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac]
    dist = cum[idx] + (cum[nxt] - cum[idx]) * frac
    stationary = stationary_before[idx] + min(stops.get(idx, 0), t - t_at[idx])
    moving = max(t - stationary, 1)
    avg = dist / moving
    # heading from the first point ahead that isn't on top of this one
    ahead = next((p for p in pts[idx + 1: idx + 20] if haversine(a, p) > 3), b)
    remaining = total - dist
    eta = now + (remaining / avg if avg else 0) / speed  # in demo time, so it matches what you'll see

    info = {
        "TYPE_ELEVATION_GAIN_ID": gain_at(dist),
        "TYPE_BATTERY_PERCENT_ID": max(5, 100 - round(t / 600)),
        "TYPE_ELAPSED_TIME_ID": t * 1000,
        "TYPE_AVERAGE_SPEED_ID": avg,
        "TYPE_DISTANCE_ID": dist,
        "TYPE_STATIONARY_TIME_ID": stationary * 1000,
        "TYPE_TIME_OF_ARRIVAL_ID": eta * 1000,
    }
    return {
        **template,
        "activityId": "demo-activity",
        "routeId": "demo.route",
        "id": "demo.tracking",
        "route": route,
        "state": state,
        "location": {"lat": round(loc[0], 5), "lng": round(loc[1], 5)},
        "bearing": round(bearing(a, ahead), 1) if a != ahead else template["bearing"],
        "activityInfo": [{"key": k, "value": {"format": "double", "value": v}} for k, v in info.items()],
        "userTrace": encode(pts[: idx + 1] + [loc]),
        "riderName": "Demo Rider",
        "createdAt": iso(loop_start),
        "updatedAt": iso(now),
    }

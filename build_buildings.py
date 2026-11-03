#!/usr/bin/env python3
"""Precompute building outlines for every Svitlobot point and city-district labels.

Writes buildings.js next to svitlo-map.html, so the map can colour houses at any zoom
without loading vector tiles of the whole country. Re-run occasionally to pick up new bots.
Uses only the free Svitlobot API and the public Overpass API (OpenStreetMap).
"""
import json, math, sys, time, urllib.parse, urllib.request
from pathlib import Path

API = "https://api.svitlobot.in.ua/website/getChannelsForMap"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "svitlo-map/1.0 (personal, non-commercial)"}
OUT = Path(__file__).with_name("buildings.js")
BATCH = 300          # points per Overpass query
SEARCH_M = 12        # look for buildings within this radius of the bot
SNAP_M = 25          # accept nearest building up to this distance if the point is outside all


def http(url, data=None, timeout=200):
    body = urllib.parse.urlencode({"data": data}).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def overpass(query):
    wait = 10
    for _ in range(6):
        try:
            return json.loads(http(OVERPASS, query))
        except Exception as e:
            print(f"  Overpass busy ({e}); retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
            wait *= 2
    raise SystemExit("Overpass did not answer; try again later")


def load_points():
    pts = set()
    for line in http(API).split("\n"):
        p = line.split(";&&&;")
        if len(p) != 12:
            continue
        cand = [(p[6], p[7])]
        for raw in p[11].strip().split(";&&;"):
            q = raw.split(";&;")
            if len(q) == 3:
                cand.append((q[1], q[2]))
        for la, lo in cand:
            try:
                pts.add((round(float(la), 6), round(float(lo), 6)))
            except ValueError:
                pass
    return sorted(pts)


def in_ring(x, y, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]; xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def dist_m(lng, lat, ring):
    kx, ky = 111320 * math.cos(math.radians(lat)), 110540
    best = math.inf
    for (ax, ay), (bx, by) in zip(ring, ring[1:]):
        ax, ay, bx, by = (ax - lng) * kx, (ay - lat) * ky, (bx - lng) * kx, (by - lat) * ky
        dx, dy = bx - ax, by - ay
        l = dx * dx + dy * dy
        t = max(0, min(1, -(ax * dx + ay * dy) / l)) if l else 0
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def simplify(line, tol):
    """Douglas-Peucker in degrees; keeps outlines light without visible loss."""
    if len(line) < 3:
        return line
    (ax, ay), (bx, by) = line[0], line[-1]
    dx, dy = bx - ax, by - ay
    l = math.hypot(dx, dy) or 1e-12
    idx, dmax = 0, 0
    for i in range(1, len(line) - 1):
        d = abs(dy * (line[i][0] - ax) - dx * (line[i][1] - ay)) / l
        if d > dmax:
            idx, dmax = i, d
    if dmax <= tol:
        return [line[0], line[-1]]
    return simplify(line[:idx + 1], tol)[:-1] + simplify(line[idx:], tol)


def stitch(ways):
    """Join a relation's outer ways end to end into closed rings."""
    ways = [w[:] for w in ways if len(w) >= 2]
    rings = []
    while ways:
        ring = ways.pop()
        while ring[0] != ring[-1]:
            for i, w in enumerate(ways):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[-2::-1]
                else:
                    continue
                ways.pop(i)
                break
            else:
                break  # broken relation in OSM: drop the open piece
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def simplify_ring(ring, tol):
    # a closed ring has equal ends, so simplify its two halves separately
    k = len(ring) // 2
    return simplify(ring[:k + 1], tol)[:-1] + simplify(ring[k:], tol)


def load_districts():
    print("districts…", file=sys.stderr)
    res = overpass('[out:json][timeout:180];area["ISO3166-1"="UA"]->.u;'
                   'rel[boundary=administrative][admin_level=10][name~" район$"](area.u);out geom;')
    labels, lines, polys, seen = [], [], [], set()
    for e in res.get("elements", []):
        outer = [[(g["lon"], g["lat"]) for g in m["geometry"]] for m in e.get("members", [])
                 if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry")]
        rings = [[[round(x, 5), round(y, 5)] for x, y in simplify_ring(r, 0.00003)] for r in stitch(outer)]
        if rings:
            polys.append({"name": e["tags"]["name"], "rings": rings})
        b = e.get("bounds")
        if b:
            labels.append({"name": e["tags"]["name"],
                           "lng": round((b["minlon"] + b["maxlon"]) / 2, 5),
                           "lat": round((b["minlat"] + b["maxlat"]) / 2, 5)})
        for m in e.get("members", []):
            if m.get("type") != "way" or m["ref"] in seen or not m.get("geometry"):
                continue
            seen.add(m["ref"])  # neighbouring districts share border ways
            line = simplify([(g["lon"], g["lat"]) for g in m["geometry"]], 0.00003)
            lines.append([[round(x, 5), round(y, 5)] for x, y in line])
    return labels, lines, polys


def main():
    if "--districts-only" in sys.argv and OUT.exists():
        data = json.loads(OUT.read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";"))
        data["districts"], data["districtLines"], data["districtPolys"] = load_districts()
        write(data)
        return
    pts = load_points()
    print(f"{len(pts)} bot points", file=sys.stderr)
    houses = {}
    for start in range(0, len(pts), BATCH):
        chunk = pts[start:start + BATCH]
        print(f"buildings {start + 1}-{start + len(chunk)}…", file=sys.stderr)
        body = "".join(f"way[building](around:{SEARCH_M},{la},{lo});" for la, lo in chunk)
        res = overpass(f"[out:json][timeout:180];({body});out geom qt;")
        ways = []
        for w in res.get("elements", []):
            ring = [(g["lon"], g["lat"]) for g in w.get("geometry", [])]
            if len(ring) >= 4:
                xs, ys = [c[0] for c in ring], [c[1] for c in ring]
                ways.append((ring, min(xs), min(ys), max(xs), max(ys)))
        pad = SNAP_M / 70000
        for la, lo in chunk:
            best, best_d = None, SNAP_M
            for ring, x0, y0, x1, y1 in ways:
                if not (x0 - pad <= lo <= x1 + pad and y0 - pad <= la <= y1 + pad):
                    continue
                d = 0 if in_ring(lo, la, ring) else dist_m(lo, la, ring)
                if d <= best_d:
                    best, best_d = ring, d
                    if d == 0:
                        break
            if best:
                houses[f"{la},{lo}"] = [[round(x, 6), round(y, 6)] for x, y in best]
        time.sleep(3)

    districts, district_lines, district_polys = load_districts()

    data = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "houses": houses,
            "districts": districts, "districtLines": district_lines, "districtPolys": district_polys}
    write(data)


def write(data):
    OUT.write_text("window.SVITLO_PRECOMPUTED = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    print(f"wrote {OUT.name}: {len(data['houses'])} houses, {len(data['districts'])} districts", file=sys.stderr)


if __name__ == "__main__":
    main()

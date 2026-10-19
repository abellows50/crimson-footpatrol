"""
Build-time geometry for "is this Cambridge address a Harvard address?" (parcels.py --update runs it; the dispatch
monitor itself only does dictionary lookups in the tables this writes).

A Harvard address is anywhere HUPD would normally respond:
  * a Harvard building or Harvard land: any parcel Harvard owns (City of Cambridge property records, incl. rentals),
    plus Harvard student-organisation buildings (data/harvard-curated.json) and Harvard's own campus map;
  * the public way DIRECTLY IN FRONT of one, on the SAME SIDE of the street ("15 Mill St" is not an official address,
    but it is the Mill St sidewalk along Lowell House);
  * an intersection with a Harvard corner (Mass Ave & Holyoke St).

How an address is decided:
  1. An official address (Master Address List) is decided by its own parcel ("1401 Mass Ave", the MBTA headhouse,
     is a road parcel: not Harvard, although the Yard is across the street).
  2. Any other number is placed along the street by interpolating between the street's real address points (through
     the street's own node graph), on the side that carries its odd/even numbers, and short rays are cast from the
     centerline toward that side every 2.5 m over +-10 m: if every ray first hits Harvard property -> Harvard; if
     only some do (a lot line), or the number lies well past the street's last address -> amber ("possible").
  3. Intersections: the parcel at each corner sector of the junction; a Harvard corner within 30 m -> Harvard.
Leave-one-out test on 19,140 real addresses: 97.8 % of Harvard addresses still alert; 0.06 % of other addresses
get a wrong loud alarm (this only matters for numbers that aren't official addresses).

Data (all public): City of Cambridge open data (Socrata): parcels c392-hjk3, street centerlines 3d3q-sfd6,
intersections xhx7-spt9, Master Address List vup6-kpwv, Property Database eey2-rv59; Harvard's campus map
(mapprod.cadm.harvard.edu, Facilities layer). Pure Python, standard library only.

    python3 harvard_geo.py addr "15 Mill St" "1401 Mass Ave"     explain addresses (uses the cached downloads)
    python3 harvard_geo.py int "Mass Ave" "Holyoke St"           explain an intersection
"""
import bisect
import collections
import datetime as dt
import heapq
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "cache")
ADAMS_HOUSE = (42.37182, -71.116627)      # Russell Hall, 26 Plympton St: "near Harvard College" is measured from here
# open areas that calls name but that aren't buildings on Harvard's map (centre points)
AREAS = {"harvard yard": (42.37441, -71.11686), "radcliffe quad": (42.38173, -71.12500), "the quad": (42.38173, -71.12500),
         "radcliffe yard": (42.37663, -71.12232), "tercentenary theatre": (42.37415, -71.11640),
         "annenberg": (42.37602, -71.11511)}
CURATED = os.path.join(HERE, "data", "harvard-curated.json")

# ------------------------------------------------------------------------------------------------ parameters
CELL = 40.0                # m, grid cell for the parcel spatial index
RAY_MAX = 45.0             # m, furthest a frontage parcel may be from the centerline (Mass Ave half-width ~ 20-30 m)
RAY_FAN = (-6.0, -3.0, 0.0, 3.0, 6.0)   # m along the street: several parallel rays so a 1-m gap between lots
#                            (or a driveway / private way strip) does not hide the frontage parcel
SIDEWALK_BACK = 1.0        # m, "sidewalk point" = this far in front of the frontage parcel boundary
CORNER_MAX = 30.0          # m, a Harvard corner parcel this close to the node -> high (p96 of all corners)
CORNER_FAR = 45.0          # m, 30-45 m -> medium (big squares: Mass Ave & Bow St 31 m, Mass Ave & Cambridge St 36 m)
EXTRAP_HIGH_M = 30.0       # m, an extrapolated position may still be "high" if it moved at most this far
PARTIAL_MIN = 0.25         # Harvard owns >= 25 % (but not a majority) of a parcel's units / land -> "P": amber only
FAR_SIDE_LEVEL = "medium"  # T-junction whose only Harvard frontage is across the through street (Mass Ave & Church
#                            St, Garden St & Peabody St): 25 of 173 Harvard pairs.  "high" if HUPD would go there
WINDOW = 10.0              # m, +- uncertainty of an interpolated position along the street (~p85 of the
#                            leave-one-out error): all frontage in the window Harvard -> high, some -> medium
WINDOW_STEP = 2.5          # m between rays inside the window
JUNCTION_SPREAD = 90.0     # m, nodes of one street pair closer than this are one (complex) junction
CTRL_MAX_D = 120.0         # m, MAL address points further than this from their street are not used as controls
EXTRAP_NUMBERS = 40        # house numbers we extrapolate beyond the last known address on a street side
EXTRAP_METRES = 60.0       # ... and never more than this many metres beyond it
ROW_TYPES = {"PRIV_ROW", "RAIL_ROW", "WATER"}

# ------------------------------------------------------------------------------------------------ projection
LAT0, LON0 = 42.3736, -71.1106                         # Harvard Square-ish; Cambridge spans < 7 km, error < 0.1 %
_phi = math.radians(LAT0)
KY = 111132.92 - 559.82 * math.cos(2 * _phi) + 1.175 * math.cos(4 * _phi)        # m per degree latitude
KX = 111412.84 * math.cos(_phi) - 93.5 * math.cos(3 * _phi) + 0.118 * math.cos(5 * _phi)   # m per degree longitude


def to_xy(lon, lat):
    return ((float(lon) - LON0) * KX, (float(lat) - LAT0) * KY)


def to_ll(p):
    return [round(p[1] / KY + LAT0, 7), round(p[0] / KX + LON0, 7)]       # [lat, lon]


# ------------------------------------------------------------------------------------------------ names
ABBR = {"st": "street", "ave": "avenue", "av": "avenue", "rd": "road", "dr": "drive", "pl": "place", "sq": "square",
        "mt": "mount", "ct": "court", "ter": "terrace", "pkwy": "parkway", "ln": "lane", "blvd": "boulevard",
        "hwy": "highway", "pk": "park", "tpk": "turnpike", "tpke": "turnpike", "cir": "circle", "gdns": "gardens",
        "mass": "massachusetts", "aly": "alley", "ext": "extension"}
# the "Street / Avenue / Place" part of a name (not "Mount", "Massachusetts")
STREET_WORDS = (set(ABBR.values()) - {"mount", "massachusetts"}) | {"street", "way", "row", "path", "walk", "plaza",
                                                                    "green", "landing", "bridge", "alley"}
STREET_ALIASES = {                                      # spoken / misheard name -> canonical key
    "bank street": "banks street", "john f kennedy street": "jfk street", "kennedy street": "jfk street",
    "j f k street": "jfk street", "de wolfe street": "dewolfe street", "dewolf street": "dewolfe street",
    "mount auburn": "mount auburn street", "massachusetts": "massachusetts avenue",
    "j f kennedy street": "jfk street", "jf kennedy street": "jfk street",
}


def norm_street(name):
    toks = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    s = " ".join(ABBR.get(t, t) for t in toks)
    return STREET_ALIASES.get(s, s)


def base_ml(ml):
    return "-".join((ml or "").split("-")[:2])


def lead_number(s):
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------------------------------------ geometry helpers
def seg_point_dist2(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return (p[0] - qx) ** 2 + (p[1] - qy) ** 2, t


def ray_hit(o, d, a, b):
    """Distance t >= 0 along unit ray o + t d to segment ab, or None."""
    ex, ey = b[0] - a[0], b[1] - a[1]
    den = d[0] * ey - d[1] * ex
    if abs(den) < 1e-12:
        return None
    wx, wy = a[0] - o[0], a[1] - o[1]
    t = (wx * ey - wy * ex) / den
    u = (wx * d[1] - wy * d[0]) / den
    if t >= 0 and -1e-9 <= u <= 1 + 1e-9:
        return t
    return None


def in_rings(p, rings):
    """Even-odd rule over all rings (outer rings and holes of every polygon of a multipolygon)."""
    x, y = p
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n - 1):
            x1, y1 = ring[i]
            x2, y2 = ring[i + 1]
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


# ------------------------------------------------------------------------------------------------ ownership
HARVARD_NAME = re.compile(r"FELLOWS? OF HARVARD|HARVARD COLLE[DG]+E|HARVARD UNIVERSITY|HARVARD UNIV\b|RADCLIFFE COLLEGE")
HARVARD_CO = re.compile(r"C/O (?:PRESIDENT (?:&|AND) FELLOWS OF HARVARD|HARVARD (?:REAL|RESL|PLANNING|UNIV))")
NOT_HARVARD = re.compile(r"COOPERATIVE SOCIETY|HARVARD (?:STREET|ST\b|SQUARE|SQ\b|TOWERS|PLACE|ADVISORS|COLLECTION|ONE)"
                         r"|EPWORTH|LLC|TRUST\b|HOLDING")
CONDO_MASTER = re.compile(r"^Condominium Building", re.I)
# Filled from data/harvard-curated.json by load_curated():
# (b) Harvard student organisations' buildings (owner names don't say "Harvard"), by parcel -> (name, "high"|"medium")
STUDENT_ORGS = {}
# owner names that are Harvard although no pattern above says so, by parcel -> note
HARVARD_EXTRA = {}
CAMPUS_CLASS = re.compile(r"college|university|educ|dormitor|libraries|museum|vacant \(private ed\)", re.I)


def owner_is_harvard(owner, co):
    o, c = (owner or "").upper(), (co or "").upper()
    if HARVARD_NAME.search(o):
        return True
    if re.match(r"PRESIDENTS? (?:&|AND) FELLOWS?\b", o) and "HARVARD" in c:      # name split over two fields
        return True
    if HARVARD_CO.search(c) and not NOT_HARVARD.search(o):                       # tax bill c/o Harvard Real Estate
        return True
    return False


# ------------------------------------------------------------------------------------------------ the model
class Model:
    def __init__(self, raw):
        self.raw = raw
        self._load_parcels()
        self._load_owners()
        self._load_streets()
        self._load_addresses()
        self._load_intersections()

    # ---------------------------------------------------------------- parcels + spatial grid
    def _load_parcels(self):
        with open(os.path.join(self.raw, "parcels.geojson")) as f:
            feats = json.load(f)["features"]
        self.parcels = []                      # list of dict(ml, ptype, rings, edges, bbox)
        self.grid = collections.defaultdict(list)
        for f in feats:
            p, g = f["properties"], f["geometry"]
            if not g:
                continue
            rings = []
            for poly in g["coordinates"]:
                for ring in poly:
                    rings.append([to_xy(c[0], c[1]) for c in ring])
            xs = [q[0] for r in rings for q in r]
            ys = [q[1] for r in rings for q in r]
            i = len(self.parcels)
            self.parcels.append({"ml": p.get("ml") or "", "ptype": p.get("poly_type") or "",
                                 "rings": rings, "bbox": (min(xs), min(ys), max(xs), max(ys))})
            for cx in range(int(math.floor(min(xs) / CELL)), int(math.floor(max(xs) / CELL)) + 1):
                for cy in range(int(math.floor(min(ys) / CELL)), int(math.floor(max(ys) / CELL)) + 1):
                    self.grid[(cx, cy)].append(i)
        self.by_ml = collections.defaultdict(list)
        for i, pc in enumerate(self.parcels):
            self.by_ml[pc["ml"]].append(i)

    def candidates(self, x0, y0, x1, y1):
        out = set()
        for cx in range(int(math.floor(min(x0, x1) / CELL)), int(math.floor(max(x0, x1) / CELL)) + 1):
            for cy in range(int(math.floor(min(y0, y1) / CELL)), int(math.floor(max(y0, y1) / CELL)) + 1):
                out.update(self.grid.get((cx, cy), ()))
        return out

    def is_row(self, i):
        pc = self.parcels[i]
        return pc["ptype"] in ROW_TYPES or not re.match(r"^\d", pc["ml"]) or "-PW" in pc["ml"] or "RR" in pc["ml"]

    def parcel_at(self, p):
        for i in self.candidates(p[0], p[1], p[0], p[1]):
            if in_rings(p, self.parcels[i]["rings"]):
                return i
        return None

    def parcel_dist(self, i, p):
        if in_rings(p, self.parcels[i]["rings"]):
            return 0.0
        best = 1e18
        for r in self.parcels[i]["rings"]:
            for k in range(len(r) - 1):
                d2, _ = seg_point_dist2(p, r[k], r[k + 1])
                best = min(best, d2)
        return math.sqrt(best)

    def near_parcels(self, p, radius):
        out = []
        for i in self.candidates(p[0] - radius, p[1] - radius, p[0] + radius, p[1] + radius):
            b = self.parcels[i]["bbox"]
            if p[0] < b[0] - radius or p[0] > b[2] + radius or p[1] < b[1] - radius or p[1] > b[3] + radius:
                continue
            d = self.parcel_dist(i, p)
            if d <= radius:
                out.append((d, i))
        return sorted(out)

    def first_hit(self, o, d, maxd=RAY_MAX, skip_row=True):
        """First parcel boundary crossed by the ray o + t d (t in [0, maxd]): [(t, parcel)] sorted, ROW skipped."""
        ex, ey = o[0] + d[0] * maxd, o[1] + d[1] * maxd
        hits = {}
        for i in self.candidates(o[0], o[1], ex, ey):
            if skip_row and self.is_row(i):
                continue
            b = self.parcels[i]["bbox"]
            if max(o[0], ex) < b[0] or min(o[0], ex) > b[2] or max(o[1], ey) < b[1] or min(o[1], ey) > b[3]:
                continue
            best = None
            for r in self.parcels[i]["rings"]:
                for k in range(len(r) - 1):
                    t = ray_hit(o, d, r[k], r[k + 1])
                    if t is not None and t <= maxd and (best is None or t < best):
                        best = t
            if best is not None:
                hits[i] = best
            elif in_rings(o, self.parcels[i]["rings"]):
                hits[i] = 0.0
        return sorted((t, i) for i, t in hits.items())

    # ---------------------------------------------------------------- owners -> parcel class
    def _load_owners(self):
        with open(os.path.join(self.raw, "assess2026.json")) as f:
            rows = json.load(f)
        mls = set(self.by_ml)
        groups = collections.defaultdict(list)
        self.join_stats = collections.Counter()
        for r in rows:
            g, b = r.get("gisid") or "", base_ml(r.get("map_lot"))
            if g in mls:
                key = g
                self.join_stats["gisid"] += 1
            elif b in mls:
                key = b
                self.join_stats["base(map_lot) (gisid missing or not a parcel)"] += 1
            else:
                key = g or b
                self.join_stats["no parcel polygon"] += 1
            groups[key].append(r)
        self.owner = {}
        for ml, rs in groups.items():
            units = [r for r in rs if not CONDO_MASTER.match(r.get("owner_name") or "")]
            condo = any(re.search(r"CONDO|CNDO", r.get("propertyclass") or "") for r in rs) or \
                any(len((r.get("map_lot") or "").split("-")) > 2 for r in rs)
            hv = [r for r in units if owner_is_harvard(r.get("owner_name"), r.get("owner_coownername"))
                  or ml in HARVARD_EXTRA]
            # dedupe by map_lot (a lot can have several building rows)
            lots = {r["map_lot"] for r in units}
            hlots = {r["map_lot"] for r in hv}
            frac = len(hlots) / len(lots) if lots else 0.0
            area = sum(float(r.get("landarea") or 0) for r in {r["map_lot"]: r for r in units}.values())
            harea = sum(float(r.get("landarea") or 0) for r in {r["map_lot"]: r for r in hv}.values())
            classes = sorted({r.get("propertyclass") or "" for r in units})
            names = collections.Counter((r.get("owner_name") or "").strip(" ,") for r in units)
            self.owner[ml] = {"lots": len(lots), "harvard_lots": len(hlots), "frac": round(frac, 3),
                              "area": area, "harvard_area": harea, "condo": condo, "classes": classes,
                              "owners": [n for n, _ in names.most_common(3)],
                              "address": rs[0].get("address"),
                              "addresses": sorted({r.get("address") for r in rs if r.get("address")})}

    def parcel_class(self, ml):
        """('C'|'H'|'S'|'P'|'N'|'R', label).  C campus, H other Harvard-owned, S student org, P amber only (partly
        Harvard-owned, or an org building we're not sure about), N not Harvard, R road."""
        if ml in STUDENT_ORGS:
            name, level = STUDENT_ORGS[ml]
            return ("S" if level == "high" else "P"), name
        idx = self.by_ml.get(ml)
        if idx and self.is_row(idx[0]):
            return "R", "road / right of way / rail / water"
        o = self.owner.get(ml)
        if not o:
            return "N", "no assessor row"
        # Harvard if it owns a strict majority of the lots/units, or (land rows) a majority of the land area
        land_major = o["area"] > 0 and o["harvard_area"] / o["area"] > 0.5
        if o["frac"] > 0.5 or land_major:
            kind = "C" if any(CAMPUS_CLASS.search(c) for c in o["classes"]) else "H"
            return kind, f"Harvard-owned ({', '.join(o['classes'][:2])}) {o['address']}"
        part = max(o["frac"], o["harvard_area"] / o["area"] if o["area"] else 0.0)
        if part >= PARTIAL_MIN:          # 12-30 Palmer St (Coop annex, 1 of 2 units), 3 University Rd (35 % of land)
            return "P", (f"partly Harvard-owned ({o['harvard_lots']}/{o['lots']} lots, "
                         f"{round(100 * part)} %) {o['address']}")
        return "N", f"{o['owners'][0] if o['owners'] else '?'} ({', '.join(o['classes'][:2])}) {o['address']}"

    def describe(self, i, dist=None):
        ml = self.parcels[i]["ml"]
        cls, label = self.parcel_class(ml)
        o = self.owner.get(ml, {})
        d = {"ml": ml, "class": cls, "harvard": cls in "CHS", "partial": cls == "P", "label": label,
             "owners": o.get("owners", []), "harvard_units": f"{o.get('harvard_lots', 0)}/{o.get('lots', 0)}",
             "addresses": self.addr_by_ml.get(ml, [])[:4] if hasattr(self, "addr_by_ml") else []}
        if dist is not None:
            d["dist_m"] = round(dist, 1)
        return d

    # ---------------------------------------------------------------- streets
    def _load_streets(self):
        with open(os.path.join(self.raw, "centerlines.geojson")) as f:
            feats = json.load(f)["features"]
        self.streets = collections.defaultdict(list)      # key -> [seg]
        aliases = []
        self.node_xy = {}
        self.node_segs = collections.defaultdict(list)    # node -> [(street key, seg)]

        def rng(a, b):
            a, b = lead_int(a), lead_int(b)
            xs = [v for v in (a, b) if v and v > 0]
            if not xs:
                return None
            return (min(xs), max(xs))

        def lead_int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        for f in feats:
            p, g = f["properties"], f["geometry"]
            if not g or not p.get("street"):
                continue
            pts = [to_xy(c[0], c[1]) for line in g["coordinates"] for c in line]
            dedup = [pts[0]]
            for q in pts[1:]:
                if q != dedup[-1]:
                    dedup.append(q)
            pts = dedup
            if len(pts) < 2:
                continue
            cum = [0.0]
            for k in range(1, len(pts)):
                cum.append(cum[-1] + math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]))
            key = norm_street(p["street"])
            seg = {"id": p.get("id"), "street": p["street"], "key": key, "pts": pts, "cum": cum, "len": cum[-1],
                   "L": rng(p.get("l_from"), p.get("l_to")), "R": rng(p.get("r_from"), p.get("r_to")),
                   "a": lead_int(p.get("fromnode")), "b": lead_int(p.get("tonode")), "alias": p.get("alias")}
            for side in ("L", "R"):
                r = seg[side]
                seg[side + "par"] = (r[0] % 2 if r and r[0] % 2 == r[1] % 2 else None)
            seg["n"] = len(self.streets[key])
            self.streets[key].append(seg)
            self.node_xy.setdefault(seg["a"], pts[0])
            self.node_xy.setdefault(seg["b"], pts[-1])
            self.node_segs[seg["a"]].append((key, seg["n"]))
            self.node_segs[seg["b"]].append((key, seg["n"]))
            if p.get("alias"):
                for al in re.split(r",\s*", p["alias"]):
                    ak = norm_street(al)
                    if ak and ak != key:
                        aliases.append((ak, key))
        # an old name only when it shares a word with today's ("medeiros avenue" -> "cardinal medeiros avenue"):
        # "boylston street" -> "jfk street" would turn every Boston / Watertown Boylston St into JFK St, and
        # "portland street" is a street of its own
        for ak, key in aliases:
            words = lambda k: {w for w in k.split() if w not in STREET_WORDS}
            if ak in self.streets or not (words(ak) & words(key) or re.match(r"\d+(?:st|nd|rd|th) ", ak)):
                continue
            STREET_ALIASES.setdefault(ak, key)

    def street_key(self, name):
        k = norm_street(name)
        k = STREET_ALIASES.get(k, k)
        if k in self.streets:
            return k
        # "Mass Ave" spoken without suffix, "Mount Auburn" etc.
        cands = [s for s in self.streets if s.startswith(k + " ")]
        return cands[0] if len(cands) == 1 else k

    # locate a point on a street: (seg n, s along seg, side +1 left / -1 right, perpendicular distance, foot)
    def locate(self, key, p, segs=None):
        best = None
        for seg in (segs or self.streets.get(key, [])):
            pts = seg["pts"]
            for k in range(len(pts) - 1):
                d2, t = seg_point_dist2(p, pts[k], pts[k + 1])
                if best is None or d2 < best[0]:
                    best = (d2, seg["n"], k, t)
        if best is None:
            return None
        d2, n, k, t = best
        seg = self.streets[key][n]
        a, b = seg["pts"][k], seg["pts"][k + 1]
        s = seg["cum"][k] + t * (seg["cum"][k + 1] - seg["cum"][k])
        foot = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        return {"seg": n, "s": s, "side": 1 if cross > 0 else -1, "d": math.sqrt(d2), "foot": foot}

    def point_at(self, key, n, s):
        seg = self.streets[key][n]
        s = max(0.0, min(seg["len"], s))
        k = max(0, min(len(seg["pts"]) - 2, bisect.bisect_right(seg["cum"], s) - 1))
        a, b = seg["pts"][k], seg["pts"][k + 1]
        L = seg["cum"][k + 1] - seg["cum"][k]
        t = 0.0 if L == 0 else (s - seg["cum"][k]) / L
        tx, ty = (b[0] - a[0]) / (L or 1), (b[1] - a[1]) / (L or 1)
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])), (tx, ty)

    # path along a street between two located positions -> list of (seg, s0, s1)
    def path(self, key, A, B):
        segs = self.streets[key]
        if A["seg"] == B["seg"]:
            return [(A["seg"], A["s"], B["s"])]
        adj = collections.defaultdict(list)
        for sg in segs:
            adj[sg["a"]].append((sg["b"], sg["n"]))
            adj[sg["b"]].append((sg["a"], sg["n"]))
        sa, sb = segs[A["seg"]], segs[B["seg"]]
        dist, prev = {}, {}
        pq = [(A["s"], sa["a"]), (sa["len"] - A["s"], sa["b"])]
        for c, nd in pq:
            if c < dist.get(nd, 1e18):
                dist[nd] = c
                prev[nd] = None
        heapq.heapify(pq)
        while pq:
            c, nd = heapq.heappop(pq)
            if c > dist.get(nd, 1e18):
                continue
            for nb, sn in adj[nd]:
                if sn in (A["seg"], B["seg"]):
                    continue
                c2 = c + segs[sn]["len"]
                if c2 < dist.get(nb, 1e18):
                    dist[nb] = c2
                    prev[nb] = (nd, sn)
                    heapq.heappush(pq, (c2, nb))
        ends = []
        if sb["a"] in dist:
            ends.append((dist[sb["a"]] + B["s"], sb["a"], 0.0))
        if sb["b"] in dist:
            ends.append((dist[sb["b"]] + sb["len"] - B["s"], sb["b"], sb["len"]))
        if not ends:
            return None
        _, nd, s_enter = min(ends)
        pieces = [(B["seg"], s_enter, B["s"])]
        while prev.get(nd) is not None:
            pn, sn = prev[nd]
            sg = segs[sn]
            pieces.append((sn, 0.0, sg["len"]) if sg["b"] == nd else (sn, sg["len"], 0.0))
            nd = pn
        pieces.append((A["seg"], A["s"], 0.0 if nd == sa["a"] else sa["len"]))
        return pieces[::-1]

    def walk(self, key, pieces, frac):
        total = sum(abs(s1 - s0) for _, s0, s1 in pieces)
        target = frac * total
        for n, s0, s1 in pieces:
            L = abs(s1 - s0)
            if target <= L or (n, s0, s1) == pieces[-1]:
                s = s0 + (target if s1 >= s0 else -target)
                return n, max(0.0, min(self.streets[key][n]["len"], s)), total
            target -= L
        return pieces[-1][0], pieces[-1][2], total

    # ---------------------------------------------------------------- addresses (Master Address List)
    def _load_addresses(self):
        with open(os.path.join(self.raw, "addresses.json")) as f:
            rows = json.load(f)
        self.mal = collections.defaultdict(list)            # (number, key) -> [row]
        self.addr_by_ml = collections.defaultdict(list)
        self.controls = collections.defaultdict(list)       # key -> [(num, seg, s, side, d, ml, full_addr)]
        self.mal_streets = set()
        for a in rows:
            num = lead_number(a.get("street_number"))
            key = norm_street(a.get("stname"))
            self.mal_streets.add(key)
            self.addr_by_ml[a.get("maplot") or ""].append(a.get("full_addr"))
            if num is None:
                continue
            p = to_xy(a["longitude"], a["latitude"])
            sfx = (a.get("street_number") or "").strip()[len(str(num)):].strip(" -").upper()
            a = dict(a, xy=p, num=num, suffix=sfx)          # "", "A", "R" (rear), "1/2", "10" (sub-address)
            self.mal[(num, key)].append(a)
            if key in self.streets:
                loc = self.locate(key, p)
                if loc and loc["d"] <= CTRL_MAX_D:
                    self.controls[key].append((num, loc["seg"], loc["s"], loc["side"], loc["d"],
                                               a.get("maplot"), a.get("full_addr")))
        for key in self.controls:
            self.controls[key].sort()

    # ---------------------------------------------------------------- intersections
    def _load_intersections(self):
        with open(os.path.join(self.raw, "intersections.geojson")) as f:
            feats = json.load(f)["features"]
        self.int_names = {}
        for f in feats:
            p = f["properties"]
            try:
                node = int(float(p.get("nodenumber")))
            except (TypeError, ValueError):
                continue
            self.int_names[node] = p.get("intersecti")
            if f["geometry"]:
                # the layer's own point wins: a few segments (Beacon St) have their from / to node labels swapped,
                # which would put the node at the wrong end of the segment
                self.node_xy[node] = to_xy(*f["geometry"]["coordinates"][:2])

    # ================================================================ resolution
    def _side_index(self):
        """Per segment: [(parity, side)] of nearby building address points; streets numbered on one side only."""
        self.seg_pts = collections.defaultdict(list)
        for key, ctr in self.controls.items():
            for num, sn, s, side, d, ml, fa in ctr:
                if d <= 60 and ml in self.by_ml:            # not ROAD / kiosks in the street
                    self.seg_pts[(key, sn)].append((num % 2, side))
        self.one_sided = set()
        for key, segs in self.streets.items():
            same = diff = 0
            for seg in segs:
                pts = self.seg_pts.get((key, seg["n"]), [])
                c = {p: collections.Counter(sd for pp, sd in pts if pp == p) for p in (0, 1)}
                if c[0] and c[1]:
                    if c[0].most_common(1)[0][0] == c[1].most_common(1)[0][0]:
                        same += 1
                    else:
                        diff += 1
            if same >= 2 and same > diff:                  # Memorial Drive: odd and even both on the north side
                self.one_sided.add(key)

    def parity_side(self, key, n_seg, par, depth=0, seen=None):
        """Which side (+1 left / -1 right of digitising direction) of a segment carries house numbers of parity par."""
        if not hasattr(self, "seg_pts"):
            self._side_index()
        seg = self.streets[key][n_seg]
        one = key in self.one_sided
        pts = self.seg_pts.get((key, n_seg), [])
        same = collections.Counter(sd for p, sd in pts if p == par)
        oth = collections.Counter(sd for p, sd in pts if p != par)
        # 1. address points of this parity on this segment (direct evidence; beats the range labels, which are
        #    swapped on a few segments such as Massach_Ave_43 in Harvard Square)
        if same:
            s, c = same.most_common(1)[0]
            if c / sum(same.values()) >= 0.75:
                return s, "address points of this parity on segment"
        # 2. the segment's own side ranges
        if seg["Lpar"] == par and seg["Rpar"] != par:
            return 1, "segment range"
        if seg["Rpar"] == par and seg["Lpar"] != par:
            return -1, "segment range"
        if one:
            # 3a. one-sided street (buildings on one side only): every number is on the side that has addresses
            allc = same + oth
            if allc:
                return allc.most_common(1)[0][0], "one-sided street: side with addresses"
            if seg["L"] and not seg["R"]:
                return 1, "one-sided street: range side"
            if seg["R"] and not seg["L"]:
                return -1, "one-sided street: range side"
        else:
            # 3b. normal street: the other parity's side, flipped
            if oth:
                return -oth.most_common(1)[0][0], "other parity on segment (flipped)"
            if seg["Lpar"] == 1 - par and seg["Rpar"] is None:
                return -1, "segment range (other side)"
            if seg["Rpar"] == 1 - par and seg["Lpar"] is None:
                return 1, "segment range (other side)"
        # walk to neighbours along the street, keeping track of digitising direction
        seen = seen or {n_seg}
        if depth < 6:
            for node in (seg["a"], seg["b"]):
                for k2, sn in self.node_segs[node]:
                    if k2 != key or sn in seen:
                        continue
                    seen.add(sn)
                    other = self.streets[key][sn]
                    same_dir = (other["a"] == seg["b"]) or (other["b"] == seg["a"])
                    r = self.parity_side(key, sn, par, depth + 1, seen)
                    if r:
                        return (r[0] if same_dir else -r[0]), "neighbouring segment"
        return None

    def advance(self, key, n_seg, s, direction, dist, report=False):
        """Move dist metres along the street from (seg, s) in direction +1/-1 of digitising, crossing nodes.
        report=True also returns the distance that could not be travelled (street ended)."""
        seen = set()
        while True:
            seg = self.streets[key][n_seg]
            target = s + direction * dist
            if 0 <= target <= seg["len"]:
                return (n_seg, target, 0.0) if report else (n_seg, target)
            used = (seg["len"] - s) if direction > 0 else s
            dist -= used
            node = seg["b"] if direction > 0 else seg["a"]
            seen.add(n_seg)
            # continue on the most collinear segment of the same street at that node
            _, t_end = self.point_at(key, n_seg, seg["len"] if direction > 0 else 0.0)
            t_end = (t_end[0] * direction, t_end[1] * direction)
            best = None
            for k2, sn in self.node_segs[node]:
                if k2 != key or sn in seen:
                    continue
                o = self.streets[key][sn]
                d2 = 1 if o["a"] == node else -1
                _, t2 = self.point_at(key, sn, 0.0 if d2 > 0 else o["len"])
                dot = t_end[0] * t2[0] * d2 + t_end[1] * t2[1] * d2
                if best is None or dot > best[0]:
                    best = (dot, sn, d2)
            if best is None or best[0] < 0.5:
                end = seg["len"] if direction > 0 else 0.0
                return (n_seg, end, max(0.0, dist)) if report else (n_seg, end)
            n_seg, direction = best[1], best[2]
            s = 0.0 if direction > 0 else self.streets[key][n_seg]["len"]

    def interpolate(self, key, num, exclude=False):
        """Estimate where house number num sits along street key: dict(seg, s, side, method, ...) or None.
        exclude=True ignores the address points of num itself (leave-one-out validation)."""
        ctr = self.controls.get(key, [])
        if exclude:
            ctr = [c for c in ctr if c[0] != num]
        par = num % 2
        same = [c for c in ctr if c[0] % 2 == par]
        other = [c for c in ctr if c[0] % 2 != par]

        def bracket(cs):
            nums = [c[0] for c in cs]
            i = bisect.bisect_left(nums, num)
            j = bisect.bisect_right(nums, num)
            if i != j:                                    # the number itself is a control (exact address)
                return cs[i], cs[i]
            return (cs[i - 1] if i > 0 else None), (cs[i] if i < len(cs) else None)

        for label, cs in (("same-parity address points", same), ("opposite-side address points", other)):
            lo, hi = bracket(cs)
            if lo and hi:
                if lo is hi:
                    return self._finish(key, num, lo[1], lo[2], par, "address point of this number", lo, hi, 0.0, 0.0)
                pieces = self.path(key, {"seg": lo[1], "s": lo[2]}, {"seg": hi[1], "s": hi[2]})
                frac = (num - lo[0]) / (hi[0] - lo[0])
                if not pieces:
                    # disconnected street pieces (Quincy St is cut by the Cambridge St underpass): a straight line
                    # between the two address points, back onto the street
                    pa, _ = self.point_at(key, lo[1], lo[2])
                    pb, _ = self.point_at(key, hi[1], hi[2])
                    loc = self.locate(key, (pa[0] + frac * (pb[0] - pa[0]), pa[1] + frac * (pb[1] - pa[1])))
                    if loc and loc["d"] <= 25.0:
                        return self._finish(key, num, loc["seg"], loc["s"], par, label + " (disconnected: straight line)",
                                            lo, hi, frac, 0.0)
                    c = lo if frac <= 0.5 else hi         # the street bends away: nearer control (a guess: amber)
                    return self._finish(key, num, c[1], c[2], par, label + " (disconnected: nearer one)",
                                        lo, hi, frac, 0.0)
                n_seg, s, total = self.walk(key, pieces, frac)
                return self._finish(key, num, n_seg, s, par, label, lo, hi, frac, total)
        # beyond the known range: extrapolate from the last two distinct numbers (same parity if possible)
        cs = same if len({c[0] for c in same}) >= 2 else ctr
        if len({c[0] for c in cs}) < 2:
            return self._interp_ranges(key, num)
        if num > cs[-1][0]:
            edge = cs[-1]
            prev = next(c for c in reversed(cs) if c[0] < edge[0])
        else:
            edge = cs[0]
            prev = next(c for c in cs if c[0] > edge[0])
        gap_n = abs(num - edge[0])
        if gap_n > EXTRAP_NUMBERS:
            return None
        pieces = self.path(key, {"seg": prev[1], "s": prev[2]}, {"seg": edge[1], "s": edge[2]})
        if not pieces:
            return None
        total = sum(abs(b - a) for _, a, b in pieces)
        metres = min(EXTRAP_METRES, gap_n * total / abs(edge[0] - prev[0]))
        last = pieces[-1]
        direction = 1 if last[2] >= last[1] else -1
        n_seg, s, short = self.advance(key, edge[1], edge[2], direction, metres, report=True)
        r = self._finish(key, num, n_seg, s, par, f"extrapolated {metres - short:.0f} m beyond {edge[6]}",
                         prev, edge, None, total)
        r["extrap_m"] = round(metres - short, 1)
        r["clamped"] = short > 0.5 or metres >= EXTRAP_METRES
        return r

    def _finish(self, key, num, n_seg, s, par, method, lo, hi, frac, total):
        ps = self.parity_side(key, n_seg, par)
        side, side_how = ps if ps else (None, "unknown")
        return {"seg": n_seg, "s": s, "side": side, "side_how": side_how, "method": method, "extrap_m": 0.0,
                "clamped": False, "between": [lo[6], hi[6]] if lo and hi else None,
                "frac": None if frac is None else round(frac, 3), "span_m": round(total, 1)}

    def _interp_ranges(self, key, num):
        """Fallback when a street has < 2 address points: the centerline side ranges."""
        par = num % 2
        for seg in self.streets.get(key, []):
            for side, sign in (("L", 1), ("R", -1)):
                r = seg[side]
                if r and r[0] % 2 == par and r[0] <= num <= r[1]:
                    f = 0.5 if r[1] == r[0] else (num - r[0]) / (r[1] - r[0])
                    return {"seg": seg["n"], "s": f * seg["len"], "side": sign, "side_how": "segment range",
                            "method": "centerline side range", "between": None, "frac": round(f, 3),
                            "span_m": round(seg["len"], 1)}
        return None

    def range_segments(self, key, num):
        """Centerline segments whose same-parity side range contains num (TIGER-style check)."""
        out = []
        for seg in self.streets.get(key, []):
            for side, sign in (("L", 1), ("R", -1)):
                r = seg[side]
                if r and r[0] % 2 == num % 2 and r[0] <= num <= r[1]:
                    out.append({"segment": seg["id"], "side": "left" if sign > 0 else "right", "range": list(r)})
        return out

    def frontage(self, key, n_seg, s, side):
        """Cast rays from the centerline toward `side`: the first parcel crossed is the frontage parcel."""
        seg = self.streets[key][n_seg]
        results = []
        for ds in RAY_FAN:
            s2 = max(0.0, min(seg["len"], s + ds))
            p, (tx, ty) = self.point_at(key, n_seg, s2)
            nrm = (-ty * side, tx * side)            # left normal = (-ty, tx)
            hits = self.first_hit(p, nrm)
            results.append((ds, p, nrm, hits[:3]))
        center = next(r for r in results if r[0] == 0.0)
        p, nrm = center[1], center[2]
        hits = center[3]
        if not hits:                                  # center ray hit nothing: take the nearest fan ray hit
            alt = sorted((h[0][0], h) for _, _, _, h in results if h)
            hits = alt[0][1] if alt else []
        front = hits[0] if hits else None
        walk_d = (front[0] - SIDEWALK_BACK) if front else 8.0
        sidewalk = (p[0] + nrm[0] * max(0.0, walk_d), p[1] + nrm[1] * max(0.0, walk_d))
        fan = collections.OrderedDict()
        for ds, _, _, h in results:
            if h:
                fan.setdefault(self.parcels[h[0][1]]["ml"], []).append(ds)
        return {"centerline_point": p, "normal": nrm, "front": front, "sidewalk": sidewalk, "fan": fan,
                "hits": hits}

    def frontage_window(self, key, n_seg, s, par, w=WINDOW, step=WINDOW_STEP):
        """Frontage parcel of every ray from s-w to s+w along the street, on the side carrying parity par."""
        out = []
        k = max(1, int(round(2 * w / step))) if w else 0
        for j in range(k + 1):
            ds = (-w + j * (2 * w / k)) if k else 0.0
            n2, s2 = self.advance(key, n_seg, s, 1 if ds >= 0 else -1, abs(ds)) if ds else (n_seg, s)
            ps = self.parity_side(key, n2, par)
            if not ps:
                continue
            p, (tx, ty) = self.point_at(key, n2, s2)
            hits = self.first_hit(p, (-ty * ps[0], tx * ps[0]))
            if hits:
                t, i = hits[0]
                ml = self.parcels[i]["ml"]
                c = self.parcel_class(ml)[0]
                out.append((round(ds, 1), ml, 2 if c in "CHS" else (1 if c == "P" else 0), round(t, 1)))
        return out

    @staticmethod
    def demote(level, g):
        """An extrapolated position (beyond the last official number) is only 'possible' unless it is short."""
        if level == "high" and (g.get("clamped") or g.get("extrap_m", 0) > EXTRAP_HIGH_M):
            return "medium"
        # between two address points on street pieces that don't connect, placed at the nearer one: a guess
        if level == "high" and "nearer one" in (g.get("method") or "") and g.get("frac") not in (None, 0.0, 1.0):
            return "medium"
        return level

    @staticmethod
    def window_level(win):
        hv = [h for _, _, h, _ in win]
        if hv and all(h == 2 for h in hv):
            return "high"
        if any(hv):
            return "medium"
        return None

    # ---------------------------------------------------------------- public: address
    def resolve_address(self, text):
        m = re.match(r"\s*(\d+)\s*([A-Za-z]?)\s+(.+)", text)
        if not m:
            return {"query": text, "error": "not '<number> <street>'"}
        num, key = int(m.group(1)), self.street_key(m.group(3))
        out = {"query": text, "number": num, "street": key, "street_known": key in self.streets}
        if key not in self.streets and key not in self.mal_streets:
            out["verdict"] = {"harvard": False, "why": "street not in Cambridge data"}
            return out
        # 1. exact Master Address List entries
        exact = sorted(self.mal.get((num, key), []), key=lambda a: (a["suffix"] != "", a["suffix"]))
        out["mal_exact"] = [{"full_addr": a["full_addr"], "suffix": a["suffix"], "maplot": self.address_parcel(a),
                             "type": a["type"],
                             "point": to_ll(a["xy"]), **self.describe_ml(self.address_parcel(a))} for a in exact]
        out["centerline_ranges"] = self.range_segments(key, num)
        # 2. geometry: interpolate along the street, cast to the side, find frontage parcel
        geo = self.interpolate(key, num) if key in self.streets else None
        if geo:
            seg = self.streets[key][geo["seg"]]
            g = {"method": geo["method"], "between": geo["between"], "frac": geo["frac"], "span_m": geo["span_m"],
                 "segment": seg["id"], "segment_ranges": {"left": seg["L"], "right": seg["R"]},
                 "side": {1: "left", -1: "right", None: "unknown"}[geo["side"]], "side_how": geo["side_how"]}
            if geo["side"]:
                fr = self.frontage(key, geo["seg"], geo["s"], geo["side"])
                g["centerline_point"] = to_ll(fr["centerline_point"])
                g["sidewalk_point"] = to_ll(fr["sidewalk"])
                g["frontage"] = self.describe(fr["front"][1], fr["front"][0]) if fr["front"] else None
                g["fan"] = {ml: ds for ml, ds in fr["fan"].items()}
                g["nearby_at_sidewalk"] = [self.describe(i, d) for d, i in
                                           self.near_parcels(fr["sidewalk"], 12.0)[:5] if not self.is_row(i)]
                win = self.frontage_window(key, geo["seg"], geo["s"], num % 2)
                g["window_m"] = WINDOW
                g["window"] = [{"ds": ds, "ml": ml, "harvard": h == 2, "partial": h == 1, "dist_m": t}
                               for ds, ml, h, t in win]
                g["window_level"] = self.window_level(win)
                g["extrap_m"], g["clamped"] = geo.get("extrap_m", 0.0), geo.get("clamped", False)
                # the other side, for contrast
                fr2 = self.frontage(key, geo["seg"], geo["s"], -geo["side"])
                g["opposite_side_frontage"] = self.describe(fr2["front"][1], fr2["front"][0]) if fr2["front"] else None
            out["geometry"] = g
        out["verdict"] = self.verdict_address(out)
        return out

    def official_level(self, rows):
        """Verdict for an official number heard without its suffix: (level, ml, why).  The bare address decides;
        a Harvard suffix sibling (65-R Mt Auburn St behind private 65) only makes it 'medium'."""
        def hv(r):
            return self.parcel_class(self.address_parcel(r))[0] in "CHS"
        bare = [r for r in rows if not r["suffix"]]
        sib = [r for r in rows if r["suffix"]]
        if bare:
            h = [r for r in bare if hv(r)]
            if h:
                return "high", self.address_parcel(h[0]), "official address on a Harvard parcel"
            p = [r for r in bare if self.parcel_class(self.address_parcel(r))[0] == "P"]
            if p:
                return "medium", self.address_parcel(p[0]), "official address on a partly Harvard-owned parcel"
            hs = [r for r in sib if hv(r)]
            if hs:
                return "medium", self.address_parcel(hs[0]), (f"official address is not Harvard, but "
                                                               f"{hs[0]['full_addr']} (same number) is")
            return None, self.address_parcel(bare[0]), "official address on a non-Harvard parcel"
        h = [r for r in sib if hv(r)]
        if h and len(h) == len(sib):
            return "high", self.address_parcel(h[0]), "only suffixed official addresses, all Harvard"
        if h:
            return "medium", self.address_parcel(h[0]), "suffixed official addresses, some Harvard"
        return None, self.address_parcel(sib[0]), "only suffixed official addresses, none Harvard"

    def address_parcel(self, row):
        """Parcel ml for a Master Address List row: its maplot, or (maplot has no polygon and no assessor row,
        e.g. '139-202', '156-110') the parcel containing the address point; 'ROAD' stays 'ROAD'."""
        ml = row.get("maplot") or ""
        if ml == "ROAD" or ml in self.by_ml or ml in self.owner:
            return ml
        i = self.parcel_at(row["xy"])
        return self.parcels[i]["ml"] if i is not None else ml

    def describe_ml(self, ml):
        cls, label = self.parcel_class(ml)
        o = self.owner.get(ml, {})
        return {"class": cls, "harvard": cls in "CHS", "partial": cls == "P", "label": label,
                "owners": o.get("owners", []),
                "harvard_units": f"{o.get('harvard_lots', 0)}/{o.get('lots', 0)}"}

    def verdict_address(self, r):
        ex = r.get("mal_exact") or []
        if ex:
            lvl, ml, why = self.official_level(self.mal[(r["number"], r["street"])])
            cls, label = self.parcel_class(ml) if ml != "ROAD" else ("R", "road")
            if ml == "ROAD":
                why = "official address on a road parcel (in the street, not a building)"
            return {"harvard": bool(lvl), "level": lvl, "why": f"{why}: {ml} ({label})"}
        g = r.get("geometry")
        if not g or not g.get("window"):
            return {"harvard": None, "level": None, "why": "number cannot be placed on this street (no alert from "
                    "geometry; the matcher may still say 'possible' if the street is a Harvard street)"}
        lvl = g["window_level"]
        hv = sorted({w["ml"] for w in g["window"] if w["harvard"] or w["partial"]})
        other = sorted({w["ml"] for w in g["window"] if not w["harvard"]})
        lvl = self.demote(lvl, g)
        where = f"{g['method']} -> {g['side']} side"
        if lvl == "high":
            return {"harvard": True, "level": "high", "why": f"not an official address; {where}; every ray within "
                    f"+-{WINDOW:.0f} m hits Harvard parcel(s) {', '.join(hv)}"}
        if lvl == "medium":
            return {"harvard": True, "level": "medium", "why": f"not an official address; {where}; boundary: Harvard "
                    f"{', '.join(hv)} and not-Harvard {', '.join(other)} within +-{WINDOW:.0f} m"}
        return {"harvard": False, "level": None, "why": f"not an official address; {where}; same-side frontage "
                f"{', '.join(other)} is not Harvard"}

    # ---------------------------------------------------------------- public: intersection
    def street_nodes(self, k1, k2):
        n1 = {sg["a"] for sg in self.streets.get(k1, [])} | {sg["b"] for sg in self.streets.get(k1, [])}
        n2 = {sg["a"] for sg in self.streets.get(k2, [])} | {sg["b"] for sg in self.streets.get(k2, [])}
        return sorted(n1 & n2)

    def arms(self, node):
        """Bearing (radians, math convention) of every street arm leaving a node, measured ~10 m out."""
        o = self.node_xy[node]
        out = []
        for key, sn in self.node_segs[node]:
            sg = self.streets[key][sn]
            pts = sg["pts"] if sg["a"] == node else sg["pts"][::-1]
            acc, q = 0.0, pts[-1]
            for k in range(1, len(pts)):
                acc += math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1])
                if acc >= 10:
                    q = pts[k]
                    break
            out.append((math.atan2(q[1] - o[1], q[0] - o[0]), sg["street"]))
        return sorted(out)

    def corners(self, node, radius=CORNER_FAR):
        """Corner parcel of every sector between adjacent street arms: the non-road parcel nearest to the node whose
        nearest point lies inside the sector.  (A single bisector ray misses corner lots that are cut back.)"""
        o = self.node_xy[node]
        arms = self.arms(node)
        if not arms:
            return []
        sectors = []
        for j in range(len(arms)):
            a1, s1 = arms[j]
            a2, s2 = arms[(j + 1) % len(arms)]
            gap = (a2 - a1) % (2 * math.pi) or 2 * math.pi
            sectors.append({"from": a1, "gap": gap, "between": [s1, s2], "best": None})
        for d, i in self.near_parcels(o, radius):
            if self.is_row(i):
                continue
            # nearest point of the parcel to the node
            best = None
            for r in self.parcels[i]["rings"]:
                for k in range(len(r) - 1):
                    d2, t = seg_point_dist2(o, r[k], r[k + 1])
                    if best is None or d2 < best[0]:
                        best = (d2, (r[k][0] + t * (r[k + 1][0] - r[k][0]), r[k][1] + t * (r[k + 1][1] - r[k][1])))
            if d == 0.0:
                ang = None                      # node inside the parcel (private way): every sector
            else:
                ang = math.atan2(best[1][1] - o[1], best[1][0] - o[0])
            for sc in sectors:
                inside = ang is None or (ang - sc["from"]) % (2 * math.pi) <= sc["gap"]
                if inside and (sc["best"] is None or d < sc["best"][0]):
                    sc["best"] = (d, i)
        out = []
        for sc in sectors:
            mid = sc["from"] + sc["gap"] / 2
            hit = sc["best"]
            kind = "corner" if sc["between"][0] != sc["between"][1] else "far side (same street both arms)"
            out.append({"between": sc["between"], "kind": kind, "bearing_deg": round((90 - math.degrees(mid)) % 360),
                        "parcel": self.describe(hit[1], hit[0]) if hit else None})
        return out

    def corner_level(self, cs):
        """high: a true corner (between two different streets) is Harvard within CORNER_MAX.
        medium: Harvard only across the through street of a T (FAR_SIDE_LEVEL), or 30-45 m away."""
        hv = [c for c in cs if c["parcel"] and c["parcel"]["harvard"]]
        near_true = [c for c in hv if c["parcel"]["dist_m"] <= CORNER_MAX and c["kind"] == "corner"]
        if near_true:
            return "high", min((c["parcel"] for c in near_true), key=lambda p: p["dist_m"])
        near_far = [c for c in hv if c["parcel"]["dist_m"] <= CORNER_MAX]
        if near_far:
            return FAR_SIDE_LEVEL, min((c["parcel"] for c in near_far), key=lambda p: p["dist_m"])
        if hv:
            return "medium", min((c["parcel"] for c in hv), key=lambda p: p["dist_m"])
        part = [c for c in cs if c["parcel"] and c["parcel"].get("partial") and c["parcel"]["dist_m"] <= CORNER_MAX]
        if part:
            return "medium", min((c["parcel"] for c in part), key=lambda p: p["dist_m"])
        return None, None

    def resolve_intersection(self, a, b):
        k1, k2 = self.street_key(a), self.street_key(b)
        nodes = self.street_nodes(k1, k2)
        out = {"query": f"{a} & {b}", "streets": [k1, k2], "nodes": []}
        how = "shared centerline node"
        if not nodes:
            # streets do not share a node: closest approach (e.g. one street stops short / divided roads)
            best = None
            for s1 in self.streets.get(k1, []):
                for p in s1["pts"]:
                    loc = self.locate(k2, p)
                    if loc and (best is None or loc["d"] < best[0]):
                        best = (loc["d"], p, loc["foot"])
            if best and best[0] < 40:
                how = f"no shared node; closest approach {best[0]:.0f} m"
                mid = ((best[1][0] + best[2][0]) / 2, (best[1][1] + best[2][1]) / 2)
                # use the nearest node of either street
                cand = sorted((math.hypot(self.node_xy[nd][0] - mid[0], self.node_xy[nd][1] - mid[1]), nd)
                              for k in (k1, k2) for sg in self.streets[k] for nd in (sg["a"], sg["b"]))
                nodes = [cand[0][1]]
        out["how"] = how
        best = (None, None, None)
        rank = {"high": 2, "medium": 1, None: 0}
        for nd in nodes:
            o = self.node_xy[nd]
            cs = self.corners(nd)
            lvl, p = self.corner_level(cs)
            near = [self.describe(i, d) for d, i in self.near_parcels(o, CORNER_MAX) if not self.is_row(i)]
            out["nodes"].append({"node": nd, "name": self.int_names.get(nd), "point": to_ll(o), "corners": cs,
                                 "level": lvl, "parcels_within_%dm" % CORNER_MAX: near})
            if rank[lvl] > rank[best[0]] or (lvl and rank[lvl] == rank[best[0]] and p["dist_m"] < best[1]["dist_m"]):
                best = (lvl, p, nd)
        if best[0]:
            lvl, p, nd = best
            out["verdict"] = {"harvard": True, "level": lvl,
                              "why": f"corner parcel {p['ml']} ({p['label']}) {p['dist_m']} m from node {nd}"}
        elif not nodes:
            out["verdict"] = {"harvard": None, "level": None, "why": "streets do not meet"}
        else:
            out["verdict"] = {"harvard": False, "level": None,
                              "why": "no Harvard corner parcel within %d m" % CORNER_FAR}
        return out


# ------------------------------------------------------------------------------------------------ precompute
def harvard_streets(m):
    """Street keys whose centerline passes within RAY_MAX of a Harvard (C/H/S) parcel."""
    out = set()
    for key, segs in m.streets.items():
        for seg in segs:
            pts = seg["pts"]
            hit = False
            for k in range(len(pts) - 1):
                a, b = pts[k], pts[k + 1]
                for i in m.candidates(min(a[0], b[0]) - RAY_MAX, min(a[1], b[1]) - RAY_MAX,
                                      max(a[0], b[0]) + RAY_MAX, max(a[1], b[1]) + RAY_MAX):
                    if m.parcel_class(m.parcels[i]["ml"])[0] in "CHSP":
                        hit = True
                        break
                if hit:
                    break
            if hit:
                out.add(key)
                break
    return sorted(out)


def assessor_numbers(text):
    """House numbers in an assessor address, refusing what could be a unit number: '47 Concord Ave' -> 47,
    '1173-1201 Massachusetts Ave' -> 1173, 1175, ... 1201; '70-10 Kirkland St' (unit 10?) -> nothing."""
    m = re.match(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s+([A-Za-z][A-Za-z .']+?)\s*$", text or "")
    if not m:
        return []
    lo, hi, street = int(m.group(1)), int(m.group(2) or m.group(1)), m.group(3)
    if hi < lo or (hi - lo) % 2 or hi - lo > 40:
        return []
    return [(n, street) for n in range(lo, hi + 1, 2)]


def harvard_lined_streets(m, step=12.0, off=12.0, share=0.8, max_points=1):
    """[(street key, label)] for streets whose both sides are Harvard parcels at >= share of the points sampled every
    step metres, and that have at most max_points address points (so numbers can't be placed on them)."""
    out = []
    for key in harvard_streets(m):
        if len(m.controls.get(key, [])) > max_points:
            continue
        sides = {1: [], -1: []}
        names = collections.Counter()
        for seg in m.streets[key]:
            s = step / 2
            while s < seg["len"]:
                p, (tx, ty) = m.point_at(key, seg["n"], s)
                for sgn in (1, -1):
                    i = m.parcel_at((p[0] - ty * sgn * off, p[1] + tx * sgn * off))
                    c = m.parcel_class(m.parcels[i]["ml"])[0] if i is not None else None
                    sides[sgn].append(c in ("C", "H", "S"))
                    if c in ("C", "H", "S"):
                        names[m.parcels[i]["ml"]] += 1
                s += step
        if all(v and sum(v) / len(v) >= share for v in sides.values()):
            out.append((key, "Harvard property on both sides"))
    return out


def official(m, key, n):
    """Runtime verdict for an official address: ('high', ml) Harvard, ('none', ml) not, or None if not official."""
    rows = m.mal.get((n, key))
    if not rows:
        return None
    lvl, ml, _ = m.official_level(rows)
    return (lvl or "none", ml)


def sweep_street(m, key):
    """{parity: [[lo, hi, level, ml, source], ...]} for every house number on the street (runtime decision)."""
    ctr = m.controls.get(key, [])
    nums = [c[0] for c in ctr] + [r[1] for sg in m.streets[key] for r in (sg["L"], sg["R"]) if r]
    if not nums:
        return {}
    top = max(nums) + EXTRAP_NUMBERS
    out = {}
    for par in (1, 0):
        runs = []
        for n in range(1 if par else 2, top + 1, 2):
            off = official(m, key, n)
            if off:
                lvl, ml, src = (None if off[0] == "none" else off[0]), off[1], "official"
            else:
                g = m.interpolate(key, n)
                if not g or not g["side"]:
                    lvl, ml, src = None, None, "unplaced"
                else:
                    win = m.frontage_window(key, g["seg"], g["s"], par)
                    lvl = m.demote(m.window_level(win), g)
                    hv = collections.Counter(w[1] for w in win if w[2])
                    ml = hv.most_common(1)[0][0] if hv else None
                    src = "geometry"
            if runs and runs[-1][2] == lvl and runs[-1][3] == ml and runs[-1][1] == n - 2:
                runs[-1][1] = n
                if src != runs[-1][4]:
                    runs[-1][4] = "mixed"
            else:
                runs.append([n, n, lvl, ml, src])
        out[str(par)] = [r for r in runs if r[2]]
    return out


def intersection_table(m):
    """{"a|b": {"level", "nodes": [...], "corners": [...]}} for every street pair that meets at a Harvard corner."""
    per_node = {}
    pairs = collections.defaultdict(set)
    for node, segs in m.node_segs.items():
        keys = sorted({k for k, _ in segs})
        nm = m.int_names.get(node) or ""
        keys = sorted(set(keys) | {m.street_key(x) for x in nm.split("&") if x.strip() and x.strip() != "<Null>"})
        if len(keys) < 2 or node not in m.node_xy:
            continue
        cs = m.corners(node)
        lvl, _ = m.corner_level(cs)
        hv = [c["parcel"] for c in cs if c["parcel"] and c["parcel"]["harvard"]]
        near = [m.describe(i, d) for d, i in m.near_parcels(m.node_xy[node], 12.0) if not m.is_row(i)]
        near_hv = [p for p in near if p["harvard"]]
        true_corner = any(c["parcel"] and c["parcel"]["harvard"] and c["kind"] == "corner" for c in cs)
        if lvl and not hv:                      # only a partly-Harvard / amber-org corner (Mass Ave & Remington St)
            hv = [c["parcel"] for c in cs if c["parcel"] and c["parcel"].get("partial")
                  and c["parcel"]["dist_m"] <= CORNER_MAX]
        per_node[node] = {"corners_harvard": hv if lvl else [], "level": lvl, "near12_harvard": near_hv,
                          "true_corner": true_corner}
        for a in range(len(keys)):
            for b in range(a + 1, len(keys)):
                pairs[(keys[a], keys[b])].add(node)
    table, stats = {}, collections.Counter()
    for (a, b), nodes in pairs.items():
        nodes = sorted(nodes)
        hv_nodes = [nd for nd in nodes if per_node[nd]["corners_harvard"]]
        near_only = [nd for nd in nodes if not per_node[nd]["corners_harvard"] and per_node[nd]["near12_harvard"]]
        if near_only:
            stats["pairs where only the 12-m proximity rule finds Harvard"] += 1
        if not hv_nodes:
            continue
        xs = [m.node_xy[nd] for nd in nodes]
        spread = max(math.hypot(p[0] - q[0], p[1] - q[1]) for p in xs for q in xs)
        level = "high" if any(per_node[nd]["level"] == "high" for nd in hv_nodes) else "medium"
        if level == "high" and spread > JUNCTION_SPREAD and len(hv_nodes) < len(nodes):
            level = "medium"                     # the streets meet twice, far apart, and only one meeting is Harvard
        stats[level] += 1
        corners = {}
        for nd in hv_nodes:
            for p in per_node[nd]["corners_harvard"]:
                corners.setdefault(p["ml"], {"ml": p["ml"], "class": p["class"], "dist_m": p["dist_m"],
                                             "addresses": p["addresses"][:2]})
        far_only = not any(per_node[nd]["true_corner"] for nd in hv_nodes)
        if far_only:
            stats["pairs Harvard only across the through street of a T"] += 1
        table[f"{a}|{b}"] = {"level": level, "nodes": nodes, "harvard_nodes": hv_nodes, "spread_m": round(spread),
                             "far_side_only": far_only,
                             "corners": sorted(corners.values(), key=lambda c: c["dist_m"])}
    return table, stats, len(pairs)


# ------------------------------------------------------------------------------------------------ curated knowledge
def load_curated(path=CURATED):
    """data/harvard-curated.json: student-org parcels, extra Harvard parcels, single-address overrides."""
    try:
        with open(path) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    STUDENT_ORGS.clear()
    HARVARD_EXTRA.clear()
    for org in cur.get("student_orgs", []):
        for ml in org.get("parcels", []):
            STUDENT_ORGS[ml] = (org["name"], org.get("level", "high"))
    HARVARD_EXTRA.update(cur.get("harvard_parcels", {}))
    return cur


# ------------------------------------------------------------------------------------------------ Harvard's map
_ADDR = re.compile(r"^\s*(\d+)(?:\.\d+)?[A-Za-z]?(?:\s*-\s*(\d+)(?:\.\d+)?[A-Za-z]?)?\s+([A-Za-z][A-Za-z .']+?)\s*$")


def expand_address(text):
    """'1533-1541 Massachusetts Ave' -> [(1533, 'Massachusetts Ave'), (1535, ...), ... (1541, ...)] (same parity);
    '79 Garden St, Cambridge, MA 02138' -> [(79, 'Garden St')]; several parts split on ';', '&', ' and '."""
    out = []
    for part in re.split(r";|&|\band\b", text or ""):
        part = part.split(",")[0]
        part = re.sub(r"(?i)\s+(?:cambridge|mail ctr|mail center|ma|boston)\b.*$", "", part)
        m = _ADDR.match(part)
        if not m:
            continue
        lo, hi, street = int(m.group(1)), int(m.group(2) or m.group(1)), m.group(3).strip()
        if hi < lo:
            lo, hi = hi, lo
        if hi - lo > 60:
            hi = lo
        step = 2 if (hi - lo) % 2 == 0 else 1          # "1533-1541": one side of the street; "1-2 Athens Ter": both
        out += [(n, street) for n in range(lo, hi + 1, step)]
    return out


class HarvardMap:
    """Buildings from Harvard's own campus map (Cambridge only): name, footprint, addresses."""

    def __init__(self, m, path):
        self.buildings = []
        try:
            with open(path) as f:
                feats = json.load(f)["features"]
        except (OSError, ValueError, KeyError):
            feats = []
        for f in feats:
            p, g = f.get("properties") or {}, f.get("geometry")
            if not g or (p.get("locality") or "").strip() != "Cambridge":
                continue
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            rings = [[to_xy(c[0], c[1]) for c in ring] for poly in polys for ring in poly]
            xs = [q[0] for r in rings for q in r]
            ys = [q[1] for r in rings for q in r]
            name = (p.get("name") or "").strip()
            b = {"name": name if name and not name[:1].isdigit() else None, "full": name,
                 "kind": "leased" if (p.get("source_type") or "") == "Non-Harvard" else "harvard",
                 "rings": rings, "bbox": (min(xs), min(ys), max(xs), max(ys)), "keys": {}}
            for field in ("e911", "address"):
                for n, street in expand_address(p.get(field)):
                    b["keys"].setdefault((n, m.street_key(street)), field)
            self.buildings.append(b)
        # the city's own address points strictly inside a footprint are that building's addresses too (not points
        # merely next to it: buildings in the Square share walls, and the neighbour's address must stay the neighbour's)
        for (n, key), rows in m.mal.items():
            for r in rows:
                if r["suffix"]:
                    continue
                for b in self._near(r["xy"], 0.0):
                    b["keys"][(n, key)] = "inside"
        # which parcel each building stands on (for naming what a stretch of sidewalk or a corner belongs to)
        for b in self.buildings:
            cx = sum(q[0] for q in b["rings"][0]) / len(b["rings"][0])
            cy = sum(q[1] for q in b["rings"][0]) / len(b["rings"][0])
            i = m.parcel_at((cx, cy))
            b["ml"] = m.parcels[i]["ml"] if i is not None else None

    def _dist(self, b, p):
        if in_rings(p, b["rings"]):
            return 0.0
        best = 1e18
        for r in b["rings"]:
            for k in range(len(r) - 1):
                best = min(best, seg_point_dist2(p, r[k], r[k + 1])[0])
        return math.sqrt(best)

    def _near(self, p, radius):
        out = []
        for b in self.buildings:
            x0, y0, x1, y1 = b["bbox"]
            if x0 - radius <= p[0] <= x1 + radius and y0 - radius <= p[1] <= y1 + radius:
                if self._dist(b, p) <= radius:
                    out.append(b)
        return out

    def name_near(self, p, radius=40.0, kind="harvard", ml=None):
        """Name of the nearest named Harvard building within radius of point p (only buildings on parcel ml, if
        given), or None."""
        best = None
        for b in self.buildings:
            if b["kind"] != kind or not b["name"] or (ml and b.get("ml") != ml):
                continue
            x0, y0, x1, y1 = b["bbox"]
            if not (x0 - radius <= p[0] <= x1 + radius and y0 - radius <= p[1] <= y1 + radius):
                continue
            d = self._dist(b, p)
            if d <= radius and (best is None or d < best[0]):
                best = (d, b["name"])
        return best[1] if best else None


# ------------------------------------------------------------------------------------------------ export
USE_WORDS = [("RETAIL-OFFIC", "shops / offices"), ("RETAIL", "shops"), ("EATING", "restaurant"), ("OFFICE", "offices"),
             ("UNIT-APT", "apartments"), ("APT", "apartments"), ("CNDO", "condos"), ("CONDO", "condos"),
             ("HOTEL", "hotel"), ("CHURCH", "church"), ("RELIGIOUS", "church"), ("NURSING", "nursing home"),
             ("ASSISTED", "assisted living"), ("HOSPITAL", "hospital"), ("SNGL-FAM", "house"), ("TWO-FAM", "house"),
             ("THREE-FM", "house"), ("MULTIUSE", "mixed use"), ("PARKING", "parking"), ("SCHOOL", "school"),
             ("CHARITABLE", "non-profit"), ("BOARDING", "boarding house"), ("COLLEGE", "college")]


def humanize_use(classes):
    s = " ".join(classes or []).upper()
    for k, v in USE_WORDS:
        if k in s:
            return v
    return (classes[0].lower() if classes else "")


def build_tables(m, hmap, curated, norm):
    """Everything the run-time Locator needs (parcels.py), keyed by norm(street name) so lookups match:
         addresses          "<num> <street>" -> [code, label]   code C/H/S Harvard (alarm), P possible (amber),
                                                               N not Harvard, R in the road (not a building)
         frontage           "<street>|<parity>" -> [[from, to, label], ...]  same-side frontage of Harvard: alarm
         frontage_possible  same, amber (lot line / past the street's last address / partly Harvard)
         spans              "<street>|<parity>" -> [lowest, highest] official number on that side
         intersections      "<a>|<b>" (sorted) -> [code, label]   H alarm (Harvard corner), M amber (across a T)
         streets, street_aliases"""
    pkey = {}
    for key, segs in m.streets.items():
        pkey[key] = norm(segs[0]["street"])
    for (n, key), rows in m.mal.items():
        pkey.setdefault(key, norm(rows[0].get("stname") or key))

    def org_or_name(ml, point=None):
        cls, lab = m.parcel_class(ml)
        if ml in STUDENT_ORGS:
            return STUDENT_ORGS[ml][0]
        if point is not None:
            nm = hmap.name_near(point, 60.0, ml=ml) or hmap.name_near(point, 15.0)
            if nm:
                return nm
        return None

    addresses, spans = {}, {}
    for (n, key), rows in m.mal.items():
        lvl, ml, why = m.official_level(rows)
        k = f"{n} {pkey[key]}"
        cls = "R" if ml == "ROAD" else m.parcel_class(ml)[0]
        if lvl == "high":
            code = cls if cls in ("C", "H", "S") else "C"
            label = org_or_name(ml)
        elif lvl == "medium":
            code, label = "P", (org_or_name(ml) or why)
        else:
            code = "R" if ml == "ROAD" else "N"
            label = "in the street" if ml == "ROAD" else humanize_use(m.owner.get(ml, {}).get("classes"))
        addresses[k] = [code, label]
        sk = f"{pkey[key]}|{n % 2}"
        lo, hi = spans.get(sk, (n, n))
        spans[sk] = [min(lo, n), max(hi, n)]

    # the assessor's own addresses for a parcel, where the city's address list has no such number ("47 Concord Ave"
    # for the Observatory, "0 Arrow St", "393 Mt Auburn St" for a private house): that parcel decides
    seeds = collections.defaultdict(set)
    for ml, o in m.owner.items():
        if ml not in m.by_ml:
            continue
        for a in o.get("addresses", []):
            for n, street in assessor_numbers(a):
                key = m.street_key(street)
                if key in pkey and (n, key) not in m.mal:
                    seeds[f"{n} {pkey[key]}"].add(ml)
    seeded = collections.Counter()
    for k, mls in seeds.items():
        if k in addresses:
            continue
        got = {m.parcel_class(ml)[0] for ml in mls}
        if len(got) != 1 or "R" in got:
            continue                                    # two parcels claim the number: leave it to the frontage
        cls, ml = got.pop(), next(iter(mls))
        if cls in ("C", "H", "S"):
            addresses[k] = [cls, org_or_name(ml)]
        elif cls == "P":
            addresses[k] = ["P", org_or_name(ml) or m.parcel_class(ml)[1]]
        else:
            addresses[k] = ["N", humanize_use(m.owner.get(ml, {}).get("classes"))]
        seeded[addresses[k][0]] += 1

    # Harvard's campus map: its own addresses for its buildings (names, and numbers the city doesn't list);
    # buildings it marks "Non-Harvard" are leased space, usually shared with other tenants: amber
    for b in hmap.buildings:
        for (n, key), src in b["keys"].items():
            if key not in pkey:
                continue                                # not a Cambridge street name ("27 Harvard Yard Mail Ctr")
            k = f"{n} {pkey[key]}"
            cur = addresses.get(k)
            official = (n, key) in m.mal
            if b["kind"] == "harvard":
                if cur and cur[0] in ("C", "H", "S", "P"):
                    if not cur[1] and b["name"]:
                        cur[1] = b["name"]
                    continue                            # a partly-Harvard building stays amber (12-30 Palmer St)
                # a range on the map ("1236-1256 Mass Ave" for Hampden Hall) must not take over another owner's
                # official address next door: only a number the city doesn't list, or a city point strictly inside
                if not official or src == "inside":
                    addresses[k] = ["C", b["name"] or "Harvard building (Harvard campus map)"]
            elif not cur or cur[0] in ("N",):
                if not official or src == "inside":
                    addresses[k] = ["P", f"Harvard-leased space{': ' + b['name'] if b['name'] else ''} (shared building)"]

    # curated: single-address overrides and amber addresses inside org buildings
    for o in curated.get("address_overrides", []):
        for n, street in expand_address(o["address"]):
            k = f"{n} {pkey.get(m.street_key(street), norm(street))}"
            addresses[k] = ["C" if o.get("level", "high") == "high" else "P", o.get("name")]
    for org in curated.get("student_orgs", []):
        for a in org.get("amber_addresses", []):
            for n, street in expand_address(a):
                k = f"{n} {pkey.get(m.street_key(street), norm(street))}"
                addresses[k] = ["P", f"shop in the {org['name']} building"]

    frontage, frontage_possible = collections.defaultdict(list), collections.defaultdict(list)
    for key in harvard_streets(m):
        for par, ivs in sweep_street(m, key).items():
            for lo, hi, lvl, ml, src in ivs:
                label = None
                if ml:
                    mid = (lo + hi) // 2
                    if mid % 2 != int(par):                 # stay on this side's parity
                        mid = mid + 1 if mid + 1 <= hi else mid - 1
                    g = m.interpolate(key, mid)
                    pt = None
                    if g and g["side"]:
                        fr = m.frontage(key, g["seg"], g["s"], g["side"])
                        if fr["front"]:
                            t, _ = fr["front"]
                            pt = (fr["centerline_point"][0] + fr["normal"][0] * t,
                                  fr["centerline_point"][1] + fr["normal"][1] * t)
                    label = org_or_name(ml, pt)
                label = label or "Harvard property"
                tgt = frontage if lvl == "high" else frontage_possible
                lst = tgt[f"{pkey[key]}|{par}"]
                if lst and lst[-1][2] == label and lst[-1][1] >= lo - 2:
                    lst[-1][1] = hi
                else:
                    lst.append([lo, hi, label])

    # short streets lined with Harvard on both sides but with too few address points to place numbers
    # (Cowperthwaite St between Mather House and Peabody Terrace, Frisbie Pl): every number is Harvard
    # (after the sweep's own intervals, so their building names come first)
    lined = harvard_lined_streets(m)
    for key, name in lined:
        top = max([int(k.split()[0]) for k in addresses if k.split(" ", 1)[1] == pkey[key]] + [20]) + 20
        for par in (0, 1):
            frontage[f"{pkey[key]}|{par}"].append([2 - par, top, name])

    inter_t, _, _ = intersection_table(m)
    intersections = {}
    for k, v in inter_t.items():
        a, b = k.split("|")
        kk = "|".join(sorted((pkey.get(a, a), pkey.get(b, b))))
        c = v["corners"][0]
        node = v["harvard_nodes"][0]
        label = org_or_name(c["ml"], m.node_xy.get(node)) or "Harvard property"
        label += " across the street" if v["far_side_only"] else " on the corner"
        code = "H" if v["level"] == "high" else "M"
        if kk not in intersections or (code == "H" and intersections[kk][0] != "H"):
            intersections[kk] = [code, label]

    # coordinates for "how far is this call from Adams House" (addresses within 1.5 km of it, and street corners)
    def near_adams(lat, lon, limit=1500.0):
        dy = (lat - ADAMS_HOUSE[0]) * 111080.5
        dx = (lon - ADAMS_HOUSE[1]) * 82364.3
        return dx * dx + dy * dy <= limit * limit
    coords = {}
    for (n, key), rows in m.mal.items():
        r = next((r for r in rows if not r["suffix"]), rows[0])
        try:
            lat, lon = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if near_adams(lat, lon):
            coords[f"{n} {pkey[key]}"] = [round(lat, 5), round(lon, 5)]
    corners = {}
    for node, segs in m.node_segs.items():
        keys = sorted({pkey.get(k, k) for k, _ in segs})
        if len(keys) < 2 or node not in m.node_xy:
            continue
        lat, lon = to_ll(m.node_xy[node])
        if not near_adams(lat, lon):
            continue
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                corners.setdefault(f"{keys[i]}|{keys[j]}", [round(lat, 5), round(lon, 5)])

    # named Harvard buildings (Harvard's campus map) and student-org buildings, for calls that name a place, not an address
    place_coords = {k: [round(v[0], 5), round(v[1], 5)] for k, v in AREAS.items()}
    for b in hmap.buildings:
        if b["kind"] != "harvard" or not b["name"]:
            continue
        ring = b["rings"][0]
        lat, lon = to_ll((sum(q[0] for q in ring) / len(ring), sum(q[1] for q in ring) / len(ring)))
        place_coords.setdefault(b["name"].lower(), [round(lat, 5), round(lon, 5)])
    for alias, real in (("harvard university health services", "smith center health services"),
                        ("huhs", "smith center health services"), ("holyoke center", "smith campus center")):
        if real in place_coords:
            place_coords.setdefault(alias, place_coords[real])
    for org in curated.get("student_orgs", []):
        for ml in org.get("parcels", [])[:1]:
            idx = m.by_ml.get(ml)
            if idx:
                ring = m.parcels[idx[0]]["rings"][0]
                lat, lon = to_ll((sum(q[0] for q in ring) / len(ring), sum(q[1] for q in ring) / len(ring)))
                place_coords.setdefault(org["name"].lower(), [round(lat, 5), round(lon, 5)])

    aliases = {}
    for a, k in STREET_ALIASES.items():
        if k in pkey and norm(a) != pkey[k]:
            aliases[norm(a)] = pkey[k]
    return {"_seeded": sum(seeded.values()), "_lined": len(lined),
            "addresses": addresses, "frontage": dict(frontage), "frontage_possible": dict(frontage_possible),
            "spans": spans, "intersections": intersections, "streets": sorted(set(pkey.values())),
            "street_aliases": aliases, "coords": coords, "corner_coords": corners, "place_coords": place_coords}


# ------------------------------------------------------------------------------------------------ download
SOCRATA = "https://data.cambridgema.gov/resource/"
HARVARD_FACILITIES = ("https://mapprod.cadm.harvard.edu/server/rest/services/Hosted/Facilities/FeatureServer/8/query?"
                      "where=1%3D1&outFields=name,address,e911,locality,source_type&outSR=4326&f=geojson"
                      "&resultRecordCount=2000")
UA = "CrimsonEMS-DispatchWatch/1.0 (student EMS dispatch monitor; builds an address table monthly)"
# file -> (url, minimum sane size of the result: fail loudly if a dataset suddenly shrinks)
def _sources(year):
    q = urllib.parse.urlencode
    return {
        "parcels.geojson": (SOCRATA + "c392-hjk3.geojson?" + q({"$limit": 200000}), 10000),
        "centerlines.geojson": (SOCRATA + "3d3q-sfd6.geojson?" + q({"$limit": 200000}), 2000),
        "intersections.geojson": (SOCRATA + "xhx7-spt9.geojson?" + q({"$limit": 200000}), 1000),
        "addresses.json": (SOCRATA + "vup6-kpwv.json?" + q({
            "$select": "full_addr,street_number,stname,maplot,type,latitude,longitude", "$limit": 200000}), 15000),
        "assess2026.json": (SOCRATA + "eey2-rv59.json?" + q({
            "$select": "map_lot,gisid,address,unit,owner_name,owner_coownername,propertyclass,landarea",
            "$where": f"yearofassessment='{year}'", "$limit": 200000}), 20000),
        "harvard_facilities.geojson": (HARVARD_FACILITIES, 300),
    }


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def _count(data):
    d = json.loads(data)
    return len(d["features"]) if isinstance(d, dict) and "features" in d else len(d)


def download(cache=CACHE, log=print):
    """Fetch every dataset into data/cache/ (about 35 MB). Returns {file: {"rows", "url"}}."""
    os.makedirs(cache, exist_ok=True)
    out = {}
    year = dt.date.today().year + 1                     # the newest fiscal year that has assessor rows
    for fname, (url, minimum) in _sources(year).items():
        if fname == "assess2026.json":
            for y in (year, year - 1, year - 2):
                url = _sources(y)[fname][0]
                data = _fetch(url)
                if _count(data) >= minimum:
                    break
        else:
            try:
                data = _fetch(url)
            except Exception as e:
                if fname == "harvard_facilities.geojson" and os.path.exists(os.path.join(cache, fname)):
                    log(f"  Harvard's campus map unreachable ({type(e).__name__}); using the cached copy")
                    out[fname] = {"rows": None, "url": url, "cached": True}
                    continue
                raise
        n = _count(data)
        if n < minimum:
            raise RuntimeError(f"{fname}: only {n} rows (expected at least {minimum}); the source may have "
                               f"changed: {url}")
        with open(os.path.join(cache, fname), "wb") as f:
            f.write(data)
        out[fname] = {"rows": n, "url": url}
        log(f"  {fname}: {n} rows")
    return out


def build(cache=CACHE, norm=None, log=print):
    """Load the cached downloads and return the Locator tables plus a short summary."""
    if norm is None:
        from parcels import _norm_street as norm
    curated = load_curated()
    m = Model(cache)
    hmap = HarvardMap(m, os.path.join(cache, "harvard_facilities.geojson"))
    t = build_tables(m, hmap, curated, norm)
    codes = collections.Counter(v[0] for v in t["addresses"].values())
    summary = {"addresses": len(t["addresses"]), "harvard": sum(codes[c] for c in "CHS"), "possible": codes["P"],
               "assessor_addresses": t.pop("_seeded", 0), "harvard_lined_streets": t.pop("_lined", 0),
               "frontage_intervals": sum(map(len, t["frontage"].values())),
               "frontage_possible_intervals": sum(map(len, t["frontage_possible"].values())),
               "intersections_harvard": sum(1 for v in t["intersections"].values() if v[0] == "H"),
               "intersections_possible": sum(1 for v in t["intersections"].values() if v[0] == "M"),
               "harvard_map_buildings": len(hmap.buildings), "student_org_parcels": len(STUDENT_ORGS)}
    return t, summary


if __name__ == "__main__":
    load_curated()
    if len(sys.argv) < 3 or sys.argv[1] not in ("addr", "int"):
        sys.exit(__doc__)
    model = Model(CACHE)
    if sys.argv[1] == "addr":
        for a in sys.argv[2:]:
            r = model.resolve_address(a)
            v = r.get("verdict", {})
            print(f"{a:28} {('Harvard' if v.get('harvard') else 'not Harvard'):12} {v.get('level') or '-':7} {v.get('why')}")
    else:
        r = model.resolve_intersection(sys.argv[2], sys.argv[3])
        v = r["verdict"]
        print(f"{r['query']:28} {('Harvard' if v.get('harvard') else 'not Harvard'):12} {v.get('level') or '-':7} {v.get('why')}")

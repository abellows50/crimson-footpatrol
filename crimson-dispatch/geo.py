"""
Where is the call, and how far is it from our base?

* Geocoding uses OpenStreetMap's Nominatim (free, no key). Its rules: at most 1 request per second and an
  identifying User-Agent, so every lookup is cached in logs/geocache.json and requests are spaced out.
* Travel times are estimates from straight-line distance (Cambridge streets add ~30-40%), not routing.
  The dashboard links to Google Maps for real turn-by-turn directions.
"""
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request

UA = "CrimsonEMS-DispatchWatch/1.0 (student EMS dispatch monitor; low volume, cached)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
VIEWBOX = "-71.20,42.43,-71.00,42.33"              # Cambridge, Somerville, Allston, Boston: preferred, not required
MISS_RETRY_S = 24 * 3600

ABBR = [(r"\bMass\.? Ave\b", "Massachusetts Avenue"), (r"\bMass Avenue\b", "Massachusetts Avenue"),
        (r"\bSt\b\.?", "Street"), (r"\bAve\b\.?", "Avenue"), (r"\bRd\b\.?", "Road"), (r"\bPl\b\.?", "Place"),
        (r"\bSq\b\.?", "Square"), (r"\bMt\b\.?", "Mount"), (r"\bPkwy\b", "Parkway"), (r"\bDr\b\.?", "Drive")]


def expand(s):
    s = (s or "").strip()
    for pat, rep in ABBR:
        s = re.sub(pat, rep, s, flags=re.I)
    return s


def haversine_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def travel(base, dest):
    d = haversine_m(base, dest)
    walk_route, drive_route = d * 1.35, d * 1.4                       # streets are ~35-40% longer than a straight line
    return {"dist_m": round(d), "dist_mi": round(d / 1609.34, 2),
            "walk_min": max(1, round(walk_route / 1.35 / 60)),        # brisk walk, 1.35 m/s
            "drive_min": max(1, round(drive_route / 7.0 / 60 + 1))}   # ~15 mph in Cambridge traffic, +1 min to get going


class Geocoder:
    def __init__(self, cache_path, log=print, fetch=None):
        self.path, self.log = cache_path, log
        self.fetch = fetch or self._fetch
        self.lock, self.net_lock = threading.Lock(), threading.Lock()
        self.last_req = 0.0
        try:
            with open(cache_path) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cache, f)
        os.replace(tmp, self.path)

    @staticmethod
    def _fetch(query):
        url = NOMINATIM + "?" + urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1,
                                                        "viewbox": VIEWBOX, "countrycodes": "us"})
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def lookup(self, query):
        """One query -> {lat, lon, display} or None. Cached (misses too, retried after a day)."""
        q = re.sub(r"\s+", " ", (query or "").strip())
        if not q:
            return None
        key = q.lower()
        with self.lock:
            hit = self.cache.get(key)
        if hit and (hit.get("lat") is not None or time.time() - hit.get("at", 0) < MISS_RETRY_S):
            return hit if hit.get("lat") is not None else None
        with self.net_lock:                                         # Nominatim: max 1 request / second
            wait = 1.1 - (time.time() - self.last_req)
            if wait > 0:
                time.sleep(wait)
            self.last_req = time.time()
            try:
                res = self.fetch(q)
            except Exception as e:
                self.log(f"Geocode failed for '{q}': {type(e).__name__}: {e}")
                return None                                         # network trouble: don't cache
        out = {"at": time.time(), "lat": None, "lon": None}
        if res:
            r = res[0]
            out.update(lat=float(r["lat"]), lon=float(r["lon"]), display=r.get("display_name", "")[:160])
        with self.lock:
            self.cache[key] = out
            try:
                self._save()
            except OSError:
                pass
        return out if out["lat"] is not None else None

    def locate(self, address=None, place=None, town=None):
        """Best guess for a call location. Tries the most specific form first."""
        town = town if town and town.lower() not in ("cambridge",) else None
        where = f"{town}, MA" if town else "Cambridge, MA"
        tries = []
        if address:
            a = expand(address)
            if "&" in a or " at " in a.lower():                     # intersection: Nominatim can't do these,
                first = re.split(r"\s*&\s*|\s+at\s+", a, flags=re.I)  # so use the first street (approximate)
                tries.append((f"{first[0]}, {where}", True))
            else:
                tries.append((f"{a}, {where}", False))
        if place:
            tries.append((f"{expand(place)}, {where}", False))
        if address and not place and re.match(r"^\d", expand(address)):
            tries.append((re.sub(r"^\d+[A-Za-z]?\s+", "", expand(address)) + f", {where}", True))   # street only
        for q, approx in tries:
            hit = self.lookup(q)
            if hit:
                return {"lat": hit["lat"], "lon": hit["lon"], "query": q, "approx": approx,
                        "display": hit.get("display", "")}
        return None

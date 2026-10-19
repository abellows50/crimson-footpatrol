"""
Is this address a Harvard address? Decided from real records, not a hand-made list.

    python3 parcels.py --update           download the city's data + Harvard's campus map and rebuild
                                          data/cambridge-addresses.json (about 1 MB; takes a minute)
    python3 parcels.py "15 Mill St" "Mass Ave and Holyoke St"     see what the lookup decides

The build (harvard_geo.py) decides, for every Cambridge address and street corner, whether it is a Harvard address:
a Harvard building or Harvard land (City of Cambridge property records, incl. Harvard-owned rentals; Harvard's own
campus map; student-organisation buildings from data/harvard-curated.json), the same-side sidewalk in front of one
("15 Mill St"), or an intersection with a Harvard corner. At run time the dispatch monitor only does lookups here.

Address codes in the lookup file:
    C / H / S   Harvard: campus building, other Harvard-owned building, student-organisation building -> alarm
    P           possibly Harvard: partly Harvard-owned, Harvard-leased space in a shared building, a shop inside a
                club building, an organisation we're not sure about -> amber
    N           somebody else's building          R   in the street itself (e.g. the Harvard MBTA headhouse)
"""
import json
import os
import re
import sys
from difflib import get_close_matches

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "cambridge-addresses.json")
_ABBREV = {"st": "street", "ave": "avenue", "av": "avenue", "rd": "road", "dr": "drive", "pl": "place", "sq": "square",
           "mt": "mount", "ct": "court", "ter": "terrace", "pkwy": "parkway", "ln": "lane", "blvd": "boulevard",
           "hwy": "highway", "pk": "park", "mass": "massachusetts", "cir": "circle", "tpke": "turnpike",
           "lndg": "landing", "n": "north", "s": "south", "e": "east", "w": "west", "gdns": "gardens"}
SUFFIX_WORDS = {"street", "avenue", "road", "drive", "place", "square", "court", "terrace", "parkway", "lane",
                "boulevard", "highway", "way", "row", "park", "circle", "path", "walk", "plaza", "gardens", "green",
                "turnpike", "landing", "walkway"}


def _norm_street(name):
    toks = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    if len(toks) > 2 and toks[0] == "st":                # "St Paul St": a saint, not a street
        toks[0] = "saint"
    return " ".join(_ABBREV.get(t, t) if i or t != "saint" else t for i, t in enumerate(toks))


def update(log=print):
    """Download everything and rebuild the lookup file (refuses to write if a dataset looks broken)."""
    import datetime as dt
    import harvard_geo
    log("Downloading City of Cambridge open data and Harvard's campus map...")
    sources = harvard_geo.download(log=log)
    log("Working out Harvard addresses, frontage and corners...")
    tables, summary = harvard_geo.build(norm=_norm_street, log=log)
    if summary["harvard"] < 300 or summary["addresses"] < 15000:
        raise RuntimeError(f"refusing to write a suspicious table: {summary}")
    out = {"version": 3, "built": dt.datetime.now().isoformat(timespec="minutes"), "summary": summary,
           "sources": sources, **tables}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH + ".tmp", "w") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
    os.replace(PATH + ".tmp", PATH)
    log(f"wrote {PATH}: " + ", ".join(f"{k} {v}" for k, v in summary.items()))


# ---------------------------------------------------------------------------- runtime: where does a call say it is?

# Whisper's usual ways of mangling Cambridge street names (seen in the logs) -> the real street's words
MISHEARD = {
    ("on", "auburn"): ("mount", "auburn"), ("mont", "auburn"): ("mount", "auburn"), ("mound", "auburn"): ("mount", "auburn"),
    ("dumpster",): ("dunster",), ("linen",): ("linnaean",), ("linnian",): ("linnaean",), ("linnean",): ("linnaean",),
    ("linear",): ("linnaean",), ("plimpton",): ("plympton",), ("plintern",): ("plympton",), ("holyoak",): ("holyoke",),
    ("de", "wolf"): ("dewolfe",), ("de", "wolfe"): ("dewolfe",), ("to", "wolf"): ("dewolfe",), ("dewolf",): ("dewolfe",),
    ("wolf",): ("dewolfe",), ("wolfe",): ("dewolfe",),
    ("coffinthwaite",): ("cowperthwaite",), ("copperweight",): ("cowperthwaite",), ("copperwaist",): ("cowperthwaite",),
    ("copperway",): ("cowperthwaite",), ("copper", "weight"): ("cowperthwaite",), ("copper", "way"): ("cowperthwaite",),
    ("mem", "drive"): ("memorial", "drive"), ("peabody", "karis"): ("peabody", "terrace"),
    ("j", "f", "k"): ("jfk",), ("john", "f", "kennedy"): ("jfk",), ("kennedy", "street"): ("jfk", "street"),
    ("jf", "kennedy"): ("jfk",), ("j", "f", "kennedy"): ("jfk",),
    ("mass",): ("massachusetts",), ("massive",): ("massachusetts",), ("mac", "avenue"): ("massachusetts", "avenue"),
    ("rinjav",): ("rindge", "avenue"), ("rinjab",): ("rindge", "avenue"), ("ring", "jav"): ("rindge", "avenue"),
    ("range", "avenue"): ("rindge", "avenue"), ("cronkett",): ("concord",), ("cloncord",): ("concord",),
    ("concordavik",): ("concord", "avenue"), ("dantus",): ("dana",), ("here", "on", "avenue"): ("huron", "avenue"),
    ("elliott",): ("eliot",), ("elliot",): ("eliot",), ("bank", "street"): ("banks", "street"),
    ("cambridge", "park"): ("cambridgepark",), ("shepherd",): ("shepard",), ("hobbit",): ("harvard",),
    ("brogway",): ("broadway",), ("vassel",): ("vassal",), ("street", "paul"): ("saint", "paul"),
    ("1st",): ("first",), ("2nd",): ("second",), ("3rd",): ("third",), ("4th",): ("fourth",), ("5th",): ("fifth",),
    ("6th",): ("sixth",), ("hall", "worthy"): ("holworthy",),
    ("mountain", "auburn"): ("mount", "auburn"), ("cooper", "thwaite"): ("cowperthwaite",), ("holy", "oak"): ("holyoke",),
    ("linnaeus",): ("linnaean",),
}
# mishearings that are also everyday words ("a dumpster fire", "massive bleeding", "her German shepherd"): only
# with a house number before them or the street word after ("95 Dumpster Street"); "Massive and Holyoke" too
UNIT_WORDS = {"engine", "ladder", "squad", "rescue", "truck", "ambulance", "paramedic", "medic", "pro", "unit", "car"}
MISHEARD_WORDS = {"dumpster", "linen", "linear", "wolf", "wolfe", "shepherd", "elliott", "elliot", "hobbit", "massive"}
# words allowed between the two streets of an intersection ("Garden Street at Waterhouse", "Church and Mass Ave",
# "Mass Avenue on Prospect"); at least one of PAIR_WORDS must be there unless both names end in Street / Ave / ...
CONNECTORS = {"and", "at", "the", "on", "corner", "of"}
PAIR_WORDS = {"and", "at", "on", "corner"}
DIRECTIONS = {"north", "south", "east", "west"}
NOT_STREET_NEXT = {"er", "hospital", "emergency", "ed", "bay", "cemetery", "house", "hall", "health", "library",
                   "center", "centre", "station", "school", "church", "medical", "pharmacy", "rehab", "copy", "coffee",
                   "mbta", "coop", "coupe", "field", "stadium", "theater", "theatre", "gate"}
# common words that must never be fuzzy-matched to a street name (incl. Whisper's versions of "sign on and respond")
STOPWORDS = {"and", "at", "the", "on", "in", "to", "of", "for", "a", "an", "is", "it", "be", "by", "with", "from", "into",
             "near", "up", "out", "off", "over", "or", "as", "that", "this", "there", "you", "your", "we", "they", "he",
             "she", "his", "her", "him", "them", "our", "unit", "apartment", "apt", "room", "floor", "number", "suite",
             "respond", "response", "sign", "male", "female", "year", "old", "party", "person", "call", "fire", "copy",
             "going", "gonna", "inside", "outside", "front", "back", "rear", "side", "lobby", "basement", "entrance",
             "pro", "paramedic", "ambulance", "medic", "engine", "squad", "ladder", "rescue", "police", "private",
             "harvard", "cambridge", "square", "street", "avenue", "road", "center", "send", "sound", "simon", "plan",
             "counter", "time", "found", "consent", "came", "cover", "set", "sat", "quality", "found", "unseen",
             "scene", "standby", "stage", "staging", "transport", "transporting", "clear", "received", "copy",
             "between"}
# street names that are everyday words ("4 West" is a hospital floor, "south on JFK Street" a direction, "her walker
# at Shepard Street" a walker): they count only with their street word ("South Street", "Walker Street")
COMMON_WORDS = {"north", "south", "east", "west", "front", "line", "field", "water", "high", "little", "new", "state",
                "school", "common", "cross", "day", "may", "bay", "child", "museum", "university", "bank", "first",
                "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "walker", "story", "bond", "grant",
                "ash", "green", "grove", "spring", "summer", "winter", "union", "market", "pleasant", "hope", "rose",
                "pine", "elm", "oak", "hill", "wood", "lee", "long", "short", "chestnut", "cottage", "college",
                # names heard in other contexts (hospitals, towns, people)
                "andrew", "james", "howard", "tyler", "wyman", "maynard", "mclean", "arlington", "belmont", "lincoln",
                "somerville", "newton", "boston"}
HARVARD = {"C": "Harvard property", "H": "Harvard-owned building", "S": "Harvard student-organization building",
           "O": "Harvard student-organization building"}
SUFFIX_SHORT = {"street": "St", "avenue": "Ave", "road": "Rd", "drive": "Dr", "place": "Pl", "square": "Sq", "court": "Ct",
                "terrace": "Ter", "parkway": "Pkwy", "lane": "Ln", "boulevard": "Blvd", "circle": "Cir", "way": "Way"}
CORRECTION = {"correction", "sorry", "apologies", "progression", "rather", "disregard", "excuse"}


def display_street(street):
    """'mount auburn street' -> 'Mount Auburn St', 'massachusetts avenue' -> 'Mass Ave'."""
    words = street.split()
    if words[:2] == ["massachusetts", "avenue"]:
        return "Mass Ave"
    up = lambda w: w.upper() if w in ("jfk",) else w.capitalize()
    return " ".join(SUFFIX_SHORT.get(w, up(w)) if i == len(words) - 1 and i else up(w) for i, w in enumerate(words))


def pick_number(readings, corr_at=None):
    """readings: [(token index, [(number, weight)])] for every time one street was said (dispatch reads an address
    twice, often digit by digit the second time). Weighted vote; a tie goes to a longer number that extends a shorter
    one (Whisper truncates 1401 to 140), then to the later reading. Readings after a correction word win."""
    readings = [(i, c) for i, c in readings if c]
    if not readings:
        return None
    if corr_at is not None:
        after = [(i, c) for i, c in readings if i > corr_at]
        if after and len(after) < len(readings):
            readings = after
    votes, last = {}, {}
    for pos, (i, c) in enumerate(readings):
        for v, w in c:
            votes[v] = votes.get(v, 0) + w
            last[v] = pos
    best = max(votes.values())
    top = [v for v, s in votes.items() if abs(s - best) < 1e-9]
    if len(top) == 1:
        return top[0]
    for v in sorted(top, key=lambda x: -len(str(x))):
        if any(str(v).startswith(str(u)) and v != u for u in top):
            return v
    return max(top, key=lambda v: last[v])


class Locator:
    """Finds the street addresses and intersections a transcript names, and says whether each is Harvard:
    a Harvard building (city records / Harvard's campus map / student orgs), the same-side sidewalk in front of
    Harvard property, or an intersection with a Harvard corner. Pure lookups at run time; see update()."""

    def __init__(self, path=PATH):
        self.ok = False
        self.path = path
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.version = d.get("version", 1)
        self.built = d.get("built")
        self.table = d["addresses"]                          # "<num> <street>" -> [code, label]
        self.frontage = d.get("frontage", {})                # "<street>|<parity>" -> [[from, to, label], ...]
        self.frontage_possible = d.get("frontage_possible", {})
        self.spans = d.get("spans", {})                      # "<street>|<parity>" -> [lowest, highest] on that side
        self.inter = d.get("intersections", {})              # "<a>|<b>" (sorted) -> ["H" alarm | "M" amber, label]
        self.coords = d.get("coords", {})                    # "<num> <street>" -> [lat, lon] (within 1.5 km of Adams)
        self.corner_coords = d.get("corner_coords", {})      # "<a>|<b>" -> [lat, lon]
        self.place_coords = d.get("place_coords", {})        # "quincy house, stone hall" -> [lat, lon] (Harvard map)
        self.coord_nums = {}                                 # street -> sorted house numbers that have coordinates
        for k in self.coords:
            n, st = k.split(" ", 1)
            if n.isdigit():
                self.coord_nums.setdefault(st, []).append(int(n))
        for v in self.coord_nums.values():
            v.sort()
        self.streets = {}                                    # name tokens -> core tokens (name without St/Ave)
        self.canon = {}                                      # name tokens (incl. aliases) -> the city's street name
        self.by_first = {}
        # aliases without a street word ("mount auburn", "cambridge common") would make "Mount Auburn Hospital"
        # a street: only aliases that end like a street name are used
        names = [(s, s) for s in d["streets"]] + [(a, r) for a, r in d.get("street_aliases", {}).items()
                                                  if set(a.split()) & SUFFIX_WORDS]
        for name, real in names:
            toks = tuple(name.split())
            core = tuple(t for t in toks if t not in SUFFIX_WORDS) or toks
            self.streets[toks] = core
            self.canon[toks] = real
            self.by_first.setdefault(core[0], []).append(toks)
        self.real_names = set(d["streets"])
        self.firsts = sorted(self.by_first)
        self._fuzzy = {}
        self.ok = True

    # -- finding street names in text
    def _first_candidates(self, tok):
        if tok in self.by_first:
            return [tok]
        if len(tok) < 4 or tok.isdigit():
            return []
        if tok not in self._fuzzy:
            self._fuzzy[tok] = get_close_matches(tok, self.firsts, n=3, cutoff=0.8)
        return self._fuzzy[tok]

    @staticmethod
    def _word_ok(a, b):
        return a == b or (len(b) > 3 and len(a) > 3 and get_close_matches(a, [b], cutoff=0.8) != [])

    @staticmethod
    def repair(tokens):
        """Fix known Whisper mishearings of street names ("on auburn" -> "mount auburn")."""
        toks, i, out = list(tokens), 0, []
        while i < len(toks):
            for n in (3, 2, 1):
                key = tuple(toks[i:i + n])
                if n == 1 and key and key[0] in MISHEARD_WORDS:
                    nxt = toks[i + 1] if i + 1 < len(toks) else ""
                    prev = toks[i - 1] if i else ""
                    house = prev.isdigit() and (i < 2 or toks[i - 2] not in UNIT_WORDS)    # not "Ladder 1, dumpster"
                    if not (nxt in SUFFIX_WORDS or house or prev in PAIR_WORDS
                            or (key[0] == "massive" and nxt in CONNECTORS)):     # "Mass Ave and dumpster"
                        continue
                if len(key) == n and key in MISHEARD:
                    out.extend(MISHEARD[key])
                    i += n
                    break
            else:
                out.append(toks[i])
                i += 1
        return out

    def mentions(self, tokens, readings):
        """Streets named in the (repaired) tokens, left to right: [{"start", "end", "street", "readings", "suffix",
        "bare"}]. readings(tokens, i) gives the candidate house numbers said just before tokens[i].
        A name counts as a street only with a house number before it or a street word after it ("Quincy House",
        "Mount Auburn Hospital" and "Blank Street Coffee" are not), or as one half of "X and/at Y"; a misspelled name
        must also be followed by its street word ("Linnian Street")."""
        toks = tokens
        found, i = [], 0
        while i < len(toks):
            best = None
            for first in self._first_candidates(toks[i]):
                for st in self.by_first[first]:
                    core = self.streets[st]
                    w = toks[i:i + len(core)]
                    if len(w) < len(core) or not all(self._word_ok(a, b) for a, b in zip(w, core)):
                        continue
                    end = i + len(core)
                    nxt = toks[end] if end < len(toks) else ""
                    suffix = [t for t in st if t in SUFFIX_WORDS]
                    if suffix and nxt in SUFFIX_WORDS and nxt not in suffix:
                        continue                                  # "Harvard Street" is not "Harvard Square"
                    run = 0                                       # "Ash Street Place": both street words
                    while run < len(suffix) and toks[end + run:end + run + 1] == [suffix[run]]:
                        run += 1
                    exact = list(w) == list(core)
                    explicit = bool(suffix) and nxt in suffix
                    has_suffix = explicit or (not suffix and exact)          # "Broadway" needs no "Street"
                    score = (has_suffix, exact, len(core), run, st[-1:] == ("street",))
                    if best is None or score > best[0]:
                        best = (score, st, end + max(run, 1 if explicit else 0), has_suffix, exact, explicit)
            if not best:
                i += 1
                continue
            _, st, end, has_suffix, exact, explicit = best
            if (toks[i] in STOPWORDS or toks[i] in COMMON_WORDS or not exact) and not explicit \
                    and len(self.streets[st]) == 1:
                i += 1
                continue                                          # "center, respond", "4 West": need "Street" after
            nxt = toks[end] if end < len(toks) else ""
            prev = toks[i - 1] if i > 0 else ""
            if (nxt in NOT_STREET_NEXT and not has_suffix) or (has_suffix and nxt in ("coffee", "copy")):
                i += 1
                continue                                          # "Mount Auburn Hospital", "Blank Street coffee"
            real = self.canon[st]
            if prev in DIRECTIONS and f"{prev} {real}" not in self.real_names:
                i = end
                continue                                          # "North Harvard Street" is in Boston
            rd = readings(toks, i)
            if not exact and not has_suffix:
                i += 1
                continue                                          # a guessed spelling needs "Street" after it
            # "Essex 10, Mass Ave": Whisper heard "at" as a small number right after another street's name
            run = i
            while run > 0 and (toks[run - 1].isdigit()):
                run -= 1
            if rd and found and found[-1]["end"] == run and not found[-1]["readings"] and \
                    {v for v, _ in rd} <= {8, 10}:
                rd = []
                has_suffix = True
            found.append({"start": i, "end": end, "street": real, "readings": rd, "suffix": has_suffix,
                          "explicit": explicit, "bare": not rd and not has_suffix, "pre": run})
            i = end
        # a bare name only counts as one half of an intersection ("Mass and Waterhouse", "Essex and Mass Ave")
        out = []
        for k, m in enumerate(found):
            if m["bare"]:
                nb = [found[j] for j in (k - 1, k + 1) if 0 <= j < len(found)]
                if not any(not x["readings"] and x["street"] != m["street"] and self._paired(toks, x, m) for x in nb):
                    continue
            out.append(m)
        return out

    @staticmethod
    def _paired(toks, a, b):
        """Can mentions a and b be the two streets of one intersection?"""
        first, second = (a, b) if a["start"] < b["start"] else (b, a)
        between = toks[first["end"]:second.get("pre", second["start"])]
        if not between:
            return first["explicit"] and second["explicit"] or second.get("pre", second["start"]) != second["start"]
        # "Mass Ave and Holyoke", "Mass Ave, corner of Holyoke Street", "Mass Ave at the corner of Quincy Street"
        return len(between) <= 4 and all(t in CONNECTORS for t in between) and any(t in PAIR_WORDS for t in between)

    # -- verdicts
    def address(self, number, street):
        """('harvard' | 'possible' | 'not' | 'unknown', label, why) for '<number> <street>' in Cambridge."""
        code, label = (self.table.get(f"{number} {street}") or [None, None])[:2]
        where = f"{number} {display_street(street)}"
        if code in HARVARD:
            what = label if label else HARVARD[code]
            return "harvard", label, f"{where}: {what}" + ("" if not label else f" ({HARVARD[code]})")
        if code == "P":
            return "possible", label, f"{where}: possibly Harvard ({label})" if label else f"{where}: possibly Harvard"
        if code == "N":
            return "not", label, f"{where}: not Harvard property" + (f" ({label})" if label else "")
        side = f"{street}|{number % 2}"
        for a, b, lab in self.frontage.get(side, []):
            if a <= number <= b:
                return "harvard", lab, f"{where}: on the sidewalk in front of {lab}"
        for a, b, lab in self.frontage_possible.get(side, []):
            if a <= number <= b:
                return "possible", lab, f"{where}: possibly in front of {lab}"
        span = self.spans.get(side) or self.spans.get(f"{street}|{1 - number % 2}")
        if code == "R" or (span and span[0] - 2 <= number <= span[1] + 2):
            return "not", None, f"{where}: not in front of Harvard property" if code != "R" else \
                f"{where}: in the street, not a building (not in front of Harvard property)"
        return "unknown", None, f"{where}: not a known Cambridge address"

    def intersection(self, a, b):
        key = "|".join(sorted((a, b)))
        v = self.inter.get(key)
        where = f"{display_street(a)} & {display_street(b)}"
        if v:
            if v[0] == "H":
                return "harvard", v[1], f"{where}: {v[1]}"
            return "possible", v[1], f"{where}: possibly Harvard ({v[1]})"
        if a in self.real_names and b in self.real_names:
            return "not", None, f"{where}: no Harvard corner"
        return "unknown", None, f"{where}: not a known intersection"

    def locate(self, tokens, readings, crew=None):
        """Every address / intersection the text names, with a verdict:
        [{"kind", "where", "verdict" (harvard/possible/not/unknown), "label", "why", "crew"}].
        crew = {("addr", num, street) or ("inter", a, b): (treat, label)} from the crew's list, treat = harvard /
        possible / not; it beats the records."""
        if not self.ok:
            return []
        crew = crew or {}
        toks = self.repair(tokens)
        ms = self.mentions(toks, readings)
        # "1337 Mass Ave, 1337 Cambridge Street, sorry, 1337 Cambridge Street": after a correction, only what follows
        corr = max((i for i, t in enumerate(toks) if t in CORRECTION or (t == "that" and i and toks[i - 1] == "make")),
                   default=None)
        if corr is not None and any(m["readings"] and m["start"] > corr for m in ms):
            ms = [m for m in ms if m["start"] > corr or not m["readings"]]
        # ignore "... between A and B" (a block description, not where the call is)
        ms = [m for m in ms if not any(t == "between" for t in toks[max(0, m["start"] - 3):m["start"]])]
        out, used = [], set()
        for k, m in enumerate(ms):
            if k in used:
                continue
            if m["readings"]:
                # the same street said again right after (the read-back): vote on the number
                group, j = [m], k + 1
                while j < len(ms) and ms[j]["street"] == m["street"] and ms[j]["readings"]:
                    group.append(ms[j])
                    used.add(j)
                    j += 1
                out.append(self._address_of(group, corr, crew))
                # "18, Linnaean Street and Walker Street": the number may be a unit's call sign (it starts the
                # transmission), so the corner counts too
                if out[-1] and k + 1 not in used:
                    out[-1]["callsign"] = m.get("pre", m["start"]) == 0
                    nxt = ms[j] if j < len(ms) else None
                    if nxt and not nxt["readings"] and nxt["street"] != m["street"] and self._paired(toks, group[-1], nxt):
                        used.add(j)
                        out.append(self._intersection_of(m["street"], nxt["street"], crew))
            elif k + 1 < len(ms) and not ms[k + 1]["readings"] and ms[k + 1]["street"] != m["street"] \
                    and self._paired(toks, m, ms[k + 1]):
                used.add(k + 1)
                out.append(self._intersection_of(m["street"], ms[k + 1]["street"], crew))
        out = [o for o in out if o]
        # "the corner of JFK and Mount Auburn, 20 JFK Street": a house number on one of the corner's streets says
        # where the call is; if that isn't Harvard, the Harvard corner is only a "possible"
        for o in out:
            if o["kind"] == "intersection" and o["verdict"] == "harvard" and not o["crew"]:
                no = next((a for a in out if a["kind"] == "address" and a["verdict"] == "not" and not a.get("callsign")
                           and a["street"] in o["streets"]), None)
                if no:
                    o["verdict"] = "possible"
                    o["why"] += f"; but the call also gives {no['where']}, which isn't Harvard"
        return out

    def _address_of(self, group, corr, crew):
        """Verdict for one street said with a house number (maybe twice: the read-back)."""
        n = pick_number([(g["start"], g["readings"]) for g in group], corr)
        if n is None:
            return None
        st = group[0]["street"]
        where = f"{n} {display_street(st)}"
        c = crew.get(("addr", n, st))
        if c:
            return {"kind": "address", "where": where, "verdict": c[0], "label": c[1], "number": n, "street": st,
                    "why": f"{where}: on the crew's {'not-Harvard' if c[0] == 'not' else c[0].replace('possible', 'possibly-Harvard').replace('harvard', 'Harvard')} list",
                    "crew": True}
        v, label, why = self.address(n, st)
        if v == "unknown":
            # no such number on this street: "21, 60 Plympton" (call sign + number) read as 2160, "12-14 Plympton"
            # read as 1214. Try the shorter readings, then the halves of the number
            alts = sorted({x for g in group for x in g["readings"] if x[0] != n}, key=lambda x: -x[1])
            alts = [a for a, _ in alts] + ([n // 100, n % 100] if 1000 <= n <= 9999 else [])
            for a in alts:
                if a and crew.get(("addr", a, st)) is None:
                    v2, label2, why2 = self.address(a, st)
                    if v2 != "unknown":
                        n, v, label = a, v2, label2
                        why = f"{why2} (heard \"{where.split()[0]}\"; {a} is the address on {display_street(st)})"
                        where = f"{a} {display_street(st)}"
                        break
        return {"kind": "address", "where": where, "verdict": v, "label": label, "why": why, "crew": False,
                "number": n, "street": st}

    def _intersection_of(self, a, b, crew):
        where = f"{display_street(a)} & {display_street(b)}"
        c = crew.get(("inter",) + tuple(sorted((a, b))))
        if c:
            return {"kind": "intersection", "where": where, "verdict": c[0], "label": c[1],
                    "why": f"{where}: on the crew's list", "crew": True, "streets": sorted((a, b))}
        v, label, why = self.intersection(a, b)
        return {"kind": "intersection", "where": where, "verdict": v, "label": label, "why": why,
                "crew": False, "streets": sorted((a, b))}

    def point(self, tokens, readings):
        """(lat, lon, where) of the first address or street corner the text names, or None. A number the city doesn't
        list takes the position of the nearest listed number on the same street (same side if there is one)."""
        if not self.ok:
            return None
        for l in self.locate(tokens, readings):
            if l["kind"] == "intersection":
                ll = self.corner_coords.get("|".join(l["streets"]))
                if ll:
                    return (*ll, l["where"])
                continue
            n, st = l["number"], l["street"]
            if st not in self.coord_nums:
                continue
            if f"{n} {st}" in self.coords:
                return (*self.coords[f"{n} {st}"], l["where"])
            nums = self.coord_nums[st]
            near = sorted(nums, key=lambda x: (abs(x - n), x % 2 != n % 2))[:1]
            if near and abs(near[0] - n) <= 40:
                return (*self.coords[f"{near[0]} {st}"], l["where"])
        return None

    def place_point(self, name):
        """[lat, lon] of a named Harvard place ("Canaday Hall", "Quincy House", "Harvard Yard"), or None."""
        n = (name or "").lower().split(" (")[0].strip()
        if n in self.place_coords:
            return self.place_coords[n]
        if len(n) < 5:
            return None
        if n in self.place_coords:
            return self.place_coords[n]
        hits = [v for k, v in self.place_coords.items() if n in k]
        return hits[0] if hits else None

    def parse_place(self, text, tokenize, readings):
        """A crew-list address ('15 Mill St', '15-31 Mill St', 'Mill St & Plympton St') -> override keys."""
        m = re.match(r"\s*(\d+)\s*-\s*(\d+)\s+(.+)", text)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            ms = self.mentions(self.repair(tokenize(f"{lo} {m.group(3)}")), readings)
            if ms and hi - lo <= 400:
                # "1300-1316 Mass Ave" is one side of the street (as address ranges are); "1-2 Athens Ter" both
                return [("addr", n, ms[0]["street"]) for n in range(lo, hi + 1, 2 if (hi - lo) % 2 == 0 else 1)]
            return []
        parts = re.split(r"\s*(?:&|\band\b|\bat\b|/)\s*", text)
        if len(parts) == 2 and not re.match(r"\s*\d", text):
            sfx = r"\b(st|street|ave|avenue|rd|road|pl|place|sq|square|dr|drive|way|ter|terrace|ct|court|pkwy|parkway|ln|lane)\b"
            ms = [self.mentions(self.repair(tokenize(p if re.search(sfx, p, re.I) else p + " street")), readings)
                  for p in parts]
            if all(ms):
                return [("inter",) + tuple(sorted((ms[0][0]["street"], ms[1][0]["street"])))]
        ms = self.mentions(self.repair(tokenize(text)), readings)
        for x in ms:
            if x["readings"]:
                return [("addr", pick_number([(x["start"], x["readings"])]), x["street"])]
        return []


Parcels = Locator          # older name


_CACHE = {}


def load(path=PATH):
    """One shared Locator per data file (reloaded when the file changes)."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    hit = _CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    loc = Locator(path)
    _CACHE[path] = (mtime, loc)
    return loc


if __name__ == "__main__":
    if "--update" in sys.argv:
        update()
    else:
        p = Locator()
        if not p.ok:
            sys.exit("No lookup file yet: run  python3 parcels.py --update")
        from matcher import tokenize, number_readings
        for a in sys.argv[1:]:
            for l in p.locate(tokenize(a), number_readings) or [{"why": "no address or intersection found",
                                                                  "verdict": "-"}]:
                print(f"{a:40} {l['verdict']:9} {l['why']}")

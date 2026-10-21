"""Keyword / location matching tolerant of radio-transcription errors.

How a call becomes a Harvard call (most specific wins):
1. Automatic: "HUPD" in any form (H U P D, HWPD, "3HU, PD", Harvard police / PD) or "private response / private
   call / respond for Harvard". Always high, whatever else the call says, except a very short line that only repeats
   Whisper's own vocabulary hint ("HUPD private response." on a burst of static): amber.
2. A named Harvard place from the rules (Houses, Yard dorms, buildings): high.
3. A street address or intersection, decided from real records (parcels.py / harvard_geo.py): a Harvard building
   (City of Cambridge property records, Harvard's campus map, student-org buildings), the same-side sidewalk in front
   of Harvard property, or an intersection with a Harvard corner -> high; partly-Harvard / leased / lot-line cases ->
   amber. Any other Cambridge address -> the vague hints (a street name, "Harvard", "private response") are dropped.
   The crew's list (Settings > Harvard places) beats the records. Another town named -> Cambridge addresses don't apply.
4. Nothing pinned down: vague hints give an amber "possible" (medium) alert.
"""
import functools
import re
from difflib import SequenceMatcher

LEVEL_RANK = {"high": 2, "medium": 1}

_NUM_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_ABBREV = {"st": "street", "ave": "avenue", "av": "avenue", "dr": "drive", "mt": "mount",
           "rd": "road", "sq": "square", "pl": "place", "blvd": "boulevard", "ter": "terrace", "ct": "court",
           "pkwy": "parkway", "ln": "lane"}


# a word right before a house-number run that makes the first number a unit call-sign ("Ambulance 5, ..."),
# not part of the address; "Mutual 8" is mutual aid
UNIT_WORDS = {"ambulance", "engine", "squad", "ladder", "truck", "rescue", "paramedic", "medic", "pro", "als", "bls",
              "car", "unit", "room", "apartment", "apt", "floor", "channel", "division", "tower", "box", "suite",
              "department", "section", "building", "bed", "level", "mutual"}
NUMBER_CUES = {"number", "no", "num"}
HOSPITAL_NEXT = {"er", "hospital", "emergency", "ed", "e", "ambulance", "bay"}
SUFFIXES = {"street", "avenue", "road", "drive", "place", "square", "court", "terrace", "parkway", "way", "lane",
            "boulevard", "highway", "row", "park", "circle"}
# Hits that on their own say little about Harvard: a street, an address range, the bare word "Harvard"
WEAK_CATEGORIES = {"Address", "Streets", "General"}
# hits that come from an address / the crew's list / the automatic checks, not from a named place in the rules
_NOT_NAMED = WEAK_CATEGORIES | {"HUPD / private", "Harvard address", "Possible address"}
# a Harvard place named only as a landmark: "1394 Mass Ave, across from Harvard Yard"
_LANDMARK = re.compile(r"\b(?:across (?:the street )?from|opposite|facing|across the street)(?: the)?$")

# ---- automatic Harvard calls (patterns checked against ~11,800 real transcripts: no false hits)
# HUPD as Whisper writes it: hupd, hwpd, hepd, hmpd (hpd); "a H U P D" is glued by tokenize() into "ahupd"
_HUPD = re.compile(r"^[ai]?h[uwmeo]?pds?$")
# "ah, PD is on scene", "hm, PD...": a filler word, then Cambridge police
_FILLER = {"ah", "ahh", "hm", "hmm", "uh", "um", "eh", "oh", "huh"}
# "a male who PD has in custody": "who" is the word, not Whisper's spelling of HUPD
_PERSON = {"male", "female", "party", "person", "patient", "caller", "man", "woman", "someone", "one", "subject",
           "guy", "lady", "individual", "people", "student", "kid", "boy", "girl", "gentleman", "somebody", "anyone",
           "unknown"}
# "Harvard (University) police / PD" with nothing but spaces between, and not a cross street
# ("Prospect at Harvard, police on scene" is Harvard Street + Cambridge police)
HARVARD_POLICE = re.compile(r"(?i)(?<!\bat )(?<!\band )\bharvard(?:\s+university)?\s+(?:police|p\.?\s?d\.?)"
                            r"(?:\s+department)?\b")
_PRIV = (r"(?:private(?:ly)?(?: (?:response|responder|responding|call|ambulance response|ambulance|transport))?"
         r"|came in privately|called in privately|coming in privately)")
_LINK = r"(?: (?:for|from|with|by|through|thru|via|called in by))?(?: the)?"
_HARV = r"(?:harvard|harvards|hupd|hupds|h[uwmeo]pd)"
_HARV_FUZZY = r"(?:harbor|harbour|hobbit|havard|harvad|harverd)"
_NOT_NAME = (r"(?! (?:street|avenue|way|square|vanguard|pilgrim|bridge|mbta|meta|mdta|mta|station|coop|coupe|"
             r"house of pizza|hospital|book|stadium avenue)\b)")
PRIVATE_HIGH = [
    re.compile(r"\b" + _PRIV + _LINK + r" " + _HARV + r"\b" + _NOT_NAME),                 # private response for Harvard
    re.compile(r"\b" + _PRIV + r"(?: [a-z]+){0,3} (?:for|from) (?:the )?" + _HARV + r"\b" + _NOT_NAME),   # ..., it's gonna be for Harvard
    re.compile(r"\b" + _PRIV + r" (?:on behalf of|requested by|request(?:ed)? from) (?:the )?" + _HARV + r"\b" + _NOT_NAME),
    # HUPD private response ("going home to Harvard. Private call" is two sentences: checked per sentence, below)
    re.compile(r"(?<!\bto )(?<!\bat )(?<!\bin )(?<!\bnear )(?<!\bfrom )(?<!\bhome )\b" + _HARV + r" " + _PRIV + r"\b"),
    # respond for Harvard, "respond in Boston for Harvard"
    re.compile(r"\brespon(?:d|se|ding)(?: [a-z]+){0,3} for (?:the )?" + _HARV + r"\b" + _NOT_NAME),
    re.compile(r"\b(?:that|the|a) harvard response\b"),                                    # pick up that Harvard response
]
_HARV_FIRST = PRIVATE_HIGH[3]
_SENTENCES = re.compile(r"(?<=[a-z0-9])[.?!]+(?:\s+|$)", re.I)
PRIVATE_MEDIUM = [                                     # "private response. Harbor, 95 Dumpster Street": amber
    re.compile(r"\b" + _PRIV + _LINK + r" " + _HARV_FUZZY + r"\b" + _NOT_NAME),
    re.compile(r"\brespon(?:d|se|ding)(?: [a-z]+)? for " + _HARV_FUZZY + r"\b" + _NOT_NAME),
]

# ---- another town named: Cambridge records and street names don't apply ("into Arlington, 1398 Mass Ave")
TOWNS = {"watertown": "Watertown", "arlington": "Arlington", "belmont": "Belmont", "somerville": "Somerville",
         "boston": "Boston", "brighton": "Boston", "allston": "Boston", "alston": "Boston", "charlestown": "Boston",
         "brookline": "Brookline", "newton": "Newton", "medford": "Medford", "everett": "Everett",
         "lexington": "Lexington", "waltham": "Waltham", "malden": "Malden", "chelsea": "Chelsea",
         "winchester": "Winchester", "sudbury": "Sudbury", "concord": "Concord", "lincoln": "Lincoln",
         "weston": "Weston", "maynard": "Maynard", "stow": "Stow", "stowe": "Stow", "boxborough": "Boxborough",
         "acton": "Acton", "bedford": "Bedford", "burlington": "Burlington", "wellesley": "Wellesley",
         "needham": "Needham", "quincy": "Quincy", "winthrop": "Winthrop", "revere": "Revere",
         "dorchester": "Boston", "roxbury": "Boston", "mattapan": "Boston", "southend": "Boston", "northend": "Boston",
         "backbay": "Boston", "jamaicaplain": "Boston"}
_TWO_WORD_TOWNS = {("water", "town"): "watertown", ("water", "count"): "watertown", ("east", "boston"): "boston",
                   ("south", "end"): "southend", ("north", "end"): "northend", ("back", "bay"): "backbay",
                   ("jamaica", "plain"): "jamaicaplain"}
# town words that are also Cambridge streets or Harvard names: only with a preposition or "Fire / Police"
_AMBIGUOUS_TOWNS = {"concord", "lincoln", "everett", "quincy", "winthrop", "lexington", "belmont", "brookline",
                    "medford", "newton", "acton", "bedford", "boston", "charlestown",
                    "southend", "northend", "backbay"}      # "the south end of the building" isn't Boston
_TOWN_PREP = {"into", "in", "to", "of"}
# after a town word these make it a street / House / hospital name: "Quincy Street", "Winthrop House", "the Quincy
# dining hall", "Concord and Garden" (an intersection), "Mount Auburn ER"
_NOT_TOWN_NEXT = {"house", "hall", "hospital", "er", "rehab", "hills", "dining", "entry", "courtyard", "tower", "towers",
                  "library", "common", "commons", "grille", "grill", "residence", "dorm", "and", "at", "on", "corner",
                  "market"}
# "the Somerville ambulance": another town's unit helping out in Cambridge, not where the call is
_UNIT_NEXT = {"ambulance", "engine", "ladder", "ems", "squad", "medic", "rescue", "truck", "units", "fd",
              "paramedic", "paramedics"}
# "they want her to go to Mass General in Boston": where the patient is going, not where the call is
_GOING = {"go", "goes", "going", "transport", "transporting", "transported", "take", "taking", "headed", "heading",
          "bring", "bringing", "destination", "transfer", "transferring", "home"}
# town words that are also Harvard Houses: "in Winthrop, 32 Mill Street" is Winthrop House
_HOUSE_TOWNS = {"quincy", "winthrop"}
# said right after the address, these name the town: "12 Bow Street, Charlestown" (not Quincy / Concord / Everett /
# Lincoln / Winthrop: Cambridge has those streets and Houses)
_TOWN_AFTER_ADDRESS = {"boston", "charlestown", "lexington", "brookline", "newton", "medford", "belmont", "acton",
                       "bedford"}


def detect_town(tokens):
    """(town, cue) if the call names another town ("into Watertown", "mutual aid Belmont", "in Alston",
    "on the Boston side", "12 Bow Street, Charlestown"), else (None, None). "Quincy Street", "Winthrop House",
    "Concord and Garden", "the Somerville ambulance" and "going to Mass General in Boston"-style transfers don't count."""
    t2, i = [], 0
    while i < len(tokens):
        pair = tuple(tokens[i:i + 2])
        if pair in _TWO_WORD_TOWNS:
            t2.append(_TWO_WORD_TOWNS[pair])
            i += 2
        else:
            t2.append(tokens[i])
            i += 1
    # "Watertown's 75 Mount Auburn Street"
    t2 = [w[:-1] if w not in TOWNS and w.endswith("s") and w[:-1] in TOWNS else w for w in t2]
    flat = " ".join(t2)
    if re.search(r"\bmutual(?:ly)? (?:aid|8|eight|agent)\b|\bmutilated (?:into|in)\b", flat):
        return next((TOWNS[w] for w in t2 if w in TOWNS), "mutual aid"), "mutual aid"
    if re.search(r"\bon the boston side\b", flat):
        return "Boston", "on the Boston side"
    for i, w in enumerate(t2):
        if w not in TOWNS:
            continue
        nxt = t2[i + 1] if i + 1 < len(t2) else ""
        prev = t2[i - 1] if i else ""
        before2 = t2[i - 2] if i > 1 else ""
        ambiguous = w in _AMBIGUOUS_TOWNS
        if nxt in SUFFIXES or (nxt in _NOT_TOWN_NEXT and (ambiguous or nxt not in ("and", "at", "on", "corner"))) \
                or nxt == "of":
            continue                                            # Quincy Street, Winthrop House, Concord and Garden
        if prev == "to" and before2 in ("going", "home", "headed", "transporting", "transport", "back", "er", "ed",
                                        "hospital", "emergency", "transfer"):
            continue                                            # a transfer destination, not where the call is
        if prev == "in":
            back = t2[max(0, i - 7):i - 1]
            if any(x in _GOING for x in back) and not any(x.isdigit() for x in back):
                continue                                        # "go to Mass General in Boston": a destination
        if ambiguous and w in _TOWN_AFTER_ADDRESS and prev in SUFFIXES and i > 1 \
                and any(x.isdigit() for x in t2[max(0, i - 6):i - 1]):
            return TOWNS[w], f"{prev} {w}"                      # "12 Bow Street, Charlestown"
        if ambiguous and w in _TOWN_AFTER_ADDRESS and nxt.isdigit() and prev not in ("to", "of", "the"):
            return TOWNS[w], f"{w} {nxt}"                       # "Ambulance 10, Medford, 20 Oxford Street"
        if nxt in _UNIT_NEXT and prev != "into":
            continue                                            # "the Somerville ambulance" helping out
        if nxt in ("fire", "police") or prev == "into":
            return TOWNS[w], f"{prev} {w}".strip()
        if prev in _TOWN_PREP or (prev == "the" and before2 in _TOWN_PREP):
            if ambiguous and w in _HOUSE_TOWNS and nxt.isdigit():
                continue                                        # "in Winthrop, 32 Mill Street": Winthrop House
            return TOWNS[w], f"{prev} {w}".strip()
        if not ambiguous and prev not in ("going", "home"):
            return TOWNS[w], w
    return None, None


_GLUED = re.compile(r"\b(\d+)([A-Za-z]{2,})\b")
_ORDINAL = re.compile(r"^(\d+)(?:st|nd|rd|th)$")
_NOT_AFTER_ORDINAL = {"floor", "fl", "grade", "of", "and", "time", "section", "district", "bed", "unit", "level", "story"}


def tokenize(text):
    # "1.4" (mileage) must not become the street number 104: glue decimals into one non-numeric token
    text = re.sub(r"(\d)\.(\d)", r"\1d\2", text or "")
    # "6JFK Street" -> "6 JFK Street", "3HU, PD" -> "3 HU PD" (not ordinals "5th", nor unit letters "4B")
    text = _GLUED.sub(lambda m: m.group(0) if m.group(2).lower() in ("st", "nd", "rd", "th")
                      else f"{m.group(1)} {m.group(2)}", text)
    t = text.lower().replace("'", "")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    toks = t.split()
    # Collapse spelled-out letter runs: "h u p d" -> "hupd"
    out, run = [], []
    for tok in toks:
        if len(tok) == 1 and tok.isalpha():
            run.append(tok)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(_ABBREV.get(tok, tok))
    if run:
        out.append("".join(run))
    # an ordinal right before a street name is the house number: "5th Cambridge Park Drive" ("6th Street" stays)
    for i, t in enumerate(out):
        m = _ORDINAL.match(t)
        nxt = out[i + 1] if i + 1 < len(out) else ""
        if m and nxt and nxt not in SUFFIXES and nxt not in _NOT_AFTER_ORDINAL:
            out[i] = m.group(1)
    return out


@functools.lru_cache(maxsize=300_000)
def _sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _bound(a, b):
    """Upper bound on _sim(a, b) from the lengths alone (cheap: lets most comparisons be skipped)."""
    la, lb = len(a), len(b)
    return 2.0 * min(la, lb) / (la + lb) if la + lb else 1.0


# words that say what kind of place, not which one: they must be heard exactly ("severe fall" is not Sever Hall,
# "going home, all set" is not Holmes Hall)
GENERIC = {"hall", "house", "club", "center", "centre", "library", "building", "gate", "school", "court", "field",
           "theater", "theatre", "church", "chapel", "yard", "museum", "lab", "laboratory", "tower", "terrace",
           "dining", "commons", "quad", "street", "avenue", "square", "road", "drive", "place"}


def _words_agree(window, phrase_tokens):
    """Same number of words: every word must roughly match ('massachusetts tab' is not 'massachusetts hall');
    a generic word ('hall', 'house') must be the word itself (or its plural)."""
    return all(a == b or (b not in GENERIC and len(b) > 3 and _bound(a, b) >= 0.75 and _sim(a, b) >= 0.75)
               or (b in GENERIC and a == b + "s")
               for a, b in zip(window, phrase_tokens))


def _phrase_hit(tokens, phrase_tokens, need=0.0):
    """Return (score, matched_text, start, end) for the best fuzzy occurrence of phrase in tokens.
    Windows that cannot reach `need` (the caller's threshold) are skipped without the expensive comparison."""
    if not phrase_tokens:
        return 0.0, "", 0, 0
    target = " ".join(phrase_tokens)
    compact = target.replace(" ", "")
    n = len(phrase_tokens)
    first = phrase_tokens[0][:1]
    best, best_txt, best_at = 0.0, "", (0, 0)
    for size in ({n - 1, n, n + 1} if n >= 3 else {n, n + 1}):
        for i in range(0, max(0, len(tokens) - size) + 1):
            window = tokens[i:i + size]
            if len(window) < size:
                continue                         # a 1-word transcript is not a 2-word name ("Harvard." is not Harvard PD)
            if n == 1 and window[0][:1] != first:
                continue                         # one-word names must start the same: "everett" is not "leverett"
            w = " ".join(window)
            wc = w.replace(" ", "")
            if w == target or wc == compact:
                return 1.0, w, i, i + size
            floor = max(need, best)
            if size == n:
                if not _words_agree(window, phrase_tokens):
                    continue
                if max(_bound(w, target), _bound(wc, compact)) <= floor:
                    continue
                s = max(_sim(w, target), _sim(wc, compact))
            else:
                # words split / run together by the transcript ("pforz heimer"): compare without spaces, strictly,
                # and only when the letters add up ("harvard university" is not "harvard university pd")
                if abs(len(wc) - len(compact)) > max(1, len(compact) // 10) or _bound(wc, compact) < max(0.9, floor):
                    continue
                s = _sim(wc, compact)
                s = s if s >= 0.9 else 0.0
            if s > best:
                best, best_txt, best_at = s, w, (i, i + size)
    return best, best_txt, best_at[0], best_at[1]


def _threshold(phrase):
    L = len(phrase.replace(" ", ""))
    if L <= 4:
        return 1.0      # short tokens (HUPD, HKS, PBH) must match exactly
    if " " not in phrase.strip():
        return 0.9      # single words ("Leverett", "Mather") need to be nearly exact
    if L <= 7:
        return 0.88
    return 0.84


def _interpret(parts):
    """parts: [(digits, spelled)]. Read as one number by joining the digits ("13 12" -> 1312, "4 0" -> 40,
    "140 1" -> 1401), with spelled tens + ones as one part ("twenty one" -> 21, "fifteen eighty five" -> 1585)."""
    merged = []
    for p in parts:
        if merged and merged[-1][1] and p[1] and merged[-1][0] in ("20", "30", "40", "50", "60", "70", "80", "90") \
                and len(p[0]) == 1 and p[0] != "0":
            merged[-1] = (str(int(merged[-1][0]) + int(p[0])), True)
        else:
            merged.append(p)
    s = "".join(p[0] for p in merged)
    return int(s) if s.isdigit() else None


def number_readings(tokens, idx):
    """[(house number, weight)] said just before tokens[idx]: 1.0 for the whole number run, 0.3 for a shorter tail
    (a bare unit number may sit in front, or the run is too long to be a Cambridge house number).
    'number 8, Harvard Yard' -> 8;  '3, 4, Mount Auburn' -> 34;  '14 to 1, Mass Ave' -> 1401;
    'Ambulance 5, Mount Auburn' -> [];  'Paramedic 21, 240 Albany' -> 240;  '21, 575, Mount Auburn' -> 575 (tail)."""
    j = idx - 1
    if j >= 0 and tokens[j] in NUMBER_CUES:
        j -= 1
    parts, k = [], j
    while k >= 0 and len(parts) < 8:           # a house number is at most 4 digits; don't read a page of digits
        t = tokens[k]
        if t.isdigit():
            parts.insert(0, (t, False))
        elif t in _NUM_WORDS:
            parts.insert(0, (str(_NUM_WORDS[t]), t not in ("oh",)))
        elif t in ("to", "o") and parts and k > 0 and tokens[k - 1].isdigit():
            parts.insert(0, ("0", False))                       # "14 to 1" = fourteen-oh-one
        elif t == "hundred" and not parts and k > 0 and (tokens[k - 1].isdigit() or tokens[k - 1] in _NUM_WORDS):
            parts.insert(0, ("00", False))                      # "13 hundred Mass Ave" = 1300
        else:
            break
        k -= 1
    if not parts:
        return []
    before = tokens[k] if k >= 0 else ""
    if before in NUMBER_CUES and k > 0:
        before = tokens[k - 1] if tokens[k - 1] not in NUMBER_CUES else ""
        if before in UNIT_WORDS and before != "unit":
            before = ""                                         # "Paramedic number 3": keep the number
    if before in UNIT_WORDS:
        if len(parts) == 1:
            return []                                           # "Ambulance 5, Mount Auburn Street"
        parts = parts[1:]                                       # "Paramedic 21, 240 Albany Street"
    out = []
    for s in range(len(parts)):
        v = _interpret(parts[s:])
        if v is None or len(str(v)) > 4 or (s and v == 0) or any(v == o for o, _ in out):
            continue
        out.append((v, 1.0 if s == 0 else 0.3))
    return out


def _numbers_before(tokens, idx):
    """The most likely house number said just before tokens[idx], or None."""
    r = number_readings(tokens, idx)
    return max(r, key=lambda x: x[1])[0] if r else None


ADAMS_HOUSE = (42.37182, -71.116627)     # Russell Hall, 26 Plympton St: "near Harvard College" is measured from here
NEAR_MINUTES = 10
HUHS_TEXT = re.compile(r"\b(huhs|hhs|h2hs|harvard (university )?health services)\b", re.I)


def walk_minutes(lat, lon, origin=ADAMS_HOUSE):
    """Same estimate as the dashboard (geo.travel): straight line x 1.35 for real streets, at a brisk 1.35 m/s."""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (origin[0], origin[1], lat, lon))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return max(1, round(2 * 6371000 * math.asin(math.sqrt(h)) * 1.35 / 1.35 / 60))


def huhs_call(hits, text):
    """A call at Harvard University Health Services (Smith Campus Center, 75 Mount Auburn St)."""
    for h in hits or []:
        blob = " ".join(str(h.get(k) or "") for k in ("term", "note", "where")).lower()
        if "health services" in blob or "75 mount auburn st" in blob:
            return True
    return bool(HUHS_TEXT.search(text or ""))


def crew_treat(place):
    """'harvard' | 'possible' | 'not' for a crew-list entry (older entries only have harvard: true/false)."""
    t = place.get("treat")
    if t in ("harvard", "possible", "not"):
        return t
    return "harvard" if place.get("harvard") else "not"


class Matcher:
    def __init__(self, config):
        self.load(config)

    def load(self, config):
        self.rules = []
        for r in config.get("rules", []):
            for phrase in [r["term"]] + list(r.get("aliases", [])):
                toks = tokenize(phrase)
                if toks:   # optional per-rule "min_score" makes a risky name stricter ("Emerson Hall" vs "Emerson Hill")
                    self.rules.append((r, phrase, toks, max(_threshold(phrase), float(r.get("min_score") or 0))))
        # words of Whisper's vocabulary hint: a short line made only of these is probably Whisper echoing its prompt
        self.prompt_words = set(tokenize(config.get("whisper_prompt", ""))) | {str(i) for i in range(30)}
        # The crew's list (Settings > Harvard places): addresses / intersections / names it has ruled Harvard,
        # possibly Harvard or not Harvard. It beats the records. ("not_harvard_places": older name, not-Harvard only)
        import parcels
        self.locator = parcels.load()
        self.crew, self.crew_names = {}, []
        places = list(config.get("crew_places", [])) + [dict(p, harvard=False) for p in config.get("not_harvard_places", [])]
        for pl in places:                       # newest first (Settings and Analyze add at the top): the first wins
            treat = crew_treat(pl)
            for a in pl.get("addresses", []):
                if self.locator.ok:
                    for key in self.locator.parse_place(a, tokenize, number_readings):
                        self.crew.setdefault(key, (treat, pl.get("label") or a))
            names = [tokenize(n) for n in pl.get("names", []) if tokenize(n)]
            if names:
                self.crew_names.append((pl, names, treat))
        self.ranges = []
        for rg in config.get("address_ranges", []):
            for name in [rg["street"]] + list(rg.get("aliases", [])):
                toks = tokenize(name)
                if toks:
                    self.ranges.append((rg, toks))

    def match(self, text):
        """Return list of hits sorted by severity: {term, level, category, heard, score, note?}."""
        if not text:
            return []
        tokens = tokenize(text)
        import parcels
        loc = parcels.load()                    # the address table was rebuilt (parcels.py --update): use the new one
        if loc is not self.locator and loc.ok:
            self.locator = loc
        hits, street_like = {}, {}
        # 1. named places from the rules (Houses, dorms, buildings, streets, "Harvard", "private response", ...)
        for rule, phrase, ptoks, need in self.rules:
            score, heard, start, end = _phrase_hit(tokens, ptoks, need)
            if score >= need:
                before = tokens[start - 1] if start > 0 else ""
                after = tokens[end] if end < len(tokens) else ""
                if before in {w.lower() for w in rule.get("not_after", [])}:
                    continue            # "Lesley University Hall" is not University Hall
                if after in {w.lower() for w in rule.get("not_before", [])}:
                    continue            # "Harvard Street", "Harvard MBTA", "Harvard Vanguard" are not Harvard
                if heard in {w.lower() for w in rule.get("not_heard", [])}:
                    continue            # a real word close to the name: "Canada" is not Canaday, "hills" not Hilles
                if len(ptoks) == 1 and rule.get("category") not in WEAK_CATEGORIES and after in SUFFIXES:
                    continue            # a one-word building name used as a street: "Boston Hills Road" is not Hilles
                if len(ptoks) == 1 and after in ("and", "at") and self.locator.ok and \
                        any(len(self.locator.streets[st]) == 1 for st in self.locator.by_first.get(heard, [])):
                    continue            # "Greenough and Arsenal": Greenough Blvd, not Greenough Hall
                key = rule["term"]
                if key not in hits or score > hits[key]["score"]:
                    hits[key] = {"term": rule["term"], "level": rule.get("level", "high"),
                                 "category": rule.get("category", ""), "heard": heard,
                                 "score": round(score, 2)}
                    if rule.get("category") not in _NOT_NAMED:
                        if _LANDMARK.search(" ".join(tokens[max(0, start - 5):start])):
                            hits[key]["level"] = "medium"
                            hits[key]["note"] = "named as a landmark (\"across from\"), not where the call is"
                        words = self.locator.repair(heard.split()) if self.locator.ok else []
                        if words and not set(words) & GENERIC:
                            street_like[key] = set(words)
        # a Harvard name that is really a street here: "25 Holworthy Street", "Fayerweather and Holworthy",
        # "4 Lamont Ave", "Greenough Blvd", "185 Hall Worthy Street" (but "Holden Green" is the place's own name)
        if street_like:
            import parcels
            used = []                           # (words of the street's name, its street words, said as an address?)
            for m in self.locator.mentions(self.locator.repair(tokens), number_readings):
                w = m["street"].split()
                used.append((set(w), {x for x in w if x in parcels.SUFFIX_WORDS}, bool(m["readings"] or m["bare"])))
            for key, words in street_like.items():
                if hits.get(key) and any(words <= name and (strong or not sfx <= words) for name, sfx, strong in used):
                    hits.pop(key)
        # 2. addresses and intersections, from the records (or the crew's list); another town -> they don't apply
        town, _cue = detect_town(tokens)
        if self.locator.ok:
            not_here = self._locations(tokens, hits, town)
        else:
            not_here = self._legacy_ranges(tokens, hits)    # no city data file: the old hand-made address ranges
        if town:
            for k in [k for k, h in hits.items() if h["category"] in ("Address", "Streets", "Harvard address",
                                                                     "Possible address") or k == "private response"]:
                hits.pop(k)
            for h in hits.values():             # "mutual aid Belmont, the Chabad House": not the Cambridge one?
                if h["category"] not in _NOT_NAMED and h["level"] == "high":
                    h["level"], h["note"] = "medium", f"another town named ({_cue}): may not be the Harvard place"
        # 3. a place on the crew's list by name: Harvard -> high, possibly -> amber, not Harvard -> vague hints dropped
        crew_yes, crew_maybe, crew_no = self._crew_names(tokens)
        for label in crew_yes:
            hits[label] = {"term": label, "level": "high", "category": "Harvard address", "heard": label.lower(),
                           "score": 1.0, "note": "on the crew's Harvard list"}
        for label in crew_maybe:
            hits.setdefault(label, {"term": label, "level": "medium", "category": "Possible address",
                                    "heard": label.lower(), "score": 1.0, "note": "on the crew's possibly-Harvard list"})
        not_here = crew_no or not_here
        if not_here and not any(h["category"] == "Harvard address" for h in hits.values()):
            for k in [k for k, h in hits.items() if h["category"] in WEAK_CATEGORIES or k == "private response"]:
                hits.pop(k)
            # the call's own address isn't Harvard: a Harvard name in it is probably a landmark ("1384 Mass Ave,
            # opposite Widener") or a mishearing ("patient name Maher"): amber, not the loud alarm
            for h in hits.values():
                if h["category"] not in _NOT_NAMED and h["level"] == "high":
                    h["level"], h["note"] = "medium", f"the address isn't Harvard ({not_here})"
        # a busy address range on its own (legacy ranges only) is "possible" unless something else says Harvard
        strong = any(h["category"] not in WEAK_CATEGORIES for h in hits.values())
        for h in hits.values():
            alone = h.pop("alone_level", None)
            if alone and not strong and LEVEL_RANK.get(alone, 2) < LEVEL_RANK[h["level"]]:
                h["level"], h["note"] = alone, "address only: nothing else says Harvard"
        # a located address on a street makes that street's own "possible" hint redundant
        located = " ".join(h.get("where", "") for h in hits.values()
                           if h["category"] in ("Harvard address", "Possible address")).lower()
        for k in [k for k, h in hits.items() if h["category"] == "Streets"]:
            core = " ".join(w for w in tokenize(k) if w not in SUFFIXES)
            if core and core in located:
                hits.pop(k)
        # 4. automatic Harvard calls: added last, so nothing above can drop them
        for a in self._auto(text, tokens):
            if a["term"] not in hits or LEVEL_RANK[a["level"]] > LEVEL_RANK[hits[a["term"]]["level"]]:
                hits[a["term"]] = a
        # drop generic hits subsumed by a more specific one (e.g. "Harvard" when "Harvard Yard" hit)
        terms = list(hits)
        for t in terms:
            for u in terms:
                if t != u and t in hits and u in hits \
                        and (t.lower() in u.lower() or t.lower() in hits[u]["heard"]) \
                        and LEVEL_RANK[hits[u]["level"]] >= LEVEL_RANK[hits[t]["level"]]:
                    hits.pop(t, None)
        pr = hits.get("private response")
        if pr and pr["level"] == "high":
            # only something that is itself a loud alarm backs up a plain "private response" (a street name doesn't)
            others = [h for t, h in hits.items() if t != "private response" and h["level"] == "high"
                      and h.get("category") not in WEAK_CATEGORIES | {"Possible address"}]
            mit = re.search(r"\bm\.?\s?i\.?\s?t\b|albany st|mass(achusetts)? institute|vassar st|ames st", text, re.I)
            if mit or not others:
                pr["level"] = "medium"
                pr["note"] = "MIT / not Harvard?" if mit else "no HUPD or Harvard location heard"
        # 5. a very short line made of Whisper's own vocabulary hint is probably an echo on static: amber only
        if any(h["level"] == "high" for h in hits.values()) and self.looks_like_prompt_echo(tokens):
            for h in hits.values():
                if h["level"] == "high":
                    h["level"] = "medium"
                    h["note"] = (h.get("note", "") + "; very short line: Whisper may be echoing its prompt").lstrip("; ")
        for h in hits.values():
            if not_here and h["level"] == "high" and h.get("category") != "Harvard address":
                h["note"] = (h.get("note", "") + f"; address: {not_here}").lstrip("; ")
        return sorted(hits.values(), key=lambda h: (-LEVEL_RANK[h["level"]], -h["score"]))

    def place_minutes(self, hits, text):
        """Walking minutes from Adams House to where a Harvard call is: the Harvard address / corner it matched,
        else a named Harvard place, else any address in the text; None if it can't be placed (or another town)."""
        loc = self.locator
        if not loc.ok:
            return None
        toks = tokenize(text or "")
        if detect_town(toks)[0]:
            return None
        pts = [loc.point(tokenize(h["where"]), number_readings) for h in hits if h.get("where")]
        pts = [p[:2] for p in pts if p]
        if not pts:
            pts = [pp for pp in (loc.place_point(h["term"]) for h in hits if h.get("level") == "high") if pp]
        if not pts:
            pt = loc.point(toks, number_readings)
            pts = [pt[:2]] if pt else []
        return min(walk_minutes(p[0], p[1]) for p in pts) if pts else None

    def apply_alarm_filters(self, hits, text, filters):
        """The crew's alarm filters (Settings > Harvard places): a Harvard call at HUHS, or one that isn't within a
        10-minute walk of Adams House, stays on the screen as an amber "possible" alert instead of the loud alarm."""
        if not any(h["level"] == "high" for h in hits):
            return hits
        why = None
        if filters.get("no_huhs") and huhs_call(hits, text):
            why = "HUHS call (alarm filter: no loud alarm for HUHS)"
        elif filters.get("near_only"):
            mins = self.place_minutes(hits, text)
            if mins is None:
                why = "place not pinned down (alarm filter: only within a 10-min walk of Adams House)"
            elif mins > NEAR_MINUTES:
                why = f"{mins} min walk from Adams House (alarm filter: only within {NEAR_MINUTES} min)"
        if not why:
            return hits
        out = []
        for h in hits:
            h = dict(h)
            if h["level"] == "high":
                h["level"], h["note"] = "medium", (h.get("note", "") + "; " + why).lstrip("; ")
            out.append(h)
        return out

    def looks_like_prompt_echo(self, tokens):
        """4 words or fewer, or 12 or fewer that are all words of the Whisper prompt (or small numbers). Every real
        HUPD / private-for-Harvard dispatch in the logs is at least 10 words."""
        if not tokens:
            return False
        if len(tokens) <= 4:
            return True
        return len(tokens) <= 12 and all(w in self.prompt_words for w in tokens)

    def explain(self, text):
        """What the address logic saw, for "Try the matcher" and the Analyze conflicts list."""
        tokens = tokenize(text or "")
        town, cue = detect_town(tokens)
        locs = [] if town or not self.locator.ok else self.locator.locate(tokens, number_readings, self.crew)
        return {"town": town, "town_cue": cue, "locations": locs, "auto": [a["term"] for a in self._auto(text, tokens)],
                "city_data": self.locator.ok, "city_data_built": getattr(self.locator, "built", None)}

    def _auto(self, text, tokens):
        """HUPD named, or Harvard's own private-ambulance contract named: always a Harvard call."""
        out, heard = [], None
        for i in range(len(tokens)):
            for n in (1, 2, 3):
                seg = tokens[i:i + n]
                if len(seg) < n or (n > 1 and any(len(t) > 4 for t in seg)):
                    break
                if n > 1 and seg[0] in _FILLER:
                    break                                   # "ah, PD is on scene": a filler word, then Cambridge PD
                core = re.sub(r"^(?:3|thru|through)", "", "".join(seg))   # "3HU, PD" = "through HUPD"
                if _HUPD.match(core) and (n == 1 or core.lstrip("ai").startswith("h")):
                    heard = " ".join(seg)
                    break
            if heard:
                break
            if tokens[i] == "who" and tokens[i + 1:i + 2] == ["pd"] and (not i or tokens[i - 1] not in _PERSON):
                heard = "who pd"
                break
        if not heard:
            m = HARVARD_POLICE.search(text)
            heard = m.group(0).lower() if m else None
        if heard:
            out.append({"term": "HUPD", "level": "high", "category": "HUPD / private", "heard": heard,
                        "score": 1.0, "note": "HUPD named: always a Harvard call"})
        flat = " ".join(tokens)
        sentences = [" ".join(tokenize(x)) for x in _SENTENCES.split(text)]
        for pat in PRIVATE_HIGH:
            m = next((m for m in (pat.search(x) for x in (sentences if pat is _HARV_FIRST else [flat])) if m), None)
            if m:
                out.append({"term": "Private response for Harvard", "level": "high", "category": "HUPD / private",
                            "heard": m.group(0), "score": 1.0, "note": "Harvard's private call: always a Harvard call"})
                break
        else:
            for pat in PRIVATE_MEDIUM:
                m = pat.search(flat)
                if m:
                    out.append({"term": "Private response for Harvard?", "level": "medium",
                                "category": "HUPD / private", "heard": m.group(0), "score": 0.8,
                                "note": f"sounds like \"private response for Harvard\" (heard \"{m.group(0)}\")"})
                    break
        return out

    def _locations(self, tokens, hits, town):
        """Addresses and intersections, decided from the records / Harvard's map / the crew's list.
        Adds a hit per Harvard (high) or possibly-Harvard (amber) location; returns the first non-Harvard address
        when nothing is Harvard, so the vague hints can be dropped."""
        if town:
            return None
        locs = self.locator.locate(tokens, number_readings, self.crew)
        yes = [l for l in locs if l["verdict"] == "harvard"]
        maybe = [l for l in locs if l["verdict"] == "possible"]
        for l in yes + maybe:
            label = l.get("label") or ""
            named = label and not label[:1].isdigit() and label.lower() not in l["where"].lower() \
                and not label.lower().startswith(("harvard property", "harvard-owned", "possibly", "partly",
                                                  "shop in", "harvard-leased", "official address"))
            term = f"{l['where']} ({label})" if named else l["where"]
            high = l["verdict"] == "harvard"
            hits[term] = {"term": term, "level": "high" if high else "medium",
                          "category": "Harvard address" if high else "Possible address",
                          "heard": l["where"].lower(), "score": 1.0, "note": l["why"], "where": l["where"]}
        no = [l for l in locs if l["verdict"] == "not"]
        return no[0]["where"] if no and not yes and not maybe else None

    def _crew_names(self, tokens):
        yes, maybe, no = [], [], None
        for pl, names, treat in self.crew_names:
            for nt in names:
                need = _threshold(" ".join(nt))
                score, _h, _s, _e = _phrase_hit(tokens, nt, need)
                if score >= need:
                    label = pl.get("label") or " ".join(nt)
                    if treat == "harvard":
                        yes.append(label)
                    elif treat == "possible":
                        maybe.append(label)
                    else:
                        no = no or label
                    break
        return yes, maybe, no

    def _legacy_ranges(self, tokens, hits):
        """Before the city data existed: hand-made "<number> <street>" ranges from config address_ranges."""
        numbered = {}                    # street name tokens -> house numbers heard on it
        for rg, stoks in self.ranges:
            n = len(stoks)
            for i in range(len(tokens) - n + 1):
                window = tokens[i:i + n]
                exactish = window == stoks or (_sim(" ".join(window), " ".join(stoks)) >= 0.85
                                               and _words_agree(window, stoks))   # "Galen Street" is not Garden
                if not exactish:
                    continue
                nxt = tokens[i + n] if i + n < len(tokens) else ""
                if nxt in HOSPITAL_NEXT:
                    continue            # "Mount Auburn ER" / "Mount Auburn Hospital" is the hospital, not the street
                if not (set(stoks) & SUFFIXES) and nxt not in SUFFIXES and not (set(stoks) & {"ave", "st"}):
                    continue            # a bare alias like "Mt Auburn" needs "Street" after it to count as an address
                num = _numbers_before(tokens, i)
                if num is not None:
                    numbered.setdefault(rg["street"], set()).add(num)
                if num is not None and rg["from"] <= num <= rg["to"]:
                    hits[rg["label"]] = {"term": rg["label"], "level": rg.get("level", "high"),
                                         "category": "Address", "heard": f"{num} {' '.join(window)}",
                                         "score": 1.0, "alone_level": rg.get("alone_level")}
        # a street name whose house numbers are all outside its Harvard stretch: "255 Garden Street" is not the Quad
        for k in [k for k, h in hits.items() if h["category"] == "Streets"]:
            for rg, stoks in self.ranges:
                if tokenize(k) == tokenize(rg["street"]) and numbered.get(rg["street"]) and not any(
                        r["from"] <= n <= r["to"] for n in numbered[rg["street"]]
                        for r, _ in self.ranges if r["street"] == rg["street"]):
                    hits.pop(k, None)
                    break
        return None


DISPATCH_WORDS = ("respond", "responding", "medical", "ambulance", "als", "bls", "pro",
                  "engine", "rescue", "squad", "unconscious", "fall", "intox", "etoh",
                  "seizure", "chest", "breathing", "injury", "assault", "overdose")


def looks_like_dispatch(text):
    toks = set(tokenize(text))
    return any(w in toks for w in DISPATCH_WORDS)

"""
Call tracking ("the board") built from radio transcripts.

Every transcribed transmission is fed to Board.ingest(). From the noisy text it pulls out:
  * units      - Pro N / Paramedic N / Engine N / Ladder N / Rescue N / Squad N ...
  * addresses  - "425 Mass Ave", "4-1 Jackson Street" (= 41), "five Cambridge Park Drive"
  * places     - Harvard locations from the matcher, "Campion Health Center", ...
  * complaint  - fall, chest pain, intox/ETOH, motor vehicle accident, fire alarm ...
  * status     - dispatched, responding, on scene, transporting, at hospital, clear

Dispatches open incidents; later traffic updates them. Most status replies on the radio don't
say who is talking ("Transporting.", "We're clear."), so those are attached to the most recently
active incident on the same talkgroup and marked as *inferred*.

This is best-effort parsing of automatic transcripts: it will sometimes miss or mis-assign
things, and the dashboard says so.
"""
import json
import re
import threading
import uuid
from difflib import SequenceMatcher

from matcher import _NUM_WORDS
from acuity import classify_incident

STATUS_ORDER = ["dispatched", "responding", "on scene", "transporting", "at hospital", "clear"]
STATUS_LABEL = {"dispatched": "Dispatched", "responding": "Responding", "on scene": "On scene",
                "transporting": "Transporting", "at hospital": "At hospital", "clear": "Available"}

DISPATCH_MERGE_MS = 20 * 60_000     # a new dispatch to the same address within this joins the incident
SPLIT_KEYUP_MS = 45_000             # keyups this close on one talkgroup are one dispatch
THREAD_MS = 45_000                  # a unit calls in / is called, then its report comes within this
INFER_WINDOW_MS = 3 * 60_000        # an unlabelled status ("Clear with an AMA") joins a call only if the transmission
                                    # just before it on that channel, this recently, was about that same call
IDLE_CLOSE_MS = 45 * 60_000         # incidents with no traffic for this long are closed
TRANSPORT_IDLE_CLOSE_MS = 90 * 60_000
UNIT_FORGET_MS = 3 * 3600_000       # drop units from the strip after this long without traffic
CLOSED_KEEP = 25

TOWNS = ["Watertown", "Belmont", "Somerville", "Arlington", "Concord", "Weston", "Waltham", "Lexington",
         "Newton", "Boston", "Brookline", "Lincoln", "Medford", "Everett", "Wellesley", "Sudbury",
         "Wayland", "Needham", "Winchester", "Malden", "Chelsea", "Revere", "Charlestown", "Brighton", "Allston",
         "Dorchester", "Roxbury", "Jamaica Plain", "East Boston", "South Boston", "Back Bay", "Fenway"]
TOWN_GARBLES = {"charlottetown": "Charlestown", "charleston": "Charlestown", "charles town": "Charlestown",
                "brighten": "Brighton", "alston": "Allston"}
HOSPITALS = {
    "mount auburn": "Mount Auburn", "mt auburn": "Mount Auburn", "auburn hospital": "Mount Auburn",
    "cambridge hospital": "Cambridge Hospital", "cha": "Cambridge Hospital", "whidden": "CHA Everett",
    "mass general": "MGH", "mgh": "MGH", "general hospital": "MGH",
    "beth israel": "Beth Israel", "bi ": "Beth Israel", "bidmc": "Beth Israel",
    "brigham": "Brigham", "childrens": "Children's", "children's": "Children's",
    "tufts": "Tufts", "boston medical": "BMC", "bmc": "BMC", "somerville hospital": "Somerville Hospital",
    "emerson hospital": "Emerson Hospital", "newton wellesley": "Newton-Wellesley",
    "spaulding": "Spaulding", "st elizabeth": "St. Elizabeth's", "saint elizabeth": "St. Elizabeth's",
    "lahey": "Lahey", "melrose": "Melrose-Wakefield",
    "the mount": "Mount Auburn", "the general": "MGH", "the emerson": "Emerson Hospital", "the lady": "Lahey",
    "the brigham": "Brigham", "the bi": "Beth Israel", "the cambridge": "Cambridge Hospital", "the whidden": "CHA Everett",
    "st e's": "St. Elizabeth's", "the children's": "Children's",
}
COMPLAINTS = [
    # (regex, label, als?)  first match wins, so put specific ones first
    (r"motor vehicle (accident|crash|collision|access)|\bmva\b|\bmvc\b|\bmba\b|bicycl\w* (a )?struck|bike (a )?struck|car accident|vehicle accident|pedestrian struck|ped struck|struck by (a )?(car|vehicle)|bicycl\w* (accident|crash)|bike (accident|crash)", "Motor vehicle accident", False),
    (r"cardiac arrest|\bcpr\b|not breathing|\bdoa\b", "Cardiac arrest", True),
    (r"chest pain|chest pains|chest pressure", "Chest pain", True),
    (r"difficulty breathing|trouble breathing|short(ness)? of breath|\bsob\b|respiratory|physical breathing|asthma", "Difficulty breathing", True),
    (r"unconscious|unresponsive|passed out|syncop\w*|fainted|loss of consciousness", "Unconscious / syncope", True),
    (r"altered mental|\bams\b|altered (status|mental)|confus\w+", "Altered mental status", True),
    (r"seizure|seizing", "Seizure", True),
    (r"stroke|\bcva\b|facial droop|slurred speech", "Stroke", True),
    (r"overdose|\bod\b|narcan|opioid", "Overdose", True),
    (r"allergic reaction|anaphyla\w*|epi ?pen", "Allergic reaction", True),
    (r"diabet\w*|blood sugar|hypoglyc\w*", "Diabetic", True),
    (r"intox\w*|\betoh\b|e\.?t\.?o\.?h|\beth\b|\be\.t\.h\b|alcohol|drunk|intoxicated", "Intoxication (ETOH)", False),
    (r"head strike|head straight|head injury|hit (his|her|their) head", "Fall / head strike", False),
    (r"\bfall\b|\bfell\b|\bfallen\b|lift assist|farrah? paul|fair a paul|for a paul|\ba fault\b", "Fall", False),
    (r"assault|stab\w*|gunshot|shot\b|\bfight\b", "Assault / trauma", False),
    (r"abdominal pain|stomach pain", "Abdominal pain", False),
    (r"psych\w*|suicid\w*|section 12|emotional", "Psych", False),
    (r"bleeding|laceration|hemorrhag\w*", "Bleeding / laceration", False),
    (r"dizz\w*|weakness|general illness|sick person|not feeling well|vomit\w*|nausea", "Sick / dizziness", False),
    (r"\bpain\b", "Pain", False),
    (r"medical alarm|medical assist|medical call|\bmedical\b", "Medical", False),
    (r"fire alarm|for the alarm|alarm sounding|box alarm|\balarm\b|odor of (smoke|gas)|smoke|carbon monoxide|\bco detector|gas leak|\bfire\b", "Fire alarm / fire", False),
    (r"elevator", "Elevator", False),
    (r"\btransfer\b|interfacility", "Transfer", False),
]
STREET_SUFFIX = (r"Street|St|Avenue|Ave|Av|Road|Rd|Drive|Dr|Place|Pl|Square|Sq|Turnpike|Tpke|Way|Lane|Ln|"
                 r"Court|Ct|Terrace|Ter|Boulevard|Blvd|Parkway|Pkwy|Highway|Hwy|Circle|Park|Row|Plaza|Wharf|Mall|Yard")
PLACE_SUFFIX = (r"House|Hall|Center|Centre|Building|School|Station|Library|Church|Cinema|Hospital|Hotel|Inn|"
                r"Museum|Theater|Theatre|Field|Stadium|Market|Garage|Infirmary|Tower|Towers|Apartments|Complex|Gym")
STOP = {"the", "to", "a", "an", "and", "of", "for", "on", "in", "at", "respond", "responding", "sign", "with",
        "is", "it", "be", "going", "that", "this", "we", "were", "are", "you", "your", "from", "off", "by",
        "unit", "room", "floor", "channel", "year", "old", "female", "male", "party"}

_PARAMEDIC_GARBLES = {"pyramid", "aromatic", "pharmatic", "permanic", "paramount", "permanente", "parametic",
                      "paramedics", "paramedical", "parametric", "pharmacy", "paramed", "para", "medic",
                      "paramedic", "pardon my", "paramedic's", "flautical", "fairbanks"}
_AMBULANCE_GARBLES = {"ambulance", "ambulatic", "ambulances", "amb", "annual", "ambient", "ambulant", "williams"}
_FIRE_TYPES = {"engine": "Engine", "ladder": "Ladder", "truck": "Ladder", "rescue": "Rescue", "squad": "Squad",
               "tower": "Tower", "car": "Car", "deputy": "Deputy", "division": "Division", "battalion": "Battalion"}

STATUS_PATTERNS = [
    # (regex, status)  checked in order; the first that matches is the transmission's status
    (r"\bcancel\w*|\bdisregard\b|no ems (required|needed)|\bclear (with )?no ems\b", "clear"),
    (r"\bwaiting (on|for) (the )?nurse\b|you'?re at the (mount|general|emerson|brigham|hospital)\b|"
     r"\bat (the )?(hospital|mount auburn|cambridge hospital|mgh|brigham|beth israel)|\boff at\b|arriv\w* at (mount auburn|the hospital|cambridge hospital|mgh)|waiting for a (room|bed)|\bheavy delay\b", "at hospital"),
    (r"\btransport\w*|\bpre-?transport\w*|en route to (mount auburn|cambridge hospital|mgh|the hospital|brigham|beth israel)|going to (mount auburn|cambridge hospital|mgh)|"
     r"\btaking (one|1|a|two|2) patients?\b|\b(als|bls) to (the )?(mount|general|emerson|lady|lahey|brigham|bi|cambridge|whidden|mgh|children)", "transporting"),
    (r"\bon (location|scene)\b|\bon-?scene\b|\barrived\b|\bwe're here\b|\bon arrival\b", "on scene"),
    (r"\bclear\w*\b|\bavailable\b|\bin service\b|\bback in quarters\b|\bin quarters\b|\breturning to (quarters|base)\b", "clear"),
    (r"\bresponding\b|\ben ?route\b|\banswering\b|show (us|me) responding|\bon (our|the) way\b", "responding"),
]
DISPATCH_RE = re.compile(
    r"\bsign(ed)?[\s,-]*(on|un|in)\b|\bsign on\b|\brespond\b(?!ing)|\bresponds? to\b|\bprivate response\b|"
    r"\bpick up (that|the|this) (\w+ )?(call|response)\b|\bstage for police\b|\brespondent\b|"
    r"\bfor (the|an?) (alarm|medical|fire)\b|\byou'?re (going to be )?responding\b|\byou are responding\b|"
    r"\bfor (the|an?) (?:[\w'-]+ ){1,3}(fire alarm|alarm|medical|fire|odor|investigation)\b|"
    r"\bfor (the|an?) (elevator|odor|smoke|investigation|lockout|water problem|wires? down|gas leak)\b", re.I)


WEAK_DISPATCH_RE = re.compile(
    r"\b(it'?s|this is|that'?s) going to be\b|\bgoing to be (for|a|an|at|on|in)\b|\breported\b|\bcoming in for\b|"
    r"\btake one\b|\bi have (one|a call)\b|\bwe'?re getting a call\b|\bcall (at|on|in|for)\b|\bfor (a|an) \d", re.I)

_ST = r"(?:[A-Z][A-Za-z']+\s+){1,2}(?:%s)\b" % STREET_SUFFIX
_INTERSECTION_RE = re.compile(r"(%s)\.?\s*(?:at|and|&|by|near|,)\s+(%s)" % (_ST, _ST))


def extract_intersection(text):
    m = _INTERSECTION_RE.search(text)
    if not m:
        return None
    a, b = (re.sub(r"\s+", " ", x).strip(" .") for x in m.groups())
    if any(w.lower() in STOP for w in (a.split()[0], b.split()[0])):
        return None
    label = f"{a} & {b}"
    return label, " & ".join(sorted([a.lower(), b.lower()]))


def _now_ms():
    import time
    return int(time.time() * 1000)


def _sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _num(tok):
    tok = tok.lower().strip(".,")
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


# ------------------------------------------------------------------------------------------ parsing

def extract_units(text, talkgroup_name=""):
    """Return canonical unit ids mentioned in a transmission, in order, e.g. ['Pro 15', 'Engine 6']."""
    t = text.replace("’", "'")
    toks = re.findall(r"[A-Za-z']+|\d+", t)
    low = [x.lower() for x in toks]
    units = []

    def add(u):
        if u not in units:
            units.append(u)

    i = 0
    while i < len(low) - 1:
        w, nxt = low[i], low[i + 1]
        n = _num(nxt)
        # "pardon my 17"
        if w == "pardon" and i + 2 < len(low) and low[i + 1] == "my" and _num(low[i + 2]) is not None:
            add(f"Paramedic {_num(low[i + 2])}")
            i += 3
            continue
        if n is not None and 0 < n < 100:
            if w in _FIRE_TYPES:
                add(f"{_FIRE_TYPES[w]} {n}")
            elif w in _PARAMEDIC_GARBLES or (len(w) >= 6 and _sim(w, "paramedic") >= 0.72):
                add(f"Paramedic {n}")
            elif w in ("pro", "pros") or w in _AMBULANCE_GARBLES or (len(w) >= 6 and _sim(w, "ambulance") >= 0.75):
                add(f"Pro {n}")
            elif w in ("als", "bls") and i == 0:
                add(f"{w.upper()} {n}")
            elif w == "mit":                               # MIT EMS ambulances: "MIT 8"
                add(f"MIT {n}")
        i += 1
    # "ALS2" / "Squad3" written as one token
    for m in re.finditer(r"\b(engine|ladder|squad|rescue|als|bls)(\d{1,2})\b", t, re.I):
        k = m.group(1).lower()
        add(f"{_FIRE_TYPES.get(k, k.upper())} {int(m.group(2))}" if k in _FIRE_TYPES else f"{k.upper()} {int(m.group(2))}")
    pro_tg = "pro" in talkgroup_name.lower()
    # Pro calls ambulances by bare number at the start of a dispatch: "15, sign on and respond ..."
    # or with a garbled first word: "Annual 4. Sign on to respond", "Fairbanks 5, sign un-responded"
    m = re.match(r"^\W*(?:[A-Za-z']+[\s,]+){0,2}?(\d{1,2})\b[\s,.]*(?:sign|respond|private response|take one|this is going|it'?s going|you'?ve been|you can cancel)", t, re.I)
    if m and pro_tg and not any(u.endswith(f" {int(m.group(1))}") for u in units):
        add(f"Pro {int(m.group(1))}")
    # "17, 21, and 6 are all clear"
    m = re.match(r"^\W*((?:\d{1,2}\W+(?:and\W+)?){1,5})(?:are|is)?\s*(?:all\s+)?clear", t, re.I)
    if m and pro_tg:
        for n in re.findall(r"\d{1,2}", m.group(1)):
            add(f"Pro {int(n)}")
    return units


_ADDR_RE = re.compile(
    r"(?<![\w-])(\d{1,5}(?:\s*-\s*\d{1,3})?|(?:%s))[\s,]+((?:[A-Z][A-Za-z'.]*\s+){0,2}?)(%s)\b\.?" % (
        "|".join(k for k in _NUM_WORDS if _NUM_WORDS[k] < 20), STREET_SUFFIX),
    re.I)


def extract_address(text):
    """'425, Mass Ave.' -> ('425 Mass Ave', '425 mass ave'); '4-1 Jackson Street' -> 41 Jackson Street."""
    best = None
    for m in _ADDR_RE.finditer(text):
        raw_num, name, suffix = m.group(1), m.group(2).strip(), m.group(3)
        if not name:
            continue
        words = name.split()
        if any(w.lower().strip(".,") in STOP for w in words):
            continue
        if raw_num.lower() in _NUM_WORDS:
            num = _NUM_WORDS[raw_num.lower()]
        else:
            parts = [p for p in re.split(r"\s*-\s*", raw_num) if p]
            # radio style digit-by-digit: "4-1" -> 41, "1-4" -> 14; but "13-21" is a unit range -> 13
            num = int("".join(parts)) if all(len(p) == 1 for p in parts) else int(parts[0])
        if num == 0 or num > 9999:
            continue
        # skip things like "Engine 6, Squad 2" caught as a number + word
        prev = text[max(0, m.start() - 12):m.start()].lower()
        if re.search(r"(engine|squad|ladder|truck|rescue|pro|paramedic|medic|channel|unit|room|ambulance)\W*$", prev):
            continue
        suf = {"st": "Street", "ave": "Ave", "av": "Ave", "avenue": "Ave", "rd": "Road", "dr": "Drive",
               "pl": "Place", "sq": "Square", "tpke": "Turnpike", "ln": "Lane", "ct": "Court", "ter": "Terrace",
               "blvd": "Boulevard", "pkwy": "Parkway", "hwy": "Highway"}.get(suffix.lower().rstrip("."), suffix.title())
        nice_name = " ".join(w.strip(".,").capitalize() if w.islower() else w.strip(".,") for w in words)
        # "Cambridge Park Drive": the regex stopped at "Park"; take the real suffix that follows
        more = re.match(r"\s+(%s)\b" % STREET_SUFFIX, text[m.end():], re.I)
        if more and suffix.lower() in ("park", "square", "yard", "row", "place", "way"):
            nice_name, suf = f"{nice_name} {suffix.title()}", more.group(1).title()
        label = f"{num} {nice_name} {suf}"
        key = f"{num} {nice_name.lower()} {suf.lower()}"
        if best is None:
            best = (label, key)
    if best is None:
        # streets without a suffix word: "373, Broadway"
        m = re.search(r"\b(\d{1,4})[,\s]+(Broadway|Concord Turnpike|Fresh Pond|Alewife Brook)\b", text)
        if m and not re.search(r"(engine|squad|ladder|truck|rescue|pro|paramedic|medic|channel|unit|room|ambulance)\W*$",
                               text[max(0, m.start() - 12):m.start()].lower()):
            best = (f"{int(m.group(1))} {m.group(2)}", f"{int(m.group(1))} {m.group(2).lower()}")
    return best


def extract_place(text):
    m = re.search(r"((?:[A-Z][A-Za-z'.]*\s+){1,3}(?:%s))\b" % PLACE_SUFFIX, text)   # "Central Square T Station"
    if not m:
        return None
    words = [w for w in m.group(1).split() if w.lower() not in STOP]
    if len(words) < 2:
        return None
    return " ".join(words)


def extract_town(text):
    # a town name, but not when it's part of a street ("Concord Road", "Belmont Street")
    for g, real in TOWN_GARBLES.items():
        text = re.sub(r"\b%s\b" % re.escape(g), real, text, flags=re.I)
    rx = r"(?<!Newton[\s-])\b(%s)\b(?![\s,-]+(?:%s|Wellesley|Hospital|Medical)\b)" % ("|".join(TOWNS), STREET_SUFFIX)
    m = re.search(rx, text, re.I)
    return m.group(1).title() if m else None


def extract_complaint(text):
    low = text.lower()
    for rx, label, als in COMPLAINTS:
        if re.search(rx, low):
            return label, als
    return None, False


TRANSPORT_LEVEL_RE = re.compile(
    r"\b(ALS|BLS)\b(?=[^.]{0,40}\b(to|transport\w*|going|en route|into|out)\b)|"
    r"\b(transport\w*|taking (one|1|a|two|2) patients?)\b[^.]{0,30}\b(ALS|BLS)\b", re.I)


def extract_transport_level(text):
    """'We are taking one patient, BLS to the General' -> 'BLS' (not the dispatch's 'ALS, sign on and respond')."""
    if re.search(r"sign (on|in)|\brespond\b", text, re.I):
        return None
    m = TRANSPORT_LEVEL_RE.search(text)
    if not m:
        return None
    return (m.group(1) or m.group(5) or "").upper() or None


def extract_status(text):
    low = text.lower()
    for rx, st in STATUS_PATTERNS:
        if re.search(rx, low):
            return st
    return None


def extract_hospital(text):
    low = re.sub(r"mount auburn (street|st)\b|mt\.? auburn (street|st)\b", " ", text.lower())   # the street, not the hospital
    for k, v in HOSPITALS.items():
        if re.search(r"\b" + re.escape(k.strip()) + r"\b", low):
            return v
    return None


def extract_age(text):
    m = re.search(r"\b(\d{1,3})[\s-]*(?:year|yr)s?[\s-]*old\b", text, re.I)
    if m and 0 < int(m.group(1)) < 110:
        sex = re.search(r"\b(female|woman|girl|male|man|boy)\b", text, re.I)
        s = ""
        if sex:
            s = " F" if sex.group(1).lower() in ("female", "woman", "girl") else " M"
        return f"{int(m.group(1))}{s}"
    return None


# Pro dispatch telling the crew the fire department is also going: "Call on the fire", "call on the Arlington
# Fire", "all on with fire", "Call Ambulance, fire". Not a fire call, and a sign of a dispatch.
FIRE_NOTIFY_RE = re.compile(
    r"\b(?:call(?:ed|ing)?|all)\s+(?:on|in|out)(?:\s+with)?\s+(?:the\s+)?(?:[A-Z]\w+\s+)?fire(?:\s+department)?\b|"
    r"\bcall ambulance,?\s*fire\b|\bon with (?:the )?fire\b", re.I)
_CALLSIGN_RE = re.compile(
    r"(\bto\s+|^\W*|,\s*)fire\s*alarm\b(?!\s+(?:sounding|activation|going off|at\b))|"
    r"\bfire\s*alarm\s*(?:,\s*)?(?:answering|go ahead|copies|copy|received)\b", re.I)


def normalize(text):
    """Undo common transcript run-togethers before parsing."""
    # "truck 3300 Franklin Street" = "truck 3, 300 Franklin Street" (Cambridge units are single digits)
    text = re.sub(r"\b(engine|truck|ladder|squad|rescue)\s+(\d)(\d{2,4})(?=[\s,]+[A-Z])", r"\1 \2, \3", text, flags=re.I)
    # MIT's ambulance "MIT 8" is often heard as "M-I-T-A" / "MITA" / "M.I.T. A" ("eight" ~ "A")
    text = re.sub(r"\bM[\s.-]*I[\s.-]*T[\s.,-]*A\b|\bMITA\b", "MIT 8", text)
    return text


def parse(text, talkgroup_name=""):
    text = normalize(text)
    # "Squad 2 to Fire Alarm" / "Fire Alarm, answering": that's Cambridge fire dispatch's call sign,
    # not a fire alarm. Blank it out for complaint/dispatch detection only.
    no_callsign = FIRE_NOTIFY_RE.sub(" ", _CALLSIGN_RE.sub(" ", text))
    units = extract_units(text, talkgroup_name)
    addr = extract_address(text) or extract_intersection(text)
    complaint, als = extract_complaint(no_callsign)
    lvl = "ALS" if re.search(r"\bals\b", text, re.I) else ("BLS" if re.search(r"\bbls\b", text, re.I) else None)
    hospital = extract_hospital(text)
    weak = bool(WEAK_DISPATCH_RE.search(no_callsign))
    if not complaint and hospital and weak and re.search(r"\b(going to|to|for)\b", text, re.I):
        complaint = "Transfer"                     # "21, take one ... going to Newton Wellesley"
    return {
        "units": units,
        "address": addr[0] if addr else None,
        "address_key": addr[1] if addr else None,
        "place": extract_place(text),
        "town": extract_town(text),
        "complaint": complaint,
        "als": als,
        "level": lvl,
        "age": extract_age(text),
        "status": extract_status(text),
        "hospital": hospital,
        "dispatch": bool(DISPATCH_RE.search(no_callsign)),
        "fire_notify": bool(FIRE_NOTIFY_RE.search(text)),
        "weak_dispatch": weak,
    }


# ------------------------------------------------------------------------------------------ the board

# ------------------------------------------------------------------------------------------ EMS vs fire
FIRE_COMPLAINTS = {"Fire alarm / fire", "Elevator"}
_COMPLAINT_LABELS = {label for _, label, _ in COMPLAINTS}
EMS_UNIT_PREFIX = ("Pro ", "Paramedic ", "ALS ", "BLS ", "MIT ")
# an actual fire, as opposed to an alarm activation / odor / smoke detector / elevator
REAL_FIRE_RE = re.compile(
    r"\bworking fire\b|\bstructure fire\b|\b(smoke|fire|flames?) showing\b|\bvisible (fire|flames?)\b|\bflames?\b|"
    r"\b(car|vehicle|auto|brush|grass|dumpster|rubbish|trash|outside|kitchen|stove|apartment|building|house|room|"
    r"electrical|mattress|roof|attic|basement)\s+fire\b|\bfire in the\b|\bon fire\b|\bsmoke (in|from|coming)\b|"
    r"\bheavy smoke\b|\bsecond alarm\b|\b2nd alarm\b|\bthird alarm\b|\b3rd alarm\b|\bentrapment\b", re.I)


def call_kind(complaint, units, texts):
    """'ems', 'fire' (alarm, odor, elevator...), 'fire_maybe' (fire units, no reason heard yet) or 'fire_real' (an actual fire)."""
    units = list(units or [])
    if complaint and complaint not in _COMPLAINT_LABELS:     # free-text complaint (e.g. from the AI reader)
        complaint = extract_complaint(complaint)[0] or complaint
    has_ems_unit = any(u.startswith(EMS_UNIT_PREFIX) for u in units)
    if complaint and complaint not in FIRE_COMPLAINTS:
        return "ems"
    real = any(REAL_FIRE_RE.search(t or "") for t in texts)
    if not complaint:
        if has_ems_unit or not units:
            return "ems"                     # unknown: keep it visible rather than risk hiding a medical call
        return "fire_real" if real else "fire_maybe"   # only fire apparatus, no reason heard yet
    if real:
        return "fire_real"
    # Pro ambulances aren't sent to plain fire alarms: an ambulance on the call means it's medical
    return "ems" if has_ems_unit else "fire"


_ADDR_SUBS = [(r"\bmassachusetts\b", "mass"), (r"\bavenue\b|\bav\b", "ave"), (r"\bstreet\b", "st"),
              (r"\broad\b", "rd"), (r"\bdrive\b", "dr"), (r"\bplace\b", "pl"), (r"\bsquare\b", "sq"),
              (r"\bcourt\b", "ct"), (r"\bterrace\b", "ter"), (r"\bparkway\b", "pkwy"), (r"\bboulevard\b", "blvd"),
              (r"\bmount\b", "mt"), (r"\bsaint\b", "st")]


def norm_addr(s):
    """'1400 Massachusetts Avenue, Unit 3, Cambridge' -> '1400 mass ave'; intersections are order-independent."""
    if not s:
        return None
    t = s.lower()
    t = re.split(r",|\bunit\b|\bapt\b|\bapartment\b|\broom\b|\bfloor\b|#|\(", t)[0]
    for rx, sub in _ADDR_SUBS:
        t = re.sub(rx, sub, t)
    t = re.sub(r"\bcambridge\b|\bma\b", " ", t)
    parts = [" ".join(re.sub(r"[^a-z0-9 ]", " ", x).split()) for x in re.split(r"\s*(?:&|\band\b|\bat\b|/)\s*", t)]
    parts = [x for x in parts if x]
    if not parts:
        return None
    return " & ".join(sorted(parts)) if len(parts) > 1 else parts[0]


def norm_place(s):
    if not s:
        return None
    t = re.sub(r"\(.*?\)", " ", s.lower())
    t = " ".join(w for w in re.sub(r"[^a-z0-9 ]", " ", t).split() if w not in ("the", "of", "at", "cambridge"))
    return t if len(t) >= 5 else None


MERGE_WINDOW_MS = 30 * 60_000


class Board:
    def __init__(self, harvard_categories_exclude=("HUPD / private", "General")):
        self.lock = threading.Lock()
        self.incidents = {}          # id -> incident (open)
        self.closed = []             # most recent last
        self.aliases = {}            # merged-away id -> surviving id
        self.merges = []             # (src, dst, why) not yet handled by the server
        self.units = {}              # unit id -> unit state
        self.last_by_tg = {}         # talkgroup -> (time, incident id)
        self.thread = {}             # talkgroup -> (time, unit): who the conversation on that channel is with
        self.last_tx_by_tg = {}      # talkgroup -> (time, incident id or None) of the last transmission heard there
        self.last_units_by_tg = {}   # talkgroup -> (time, units named), e.g. "MIT 8, go ahead"
        self.exclude_cats = set(harvard_categories_exclude)
        self.fixes = {}              # audio_url -> {"action": "remove"|"move", "to": incident id}: crew corrections

    # -- helpers
    def _new_incident(self, rec, p):
        # id derived from the first transmission, so rebuilding the board after a restart gives the same ids
        iid = "i" + (str(rec.get("id") or "")[-9:] or uuid.uuid4().hex[:9])
        inc = {"id": iid, "opened": rec["time"], "last": rec["time"], "closed": None,
               "times": {"dispatched": rec["time"]},
               "address": p["address"], "address_key": p["address_key"], "place": p["place"], "town": p["town"],
               "complaint": p["complaint"], "level": p["level"] or ("ALS" if p["als"] else None), "age": p["age"],
               "hospital": None, "status": "dispatched", "units": {}, "harvard": None, "harvard_terms": [],
               "talkgroups": [rec["talkgroup_name"]], "timeline": []}
        self.incidents[inc["id"]] = inc
        return inc

    def _key(self, inc):
        return inc["address_key"] or (inc["place"] or "").lower() or None

    def _find_by_location(self, p, t):
        k = p["address_key"] or (p["place"] or "").lower() or None
        hk = None
        for inc in self.incidents.values():
            if t - inc["last"] > DISPATCH_MERGE_MS:
                continue
            if k and (k == inc["address_key"] or k == (inc["place"] or "").lower()):
                if p["address_key"] and inc["address_key"] and p["address_key"] != inc["address_key"]:
                    a, b = p["address_key"].split(" ", 1), inc["address_key"].split(" ", 1)
                    if not (a[0] == b[0] and _sim(a[1], b[1]) > 0.75):
                        continue                                  # same place word, different address: different call
                return inc
            if p["address_key"] and inc["address_key"]:
                a, b = p["address_key"].split(" ", 1), inc["address_key"].split(" ", 1)
                if a[0] == b[0] and _sim(a[1], b[1]) > 0.75:      # same number, near-same street
                    return inc
        return hk

    _BARE_ACK = re.compile(r"^\W*(?:(?:go ahead|go|very good|okay|ok|good|all right|alright|thank you)[\s,]*)?"
                           r"(\d{1,2})\b[\s,.!]*(?:go ahead|go|pro|pro base|calling|answering)?\W*$", re.I)

    def _bare_unit(self, text, tg):
        """Pro calls ambulances by number: "17, go ahead" / "Go ahead, 17" -> the unit 17 (whichever kind we know)."""
        if not re.search(r"pro|intercept", tg, re.I):
            return None
        m = self._BARE_ACK.match(text)
        if not m or not 0 < int(m.group(1)) < 40:
            return None
        n = int(m.group(1))
        for fam in ("Paramedic", "Pro", "ALS", "BLS", "MIT"):
            if f"{fam} {n}" in self.units:
                return f"{fam} {n}"
        return f"Pro {n}"

    def _same_unit(self, u, pool=None):
        """'Ambulance 11' (Pro 11) and 'Paramedic 11' are often the same truck said two ways."""
        pool = self.units if pool is None else pool
        if u in pool:
            return u
        m = re.match(r"(Pro|Paramedic) (\d+)$", u)
        if m:
            other = ("Paramedic " if m.group(1) == "Pro" else "Pro ") + m.group(2)
            if other in pool:
                return other
        return u

    def _find_by_unit(self, units, t):
        for u in units:
            u = self._same_unit(u)
            st = self.units.get(u)
            if st and st.get("incident") in self.incidents and t - st["last"] < 4 * 3600_000:
                return self.incidents[st["incident"]]
        return None

    def _set_unit(self, unit, status, inc, t, tg, inferred=False):
        st = self.units.setdefault(unit, {"unit": unit, "status": None, "incident": None, "last": t,
                                          "talkgroup": tg, "since": t})
        if st["status"] != status:
            st["since"] = t
        st.update(status=status, last=t, talkgroup=tg, inferred=inferred)
        if inc is not None:
            # first time the call reached each status, overall and for the ambulance (Pro / Paramedic)
            inc.setdefault("times", {}).setdefault(status, t)
            if unit.startswith(("Pro ", "Paramedic ", "ALS ", "BLS ")):
                inc["times"].setdefault("ems_" + status.replace(" ", "_"), t)
            st["incident"] = inc["id"] if status != "clear" else None
            u = inc["units"].setdefault(unit, {"unit": unit, "status": status, "since": t})
            if u["status"] != status:
                u["since"] = t
            u["status"], u["inferred"] = status, inferred
        elif status == "clear":
            st["incident"] = None

    def _update_incident_status(self, inc):
        sts = [u["status"] for u in inc["units"].values()]
        live = [s for s in sts if s != "clear"]
        if sts and not live:
            inc["status"] = "clear"
            inc.setdefault("times", {}).setdefault("all_clear", max(u["since"] for u in inc["units"].values()))
        elif live:
            inc["status"] = max(live, key=STATUS_ORDER.index)

    def _close(self, inc, t, why):
        inc["closed"], inc["close_reason"] = t, why
        for u in inc["units"].values():
            st = self.units.get(u["unit"])
            if st and st.get("incident") == inc["id"]:
                st["incident"] = None
        self.incidents.pop(inc["id"], None)
        self.closed.append(inc)
        del self.closed[:-CLOSED_KEEP]

    def _housekeep(self, t):
        for inc in list(self.incidents.values()):
            idle = t - inc["last"]
            limit = TRANSPORT_IDLE_CLOSE_MS if inc["status"] in ("transporting", "at hospital") else IDLE_CLOSE_MS
            if inc["status"] == "clear" and idle > 3 * 60_000:
                self._close(inc, t, "all units clear")
            elif idle > limit:
                self._close(inc, t, "no traffic")
        for u, st in list(self.units.items()):
            if t - st["last"] > UNIT_FORGET_MS:
                self.units.pop(u)

    def _apply_hits(self, inc, rec):
        for h in rec.get("hits") or []:
            if h.get("suspect"):
                continue
            if h["term"] not in inc["harvard_terms"]:
                inc["harvard_terms"].append(h["term"])
            if h["level"] == "high" or inc["harvard"] is None:
                inc["harvard"] = "high" if h["level"] == "high" else (inc["harvard"] or "medium")

    def _fill(self, inc, p):
        for k in ("address", "address_key", "place", "town", "complaint", "age"):
            if p.get(k) and not inc.get(k):
                inc[k] = p[k]
        if p.get("hospital") and p.get("complaint") == "Transfer" and not inc.get("hospital"):
            inc["hospital"] = p["hospital"]
        if p.get("level"):
            inc["level"] = p["level"]
        elif p.get("als") and not inc.get("level"):
            inc["level"] = "ALS"

    # -- main entry
    def ingest(self, rec):
        """Feed one processed transmission (the record Monitor.process builds). Returns True if the board changed."""
        text = (rec.get("text") or "").strip()
        if not text or rec.get("suspect"):
            return False
        t, tg = rec["time"], rec["talkgroup_name"]
        p = parse(text, tg)
        # Who is talking? A unit calls in or is called ("Pro base, paramedic 9" / "17, go ahead") and the next
        # keyups on that channel are that unit's report, usually without its name.
        if not p["units"]:
            b = self._bare_unit(text, tg)
            if b:
                p = dict(p, units=[b])
        implied = None
        if not p["units"]:
            th = self.thread.get(tg)
            if th and t - th[0] <= THREAD_MS:
                implied = th[1]
        if len(p["units"]) == 1:
            self.thread[tg] = (t, p["units"][0])
        elif len(p["units"]) > 1:
            self.thread.pop(tg, None)
        elif implied:
            self.thread[tg] = (t, implied)      # the same conversation continues
        if p["units"]:
            self.last_units_by_tg[tg] = (t, p["units"])
        fix = self.fixes.get(rec.get("audio_url"))
        if fix and fix.get("action") == "remove":
            return False                          # the crew said this transmission isn't part of the call it joined
        with self.lock:
            self._housekeep(t)
            inc, inferred = None, False
            prev = self.last_by_tg.get(tg)
            prev_inc = self.incidents.get(prev[1]) if prev else None
            prev_tx = self.last_tx_by_tg.get(tg)
            self.last_tx_by_tg[tg] = (t, None)         # updated below if this transmission joins a call

            has_loc = bool(p["address"] or p["place"])
            is_dispatch = p["dispatch"] and (has_loc or p["units"] or p["complaint"])
            # "Paramedic 8, this is going to be Dundee Road at Mass Ave ... difficulty breathing"
            if not is_dispatch and p["weak_dispatch"] and p["units"] and (has_loc or p["complaint"]) \
                    and p["status"] not in ("clear", "on scene", "at hospital"):
                is_dispatch = True
            # Fire-channel style dispatch: "Engine 6, Squad 2, 479 Franklin Street, ... Medical Assist" and
            # "Engine 1, Ladder 4, responding, 3 Walker Street, for the ... fire alarm": the dispatcher's "responding"
            # is part of the dispatch, not a unit reporting in.
            if not is_dispatch and p["units"] and (p["address"] or p["place"]) and ("fire" in tg.lower()) \
                    and p["status"] in (None, "responding") and (p["complaint"] or len(p["units"]) >= 2 or p["address"]):
                is_dispatch = True
            # Pro dispatcher describing a call: an address plus who / what, often "call on the fire" or
            # "it's a 23-year-old female" ("Number 20, Brattle Street ... Central Rock Gym ... call on the fire")
            if not is_dispatch and p["address"] and (p["age"] or p["complaint"]) and re.search(r"pro|intercept", tg, re.I) \
                    and (p["fire_notify"] or p["weak_dispatch"] or re.search(r"coming in as|year[- ]old", text, re.I)) \
                    and p["status"] in (None, "responding"):
                is_dispatch = True
            # A crew reporting its own new call: "We are responding to 31 Bowker Street in Boston for an active seizure"
            if not is_dispatch and p["address"] and re.search(r"\bresponding (to|into)\b", text, re.I) \
                    and (p["complaint"] or p["town"] or re.search(r"mutual aid", text, re.I)):
                is_dispatch = True
            # Any channel: a unit + an address + what the call is ("Paramedic 18, ... 15 Cochran Lane ... psych")
            if not is_dispatch and (p["units"] or implied) and p["address"] and p["complaint"] \
                    and p["status"] in (None, "responding"):
                is_dispatch = True                      # "Paramedic 1." / "Garden Street and Mass Ave, a motor vehicle accident"

            forced = self.incidents.get(self._resolve(fix["to"])) if fix and fix.get("to") else None
            if forced is not None:
                inc, is_dispatch = forced, False
                p = dict(p, dispatch=False)
            elif is_dispatch:
                inc = self._find_by_location(p, t) if has_loc else None
                if inc is None and prev_inc and t - prev[0] < SPLIT_KEYUP_MS and not has_loc:
                    inc = prev_inc                       # second half of a split dispatch
                if inc is None and not has_loc and p["units"]:
                    cand = self._find_by_unit(p["units"], t)
                    if cand and t - cand["last"] < DISPATCH_MERGE_MS:
                        inc = cand                       # "21, ... for the psych call" = the call 21 is already on
                if inc is None and prev_inc and t - prev[0] < SPLIT_KEYUP_MS and has_loc \
                        and not prev_inc["address"] and not prev_inc["place"]:
                    inc = prev_inc                       # first keyup had units, this one has the address
                if inc is None:
                    inc = self._new_incident(rec, p)
                if not p["units"] and not inc["units"] and implied:
                    p = dict(p, units=[implied])        # the unit named in the keyup just before ("MIT 8, go ahead")
                for u in p["units"]:
                    cur = self.units.get(u)
                    if cur and cur.get("incident") and cur["incident"] != inc["id"] and cur["incident"] in self.incidents:
                        # unit re-assigned ("disregard that transfer, pick up that Harvard response")
                        old = self.incidents[cur["incident"]]
                        old["units"].pop(u, None)
                        old["timeline"].append({"time": t, "talkgroup_name": tg, "text": f"{u} reassigned to another call",
                                                "audio_url": None, "status": None, "dispatch": False,
                                                "inferred": False, "note": True})
                        self._update_incident_status(old)
                        if not old["units"]:
                            self._close(old, t, f"{u} reassigned")
                    self._set_unit(u, "dispatched", inc, t, tg)
            else:
                # Not a dispatch: update an existing incident if we can tell which one
                if has_loc:
                    inc = self._find_by_location(p, t)
                if inc is None and p["units"]:
                    inc = self._find_by_unit(p["units"], t)
                # The unit this conversation is with ("Pro base, paramedic 9" ... "Transporting BLS to the Mount")
                if inc is None and implied:
                    inc = self._find_by_unit([implied], t)
                    if inc is not None:
                        inferred = True
                        p = dict(p, units=[self._same_unit(implied, inc["units"])])
                # Unlabelled status with nobody identified: only if the conversation on this channel was just about
                # that call. A busy channel moves on and then it's someone else's status.
                if inc is None and not implied and p["status"] and prev_tx and prev_tx[1] in self.incidents \
                        and t - prev_tx[0] < INFER_WINDOW_MS:
                    inc, inferred = self.incidents[prev_tx[1]], True
                # A call-in or chatter with nothing new ("Squad 2 to Fire Alarm", "Paramedic 9.", "Who is it?") only
                # tells us who is talking; it doesn't belong on the card.
                informative = bool(p["status"] or p["hospital"] or has_loc or p["complaint"]
                                   or extract_transport_level(text) or re.search(r"\b(AMA|refus\w*)\b", text, re.I))
                if inc is not None and not informative:
                    return False
                if inc is None and p["status"] == "clear" and p["units"]:
                    for u in p["units"]:
                        self._set_unit(u, "clear", None, t, tg)
                    return True
                if inc is None:
                    return False
                st = p["status"]
                if st:
                    targets = [self._same_unit(u, inc["units"]) for u in p["units"]] or \
                        ([next(iter(inc["units"]))] if len(inc["units"]) == 1 else [])
                    # "Transporting" on a call with several units: the ambulance is the one transporting
                    if not targets and st in ("transporting", "at hospital"):
                        targets = [u for u in inc["units"] if u.startswith(("Pro ", "Paramedic ", "ALS ", "BLS "))][:1]
                    if not targets and st == "clear" and inferred:
                        targets = [u for u, v in inc["units"].items() if v["status"] != "clear"
                                   and self.units.get(u, {}).get("talkgroup") == tg][:1]
                    if st == "clear" and not p["units"] and not re.search(r"cancel|disregard|no ems", text, re.I):
                        # an unlabelled "clear" right after a dispatch is almost always someone else
                        targets = [u for u in targets
                                   if not (inc["units"].get(u, {}).get("status") == "dispatched"
                                           and t - inc["units"][u]["since"] < 4 * 60_000)]
                        if not targets:
                            st = None
                    for u in targets:
                        self._set_unit(u, st, inc, t, tg, inferred=inferred and not p["units"])
                    if st and not inc["units"] and st != "clear":
                        inc["status"] = max([inc["status"], st], key=STATUS_ORDER.index)
                        inc.setdefault("times", {}).setdefault(st, t)
                    if st == "clear" and not inc["units"]:
                        inc["status"] = "clear"
                        inc.setdefault("times", {}).setdefault("clear", t)
                if p["hospital"] and (st in ("transporting", "at hospital") or "transport" in text.lower()):
                    inc["hospital"] = p["hospital"]
                lvl = extract_transport_level(text)
                if lvl:
                    inc["transport_level"] = lvl
                if re.search(r"\b(AMA|refus\w*|signed off|refusal)\b", text, re.I):
                    inc["outcome"] = "Refusal / AMA"

            self._fill(inc, p)
            if not inferred:          # a guessed attachment shouldn't be able to flag a call as Harvard
                self._apply_hits(inc, rec)
            if tg not in inc["talkgroups"]:
                inc["talkgroups"].append(tg)
            inc["last"] = max(inc["last"], t)
            inc["timeline"].append({"time": t, "talkgroup_name": tg, "text": text, "audio_url": rec.get("audio_url"),
                                    "status": p["status"], "dispatch": bool(is_dispatch), "inferred": inferred,
                                    "hits": [] if inferred else [{"term": h["term"], "level": h["level"]}
                                                                 for h in rec.get("hits") or [] if not h.get("suspect")]})
            del inc["timeline"][:-40]
            self._update_incident_status(inc)
            # acuity from the dispatch wording (+ the first few minutes of traffic) and the units sent
            early = [e["text"] for e in inc["timeline"]
                     if not e.get("note") and (e.get("dispatch") or e["time"] - inc["opened"] < 300_000)]
            inc["acuity"], inc["acuity_why"] = classify_incident(early, inc["units"].keys(), inc.get("complaint"))
            ai = inc.get("ai") or {}
            if ai.get("acuity") in ("high", "low"):            # Claude's reading wins over keywords
                inc["acuity"], inc["acuity_why"] = ai["acuity"], ["AI: " + (ai.get("acuity_reason") or ai["acuity"])]
            inc["kind"] = call_kind(inc.get("complaint"), inc["units"].keys(),
                                    [e["text"] for e in inc["timeline"] if not e.get("note")])
            self.last_by_tg[tg] = (t, inc["id"])
            self.last_tx_by_tg[tg] = (t, inc["id"])
            if inc["id"] in self.incidents:
                inc = self._auto_merge(inc, t)
            return True

    # ---------------------------------------------------------------- merging duplicate cards
    def _resolve(self, iid):
        seen = set()
        while iid in self.aliases and iid not in seen:
            seen.add(iid)
            iid = self.aliases[iid]
        return iid

    def _addr_keys(self, inc):
        keys = {norm_addr(inc.get("address")), norm_addr((inc.get("ai") or {}).get("address"))}
        return {k for k in keys if k}

    def _place_keys(self, inc):
        keys = {norm_place(inc.get("place")), norm_place((inc.get("ai") or {}).get("place"))}
        return {k for k in keys if k}

    def _same_call(self, a, b):
        """Rule-based: same normalised address or landmark, or a location-less card sharing a unit."""
        if abs(a["opened"] - b["opened"]) > MERGE_WINDOW_MS:
            return None
        ak, bk = self._addr_keys(a), self._addr_keys(b)
        if ak & bk:
            return "same address"
        for x in ak:
            for y in bk:
                xa, ya = x.split(" ", 1), y.split(" ", 1)
                if "&" not in x and "&" not in y and len(xa) == 2 and len(ya) == 2 and xa[0] == ya[0] \
                        and xa[0].isdigit() and _sim(xa[1], ya[1]) >= 0.8:
                    return "same address"
        if self._place_keys(a) & self._place_keys(b):
            return "same place"
        units_a, units_b = set(a["units"]), set(b["units"])
        no_loc = lambda i: not (self._addr_keys(i) or self._place_keys(i))
        if (no_loc(a) or no_loc(b)) and units_a & units_b and abs(a["opened"] - b["opened"]) < 10 * 60_000:
            shared = units_a & units_b
            live = all(a["units"][u]["status"] != "clear" and b["units"][u]["status"] != "clear" for u in shared)
            if live:
                return "same unit, location unclear"
        return None

    def _merge(self, src, dst, why, t=None):
        """Fold card `src` into card `dst` (lock held)."""
        t = t or _now_ms()
        seen = {(e.get("audio_url") or "", e["text"], e["time"]) for e in dst["timeline"]}
        for e in src["timeline"]:
            k = (e.get("audio_url") or "", e["text"], e["time"])
            if k not in seen:
                dst["timeline"].append(e)
                seen.add(k)
        dst["timeline"].sort(key=lambda e: e["time"])
        del dst["timeline"][:-60]
        for u, v in src["units"].items():
            cur = dst["units"].get(u)
            if not cur or v["since"] > cur["since"]:
                dst["units"][u] = v
            st = self.units.get(u)
            if st and st.get("incident") == src["id"]:
                st["incident"] = dst["id"]
        dst.setdefault("times", {})
        for k, v in (src.get("times") or {}).items():
            dst["times"][k] = min(v, dst["times"].get(k, v))
        for k in ("address", "address_key", "place", "town", "complaint", "age", "hospital", "level", "ai_summary"):
            if src.get(k) and not dst.get(k):
                dst[k] = src[k]
        rank = {None: 0, "medium": 1, "high": 2}
        if rank.get(src.get("harvard"), 0) > rank.get(dst.get("harvard"), 0):
            dst["harvard"] = src["harvard"]
        for x in src.get("harvard_terms", []):
            if x not in dst["harvard_terms"]:
                dst["harvard_terms"].append(x)
        for x in src.get("talkgroups", []):
            if x not in dst["talkgroups"]:
                dst["talkgroups"].append(x)
        if src.get("acuity") == "high" and dst.get("acuity") != "high":
            dst["acuity"], dst["acuity_why"] = src["acuity"], src.get("acuity_why")
        dst["opened"], dst["last"] = min(dst["opened"], src["opened"]), max(dst["last"], src["last"])
        dst.setdefault("merged_from", []).append({"id": src["id"], "why": why, "at": t,
                                                  "label": src.get("address") or src.get("place") or src.get("complaint") or "card"})
        dst["timeline"].append({"time": t, "talkgroup_name": "", "text": f"Merged with another card: {why}",
                                "audio_url": None, "status": None, "dispatch": False, "inferred": False, "note": True})
        self.incidents.pop(src["id"], None)
        self.closed = [c for c in self.closed if c["id"] != src["id"]]
        self.aliases[src["id"]] = dst["id"]
        for tg, (tt, iid) in list(self.last_by_tg.items()):
            if iid == src["id"]:
                self.last_by_tg[tg] = (tt, dst["id"])
        if dst.get("closed") and any(u["status"] != "clear" for u in dst["units"].values()):
            dst["closed"] = None                                     # reopened by live units on the merged card
            self.closed = [c for c in self.closed if c["id"] != dst["id"]]
            self.incidents[dst["id"]] = dst
        self._update_incident_status(dst)
        self.merges.append((src["id"], dst["id"], why))

    def _auto_merge(self, inc, t):
        """Merge `inc` with any open card that is clearly the same call. Returns the surviving card."""
        for other in list(self.incidents.values()):
            if other is inc or other["id"] == inc["id"] or other["id"] not in self.incidents \
                    or inc["id"] not in self.incidents:
                continue
            why = self._same_call(inc, other)
            if why:
                dst, src = (other, inc) if other["opened"] <= inc["opened"] else (inc, other)
                self._merge(src, dst, why, t)
                inc = dst
        return inc

    def merge(self, src_id, dst_id, why="merged by hand"):
        with self.lock:
            src_id, dst_id = self._resolve(src_id), self._resolve(dst_id)
            if src_id == dst_id:
                return None
            find = lambda i: self.incidents.get(i) or next((c for c in self.closed if c["id"] == i), None)
            src, dst = find(src_id), find(dst_id)
            if not src or not dst:
                return None
            if src["opened"] < dst["opened"] and why.startswith("AI"):
                src, dst = dst, src                                      # keep the older card
            self._merge(src, dst, why)
            return json.loads(json.dumps(dst))

    def pop_merges(self):
        with self.lock:
            out, self.merges = self.merges, []
            return out

    def get(self, iid):
        with self.lock:
            iid = self._resolve(iid)
            inc = self.incidents.get(iid) or next((c for c in self.closed if c["id"] == iid), None)
            return json.loads(json.dumps(inc, default=list)) if inc else None

    def incident_for_tg(self, tg):
        with self.lock:
            last = self.last_by_tg.get(tg)
            return self.incidents.get(last[1]) and json.loads(json.dumps(self.incidents[last[1]])) if last else None

    def apply_ai(self, iid, res):
        """Fold Claude's reading of a call into the incident. Returns the updated incident (copy) or None."""
        with self.lock:
            iid = self._resolve(iid)
            inc = self.incidents.get(iid) or next((c for c in self.closed if c["id"] == iid), None)
            if not inc:
                return None
            inc["ai"] = res
            inc["ai_summary"] = res.get("summary")
            inc["ai_not_call"] = res.get("is_call") is False and (res.get("confidence") or 0) >= 0.6
            if res.get("acuity") in ("high", "low"):
                inc["acuity"], inc["acuity_why"] = res["acuity"], ["AI: " + (res.get("acuity_reason") or res["acuity"])]
            if res.get("complaint"):
                inc["complaint"] = res["complaint"][:40]
                inc["kind"] = call_kind(inc["complaint"], inc["units"].keys(),
                                        [e["text"] for e in inc["timeline"] if not e.get("note")])
            for k_ai, k in (("address", "address"), ("place", "place"), ("town", "town"), ("patient", "age")):
                if k == "town" and str(res.get(k_ai) or "").strip().lower() in ("cambridge", "cambridge, ma"):
                    continue
                if res.get(k_ai) and not inc.get(k):
                    inc[k] = str(res[k_ai])[:80]
            if (res.get("confidence") or 0) >= 0.6:
                if res.get("status") in STATUS_ORDER and not inc.get("closed"):
                    # the AI read the whole thread: move the call's ambulance(s) to that status so it sticks
                    st, now = res["status"], _now_ms()
                    inc["ai_status"] = st
                    amb = [u for u in inc["units"] if u.startswith(EMS_UNIT_PREFIX)] or list(inc["units"])[:1]
                    for u in amb:
                        if inc["units"][u]["status"] != st:
                            inc["units"][u].update(status=st, since=now, inferred=True)
                            g = self.units.get(u)
                            if g:
                                g.update(status=st, since=now)
                                g["incident"] = None if st == "clear" else inc["id"]
                    if not inc["units"]:
                        inc["status"] = st
                    inc.setdefault("times", {}).setdefault(st, inc.get("last") or now)
                    self._update_incident_status(inc)
                if res.get("transport_level") in ("ALS", "BLS"):
                    inc["transport_level"] = res["transport_level"]
                if res.get("hospital"):
                    inc["hospital"] = str(res["hospital"])[:40]
                if res.get("outcome"):
                    inc["outcome"] = str(res["outcome"])[:40]
            if res.get("harvard") and (res.get("confidence") or 0) >= 0.5 and not inc.get("harvard"):
                inc["harvard"] = "medium"
                loc = res.get("harvard_location") or "Harvard (AI)"
                if loc not in inc["harvard_terms"]:
                    inc["harvard_terms"].append(loc)
                inc["ai_found_harvard"] = loc
            if inc["id"] in self.incidents:
                inc = self._auto_merge(inc, _now_ms())          # Claude's cleaned-up address may reveal a duplicate
            return json.loads(json.dumps(inc))

    # ---------------------------------------------------------------- crew corrections: "not part of this call"
    def _find_any(self, iid):
        iid = self._resolve(iid)
        return self.incidents.get(iid) or next((c for c in self.closed if c["id"] == iid), None)

    def _rebuild(self, inc):
        """Recompute a card from the transmissions it still has (after one was taken out or moved in)."""
        lines = sorted((e for e in inc["timeline"] if not e.get("note")), key=lambda e: e["time"])
        old_units = set(inc["units"])
        for k in ("address", "address_key", "place", "town", "complaint", "age", "hospital", "level", "transport_level", "outcome"):
            inc[k] = None
        inc["units"], inc["talkgroups"], inc["times"] = {}, [], {}
        if all("hits" in e for e in lines):            # lines recorded before this feature keep the old Harvard flag
            inc["harvard"], inc["harvard_terms"] = None, []
            for e in lines:
                for h in e["hits"]:
                    if h["term"] not in inc["harvard_terms"]:
                        inc["harvard_terms"].append(h["term"])
                    if h["level"] == "high" or inc["harvard"] is None:
                        inc["harvard"] = "high" if h["level"] == "high" else (inc["harvard"] or "medium")
        for e in lines:
            p = parse(e["text"], e["talkgroup_name"])
            self._fill(inc, p)
            if e["talkgroup_name"] not in inc["talkgroups"]:
                inc["talkgroups"].append(e["talkgroup_name"])
            st = "dispatched" if e.get("dispatch") else p["status"]
            targets = p["units"] or ([next(iter(inc["units"]))] if st and len(inc["units"]) == 1 else [])
            for u in targets:
                if st:
                    cur = inc["units"].get(u)
                    if not cur or cur["status"] != st:
                        inc["units"][u] = {"unit": u, "status": st, "since": e["time"], "inferred": bool(e.get("inferred"))}
                elif u not in inc["units"] and (e.get("dispatch") or not inc["units"]):
                    inc["units"][u] = {"unit": u, "status": "dispatched", "since": e["time"], "inferred": False}
            if st:
                inc["times"].setdefault(st, e["time"])
            if not e.get("dispatch"):
                if p["hospital"] and st in ("transporting", "at hospital"):
                    inc["hospital"] = p["hospital"]
                inc["transport_level"] = extract_transport_level(e["text"]) or inc.get("transport_level")
                if re.search(r"\b(AMA|refus\w*|signed off|refusal)\b", e["text"], re.I):
                    inc["outcome"] = "Refusal / AMA"
        if lines:
            inc["opened"] = min(inc["opened"], lines[0]["time"]) if inc.get("opened") else lines[0]["time"]
            inc["times"].setdefault("dispatched", lines[0]["time"])
            inc["last"] = lines[-1]["time"]
        inc["status"] = "dispatched"
        self._update_incident_status(inc)
        for u in old_units - set(inc["units"]):        # units that only came from the removed line
            st = self.units.get(u)
            if st and st.get("incident") == inc["id"]:
                st["incident"] = None
        early = [e["text"] for e in lines if e.get("dispatch") or e["time"] - inc["opened"] < 300_000]
        inc["acuity"], inc["acuity_why"] = classify_incident(early, inc["units"].keys(), inc.get("complaint"))
        inc["kind"] = call_kind(inc.get("complaint"), inc["units"].keys(), [e["text"] for e in lines])

    def attach(self, iid, rec):
        """Add a transmission that wasn't on any card to this card (AI check found it belongs). Returns copy or None."""
        with self.lock:
            inc = self._find_any(iid)
            if not inc or any(e.get("audio_url") == rec.get("audio_url") for e in inc["timeline"]):
                return None
            p = parse(rec.get("text") or "", rec.get("talkgroup_name", ""))
            inc["timeline"] = sorted(inc["timeline"] + [{
                "time": rec["time"], "talkgroup_name": rec.get("talkgroup_name", ""), "text": rec.get("text") or "",
                "audio_url": rec.get("audio_url"), "status": p["status"], "dispatch": False, "inferred": True, "hits": []}],
                key=lambda e: e["time"])
            self._rebuild(inc)
            return json.loads(json.dumps(inc))

    def crew_placed(self, audio_url):
        f = self.fixes.get(audio_url)
        return bool(f)

    def kind_of(self, audio_url):
        """EMS / fire kind of the call this transmission went into (None if it isn't on the board)."""
        with self.lock:
            for inc in list(self.incidents.values()) + self.closed:
                if any(e.get("audio_url") == audio_url for e in inc["timeline"]):
                    return inc.get("kind")
        return None

    def detach(self, iid, audio_url, to_iid=None, record=True):
        """Take one transmission out of a card ("not part of this call"), optionally into another card.
        Returns (source card, target card or None) as copies, or None if not found."""
        with self.lock:
            src = self._find_any(iid)
            if not src:
                return None
            entry = next((e for e in src["timeline"] if e.get("audio_url") == audio_url and not e.get("note")), None)
            if not entry or (not record and entry.get("dispatch") and not to_iid):
                return None                                  # the AI check never strips a call of its dispatch
            dst = self._find_any(to_iid) if to_iid else None
            if dst is src:
                dst = None
            src["timeline"] = [e for e in src["timeline"] if e is not entry]
            if not any(not e.get("note") for e in src["timeline"]):
                if src["id"] in self.incidents:          # nothing left: the card itself was the mistake
                    self._close(src, _now_ms(), "all its radio was moved or removed")
                src["dismissed"] = True
            else:
                self._rebuild(src)
            if dst:
                moved = dict(entry, inferred=False)
                dst["timeline"] = sorted(dst["timeline"] + [moved], key=lambda e: e["time"])
                self._rebuild(dst)
            if record:
                self.fixes[audio_url] = {"action": "move" if dst else "remove", "to": dst["id"] if dst else None,
                                         "from": src["id"]}
            for tg, (tt, tid) in list(self.last_by_tg.items()):
                if tid == src["id"] and tt == entry["time"]:
                    self.last_by_tg.pop(tg)              # don't let the next keyup attach to the wrong call again
            return json.loads(json.dumps(src)), (json.loads(json.dumps(dst)) if dst else None)

    def dismiss(self, iid, by=None):
        """Remove a call from the board (e.g. one the parser made up from garbled radio)."""
        with self.lock:
            iid = self._resolve(iid)
            inc = self.incidents.get(iid)
            if inc:
                self._close(inc, _now_ms(), f"dismissed by {by or 'someone'}")
                inc["dismissed"] = True
                return True
            n = len(self.closed)
            self.closed = [c for c in self.closed if c["id"] != iid]
            return len(self.closed) != n

    def tick(self, t=None):
        with self.lock:
            n = len(self.incidents)
            self._housekeep(t or _now_ms())
            return len(self.incidents) != n

    def snapshot(self):
        with self.lock:
            def slim(inc):
                d = {k: v for k, v in inc.items() if k != "address_key"}
                d["units"] = list(inc["units"].values())
                return d
            return {"incidents": sorted((slim(i) for i in self.incidents.values()), key=lambda i: -i["last"]),
                    "closed": [slim(i) for i in reversed(self.closed) if not i.get("dismissed")],
                    "units": sorted(self.units.values(), key=lambda u: (u["unit"].split()[0], int(u["unit"].split()[-1]) if u["unit"].split()[-1].isdigit() else 0))}

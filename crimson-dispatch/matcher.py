"""Keyword / location matching tolerant of radio-transcription errors."""
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
           "rd": "road", "sq": "square", "pl": "place"}


UNIT_WORDS = {"ambulance", "engine", "squad", "ladder", "truck", "rescue", "paramedic", "medic", "pro", "als", "bls",
              "car", "unit", "room", "apartment", "apt", "floor", "channel", "division", "tower", "box", "number", "suite"}
HOSPITAL_NEXT = {"er", "hospital", "emergency", "ed", "e", "ambulance", "bay"}
SUFFIXES = {"street", "avenue", "road", "drive", "place", "square", "court", "terrace", "parkway", "way", "lane",
            "boulevard", "highway", "row"}


def tokenize(text):
    # "1.4" (mileage) must not become the street number 104: glue decimals into one non-numeric token
    text = re.sub(r"(\d)\.(\d)", r"\1d\2", text)
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
    return out


def _sim(a, b):
    return SequenceMatcher(None, a, b).ratio()


def _words_agree(window, phrase_tokens):
    """Same number of words: every word must roughly match ('massachusetts tab' is not 'massachusetts hall')."""
    return all(a == b or (len(b) > 3 and _sim(a, b) >= 0.75) for a, b in zip(window, phrase_tokens))


def _phrase_hit(tokens, phrase_tokens):
    """Return (score, matched_text) for best fuzzy occurrence of phrase in tokens."""
    if not phrase_tokens:
        return 0.0, ""
    target = " ".join(phrase_tokens)
    compact = target.replace(" ", "")
    n = len(phrase_tokens)
    best, best_txt = 0.0, ""
    for size in ({n - 1, n, n + 1} if n >= 3 else {n, n + 1}):
        for i in range(0, max(0, len(tokens) - size) + 1):
            window = tokens[i:i + size]
            if not window:
                continue
            w = " ".join(window)
            if w == target or w.replace(" ", "") == compact:
                return 1.0, w
            if size == n:
                if not _words_agree(window, phrase_tokens):
                    continue
                s = max(_sim(w, target), _sim(w.replace(" ", ""), compact))
            else:
                # words split / run together by the transcript ("pforz heimer"): compare without spaces, strictly
                s = _sim(w.replace(" ", ""), compact)
                s = s if s >= 0.9 else 0.0
            if n == 1 and window[0][:1] != phrase_tokens[0][:1]:
                continue                         # one-word names must start the same: "everett" is not "leverett"
            if s > best:
                best, best_txt = s, w
    return best, best_txt


def _threshold(phrase):
    L = len(phrase.replace(" ", ""))
    if L <= 4:
        return 1.0      # short tokens (HUPD, HKS, PBH) must match exactly
    if " " not in phrase.strip():
        return 0.9      # single words ("Leverett", "Mather") need to be nearly exact
    if L <= 7:
        return 0.88
    return 0.84


def _numbers_before(tokens, idx):
    """Parse a street number ending just before tokens[idx]."""
    j = idx - 1
    if j >= 0 and tokens[j] in ("number", "no"):
        j -= 1
    nums = []
    k = j
    while k >= 0 and (tokens[k].isdigit() or tokens[k] in _NUM_WORDS):
        k -= 1
    if k >= 0 and tokens[k] in UNIT_WORDS:
        return None             # "Ambulance 5, Mount Auburn" / "Squad 4, Mount Auburn Street": a unit, not an address
    while j >= 0 and len(nums) < 4:
        tok = tokens[j]
        if tok.isdigit():
            nums.insert(0, tok)
        elif tok in _NUM_WORDS:
            nums.insert(0, str(_NUM_WORDS[tok]))
        else:
            break
        j -= 1
    if not nums:
        return None
    if len(nums) == 1:
        return int(nums[0])
    # "13 50" -> 1350, "twelve fifty" -> 1250, "one thousand" not handled (rare on radio)
    if len(nums) == 2 and all(len(n) <= 2 for n in nums):
        return int(nums[0]) * 100 + int(nums[1])
    if len(nums) == 2 and int(nums[0]) % 10 == 0 and int(nums[1]) < 10 and int(nums[0]) >= 20:
        return int(nums[0]) + int(nums[1])
    try:
        return int("".join(nums))
    except ValueError:
        return None


class Matcher:
    def __init__(self, config):
        self.load(config)

    def load(self, config):
        self.rules = []
        for r in config.get("rules", []):
            for phrase in [r["term"]] + list(r.get("aliases", [])):
                toks = tokenize(phrase)
                if toks:
                    self.rules.append((r, phrase, toks))
        self.ranges = []
        for rg in config.get("address_ranges", []):
            for name in [rg["street"]] + list(rg.get("aliases", [])):
                toks = tokenize(name)
                if toks:
                    self.ranges.append((rg, toks))

    def match(self, text):
        """Return list of hits sorted by severity: {term, level, category, heard, score}."""
        if not text:
            return []
        tokens = tokenize(text)
        hits = {}
        for rule, phrase, ptoks in self.rules:
            score, heard = _phrase_hit(tokens, ptoks)
            if score >= _threshold(phrase):
                key = rule["term"]
                if key not in hits or score > hits[key]["score"]:
                    hits[key] = {"term": rule["term"], "level": rule.get("level", "high"),
                                 "category": rule.get("category", ""), "heard": heard,
                                 "score": round(score, 2)}
        # Address ranges: "<number> <street>"
        for rg, stoks in self.ranges:
            n = len(stoks)
            for i in range(len(tokens) - n + 1):
                window = tokens[i:i + n]
                exactish = window == stoks or _sim(" ".join(window), " ".join(stoks)) >= 0.85
                if not exactish:
                    continue
                nxt = tokens[i + n] if i + n < len(tokens) else ""
                if nxt in HOSPITAL_NEXT:
                    continue            # "Mount Auburn ER" / "Mount Auburn Hospital" is the hospital, not the street
                if not (set(stoks) & SUFFIXES) and nxt not in SUFFIXES and not (set(stoks) & {"ave", "st"}):
                    # a bare alias like "Mt Auburn" needs "Street" after it to count as an address
                    continue
                num = _numbers_before(tokens, i)
                if num is not None and rg["from"] <= num <= rg["to"]:
                    key = rg["label"]
                    hits[key] = {"term": rg["label"], "level": rg.get("level", "high"),
                                 "category": "Address", "heard": f"{num} {' '.join(window)}",
                                 "score": 1.0}
        # Drop generic hits subsumed by a more specific one (e.g. "Harvard" when "Harvard Yard" hit)
        terms = list(hits)
        for t in terms:
            for u in terms:
                if t != u and t in hits and u in hits \
                        and (t.lower() in u.lower() or t.lower() in hits[u]["heard"]) \
                        and LEVEL_RANK[hits[u]["level"]] >= LEVEL_RANK[hits[t]["level"]]:
                    hits.pop(t, None)
        pr = hits.get("private response")
        if pr and pr["level"] == "high":
            others = [h for t, h in hits.items() if t != "private response" and h.get("category") != "General"]
            mit = re.search(r"\bm\.?\s?i\.?\s?t\b|albany st|mass(achusetts)? institute|vassar st|ames st", text, re.I)
            if mit or not others:
                pr["level"] = "medium"
                pr["note"] = "MIT / not Harvard?" if mit else "no HUPD or Harvard location heard"
        return sorted(hits.values(), key=lambda h: (-LEVEL_RANK[h["level"]], -h["score"]))


DISPATCH_WORDS = ("respond", "responding", "medical", "ambulance", "als", "bls", "pro",
                  "engine", "rescue", "squad", "unconscious", "fall", "intox", "etoh",
                  "seizure", "chest", "breathing", "injury", "assault", "overdose")


def looks_like_dispatch(text):
    toks = set(tokenize(text))
    return any(w in toks for w in DISPATCH_WORDS)

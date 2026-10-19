"""
Teach the rules from calls they missed.

When the AI finds a call the rules didn't board and the crew marks it "Real call", Claude looks at that call's
radio next to what the rules pulled out of each line and proposes small rules that would have caught it:

  * rewrite    - a mishearing to undo first ("corresponding to" -> "responding to")
  * complaint  - a phrase that means a complaint ("struck by a bicycle" -> Motor vehicle accident)
  * dispatch   - a phrase that starts a call ("heading over to")
  * street     - a street name said without "Street" / "Avenue" ("526 Beacon" -> Beacon Street)
  * place      - a place name the rules don't know ("Rock Spot Climbing")

Rules are plain data (logs/learned-rules.json), never code. Before one is kept it must pass two checks:

  1. Replay: with the rule, the rules alone now open a card for this call (from the same radio, in order).
  2. History: on the last two weeks of logged radio, the rule opens at most a few new cards elsewhere and
     changes how only a few other transmissions are read, so it doesn't turn chatter into calls.

Every learned rule is listed in Accuracy & training with an Undo button. Only public radio transcripts go
to Claude.
"""
import glob
import json
import os
import re
import threading
import time
import uuid

import board as B

MAX_NEW_ELSEWHERE = 4          # a rule may open at most this many new cards on two weeks of other radio
MAX_CHANGED_ELSEWHERE = 8      # ... and change how at most this many other transmissions are read
HISTORY_DAYS = 14
HISTORY_CAP = 600              # matching lines checked per rule
COMMON = set("""a an the and or but of to in on at for from by with is are was were be it this that we you i he
she they me my our your his her its yes no ok okay so just now go copy clear one two three four five six seven
eight nine ten pro base fire alarm engine ladder squad rescue paramedic ambulance unit over ahead back up down out
here there take get got all right going be been have has had will can do did not off into onto about as if then
call calls street avenue ave st road drive place we're you're it's that's there's i'm""".split())

LEARN_SYSTEM = """You improve a rule-based parser for Cambridge, MA EMS radio transcripts (machine transcripts, often \
garbled). The parser opens a call card when ONE transmission has a dispatch cue (e.g. "respond", "sign on", \
"responding to", "going to") plus a location (street address or known place) and a unit or a complaint. It missed \
a call that the crew confirmed was real. You get the call's lines and, for each, what the parser extracted.

Propose the FEWEST rules (usually 1, at most 3) that would let the parser catch this call on its own. Types:
- rewrite: {heard, means} a mishearing in this transcript to undo before parsing ("corresponding to" -> \
"responding to"). heard must appear in the text exactly; keep it 2+ words unless the single word is never real.
- complaint: {phrase, label} a phrase in the text that names the patient's problem, with the label to show \
(prefer an existing label from the list given).
- dispatch: {phrase} 2-4 words in the text that announce a call is starting ("heading over to"). Never a phrase \
that also appears in ordinary chatter ("going to be", "to the").
- street: {name, suffix} a street name said without its suffix ("Beacon" + "Street"). Real Cambridge-area streets only.
- place: {name} a place name in the text the parser should treat as a location.
Phrases must be copied from the transcript as they appear (the parser matches them, ignoring case). Prefer a \
rule that will recur on future radio over one that fits only this exact sentence. If the call was missed for a \
reason no rule of these types can fix, return an empty list."""

LEARN_SCHEMA = {"type": "object", "properties": {"rules": {"type": "array", "maxItems": 3, "items": {
    "type": "object", "properties": {
        "type": {"type": "string", "enum": list(B.RULE_TYPES)},
        "phrase": {"type": ["string", "null"]}, "heard": {"type": ["string", "null"]},
        "means": {"type": ["string", "null"]}, "label": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]}, "suffix": {"type": ["string", "null"]},
        "why": {"type": "string"}},
    "required": ["type", "why"]}}}, "required": ["rules"]}


def _now_ms():
    return int(time.time() * 1000)


def rule_text(r):
    """Plain-English one-liner for the dashboard."""
    t = r.get("type")
    if t == "rewrite":
        return f'Hear "{r.get("heard")}" as "{r.get("means")}"'
    if t == "complaint":
        return f'"{r.get("phrase")}" means {r.get("label")}'
    if t == "dispatch":
        return f'"{r.get("phrase")}" starts a call'
    if t == "street":
        return f'"{r.get("name")}" is {r.get("name")} {r.get("suffix") or "Street"}'
    if t == "place":
        return f'"{r.get("name")}" is a place'
    return str(r)


def _key_phrase(r):
    return {"rewrite": "heard", "complaint": "phrase", "dispatch": "phrase", "street": "name", "place": "name"}.get(r.get("type"))


def clean_rule(r, texts, existing):
    """Basic sanity checks on a proposed rule. Returns (rule or None, reason)."""
    t = r.get("type")
    if t not in B.RULE_TYPES:
        return None, "unknown type"
    k = _key_phrase(r)
    val = re.sub(r"\s+", " ", str(r.get(k) or "")).strip(" ,.")
    if not val:
        return None, "empty"
    rx = B.phrase_re(val)
    if not rx or not any(re.search(rx, x, re.I) for x in texts):
        return None, f'"{val}" isn\'t in the call\'s radio'
    words = re.findall(r"[A-Za-z0-9']+", val.lower())
    if all(w in COMMON or w.isdigit() for w in words):
        return None, f'"{val}" is too common'
    out = {"type": t, k: val, "why": str(r.get("why") or "")[:200]}
    if t == "rewrite":
        means = re.sub(r"\s+", " ", str(r.get("means") or "")).strip()
        if not means or means.lower() == val.lower() or len(val) < 5:
            return None, "rewrite needs a different meaning"
        out["means"] = means[:60]
    elif t == "complaint":
        label = re.sub(r"\s+", " ", str(r.get("label") or "")).strip()
        if not label:
            return None, "complaint needs a label"
        out["label"] = next((l for _, l, _ in B.COMPLAINTS if l.lower() == label.lower()), label[:40])
    elif t == "dispatch":
        if len(words) < 2:
            return None, "dispatch phrases need 2+ words"
    elif t == "street":
        if len(words) > 3 or not re.match(r"^[A-Za-z][A-Za-z' ]+$", val) or len(val) < 3:
            return None, "not a street name"
        out["name"] = val.title()
        out["suffix"] = (str(r.get("suffix") or "Street").strip().title())[:12]
    elif t == "place":
        if len(words) < 2:
            return None, "place names need 2+ words"
    sig = (t, out.get(k, "").lower())
    if any((e.get("type"), str(e.get(k) or "").lower()) == sig and not e.get("disabled") for e in existing):
        return None, "already learned"
    return out, ""


class RuleLearner:
    def __init__(self, reader, log_dir, log=print, history=None):
        self.reader, self.log_dir, self.log = reader, log_dir, log
        self.path = os.path.join(log_dir, "learned-rules.json")
        self.lock = threading.Lock()
        self.rules = self._load()
        self._history = history          # optional callable -> list of past transcript records (tests)
        self.results = {}                # card id -> {"state", "message", "rules"}
        B.set_learned(self.rules)

    # ---------------------------------------------------------------- storage
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.rules, f, indent=1)
        os.replace(tmp, self.path)

    def active(self):
        return [r for r in self.rules if not r.get("disabled")]

    def listing(self):
        with self.lock:
            return [dict(r, text=rule_text(r)) for r in reversed(self.rules)]

    def set_disabled(self, rid, disabled=True, by=None):
        with self.lock:
            r = next((x for x in self.rules if x.get("id") == rid), None)
            if not r:
                return None
            r["disabled"] = bool(disabled)
            r["changed_by"], r["changed_at"] = (by or "")[:40], _now_ms()
            self._save()
            B.set_learned(self.rules)
        self.log(f"Learned rule {'turned off' if disabled else 'turned back on'}: {rule_text(r)}")
        return r

    # ---------------------------------------------------------------- history (false-positive check)
    def history_texts(self):
        if self._history:
            return self._history()
        if getattr(self, "_hist_cache", None) and time.time() - self._hist_cache[0] < 600:
            return self._hist_cache[1]
        out = self._read_history()
        self._hist_cache = (time.time(), out)
        return out

    def _read_history(self):
        out, seen = [], set()
        cutoff = time.time() - HISTORY_DAYS * 86400
        for path in sorted(glob.glob(os.path.join(self.log_dir, "calls-*.jsonl"))):
            m = re.search(r"calls-(\d{4}-\d{2}-\d{2})", path)
            if not m or time.mktime(time.strptime(m.group(1), "%Y-%m-%d")) < cutoff - 86400:
                continue
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        t = r.get("text")
                        if t and (t, r.get("talkgroup_name")) not in seen and len(t) < 500:
                            seen.add((t, r.get("talkgroup_name")))
                            out.append({"text": t, "talkgroup_name": r.get("talkgroup_name") or "", "audio_url": r.get("audio_url")})
            except OSError:
                pass
        return out

    @staticmethod
    def _opens_card(rec, rules):
        with B.trying_rules(rules):
            b = B.Board()
            return bool(b.ingest({"id": "h", "time": 1_800_000_000_000, "talkgroup_name": rec["talkgroup_name"],
                                  "text": rec["text"], "audio_url": "h"})) and bool(b.incidents)

    @staticmethod
    def _reading(rec, rules):
        """What the parser takes from one transmission, as far as calls are concerned."""
        with B.trying_rules(rules):
            p = B.parse(rec["text"], rec["talkgroup_name"])
        cue = p["dispatch"] and bool(p["address"] or p["place"] or p["units"] or p["complaint"])
        return (cue, p["address"], p["place"], p["complaint"], tuple(p["units"]), p["status"])

    def elsewhere(self, rule, base, exclude_urls):
        """On past radio: (transmissions that would open a new card, transmissions read differently, examples)."""
        rx = re.compile(B.phrase_re(rule.get(_key_phrase(rule))), re.I)
        if rule.get("type") == "street":
            rx = re.compile(r"\b\d{1,4}[,\s]+" + B.phrase_re(rule["name"]), re.I)
        new, changed, hits, checked = 0, 0, [], 0
        for rec in self.history_texts():
            if rec.get("audio_url") in exclude_urls or not rx.search(rec["text"]):
                continue
            checked += 1
            if checked > HISTORY_CAP:
                break
            if self._reading(rec, base + [rule]) != self._reading(rec, base):
                changed += 1
                hits.append(rec["text"][:120])
                if self._opens_card(rec, base + [rule]) and not self._opens_card(rec, base):
                    new += 1
        return new, changed, hits

    def new_cards_elsewhere(self, rule, base, exclude_urls):
        new, _, hits = self.elsewhere(rule, base, exclude_urls)
        return new, hits

    @staticmethod
    def replay_catches(lines, context, rules):
        """Feed the call's radio (with the few minutes before it) to a fresh board: did the rules put it on a card?"""
        with B.trying_rules(rules):
            b = B.Board()
            for r in sorted(context + lines, key=lambda r: r["time"]):
                try:
                    b.ingest(dict(r, hits=r.get("hits") or []))
                except Exception:
                    pass
            return any(b.card_of(r["audio_url"]) for r in lines
                       if B.informative(r["text"], r["talkgroup_name"]))

    # ---------------------------------------------------------------- the main step
    def learn_async(self, inc, context, on_done=None):
        self.results[inc["id"]] = {"state": "working", "message": "Working out which rule missed it…", "rules": []}
        threading.Thread(target=self._learn_safe, args=(inc, context, on_done), name="learn", daemon=True).start()

    def _learn_safe(self, inc, context, on_done):
        try:
            res = self.learn(inc, context)
        except Exception as e:
            res = {"state": "error", "message": f"Couldn't learn from this call ({type(e).__name__}).", "rules": []}
            self.log(f"Rule learning failed for {inc['id']}: {type(e).__name__}: {str(e)[:200]}")
        self.results[inc["id"]] = res
        if on_done:
            on_done(inc["id"], res)

    def learn(self, inc, context):
        """inc: the confirmed card (with timeline). context: transcript records from just before it."""
        lines = [{"time": e["time"], "talkgroup_name": e.get("talkgroup_name", ""), "text": e.get("text") or "",
                  "audio_url": e.get("audio_url"), "id": "L" + str(k)}
                 for k, e in enumerate(inc["timeline"]) if e.get("audio_url") and not e.get("note") and e.get("text")]
        if not lines:
            return {"state": "none", "message": "No radio on this call to learn from.", "rules": []}
        urls = {r["audio_url"] for r in lines}
        context = [r for r in context if r.get("audio_url") not in urls and r.get("text")
                   and lines[0]["time"] - 180_000 <= r["time"] < lines[0]["time"]]
        base = self.active()
        if self.replay_catches(lines, context, base):
            return {"state": "none", "message": "The rules already catch this call now; nothing to change.", "rules": []}

        # what the rules saw, line by line
        rows = []
        for r in lines:
            p = B.parse(r["text"], r["talkgroup_name"])
            got = {k: v for k, v in p.items() if v and k not in ("address_key", "als")}
            rows.append(f'[{r["talkgroup_name"]}] "{r["text"]}"\n   parser got: {json.dumps(got) if got else "nothing"}')
        labels = sorted({l for _, l, _ in B.COMPLAINTS})
        prompt = (f"CONFIRMED CALL: {inc.get('complaint') or '?'} at {inc.get('address') or inc.get('place') or '?'}"
                  f"{' (AI: ' + inc['ai_reason'] + ')' if inc.get('ai_reason') else ''}\n\nTHE CALL'S LINES, in order:\n"
                  + "\n".join(rows) + "\n\nExisting complaint labels: " + ", ".join(labels))
        out = self.reader._run(prompt, LEARN_SYSTEM, LEARN_SCHEMA, timeout=180)
        texts = [r["text"] for r in lines]
        cands, notes = [], []
        for r in (out or {}).get("rules") or []:
            c, why = clean_rule(r, texts, self.rules)
            if c:
                cands.append(c)
            else:
                notes.append(f"{rule_text(r)}: {why}")
        if not cands:
            return {"state": "none", "rules": [],
                    "message": "No safe rule found for this one (the AI will keep catching calls like it)."
                               + (f" Rejected: {'; '.join(notes)[:200]}" if notes else "")}

        # history check, one candidate at a time
        scored = []
        for c in cands:
            n, changed, hits = self.elsewhere(c, base, urls)
            c["new_elsewhere"], c["changed_elsewhere"] = n, changed
            if n > MAX_NEW_ELSEWHERE or changed > MAX_CHANGED_ELSEWHERE:
                notes.append(f"{rule_text(c)}: would change {changed} other transmissions in past radio")
                continue
            scored.append(c)
        # least side effects first; on a tie, the more specific (longer) phrase
        scored.sort(key=lambda c: (c["new_elsewhere"], c["changed_elsewhere"], -len(str(c.get(_key_phrase(c))))))

        # smallest set (in that order) that makes the replay catch the call
        keep = []
        for c in scored:
            keep.append(c)
            if self.replay_catches(lines, context, base + keep):
                break
        else:
            keep = []
        if not keep:
            return {"state": "none", "rules": [],
                    "message": "Couldn't find a rule that catches this call without false alarms; the AI will keep "
                               "catching calls like it." + (f" ({'; '.join(notes)[:200]})" if notes else "")}
        # drop any rule the call didn't actually need
        for c in list(keep):
            rest = [x for x in keep if x is not c]
            if rest and self.replay_catches(lines, context, base + rest):
                keep = rest
        with self.lock:
            for c in keep:
                c.update(id=uuid.uuid4().hex[:10], at=_now_ms(), learned_from=inc["id"],
                         example=lines[0]["text"][:200])
                self.rules.append(c)
            self._save()
            B.set_learned(self.rules)
        for c in keep:
            self.log(f"Learned rule from {inc['id']}: {rule_text(c)} ({c['new_elsewhere']} other new card(s) in past radio)")
        return {"state": "learned", "rules": [dict(c, text=rule_text(c)) for c in keep],
                "message": "Rules updated: " + "; ".join(rule_text(c) for c in keep)}

"""
AI grouping pass: the rules make call cards instantly; about once a minute (only when new radio has come in)
Claude re-reads the last ~40 minutes of radio and says which transmissions belong to which call. The cards
are then corrected: lines moved to the right call, duplicate cards merged, calls the rules missed created,
and status / ALS-BLS / hospital / outcome filled in.

  * Crew corrections (the ✕ on a line) always win and are never undone.
  * The AI never deletes a card or strips a call of its dispatch line; it only moves, adds and merges.
  * Only public radio transcripts are sent to Claude.
"""
import json
import os
import re
import threading
import time

WINDOW_MS = 30 * 60_000             # how much radio each pass re-reads (shrinks automatically if Claude is slow)
MIN_WINDOW_MS = 12 * 60_000
MAX_LINES = 150
TIMEOUT_S = 240
FAST = True          # corrections-only answers, and only the radio since the last pass (+ a little overlap):
ROLL_MS = 8 * 60_000 # Claude's time grows with how much it reads, so each pass reads ~8 min instead of 30
ROLL_LINES = 60
EVERY_S = 30          # passes are ~10 s now, so check twice a minute (only when new radio came in)
SWEEP_EVERY_S = 180   # missed-call review: every 3 min, re-read the last 20 min looking only for calls with no card
SWEEP_MS = 20 * 60_000
SWEEP_LINES = 120
SWEEP_MAX_PER_HOUR = 20

SYSTEM = """You sort Cambridge, MA EMS / fire radio (machine transcripts, often garbled) into CALLS for a \
student EMS crew's call board. A call = one patient / incident: it starts with a dispatch (a unit sent to a place for \
a reason) or a unit reporting its own new call, and continues with that unit's traffic (responding, on scene, \
transporting ALS/BLS to a hospital, at hospital, clear, refusal).

How the radio works: a unit calls ("Pro base, paramedic 9" / "Ambulance 4 to Pro") or is called ("17, go ahead"), \
then the SAME unit gives its report in the next 1-3 keyups, usually without its name. An unlabelled report belongs \
to whoever called in just before it on that channel; if someone else called in in between, it is theirs. Pro's \
dispatcher often gives the unit first ("Paramedic 7, Pro") and then the address and complaint in the next keyup. \
"Call on the fire" means the fire department is also being sent (still an EMS call). Fire dispatch ("Fire Alarm") \
sends Engines / Ladders / Squads / Rescue, often with a Paramedic for medicals. A Pro ambulance and fire units at the \
same address for the same patient are ONE call.
Garbles: "MIT 8" = "M-I-T-A", "MITA", "NYC"; Paramedic = "Primark", "Paramount", "Permanente", "Pyramid"; Ambulance = \
"Annual", "Ambient", "Williams"; "Farrah Paul" = "for a fall"; "MBA" = MVA. Hospitals: "the Mount" = Mount Auburn, \
"the General" = MGH, "the lady" = Lahey, "the Emerson" = Emerson Hospital.

You also get the current CARDS (id, what, where, units, the line ids on them). For each call give card_id = the \
existing card it matches (same incident), or null if no card has it. Put a line in a call only if you're fairly \
sure; leave out call-ins with no content, chatter and radio checks. Never put one line in two calls. Include calls \
that started before this window if their later traffic is here (card_id if a card exists). Units canonical: \
"Pro 4" for Ambulance 4, "Paramedic 21", "MIT 8", "Engine 6". status = where the call stands at its last line: \
dispatched, responding, on scene, transporting, at hospital, clear. transport_level = ALS/BLS only if the patient \
was transported and the radio says which (not the dispatch level)."""

SCHEMA = {"type": "object", "properties": {"calls": {"type": "array", "items": {"type": "object", "properties": {
    "card_id": {"type": ["string", "null"]},
    "unit": {"type": ["string", "null"]},
    "location": {"type": ["string", "null"]},
    "complaint": {"type": ["string", "null"]},
    "kind": {"type": "string", "enum": ["ems", "fire"]},
    "status": {"type": ["string", "null"], "enum": ["dispatched", "responding", "on scene", "transporting",
                                                      "at hospital", "clear", None]},
    "transport_level": {"type": ["string", "null"], "enum": ["ALS", "BLS", None]},
    "hospital": {"type": ["string", "null"]},
    "outcome": {"type": ["string", "null"]},
    "lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["card_id", "unit", "location", "complaint", "kind", "status", "transport_level", "hospital", "lines"]}}},
    "required": ["calls"]}


def canon_unit(u):
    m = re.match(r"\s*(ambulance|pro|amb)\s*(\d+)\s*$", u or "", re.I)
    if m:
        return f"Pro {int(m.group(2))}"
    m = re.match(r"\s*(paramedic|medic)\s*(\d+)\s*$", u or "", re.I)
    if m:
        return f"Paramedic {int(m.group(2))}"
    m = re.match(r"\s*(mit|als|bls|engine|ladder|squad|rescue)\s*(\d+)\s*$", u or "", re.I)
    if m:
        k = m.group(1).lower()
        return f"{k.upper() if k in ('mit', 'als', 'bls') else k.title()} {int(m.group(2))}"
    return None


def build_prompt(recs, cards):
    """recs: transcripts (oldest first) with time, talkgroup_name, text, audio_url. cards: board incidents."""
    hm = lambda ms: time.strftime("%H:%M:%S", time.localtime(ms / 1000))
    ids, rows = {}, []
    url_to_id = {}
    for k, r in enumerate(recs, 1):
        rid = f"R{k}"
        ids[rid] = r["audio_url"]
        url_to_id[r["audio_url"]] = rid
        rows.append(f"{rid} {hm(r['time'])} [{r['talkgroup_name']}] {r['text']}")
    crows = []
    for c in cards:
        on = [url_to_id[e["audio_url"]] for e in c["timeline"] if e.get("audio_url") in url_to_id]
        if not on:
            continue
        units = c["units"].keys() if isinstance(c["units"], dict) else [u["unit"] for u in c["units"]]
        crows.append(f"{c['id']}: {c.get('complaint') or 'call'} @ {c.get('address') or c.get('place') or '?'} "
                     f"units {', '.join(units) or '-'} lines {' '.join(on)}")
    prompt = (f"CARDS now on the board:\n" + ("\n".join(crows) or "(none)") +
              f"\n\nRADIO, oldest first ({len(rows)} lines):\n" + "\n".join(rows))
    return prompt, ids


# ---------------------------------------------------------------- fast "corrections only" pass
TG_CODE = [("pro ems", "PE"), ("intercept", "PI"), ("fire - ems", "FE"), ("dispatch", "FD"), ("citywide", "FC")]

DELTA_SYSTEM = SYSTEM.split("You also get the current CARDS")[0] + """You get the CARDS the board already has (with \
their state and line ids) and the RADIO (line ids, time, channel code: PE = Pro EMS, PI = Pro Intercept, FE = Fire EMS, \
FD = Fire dispatch). Return ONLY corrections:
- a card that is missing lines that clearly belong to it (add), has lines that are someone else's (remove), \
or whose unit / status / transport / hospital / outcome is wrong or out of date.
Only fix cards that exist (card_id is always one of the CARDS); new calls are found by a separate check. Use null for any field you aren't changing. Leave out cards that are right. If everything is right, return an \
empty list. Never put a line on two cards. Leave out call-ins with no content and chatter."""

DELTA_SCHEMA = {"type": "object", "properties": {"fixes": {"type": "array", "items": {"type": "object", "properties": {
    "card_id": {"type": ["string", "null"]},
    "add": {"type": "array", "items": {"type": "string"}},
    "remove": {"type": "array", "items": {"type": "string"}},
    "unit": {"type": ["string", "null"]}, "location": {"type": ["string", "null"]}, "complaint": {"type": ["string", "null"]},
    "status": {"type": ["string", "null"], "enum": ["dispatched", "responding", "on scene", "transporting",
                                                      "at hospital", "clear", None]},
    "transport_level": {"type": ["string", "null"], "enum": ["ALS", "BLS", None]},
    "hospital": {"type": ["string", "null"]}, "outcome": {"type": ["string", "null"]}},
    "required": ["card_id", "add", "remove", "status"]}}}, "required": ["fixes"]}


SWEEP_SYSTEM = SYSTEM.split("You also get the current CARDS")[0] + """Your only job now: find CALLS THE BOARD MISSED. \
You get the CARDS already on the board and the RADIO (line ids, time, channel code: PE = Pro EMS, PI = Pro Intercept, \
FE = Fire EMS, FD = Fire dispatch). Lines already on a card end with [on <card id>]. Report a call only if the radio \
clearly shows a unit being sent to (or reporting it is going to) a place for a patient or medical reason, and NO card \
covers it (not even under a different address spelling or unit). For each: lines = its line ids (its dispatch first, \
then that unit's later traffic that isn't on another card), unit, location, complaint, status at its last line, and \
reason = a few words quoting what makes it a call. Fire-only calls (alarms, odors, no patient) don't count. If nothing \
was missed, return an empty list; when unsure, leave it out."""

SWEEP_SCHEMA = {"type": "object", "properties": {"missed": {"type": "array", "items": {"type": "object", "properties": {
    "lines": {"type": "array", "items": {"type": "string"}},
    "unit": {"type": ["string", "null"]}, "location": {"type": ["string", "null"]}, "complaint": {"type": ["string", "null"]},
    "status": {"type": ["string", "null"], "enum": ["dispatched", "responding", "on scene", "transporting",
                                                      "at hospital", "clear", None]},
    "reason": {"type": "string"}},
    "required": ["lines", "unit", "location", "complaint", "status", "reason"]}}}, "required": ["missed"]}


def build_sweep_prompt(recs, cards, card_of, not_calls=()):
    hm = lambda ms: time.strftime("%H:%M:%S", time.localtime(ms / 1000))
    ids, rows = {}, []
    for k, r in enumerate(recs, 1):
        rid = f"R{k}"
        ids[rid] = r["audio_url"]
        on = card_of(r["audio_url"])
        rows.append(f"{rid} {hm(r['time'])} {_tg(r['talkgroup_name'])}| {r['text']}" + (f" [on {on}]" if on else ""))
    crows = []
    for c in cards:
        units = c["units"].values() if isinstance(c["units"], dict) else c["units"]
        crows.append(f"{c['id']}: {c.get('complaint') or 'call'} @ {c.get('address') or c.get('place') or '?'} | "
                     f"{', '.join(u['unit'] for u in units) or 'no unit'}")
    p = "CARDS:\n" + ("\n".join(crows) or "(none)")
    if not_calls:
        p += "\n\nThe crew said these earlier AI finds were NOT calls (don't repeat that kind of mistake):\n" + \
             "\n".join(f"- {t[:160]}" for t in list(not_calls)[-8:])
    return p + f"\n\nRADIO ({len(rows)} lines):\n" + "\n".join(rows), ids


def _tg(name):
    low = (name or "").lower()
    return next((c for k, c in TG_CODE if k in low), "??")


def build_delta_prompt(recs, cards):
    hm = lambda ms: time.strftime("%H:%M:%S", time.localtime(ms / 1000))
    ids, url_to_id, rows = {}, {}, []
    for k, r in enumerate(recs, 1):
        rid = f"R{k}"
        ids[rid] = r["audio_url"]
        url_to_id[r["audio_url"]] = rid
        rows.append(f"{rid} {hm(r['time'])} {_tg(r['talkgroup_name'])}| {r['text']}")
    crows = []
    for c in cards:
        on = [url_to_id[e["audio_url"]] for e in c["timeline"] if e.get("audio_url") in url_to_id]
        earlier = sum(1 for e in c["timeline"] if e.get("audio_url") and e["audio_url"] not in url_to_id and not e.get("note"))
        if earlier:
            on.append(f"(+{earlier} earlier)")
        units = c["units"].values() if isinstance(c["units"], dict) else c["units"]
        us = ", ".join(f"{u['unit']} {u['status']}" for u in units) or "no unit"
        extra = " ".join(x for x in [c.get("transport_level"), ("-> " + c["hospital"]) if c.get("hospital") else None,
                                     c.get("outcome")] if x)
        crows.append(f"{c['id']}: {c.get('complaint') or 'call'} @ {c.get('address') or c.get('place') or '?'} | {us}"
                     f"{' | ' + extra if extra else ''} | lines {' '.join(on)}")
    return ("CARDS:\n" + ("\n".join(crows) or "(none)") + f"\n\nRADIO ({len(rows)} lines):\n" + "\n".join(rows)), ids


# ---------------------------------------------------------------- is this really the start of a call?
_CUE_RE = re.compile(r"\b(respond\w*|sign(?:ed)?[\s,-]*on|take|taking|head\w*\s+(?:over\s+|up\s+|out\s+)?to|"
                     r"go(?:ing)?\s+(?:over\s+)?to|private (?:call|response)|call for you|pick up|en ?route to|"
                     r"coming in (?:as|for)|for (?:a|an|the)\b|there'?s (?:one|a call)|i (?:have|got) (?:one|a call)|"
                     r"one (?:in|at|on) the\b|another one\b)", re.I)
_NOT_A_PLACE = re.compile(r"^\W*(unknown|unclear|none|n/?a|cambridge|boston|somerville|fire\s*alarm|pro\s*base|"
                          r"fire quarters.*|quarters.*|.*hospital.*|mount auburn|the mount|mgh|mass general|emerson.*|"
                          r"brigham.*|beth israel.*|lahey.*|spaulding.*|children'?s.*|st\.? elizabeth.*)\W*$", re.I)


def dispatch_anchor(lines, location, board_mod=None):
    """The line that starts a call the rules missed, or None if no line really does. A start needs a place that is
    actually said in that line (not a hospital, not 'unknown') and a dispatch cue ('respond', 'take', 'head over to',
    'for a fall' ...). Mid-call traffic ('transporting BLS to the Mount', '41, we're off at the Mount') never counts."""
    from board import parse, normalize
    loc = (location or "").strip()
    if not loc or _NOT_A_PLACE.match(loc) or not re.search(r"[A-Za-z]{3}", loc):
        return None
    toks = [t for t in re.findall(r"[A-Za-z0-9']+", loc) if (t.isdigit() or len(t) >= 4)
            and t.lower() not in ("street", "avenue", "road", "drive", "place", "cambridge", "boston", "unit", "apartment")]
    for r in lines:
        text = normalize(r.get("text") or "")
        p = parse(text, r.get("talkgroup_name", ""))
        said = bool(p["address"] or p["place"]) or any(re.search(r"\b%s\b" % re.escape(t), text, re.I) for t in toks)
        if not said:
            continue
        if p["hospital"] and not p["address"] and p["status"] in ("transporting", "at hospital", "clear"):
            continue                                        # a transport, not a new call
        if p["dispatch"] or p["weak_dispatch"] or p["fire_notify"] or _CUE_RE.search(text):
            return r
        if p["address"] and not p["status"] and not p["hospital"]:
            return r                                        # "Bounce over to 22 Kirkland Place"
    return None


def fixes_to_calls(fixes, board, ids):
    """Corrections -> the 'calls' shape apply_grouping understands (+ explicit removals)."""
    calls, removals = [], []
    for f in fixes:
        cid = f.get("card_id")
        keep_lines = list(f.get("add") or [])
        if cid:
            c = board.get(cid)
            if c:   # keep the card's current lines so apply_grouping recognises it
                url_ids = {v: k for k, v in ids.items()}
                keep_lines += [url_ids[e["audio_url"]] for e in c["timeline"] if e.get("audio_url") in url_ids
                               and url_ids[e["audio_url"]] not in (f.get("remove") or [])]
            for x in f.get("remove") or []:
                if ids.get(x):
                    removals.append((cid, ids[x]))
        calls.append({"card_id": cid, "unit": f.get("unit"), "location": f.get("location"), "complaint": f.get("complaint"),
                      "kind": "ems", "status": f.get("status"), "transport_level": f.get("transport_level"),
                      "hospital": f.get("hospital"), "outcome": f.get("outcome"), "lines": keep_lines})
    return calls, removals


class CallGrouper:
    """Runs the AI grouping pass for one Monitor (live board) in a background thread."""

    def __init__(self, reader, monitor, log, every_s=EVERY_S, max_per_hour=60):
        self.reader, self.m, self.log = reader, monitor, log
        self.every_s, self.max_per_hour = every_s, max_per_hour
        self.last_run, self.last_seen, self.recent = 0.0, 0, []
        self.status = "starting"
        self.window_ms = WINDOW_MS
        self.sweep_last, self.sweep_recent, self.sweep_seen = 0.0, [], set()
        self.sweep_status, self.sweep_found = "waiting", 0
        self.not_calls = []                  # first lines of AI finds the crew marked "not a call"
        threading.Thread(target=self._loop, name="ai-grouper", daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(5)
            try:
                if not self.reader.enabled():
                    self.status = "waiting for AI"
                    continue
                newest = max((r["time"] for r in list(self.m.calls) if r.get("text")), default=0)
                if time.time() - self.sweep_last >= SWEEP_EVERY_S and newest:
                    self._sweep_tick(newest)
                if newest <= self.last_seen or time.time() - self.last_run < self.every_s:
                    continue
                self.recent = [t for t in self.recent if time.time() - t < 3600]
                if len(self.recent) >= self.max_per_hour:
                    self.status = "hourly limit reached"
                    continue
                self.last_run = time.time()
                self.recent.append(self.last_run)
                self.last_seen = newest
                self.status = "busy"
                n = self.run_once(now_ms=newest)
                if n:
                    self.m._publish_board()
                    self.log(f"AI grouping: {n} change(s) to the call board")
                self.status = f"ready (last pass changed {n})"
            except Exception as e:
                self.status = f"error: {type(e).__name__}"
                if type(e).__name__ == "TimeoutExpired":
                    # too much radio for one pass on this machine: read a shorter stretch next time
                    self.window_ms = max(MIN_WINDOW_MS, int(self.window_ms * 0.6))
                    self.log(f"AI grouping: Claude took over {TIMEOUT_S} s; next passes read the last "
                             f"{self.window_ms // 60_000} min instead")
                else:
                    self.log(f"AI grouping failed: {type(e).__name__}: {str(e)[:200]}")

    def _sweep_tick(self, newest):
        self.sweep_last = time.time()
        self.sweep_recent = [t for t in self.sweep_recent if time.time() - t < 3600]
        if len(self.sweep_recent) >= SWEEP_MAX_PER_HOUR:
            self.sweep_status = "hourly limit reached"
            return
        try:
            n = self.sweep_once(now_ms=newest)
            if n is None:
                self.sweep_status = "nothing new to check"
                return
            self.sweep_recent.append(time.time())
            self.sweep_status = f"last check found {n} missed call(s)"
            if n:
                self.sweep_found += n
                self.m._publish_board()
                self.log(f"AI review: found {n} call(s) the rules missed (marked 'found by AI' for the crew to check)")
        except Exception as e:
            self.sweep_status = f"error: {type(e).__name__}"
            self.log(f"AI missed-call review failed: {type(e).__name__}: {str(e)[:200]}")

    def sweep_once(self, now_ms, recs=None):
        """Look for calls with no card. Returns the number of cards made, or None if there was nothing new to check
        (no Claude call is made unless radio that is on no card has come in since the last check)."""
        from board import informative
        recs = recs if recs is not None else [r for r in list(self.m.calls) if r.get("text")]
        recs = [r for r in recs if now_ms - SWEEP_MS <= r["time"] <= now_ms and r.get("audio_url")
                and "citywide" not in (r.get("talkgroup_name") or "").lower()][-SWEEP_LINES:]
        board = self.m.board
        loose = [r["audio_url"] for r in recs if not board.card_of(r["audio_url"])
                 and informative(r.get("text"), r.get("talkgroup_name", ""))]
        if not any(u not in self.sweep_seen for u in loose):
            return None
        self.sweep_seen.update(loose)
        snap = board.snapshot()
        cards = [c for c in snap["incidents"] + snap["closed"] if c["last"] >= now_ms - SWEEP_MS - 30 * 60_000
                 and not c.get("dismissed") and c.get("kind") not in ("fire",)]
        prompt, ids = build_sweep_prompt(recs, cards, board.card_of, self.not_calls)
        out = self.reader._run(prompt, SWEEP_SYSTEM, SWEEP_SCHEMA, timeout=TIMEOUT_S)
        by_url = {r["audio_url"]: r for r in recs}
        made = 0
        for call in (out or {}).get("missed") or []:
            lines = [x for x in call.get("lines") or [] if ids.get(str(x).strip())]
            urls = [ids[str(x).strip()] for x in lines]
            urls.sort(key=lambda u: by_url[u]["time"])
            anchor = dispatch_anchor([by_url[u] for u in urls if not board.card_of(u)], call.get("location"))
            if not anchor:
                key = (call.get("location") or "").lower()
                if urls and key not in getattr(self, "skipped", set()):
                    self.skipped = getattr(self, "skipped", set()) | {key}
                    self.log(f"AI review: skipped a possible call at {call.get('location')!r}: no line on the radio "
                             "actually sends a unit there")
                continue
            first = anchor["audio_url"]
            lines.sort(key=lambda x: by_url[ids[str(x).strip()]]["time"])
            lines.remove(next(x for x in lines if ids[str(x).strip()] == first))
            lines.insert(0, next(k for k, v in ids.items() if v == first))
            c = dict(call, card_id=None, kind="ems", transport_level=None, hospital=None, outcome=None,
                     lines=lines, found_by="review", ai_reason=str(call.get("reason") or "")[:160])
            before = {i["id"] for i in board.snapshot()["incidents"]}
            apply_grouping(board, [c], ids, by_url, self.log)
            made += len({i["id"] for i in board.snapshot()["incidents"]} - before)
        return made

    def run_once(self, now_ms, recs=None):
        recs = recs if recs is not None else [r for r in list(self.m.calls) if r.get("text")]
        win = getattr(self, "window_ms", WINDOW_MS)
        read_ms, cap = (min(win, ROLL_MS), ROLL_LINES) if FAST else (win, MAX_LINES)
        recs = [r for r in recs if now_ms - read_ms <= r["time"] <= now_ms and r.get("audio_url")
                and "citywide" not in (r.get("talkgroup_name") or "").lower()][-cap:]
        if len(recs) < 3:
            return 0
        snap = self.m.board.snapshot()
        cards = [c for c in snap["incidents"] + snap["closed"] if c["last"] >= now_ms - win
                 and not c.get("dismissed") and c.get("kind") not in ("fire",)]
        t0 = time.time()
        if FAST:
            prompt, ids = build_delta_prompt(recs, cards)
            out = self.reader._run(prompt, DELTA_SYSTEM, DELTA_SCHEMA, timeout=TIMEOUT_S)
            calls, removals = fixes_to_calls((out or {}).get("fixes") or [], self.m.board, ids)
            n = 0
            for cid, url in removals:
                if self.m.board.unit_line(cid, url):
                    continue                         # that unit's own traffic stays on its call
                if DEBUG:
                    self.log(f"  [ai] remove {url[-14:]} from {cid} ({_d(self.m.board, cid)})")
                if not self.m.board.crew_placed(url) and self.m.board.detach(cid, url, record=False):
                    n += 1
            res = {"calls": calls}
        else:
            prompt, ids = build_prompt(recs, cards)
            res = self.reader._run(prompt, SYSTEM, SCHEMA, timeout=TIMEOUT_S)
            n = 0
        self.last_took = time.time() - t0
        took = time.time() - t0
        if took < 60 and getattr(self, "window_ms", WINDOW_MS) < WINDOW_MS:
            self.window_ms = min(WINDOW_MS, self.window_ms + 5 * 60_000)     # fast again: widen back out
        if not isinstance(res, dict):
            return 0
        return n + apply_grouping(self.m.board, res.get("calls") or [], ids, {r["audio_url"]: r for r in recs}, self.log,
                                  allow_new=not FAST)


DEBUG = os.environ.get("AIGROUP_DEBUG") == "1"


def _d(board, iid):
    c = board.get(iid) or {}
    return f"{c.get('address') or c.get('place')} {list(c.get('units') or [])}"


def apply_grouping(board, calls, ids, recs_by_url, log=print, allow_new=True):
    """Reconcile the board with Claude's grouping. Returns how many changes were made. New cards are only made
    when allow_new, and only from a line that really starts a call (see dispatch_anchor)."""
    changes = 0
    valid = {c["id"] for c in board.snapshot()["incidents"]} | {c["id"] for c in board.snapshot()["closed"]}
    used = set()
    for call in calls:
        urls = []
        for x in call.get("lines") or []:
            u = ids.get(str(x).strip())
            if u and u not in used:
                urls.append(u)
                used.add(u)
        if not urls:
            continue
        # which card is this call? Claude's pick, else the card holding most of its lines
        holders = {}
        for u in urls:
            h = board.card_of(u)
            if h:
                holders[h] = holders.get(h, 0) + 1
        target = call.get("card_id") if call.get("card_id") in valid else None
        if not target and holders:
            target = max(holders, key=holders.get)
        if not target:
            if not allow_new or not call.get("location") or call.get("kind") != "ems":
                continue                                   # nothing to anchor a new card on
            first = dispatch_anchor([recs_by_url[u] for u in urls if u in recs_by_url], call.get("location"))
            if not first:
                log(f"AI: ignored a possible call with no dispatch line ({call.get('location')})")
                continue
            target = board.create_from_ai(first, call)
            if not target:
                continue
            valid.add(target)
            changes += 1
        # merge other cards that hold this call's lines (same incident split in two)
        for h in list(holders):
            h2 = board.resolve(h)
            if h2 != board.resolve(target) and holders[h] >= 1:
                other = board.get(h2)
                if other and all(e.get("audio_url") in urls or e.get("note") or not e.get("audio_url")
                                 for e in other["timeline"]) and not board.crew_touched(h2):
                    if DEBUG:
                        log(f"  [ai] merge {h2} ({_d(board, h2)}) into {target} ({_d(board, target)})")
                    if board.merge(h2, target, why="AI: same incident"):
                        changes += 1
        target = board.resolve(target)
        from board import informative
        for u in urls:
            if board.crew_placed(u) or board.other_speaker(u):
                if board.other_speaker(u) and recs_by_url.get(u):
                    # don't let a line we refused still set the status / outcome
                    from board import parse as _parse
                    txt = recs_by_url[u].get("text") or ""
                    if _parse(txt, recs_by_url[u].get("talkgroup_name", ""))["status"] == call.get("status"):
                        call = dict(call, status=None)
                    if re.search(r"\b(AMA|refus\w*|cancel\w*)\b", txt, re.I):
                        call = dict(call, outcome=None)
                continue                     # crew decided, or an unidentified caller said it (not this unit's report)
            r = recs_by_url.get(u)
            if r and not informative(r.get("text"), r.get("talkgroup_name", "")):
                continue                     # call-ins / "go ahead" only say who is talking: keep cards uncluttered
            h = board.card_of(u)
            if h == target:
                continue
            if h:
                if board.unit_line(h, u, to_iid=target):
                    continue                 # "Paramedic 5, on location" stays with Paramedic 5's call
                if DEBUG:
                    log(f"  [ai] move \"{recs_by_url.get(u, {}).get('text', '')[:50]}\" {h} ({_d(board, h)}) -> {target} ({_d(board, target)})")
                if board.detach(h, u, to_iid=target, record=False):
                    changes += 1
            elif recs_by_url.get(u):
                if DEBUG:
                    log(f"  [ai] attach \"{recs_by_url[u].get('text', '')[:50]}\" -> {target} ({_d(board, target)})")
                if board.attach(target, recs_by_url[u]):
                    changes += 1
        if DEBUG:
            log(f"  [ai] set {target} ({_d(board, target)}): unit={call.get('unit')} status={call.get('status')}")
        if board.set_from_ai(target, call):
            changes += 1
    return changes

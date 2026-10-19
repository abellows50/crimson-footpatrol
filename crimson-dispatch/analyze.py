"""
Analyze: run a long stretch of one talkgroup (Pro EMS, TG 1227) through the normal pipeline, a day at a time,
and count dispatches and Harvard calls by hour of day and day of week (the /analyze page).

* Calls are listed from OpenMHz for that talkgroup only, one Eastern day at a time, with a pause between pages.
* A clip already transcribed in any log (live, replay, demo cache) reuses that transcript; only new clips go
  to Whisper, and live traffic always goes first. Long mode (--long) applies as everywhere else.
* Every processed transmission is kept in logs/replay-analyze-calls.jsonl, so analyses add up across runs and a
  window that was already analyzed costs nothing the second time. Harvard alerts go to logs/analyze-alerts.json,
  and the stretches of time covered to logs/analyze-coverage.json (so averages only count hours we looked at).
* "Re-score" re-runs everything already analyzed through today's rules from the transcripts on file (no downloads,
  no Whisper), so a rule change shows up in the charts at once. Crew rulings remember the call's radio clips, so
  they stay attached to the same call when its alerts are rebuilt.
"""
import argparse
import collections
import re
import datetime as dt
import glob
import hashlib
import json
import os
import threading
import time
import urllib.parse
from zoneinfo import ZoneInfo

from board import Board

TZ = ZoneInfo("America/New_York")
TALKGROUP = "1227"                       # Cambridge - Pro EMS
REFRESH_EVERY = 50                       # rebuild the page's results every N newly analyzed calls
PAGE_PAUSE_S = 1.5                       # between OpenMHz listing pages: stay polite
LOG_TAG = "analyze"                      # the private Monitor logs to logs/replay-analyze-calls.jsonl
RESCORE_TAG = "analyze-rescore"          # a re-score builds logs/replay-analyze-rescore-*.jsonl, then swaps them in
ALERTS = "analyze-alerts.json"
COVERAGE = "analyze-coverage.json"
# Crew rulings. Ruled out: {"alerts": {id: ruling}, "dispatches": {id: ruling}};
# confirmed Harvard: {"harvard_alerts": {...}, "harvard_dispatches": {...}}.
# A ruling is {"by", "at", "urls": [the call's audio clips], "reviewed": bool}: it applies to any alert or dispatch
# sharing a clip, so it survives a re-score (which gives alerts new ids).
NOT_HARVARD = "analyze-not-harvard.json"
KNOWN = b"analyze-known:"


def _now_ms():
    return int(time.time() * 1000)


def _day_start(ms):
    d = dt.datetime.fromtimestamp(ms / 1000, TZ)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def _next_day(ms):
    d = dt.datetime.fromtimestamp(ms / 1000, TZ).date() + dt.timedelta(days=1)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() * 1000)


def parse_day(s):
    """'YYYY-MM-DD' (Eastern) -> epoch ms of that midnight."""
    try:
        d = dt.date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        raise ValueError(f"bad date {s!r}; use YYYY-MM-DD")
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() * 1000)


def _iso_ms(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _merge(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _read_jsonl(path):
    try:
        with open(path) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass
    except OSError:
        return


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class _KnownFetcher:
    """Clips we already have a transcript for are never downloaded: the 'audio' is just a marker + the URL."""
    def __init__(self, real, known):
        self.real, self.known = real, known
        self.name = getattr(real, "name", "?")

    def get_json(self, url):
        return self.real.get_json(url)

    def get_bytes(self, url):
        if url in self.known:
            return KNOWN + url.encode()
        return self.real.get_bytes(url)


class _KnownTranscriber:
    def __init__(self, real, known):
        self.real, self.known = real, known
        self.name = getattr(real, "name", "?")
        self.last_rescued = False

    def transcribe(self, path, prompt):
        with open(path, "rb") as f:
            head = f.read(len(KNOWN) + 4096)
        if head.startswith(KNOWN):
            self.last_rescued = False
            return self.known.get(head[len(KNOWN):].decode(), "")
        text = self.real.transcribe(path, prompt)
        self.last_rescued = getattr(getattr(self.real, "inner", self.real), "last_rescued", False)
        return text


class AnalyzeJob:
    def __init__(self, monitor, make_child, start_ms, end_ms, log_dir, log, tg=TALKGROUP):
        self.monitor, self.make_child, self.log_dir, self.log, self.tg = monitor, make_child, log_dir, log, tg
        self.start_ms, self.end_ms = start_ms, end_ms
        self.state, self.error = "starting", None
        self.cancelled = False
        self.created, self.finished = _now_ms(), None
        self.days_total = 0
        self.days_done = 0
        self.day = None                      # start of the day being worked on
        self.listed = self.done = 0          # calls in the current day / processed so far in it
        self.counts = {"processed": 0, "already": 0, "reused": 0, "whisper": 0, "skipped_short": 0, "failed": 0,
                       "alerts": 0}
        self.child = None
        self.partial = None                  # [day start, last call handled] of the day in progress

    def running(self):
        return self.finished is None

    def cancel(self):
        self.cancelled = True

    def info(self):
        return {"state": self.state, "error": self.error, "start": self.start_ms, "end": self.end_ms,
                "days_total": self.days_total, "days_done": self.days_done, "day": self.day,
                "listed": self.listed, "done": self.done, "counts": dict(self.counts),
                "created": self.created, "finished": self.finished,
                "long": getattr(self.monitor.args, "long", None)}

    # -- data
    def _known(self):
        """Transcripts we already have for this talkgroup, by audio URL (the words Whisper heard, before
        crew fixes and the junk filter, so today's pipeline re-applies them)."""
        files = (sorted(glob.glob(os.path.join(self.log_dir, "calls-*.jsonl")))
                 + sorted(glob.glob(os.path.join(self.log_dir, "replay-2*-calls.jsonl")))
                 + [os.path.join(self.log_dir, "demo-cache.jsonl")])
        known = {}
        for f in files:
            for r in _read_jsonl(f):
                if str(r.get("talkgroup")) == self.tg and r.get("audio_url") and not r.get("error"):
                    known[r["audio_url"]] = r.get("asr_text") or r.get("raw_text") or r.get("text") or ""
        return known

    def _list_day(self, start_ms, end_ms):
        a = self.monitor.args
        base = f"{a.api_base.rstrip('/')}/{self.monitor.config.get()['system']}/calls/older?"
        found, cursor = {}, end_ms
        for _ in range(400):
            if self.cancelled:
                break
            q = {"filter-type": "talkgroup", "filter-code": self.tg, "time": cursor}
            batch = self.monitor.fetcher.get_json(base + urllib.parse.urlencode(q)).get("calls", [])
            if not batch:
                break
            times = [_iso_ms(c["time"]) for c in batch]
            for c, t in zip(batch, times):
                if start_ms <= t < end_ms:
                    found[c["_id"]] = c
            self.listed = len(found)
            oldest = min(times)
            if oldest >= cursor or oldest < start_ms:
                break
            cursor = oldest
            time.sleep(PAGE_PAUSE_S)
        return sorted(found.values(), key=lambda c: c["time"])

    def _save_alerts(self):
        path = os.path.join(self.log_dir, ALERTS)
        saved = _read_json(path, {})
        for a in list(self.child.alerts):
            if not a.get("test"):
                saved[a["id"]] = a
        _write_json(path, saved)

    def _add_coverage(self, a, b):
        path = os.path.join(self.log_dir, COVERAGE)
        cov = _read_json(path, {})
        cov[self.tg] = _merge(cov.get(self.tg, []) + [[a, b]])
        _write_json(path, cov)

    # -- run
    def run(self):
        try:
            self.state = "loading transcripts"
            known = self._known()
            store = os.path.join(self.log_dir, f"replay-{LOG_TAG}-calls.jsonl")
            have = {r.get("id") for r in _read_jsonl(store) if not r.get("error")}   # failed clips get retried
            self.log(f"Analyze: {len(known)} transcripts on file for TG {self.tg}; {len(have)} calls already analyzed")
            if self.monitor.transcriber is None:
                self.state = "waiting for Whisper"
            while self.monitor.transcriber is None and not self.cancelled:
                if str(self.monitor.status.get("transcriber", "")).startswith("FAILED"):
                    raise RuntimeError("transcriber failed to load")
                time.sleep(1)
            self.child = self.make_child(_KnownFetcher(self.monitor.fetcher, known))
            self.child.alerts = collections.deque()      # a month of alerts: don't drop old ones before they're saved
            self.child.log_tag = LOG_TAG
            self.child.feedback = self.monitor.feedback
            self.child.transcriber = _KnownTranscriber(self.monitor.transcriber, known)

            days, d = [], _day_start(self.start_ms)
            while d < self.end_ms:
                days.append((max(d, self.start_ms), min(_next_day(d), self.end_ms)))
                d = _next_day(d)
            self.days_total = len(days)
            for a, b in days:
                if self.cancelled:
                    break
                self.day, self.listed, self.done = a, 0, 0
                self.state = "listing"
                calls = self._list_day(a, b)
                self.state = "transcribing"
                last = a
                for c in calls:
                    if self.cancelled:
                        break
                    while self.monitor.work.qsize() and not self.cancelled:
                        time.sleep(0.5)                 # live traffic always goes first
                    self.done += 1
                    if c["_id"] in have:
                        self.counts["already"] += 1
                        last = _iso_ms(c["time"])
                        continue
                    skipped_before = self.child.status.get("skipped_short", 0)
                    reused = c.get("url") in known
                    try:
                        self.child.process(c, backfill=False)
                    except Exception as e:
                        self.log(f"Analyze: couldn't process a call ({type(e).__name__}: {e})")
                        continue
                    last = _iso_ms(c["time"])
                    self.partial = [a, last]
                    if self.child.status.get("skipped_short", 0) > skipped_before:
                        self.counts["skipped_short"] += 1
                        continue
                    rec = self.child.calls[-1] if self.child.calls else {}
                    if rec.get("id") == c["_id"] and rec.get("error"):
                        self.counts["failed"] += 1      # download / Whisper failed: not marked done, retried next run
                        continue
                    have.add(c["_id"])
                    self.counts["processed"] += 1
                    self.counts["reused" if reused else "whisper"] += 1
                    if self.counts["processed"] % REFRESH_EVERY == 0:
                        self.counts["alerts"] = len(self.child.alerts)
                        self._save_alerts()             # so the page's next refresh includes them
                self.counts["alerts"] = len(self.child.alerts)
                self._save_alerts()
                # a cancelled day only counts as covered up to the last call handled
                self._add_coverage(a, b if not self.cancelled else max(a, last))
                self.partial = None
                if not self.cancelled:
                    self.days_done += 1
                self.log(f"Analyze: {dt.datetime.fromtimestamp(a / 1000, TZ):%a %m-%d} done "
                         f"({len(calls)} calls; {self.days_done}/{self.days_total} days)")
            self.state = "cancelled" if self.cancelled else "done"
        except Exception as e:
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            self.log(f"Analyze failed: {self.error}")
        self.finished = _now_ms()


class _NoWhisper:
    """A re-score never transcribes: every clip already has its transcript on file."""
    name = "re-score (transcripts on file)"

    def transcribe(self, path, prompt):
        return ""


class RescoreJob:
    """Re-run every analyzed transmission through today's pipeline (junk filter, crew transcript fixes, Harvard
    rules, call board, alert grouping) from the transcripts on file. Nothing is replaced unless it finishes."""
    kind = "rescore"

    def __init__(self, analyzer):
        self.an = analyzer
        self.state, self.error = "starting", None
        self.cancelled = False
        self.created, self.finished = _now_ms(), None
        self.total = self.done = 0
        self.days_done = 0
        self.partial = None
        self.counts = {"processed": 0, "alerts": 0}

    def running(self):
        return self.finished is None

    def cancel(self):
        self.cancelled = True

    def info(self):
        return {"kind": self.kind, "state": self.state, "error": self.error, "total": self.total, "done": self.done,
                "counts": dict(self.counts), "created": self.created, "finished": self.finished}

    def run(self):
        an, d = self.an, self.an.log_dir
        store = os.path.join(d, f"replay-{LOG_TAG}-calls.jsonl")
        out_calls = os.path.join(d, f"replay-{RESCORE_TAG}-calls.jsonl")
        out_alerts = os.path.join(d, f"replay-{RESCORE_TAG}-alerts.jsonl")
        try:
            for p in (out_calls, out_alerts):
                if os.path.exists(p):
                    os.remove(p)
            an.remember_ruling_urls()
            recs = {}
            for r in _read_jsonl(store):
                if r.get("id") and r.get("audio_url") and not r.get("error"):
                    recs[r["id"]] = r
            recs = sorted(recs.values(), key=lambda r: r["time"])
            self.total = len(recs)
            # the words Whisper heard (before crew fixes and the junk filter, which today's pipeline re-applies)
            known = {r["audio_url"]: r.get("asr_text") or r.get("raw_text") or r.get("text") or "" for r in recs}
            # the rules this re-score uses (an edit while it runs gets a re-score of its own afterwards)
            sig = rules_signature(an.monitor.config.get())
            child = an.make_child(_KnownFetcher(an.monitor.fetcher, known))
            child.log_tag = RESCORE_TAG
            child.feedback = an.monitor.feedback
            child.transcriber = _KnownTranscriber(_NoWhisper(), known)
            child.args = argparse.Namespace(**{**vars(child.args), "long": None})   # keep every call already analyzed
            child.alerts = collections.deque()
            self.state = "re-scoring"
            for r in recs:
                if self.cancelled:
                    break
                call = {"_id": r["id"], "talkgroupNum": r.get("talkgroup"), "url": r["audio_url"], "len": r.get("len"),
                        "time": dt.datetime.fromtimestamp(r["time"] / 1000, dt.timezone.utc).isoformat().replace("+00:00", "Z")}
                try:
                    child.process(call, backfill=False)
                    self.counts["processed"] += 1
                except Exception as e:
                    an.log(f"Re-score: couldn't process a call ({type(e).__name__}: {e}); keeping it as it was")
                    with open(out_calls, "a") as f:     # never lose an analyzed call
                        f.write(json.dumps(r) + "\n")
                self.done += 1
                self.counts["alerts"] = len(child.alerts)
            if self.cancelled:
                for p in (out_calls, out_alerts):
                    if os.path.exists(p):
                        os.remove(p)
                self.state = "cancelled"
            else:
                with an.lock:
                    if os.path.exists(out_calls):
                        os.replace(out_calls, store)
                    _write_json(os.path.join(d, ALERTS), {a["id"]: a for a in child.alerts if not a.get("test")})
                    if os.path.exists(out_alerts):
                        os.replace(out_alerts, os.path.join(d, f"replay-{LOG_TAG}-alerts.jsonl"))
                self.state = "done"
                an.mark_scored(sig)
                an.log(f"Re-score: {self.done} calls, {len(child.alerts)} Harvard alerts with today's rules")
                if rules_signature(an.monitor.config.get()) != sig:
                    an.rescore_if_rules_changed(5.0)     # the rules changed again while it ran
        except Exception as e:
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            an.log(f"Re-score failed: {self.error}")
        self.finished = _now_ms()
        an._dq_version += 1
        an.warm()


RULING_KINDS = ("alerts", "dispatches", "harvard_alerts", "harvard_dispatches")
HUHS_TEXT = re.compile(r"\b(huhs|hhs|h2hs|harvard (university )?health services)\b", re.I)


def is_huhs(alert):
    """A call at Harvard University Health Services (Smith Campus Center, 75 Mount Auburn St)."""
    for h in alert.get("hits") or []:
        blob = " ".join(str(h.get(k) or "") for k in ("term", "note", "where")).lower()
        if "health services" in blob or "75 mount auburn st" in blob:
            return True
    return bool(HUHS_TEXT.search(alert.get("context") or alert.get("text") or ""))
ADAMS_HOUSE = (42.37182, -71.116627)        # Russell Hall, 26 Plympton St: "near Harvard College" is measured from here
NEAR_MINUTES = 10                           # "near Harvard College" = within this many minutes' walk of Adams House


def walk_minutes(lat, lon, origin=ADAMS_HOUSE):
    """Same estimate as the dashboard (geo.travel): straight line x 1.35 for real streets, at a brisk 1.35 m/s."""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (origin[0], origin[1], lat, lon))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    d = 2 * 6371000 * math.asin(math.sqrt(h))
    return max(1, round(d * 1.35 / 1.35 / 60))
META = "analyze-meta.json"                  # {"rules": signature of the rules the analyzed calls were scored with}


_HERE = os.path.dirname(os.path.abspath(__file__))


def _hash_files(names):
    h = hashlib.sha1()
    for name in names:
        try:
            with open(os.path.join(_HERE, name), "rb") as f:
                h.update(f.read())
        except OSError:
            pass
    return h.hexdigest()


# the code that decides "Harvard or not" (matching, address lookups, the junk filter, the call board's fire / EMS
# call, alert grouping in server.py), as this process loaded it: an edit on disk counts once the server restarts
_CODE = _hash_files(["matcher.py", "parcels.py", "server.py", "board.py", "textfilter.py"])
_DATA = {}                                  # address-data file -> (mtime, size, hash)


def _data_hash():
    path = os.path.join(_HERE, "data", "cambridge-addresses.json")
    try:
        st = os.stat(path)
    except OSError:
        return ""
    key = (st.st_mtime, st.st_size)
    if _DATA.get(path, (None,))[:1] != (key,):
        _DATA[path] = (key, _hash_files([path]))
    return _DATA[path][1]


def rules_signature(config):
    """Changes whenever anything that decides "Harvard or not" changes: the rules, the crew's list, the alarm
    filters, the city/Harvard address data, or the code itself."""
    h = hashlib.sha1()
    cfg = {k: config.get(k) for k in ("rules", "crew_places", "not_harvard_places", "address_ranges", "whisper_prompt",
                                      "alarm_filters")}
    h.update(json.dumps(cfg, sort_keys=True).encode())
    h.update(_CODE.encode())
    h.update(_data_hash().encode())
    return h.hexdigest()[:16]


class Analyzer:
    """Owns the one running job and the (cached) results for the page."""
    def __init__(self, monitor, make_child, log_dir, log, tg=TALKGROUP):
        self.monitor, self.make_child, self.log_dir, self.log, self.tg = monitor, make_child, log_dir, log, tg
        self.job = None
        self.lock = threading.Lock()
        self._cache = (None, None)
        self._dq_version = 0
        self._timer = None
        self._walk = {}                     # (dispatch id, where, data build) -> [lat, lon, place] or None
        self._judged = {}                   # (rules signature, text) -> judge() / explain() result
        self._awalk = {}                    # (alert id, data build) -> [lat, lon, place] or None
        self._texts = {}                    # ("alerts" | "dispatches", id) -> its transmissions' text (latest results)

    def _urls_of(self, kind, item_id):
        """The audio clips of an alert or a dispatch in the latest results."""
        res = self._cache[1] or self.results()
        if kind == "alerts":
            return next((set(a["urls"]) for a in res.get("harvard", []) if a["id"] == item_id), set())
        return next(({e["audio_url"] for e in c["timeline"] if e.get("audio_url")}
                     for c in res.get("calls", []) if c["id"] == item_id), set())

    def set_ruling(self, kind, item_id, ruling, by=None):
        """ruling: 'not' (not a Harvard call), 'harvard' (it is one), 'clear' (back to what the rules say) or 'keep'
        (the crew has seen that the rules now disagree and keeps its ruling), for an alert or a dispatch.
        A ruling applies to every alert / dispatch sharing one of its radio clips."""
        if kind not in ("alerts", "dispatches") or not item_id or ruling not in ("not", "harvard", "clear", "keep"):
            raise ValueError("kind must be 'alerts' or 'dispatches' with an id, and ruling 'not', 'harvard', 'clear' or 'keep'")
        path, item_id = os.path.join(self.log_dir, NOT_HARVARD), str(item_id)
        urls = self._urls_of(kind, item_id)
        with self.lock:
            dq = _read_json(path, {})
            for k in RULING_KINDS:
                dq.setdefault(k, {})
            mine = [(k, rid) for k in RULING_KINDS for rid, r in dq[k].items()
                    if rid == item_id or (urls and urls & set(r.get("urls") or []))]
            if ruling == "keep":
                for k, rid in mine:
                    dq[k][rid]["reviewed"] = True
            else:
                for k, rid in mine:          # a new ruling replaces any earlier one on this call, from either side
                    dq[k].pop(rid, None)
                if ruling != "clear":
                    dq[kind if ruling == "not" else "harvard_" + kind][item_id] = {
                        "by": (by or "")[:40], "at": _now_ms(), "urls": sorted(urls)}
            # "Follow the new rules": until the re-score with these rules lands, show the verdict the button promised
            sig = rules_signature(self.monitor.config.get())
            cleared = {k: v for k, v in (dq.get("cleared") or {}).items() if v.get("sig") == sig}
            if ruling == "clear":
                level, _ = self.judge_texts(self._texts.get((kind, item_id)) or [])
                cleared[item_id] = {"kind": kind, "urls": sorted(urls), "sig": sig, "level": level, "at": _now_ms()}
            else:
                cleared.pop(item_id, None)
            dq["cleared"] = cleared
            _write_json(path, dq)
            self._dq_version += 1
        return dq

    def remember_ruling_urls(self):
        """Older rulings only knew the alert id: record their clips so they survive a re-score."""
        path = os.path.join(self.log_dir, NOT_HARVARD)
        with self.lock:
            dq = _read_json(path, {})
            alerts = _read_json(os.path.join(self.log_dir, ALERTS), {})
            res = self._cache[1] or {}
            calls = {c["id"]: c for c in res.get("calls", [])}
            changed = False
            for k in RULING_KINDS:
                for rid, r in dq.get(k, {}).items():
                    if r.get("urls"):
                        continue
                    if k.endswith("alerts") and rid in alerts:
                        a = alerts[rid]
                        r["urls"] = sorted({x.get("audio_url") for x in a.get("transmissions") or [] if x.get("audio_url")}
                                           or {a.get("audio_url")} - {None})
                    elif k.endswith("dispatches") and rid in calls:
                        r["urls"] = sorted({e["audio_url"] for e in calls[rid]["timeline"] if e.get("audio_url")})
                    changed = changed or bool(r.get("urls"))
            if changed:
                _write_json(path, dq)

    def dispatch_point(self, c):
        """[lat, lon, place] of a dispatch (its address or street corner), or None if unknown / another town /
        farther than the address table covers (about a 25-minute walk from Adams House)."""
        m = getattr(self.monitor, "matcher", None)
        loc = getattr(m, "locator", None)
        if not loc or not loc.ok:
            return None
        key = (c["id"], c.get("where"), getattr(loc, "built", None))
        if key in self._walk:
            return self._walk[key]
        from matcher import tokenize, detect_town, number_readings
        texts = [c.get("where") or ""] + [e["text"] for e in c["timeline"] if not e["note"]][:3]
        out = None
        if not (c.get("town") and c["town"].lower() != "cambridge"):
            for t in texts:
                if not t:
                    continue
                toks = tokenize(t)
                if detect_town(toks)[0]:
                    break
                pt = loc.point(toks, number_readings)
                if pt:
                    out = [pt[0], pt[1], pt[2]]
                    break
        self._walk[key] = out
        return out

    def dispatch_walk(self, c):
        """Walking minutes from Adams House to a dispatch, or None (see dispatch_point)."""
        p = self.dispatch_point(c)
        return walk_minutes(p[0], p[1]) if p else None

    def _memo(self, kind, text, fn):
        cfg = getattr(getattr(self.monitor, "config", None), "get", lambda: {})()
        key = (kind, rules_signature(cfg) if cfg else None, text)
        if key not in self._judged:
            if len(self._judged) > 20000:
                self._judged.clear()
            self._judged[key] = fn(text)
        return self._judged[key]

    def alert_point(self, a, call):
        """[lat, lon, place] of a Harvard call: the Harvard address / corner it names, else the named Harvard place it
        matched ("Canaday Hall", "Quincy House"), else any address it names, else its dispatch's; None if unknown."""
        m = getattr(self.monitor, "matcher", None)
        loc = getattr(m, "locator", None)
        if not loc or not loc.ok:
            return None
        key = (a["id"], getattr(loc, "built", None))
        if key in self._awalk:
            return self._awalk[key]
        from matcher import tokenize, detect_town, number_readings
        out = None
        toks = tokenize(a.get("context") or a.get("text") or "")
        if not detect_town(toks)[0]:
            # the place that made it a Harvard call comes first: its Harvard address / corner, then a named Harvard place
            pts = [loc.point(tokenize(w), number_readings) for w in a.get("wheres") or []]
            pts = [[p[0], p[1], p[2]] for p in pts if p]
            if not pts:
                pts = [[pp[0], pp[1], t.split(" (")[0]] for t in a.get("terms") or [] for pp in [loc.place_point(t)] if pp]
            if not pts:
                pt = loc.point(toks, number_readings)
                pts = [[pt[0], pt[1], pt[2]]] if pt else []
            if pts:
                out = min(pts, key=lambda p: walk_minutes(p[0], p[1]))
            elif call is not None:
                out = self.dispatch_point(call)
        self._awalk[key] = out
        return out

    def alert_walk(self, a, call):
        """Walking minutes from Adams House to a Harvard call, or None (see alert_point)."""
        p = self.alert_point(a, call)
        return walk_minutes(p[0], p[1]) if p else None

    def judge(self, text):
        """'high' | 'possible' | 'none': what today's rules make of this call (remembered until the rules change)."""
        return self._memo("judge", text, self._judge)

    def _judge(self, text):
        m = getattr(self.monitor, "matcher", None)
        hits = m.match(text) if m and text else []
        return "high" if any(h["level"] == "high" for h in hits) else ("possible" if hits else "none")

    def judge_texts(self, texts):
        """(verdict, text) for a call made of several transmissions, the way the pipeline sees it: each transmission
        on its own and together with the one before it; the strongest verdict wins."""
        texts = [t for t in texts if t]
        cands = texts + [f"{a}. {b}" for a, b in zip(texts, texts[1:])]
        rank = {"high": 2, "possible": 1, "none": 0}
        best = ("none", texts[0] if texts else "")
        for t in cands:
            v = self.judge(t)
            if rank[v] > rank[best[0]]:
                best = (v, t)
        return best

    def explain(self, text):
        """Plain-language reasons for what today's rules decide about a call (for the conflicts list)."""
        return self._memo("explain", text, self._explain)

    def _explain(self, text):
        m = getattr(self.monitor, "matcher", None)
        if not m or not text:
            return []
        hits = m.match(text)
        high = [h for h in hits if h["level"] == "high"]
        if high:
            return [h["note"] if h.get("note", "").startswith(h["term"].split(" (")[0])
                    else h["term"] + (f": {h['note']}" if h.get("note") else "") for h in high]
        ex = m.explain(text) if hasattr(m, "explain") else {"locations": [], "town": None}
        out = []
        if ex.get("town"):
            out.append(f"names another town ({ex['town']}), so Cambridge addresses don't apply")
        verdict = {"harvard": "Harvard", "not": "not Harvard", "possible": "possibly Harvard", "unknown": "unknown"}
        out += [f"{l['why']} → {verdict.get(l['verdict'], l['verdict'])}" for l in ex.get("locations", [])]
        poss = [h for h in hits if h["level"] != "high"]
        if poss:
            out.append("only a possible (amber) hint: " + ", ".join(h["term"] for h in poss))
        return out or ["nothing in the call names HUPD, a Harvard place, or a Harvard address"]

    def scored_with(self):
        return _read_json(os.path.join(self.log_dir, META), {}).get("rules")

    def mark_scored(self, sig=None):
        _write_json(os.path.join(self.log_dir, META), {"rules": sig or rules_signature(self.monitor.config.get()),
                                                         "at": _now_ms()})

    def rescore_if_rules_changed(self, delay=20.0):
        """Re-score everything already analyzed when the rules (or address data, or code) changed since it was
        scored. Debounced: several quick Settings edits lead to one re-score."""
        if self._timer:
            self._timer.cancel()

        def go():
            self._timer = None
            store = os.path.join(self.log_dir, f"replay-{LOG_TAG}-calls.jsonl")
            if not os.path.exists(store) or self.scored_with() == rules_signature(self.monitor.config.get()):
                self.warm()
                return
            if self.job and self.job.running():
                self.rescore_if_rules_changed(60.0)          # an analysis is running: try again later
                return
            self.log("Analyze: the rules changed since these calls were scored; re-scoring with the current rules")
            try:
                self.rescore()
            except RuntimeError:
                self.rescore_if_rules_changed(60.0)

        self._timer = threading.Timer(delay, go)
        self._timer.daemon = True
        self._timer.start()

    def warm(self):
        """Build the page's results in the background, so the first visit to /analyze doesn't wait for them."""
        def go():
            try:
                self.results()
            except Exception as e:
                self.log(f"Analyze: couldn't prepare results ({type(e).__name__}: {e})")
        threading.Thread(target=go, name="analyze-warm", daemon=True).start()

    def rescore(self):
        with self.lock:
            if self.job and self.job.running():
                raise RuntimeError("an analysis is already running; wait for it or cancel it first")
            self.job = RescoreJob(self)
        threading.Thread(target=self.job.run, name="analyze-rescore", daemon=True).start()
        return self.job.info()

    def start(self, start_day, end_day):
        start_ms, end_ms = parse_day(start_day), _next_day(parse_day(end_day))   # 'to' day is inclusive
        end_ms = min(end_ms, _now_ms())
        if end_ms <= start_ms:
            raise ValueError("'To' must be on or after 'From', and not in the future.")
        with self.lock:
            if self.job and self.job.running():
                raise RuntimeError("an analysis is already running; cancel it first")
            self.job = AnalyzeJob(self.monitor, self.make_child, start_ms, end_ms, self.log_dir, self.log, self.tg)
        threading.Thread(target=self.job.run, name="analyze", daemon=True).start()
        return self.job.info()

    def cancel(self):
        if self.job and self.job.running():
            self.job.cancel()
        return self.job.info() if self.job else None

    def results(self):
        """Dispatches (call-board incidents rebuilt from every analyzed transmission), Harvard alerts and
        coverage. While a job runs this is rebuilt every REFRESH_EVERY analyzed calls (and at each day's end);
        otherwise only when the files change."""
        paths = [os.path.join(self.log_dir, n) for n in (f"replay-{LOG_TAG}-calls.jsonl", ALERTS, COVERAGE)]
        job = self.job
        if job and job.running() and getattr(job, "kind", "") == "rescore":
            sig = ("rescore", id(job), self._dq_version)    # the old results until it's done (plus the crew's clicks)
        elif job and job.running():
            sig = ("live", id(job), job.counts["processed"] // REFRESH_EVERY, job.days_done, job.state, self._dq_version)
        else:
            cfg = getattr(getattr(self.monitor, "config", None), "get", lambda: {})()
            sig = tuple((os.path.getmtime(p), os.path.getsize(p)) if os.path.exists(p) else None for p in paths) + \
                (self._dq_version, rules_signature(cfg))      # new rules -> conflicts are judged again
        if self._cache[0] == sig:
            return self._cache[1]
        partial = getattr(job, "partial", None) if job and job.running() else None
        recs = {}
        for r in _read_jsonl(paths[0]):
            if str(r.get("talkgroup")) == self.tg and r.get("id"):
                recs[r["id"]] = r
        board, incidents = Board(), {}
        for r in sorted(recs.values(), key=lambda r: r["time"]):
            try:
                board.ingest(r)
            except Exception:
                continue
            for inc in board.incidents.values():
                incidents[inc["id"]] = inc
        calls = [{"id": iid, "t": inc["opened"], "last": inc.get("last"), "complaint": inc.get("complaint"),
                  "where": inc.get("address") or inc.get("place"), "town": inc.get("town"),
                  "harvard": inc.get("harvard"), "harvard_terms": inc.get("harvard_terms") or [],
                  "age": inc.get("age"), "level": inc.get("level"), "acuity": inc.get("acuity"),
                  "hospital": inc.get("hospital"), "outcome": inc.get("outcome"), "kind": inc.get("kind"),
                  "units": [u["unit"] for u in (inc.get("units") or {}).values()],
                  "timeline": [{"t": e["time"], "text": e.get("text") or "", "audio_url": e.get("audio_url"),
                                "note": bool(e.get("note")), "hits": [h["term"] for h in e.get("hits") or []]}
                               for e in inc.get("timeline") or []]}
                 for iid, inc in incidents.items() if iid not in board.aliases]
        calls.sort(key=lambda c: c["t"])
        for c in calls:
            c["walk_min"] = self.dispatch_walk(c)
        tg_name = self.monitor.tg_names().get(self.tg, "")
        cfg = getattr(getattr(self.monitor, "config", None), "get", lambda: {})()
        sig_now = rules_signature(cfg) if cfg else None
        current = self.scored_with() == sig_now     # stored levels come from today's rules (else a re-score is due)
        alerts = [{"id": a["id"], "t": a["time"], "level": a["level"], "context": a.get("context") or a.get("text") or "",
                   "texts": [x.get("text") or "" for x in a.get("transmissions") or []] or [a.get("text") or ""],
                   "wheres": [h["where"] for h in a.get("hits", []) if h.get("where")], "huhs": is_huhs(a),
                   "urls": [x.get("audio_url") for x in a.get("transmissions") or [] if x.get("audio_url")] or [a.get("audio_url")], "terms": [h["term"] for h in a.get("hits", [])],
                   "text": a.get("text") or "", "audio_url": a.get("audio_url"), "acuity": a.get("acuity")}
                  for a in _read_json(paths[1], {}).values() if a.get("talkgroup_name") == tg_name]
        alerts.sort(key=lambda a: a["t"])
        # crew rulings, matched by id or by a shared radio clip (so an alert's ruling also covers its dispatch,
        # and rulings survive a re-score); if a call somehow has two, the latest wins
        dq = _read_json(os.path.join(self.log_dir, NOT_HARVARD), {})
        by_url, by_id = {}, {}
        for k in RULING_KINDS:
            verdict = "harvard" if k.startswith("harvard_") else "not"
            for rid, r in dq.get(k, {}).items():
                e = (r.get("at", 0), verdict, r, k, rid)
                if rid not in by_id or e[0] > by_id[rid][0]:
                    by_id[rid] = e
                for u in r.get("urls") or []:
                    if u not in by_url or e[0] > by_url[u][0]:
                        by_url[u] = e

        def ruling_of(item_id, urls):
            cands = [by_url[u] for u in urls if u in by_url] + ([by_id[item_id]] if item_id in by_id else [])
            return max(cands, key=lambda e: e[0]) if cands else None

        # "Follow the new rules" clicked while the stored levels are from older rules: the verdict it promised
        cleared = [v for v in (dq.get("cleared") or {}).values() if v.get("sig") == sig_now and not current]
        cleared_url = {u: v for v in sorted(cleared, key=lambda v: v.get("at", 0)) for u in v.get("urls") or []}

        def cleared_of(urls):
            return next((cleared_url[u] for u in urls if u in cleared_url), None)

        url_dispatch = {e["audio_url"]: c for c in calls for e in c["timeline"] if e.get("audio_url")}
        with_alert = set()
        for a in alerts:
            linked = list({c["id"]: c for c in (url_dispatch[u] for u in a["urls"] if u in url_dispatch)}.values())
            a["dispatch_id"] = linked[0]["id"] if linked else None
            with_alert.update(c["id"] for c in linked)
            rl = ruling_of(a["id"], a["urls"])
            a["removed"] = rl[2] if rl and rl[1] == "not" else None
            a["confirmed"] = rl[2] if rl and rl[1] == "harvard" else None
            cl = None if rl else cleared_of(a["urls"])
            if cl:
                a["level"] = {"high": "high", "possible": "medium"}.get(cl["level"])
            a["rules_level"] = a["level"]                       # what today's rules said, before any ruling
            if a["confirmed"]:
                a["level_was"], a["level"] = a["level"], "high"
        rule_of_call = {}
        for c in calls:
            urls = [e["audio_url"] for e in c["timeline"] if e.get("audio_url")]
            rl = ruling_of(c["id"], urls)
            rule_of_call[c["id"]] = rl
            cl = None if rl else cleared_of(urls)
            if cl:
                c["harvard"] = {"high": "high", "possible": "medium"}.get(cl["level"])
            c["rules_harvard"] = c["harvard"]
            if rl and rl[1] == "not":
                c["removed"] = rl[2]
                c["harvard_was"], c["harvard"] = c["harvard"], None
            elif rl and rl[1] == "harvard":
                c["confirmed"] = rl[2]
                c["harvard_was"], c["harvard"] = c["harvard"], "high"
                if c["id"] not in with_alert:
                    # marked Harvard by the crew though the rules never alerted: it joins the Harvard calls
                    first = next((e for e in c["timeline"] if not e["note"]), c["timeline"][0] if c["timeline"] else {})
                    alerts.append({"id": "d-" + c["id"], "t": c["t"], "level": "high", "level_was": None,
                                   "rules_level": None, "terms": ["marked Harvard by crew"], "text": first.get("text", ""),
                                   "context": " ".join(e["text"] for e in c["timeline"] if not e["note"]),
                                   "audio_url": first.get("audio_url"), "acuity": c.get("acuity"), "urls": urls,
                                   "dispatch_id": c["id"], "removed": None, "confirmed": c["confirmed"], "manual": True})
        alerts = [a for a in alerts if a["level"]]          # "Follow the new rules: not Harvard"
        self._texts = {("alerts", a["id"]): a["texts"] for a in alerts}
        self._texts.update({("dispatches", c["id"]): [e["text"] for e in c["timeline"] if not e["note"]] for c in calls})
        # how far each Harvard call is from Adams House ("near Harvard College" checkbox on the page)
        by_id = {c["id"]: c for c in calls}
        for a in alerts:
            pt = self.alert_point(a, by_id.get(a.get("dispatch_id")))
            a["walk_min"] = walk_minutes(pt[0], pt[1]) if pt else None
            a["point"] = pt                                  # [lat, lon, place] for the map, or None
            if a.get("huhs") and a.get("dispatch_id") in by_id:
                by_id[a["dispatch_id"]]["huhs"] = True
            c = by_id.get(a.get("dispatch_id"))
            if c is not None and c.get("walk_min") is None and a["walk_min"] is not None:
                c["walk_min"] = a["walk_min"]
        # Your rulings vs. TODAY's rules (not the rules in force when the call was first analyzed), so "follow the new
        # rules" always means the current ones. When the stored results were scored with today's rules, their own
        # verdict is what the rules say; otherwise each transmission (and pair, as the pipeline sees them) is judged.
        conflicts = []
        for a in alerts:
            r = a.get("removed") or a.get("confirmed")
            if not r or r.get("reviewed"):
                continue
            texts = a["texts"] if not a.get("manual") else self._texts.get(("dispatches", a["dispatch_id"]), a["texts"])
            now, why_text = self.judge_texts(texts + ([a["context"]] if not a.get("manual") else []))
            if current:
                now = {"high": "high", "medium": "possible"}.get(a.get("rules_level"), "none")
            if (a.get("removed") and now == "high") or (a.get("confirmed") and now != "high"):
                conflicts.append({"kind": "dispatches" if a.get("manual") else "alerts",
                                  "id": a["dispatch_id"] if a.get("manual") else a["id"], "t": a["t"],
                                  "ruling": "not" if a.get("removed") else "harvard", "rules": now,
                                  "text": a["text"], "context": why_text, "audio_url": a["audio_url"],
                                  "where": next((c["where"] for c in calls if c["id"] == a.get("dispatch_id")), None)})
        for c in calls:                                  # a ruled-out dispatch that never had an alert
            rl = rule_of_call.get(c["id"])
            if current or not rl or rl[1] != "not" or c["id"] in with_alert or rl[2].get("reviewed"):
                continue                                 # (scored with today's rules and no alert: they agree)
            now, why_text = self.judge_texts(self._texts[("dispatches", c["id"])])
            if now == "high":
                first = next((e for e in c["timeline"] if not e["note"]), {})
                conflicts.append({"kind": "dispatches", "id": c["id"], "t": c["t"], "ruling": "not", "rules": "high",
                                  "text": first.get("text", ""), "context": why_text, "audio_url": first.get("audio_url"),
                                  "where": c.get("where")})
        conflicts.sort(key=lambda x: -x["t"])
        for x in conflicts:                          # what today's rules decide for this call, and why
            x["rules_why"] = self.explain(x.pop("context"))
        for a in alerts:
            a.pop("context", None)
            a.pop("wheres", None)
            a.pop("texts", None)
        alerts.sort(key=lambda a: a["t"])
        out = {"talkgroup": self.tg, "talkgroup_name": tg_name, "transmissions": len(recs),
               "calls": calls, "harvard": alerts, "conflicts": conflicts,
               "coverage": _merge(_read_json(paths[2], {}).get(self.tg, []) + ([list(partial)] if partial else [])),
               "updated": _now_ms(), "updated_at": job.counts["processed"] if job else None,
               "refresh_every": REFRESH_EVERY, "near_minutes": NEAR_MINUTES, "near_label": "Adams House",
               "near_origin": list(ADAMS_HOUSE),
               "stale": bool(recs) and not current}
        self._cache = (sig, out)
        return out

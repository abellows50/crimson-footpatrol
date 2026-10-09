"""
"Our calls": the calls your crew actually responds to, with every milestone time.

A call is added from an alarm ("We're responding"), from a board card, from a suggested Harvard call,
or by hand. Times come from two places:
  * auto   - filled from the radio (alert time, dispatch, Pro on scene, transport, all clear)
  * marked - set by a person on the dashboard; a mark always wins over the auto value
Everything is saved to logs/our-calls.json so it survives restarts and is shared by every viewer.
"""
import json
import os
import threading
import time
import uuid

from acuity import classify_incident

MILESTONES = [
    ("alerted", "Alerted"),
    ("dispatched", "Dispatched"),
    ("enroute", "En route"),
    ("onscene", "On scene"),
    ("contact", "Patient contact"),
    ("vitals", "Vitals"),
    ("pro_onscene", "Pro on scene"),
    ("transport", "Transport / handoff"),
    ("clear", "Clear"),
]
MILESTONE_KEYS = [k for k, _ in MILESTONES]
# where each auto time comes from on the board incident (first key that exists wins)
AUTO_FROM_INCIDENT = {
    "dispatched": ["dispatched"],
    "pro_onscene": ["ems_on_scene", "on scene"],
    "transport": ["ems_transporting", "transporting"],
    # "clear" is deliberately not filled from the radio: Pro / Fire clearing doesn't mean our crew is back
}
OUTCOMES = ["", "Transported by Pro", "Turned over to Pro", "Refusal (RMA)", "Treated and released",
            "Cancelled en route", "Unfounded / no patient", "Other"]
EDITABLE = {"title", "location", "outcome", "notes", "responders", "source"}
SOURCES = ["Radio", "Flag-down", "Walk-in", "Phone / HUPD call", "Event standby", "Other"]


def _now_ms():
    return int(time.time() * 1000)


class CallLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.calls = {}
        try:
            with open(path) as f:
                self.calls = json.load(f).get("calls", {})
        except (OSError, ValueError):
            pass
        if any([self._own_acuity(c) for c in self.calls.values()]):    # rate calls logged before acuity existed
            try:
                self._save()
            except OSError:
                pass

    # ---------------------------------------------------------------- storage
    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"calls": self.calls}, f, indent=1)
        os.replace(tmp, self.path)

    def list(self):
        with self.lock:
            return sorted((json.loads(json.dumps(c)) for c in self.calls.values()), key=lambda c: -c["created"])

    def get(self, cid):
        with self.lock:
            c = self.calls.get(cid)
            return json.loads(json.dumps(c)) if c else None

    def by_incident(self, incident_id):
        with self.lock:
            for c in self.calls.values():
                if incident_id and c.get("incident_id") == incident_id:
                    return c["id"]
        return None

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _apply_incident(c, inc):
        """Copy what the radio tells us onto the call (never overriding a person's mark or edit)."""
        changed = False
        if (c["times"].get("clear") or {}).get("src") == "auto":     # left over from older versions
            c["times"].pop("clear")
            changed = True
        times = inc.get("times") or {}
        for key, sources in AUTO_FROM_INCIDENT.items():
            t = next((times[s] for s in sources if times.get(s)), None)
            cur = c["times"].get(key)
            if t and (cur is None or (cur.get("src") == "auto" and cur.get("t") != t)):
                c["times"][key] = {"t": t, "src": "auto"}
                changed = True
        where = " · ".join(x for x in [inc.get("address"), inc.get("place") if inc.get("place") != inc.get("address") else None,
                                       inc.get("town")] if x)
        if where and not c.get("location_edited") and c.get("location") != where:
            c["location"] = where
            changed = True
        what = inc.get("complaint") or ("Harvard call" if inc.get("harvard") else None)
        if what and not c.get("title_edited") and c.get("title") != what:
            c["title"] = what
            changed = True
        for k in ("harvard_terms", "units", "hospital", "level", "age", "acuity", "acuity_why", "ai_summary"):
            v = inc.get(k)
            if k == "units":
                v = [u["unit"] for u in inc.get("units") or []]
            if k in ("acuity", "acuity_why") and c.get("acuity_set"):
                continue
            if k == "acuity" and v:
                c["acuity_src"] = "board"
            if v and c.get(k) != v:
                c[k] = v
                changed = True
        tl = [{"time": e["time"], "text": e["text"], "audio_url": e.get("audio_url"),
               "talkgroup_name": e.get("talkgroup_name")} for e in inc.get("timeline", []) if not e.get("note")]
        if tl and len(tl) != len(c.get("transmissions") or []):
            c["transmissions"] = tl
            changed = True
        if CallLog._own_acuity(c):
            changed = True
        return changed

    @staticmethod
    def _own_acuity(c):
        """Fill acuity from the call's own title / notes / radio when the board didn't (crew choice always wins)."""
        if c.get("acuity_set"):
            return False
        texts = [c.get("title") or "", c.get("notes") or ""] + [e.get("text") or "" for e in c.get("transmissions") or []]
        a, why = classify_incident(texts, c.get("units") or [], c.get("title"))
        if c.get("acuity_src") == "board" and c.get("acuity"):
            return False                                      # the board's answer (it saw the whole call) stands
        if (a, why) != (c.get("acuity"), c.get("acuity_why")):
            c["acuity"], c["acuity_why"], c["acuity_src"] = a, why, "call text"
            return True
        return False

    # ---------------------------------------------------------------- actions
    def add(self, by=None, incident=None, alert_time=None, title=None, location=None, mark=None,
            source=None, notes=None, acuity=None, times=None, after_the_fact=False):
        with self.lock:
            if incident:
                for c in self.calls.values():
                    if c.get("incident_id") == incident["id"]:
                        if mark and not c["times"].get(mark):
                            c["times"][mark] = {"t": _now_ms(), "src": "marked", "by": by, "at": _now_ms()}
                            self._save()
                        return json.loads(json.dumps(c))
            now = _now_ms()
            c = {"id": uuid.uuid4().hex[:8], "created": now, "created_by": (by or "")[:40],
                 "incident_id": incident["id"] if incident else None,
                 "title": (title or "")[:120], "location": (location or "")[:160],
                 "times": {}, "outcome": "", "notes": "", "responders": "", "transmissions": []}
            if alert_time:
                c["times"]["alerted"] = {"t": int(alert_time), "src": "auto"}
            if incident:
                self._apply_incident(c, incident)
            c["source"] = (source or ("Radio" if incident or alert_time else "Other"))[:40]
            if notes:
                c["notes"] = str(notes)[:4000]
            if acuity in ("high", "low"):
                c.update(acuity=acuity, acuity_set=True, acuity_why=["set by crew"], acuity_src="crew")
            for k, t in (times or {}).items():                      # times given in the "new call" form
                if k in MILESTONE_KEYS and t:
                    c["times"][k] = {"t": int(t), "src": "marked", "by": (by or "")[:40], "at": now}
            if after_the_fact:
                c["after_the_fact"] = True
            if mark in MILESTONE_KEYS:
                c["times"][mark] = {"t": now, "src": "marked", "by": (by or "")[:40], "at": now}
            self._own_acuity(c)
            self.calls[c["id"]] = c
            self._save()
            return json.loads(json.dumps(c))

    def mark(self, cid, key, t, by=None):
        """t: epoch ms, 'now', or None (remove the mark; the auto value comes back on the next sync)."""
        if key not in MILESTONE_KEYS:
            raise ValueError("unknown milestone")
        with self.lock:
            c = self.calls.get(cid)
            if not c:
                raise KeyError(cid)
            if t is None:
                c["times"].pop(key, None)
                c.setdefault("needs_resync", True)
            else:
                t = _now_ms() if t == "now" else int(t)
                c["times"][key] = {"t": t, "src": "marked", "by": (by or "")[:40], "at": _now_ms()}
            self._save()
            return json.loads(json.dumps(c))

    def update(self, cid, fields):
        with self.lock:
            c = self.calls.get(cid)
            if not c:
                raise KeyError(cid)
            for k, v in fields.items():
                if k in EDITABLE:
                    c[k] = str(v or "")[:4000]
                    if k in ("title", "location"):
                        c[k + "_edited"] = True
            self._own_acuity(c)
            self._save()
            return json.loads(json.dumps(c))

    def restore(self, call):
        """Undo a delete: put the call back exactly as it was."""
        if not isinstance(call, dict) or not call.get("id") or not isinstance(call.get("times"), dict):
            raise ValueError("nothing to restore")
        with self.lock:
            self.calls[call["id"]] = call
            self._save()
            return json.loads(json.dumps(call))

    def set_acuity(self, cid, acuity):
        with self.lock:
            c = self.calls.get(cid)
            if not c:
                raise KeyError(cid)
            if acuity:
                c["acuity"], c["acuity_set"], c["acuity_why"], c["acuity_src"] = acuity, True, ["set by crew"], "crew"
            else:
                c.update(acuity=None, acuity_set=False, acuity_why=[], acuity_src=None)
                self._own_acuity(c)
            self._save()
            return json.loads(json.dumps(c))

    def relink(self, src_incident, dst_incident):
        """Board cards src -> dst were merged: point logged calls at the surviving card, and if the crew
        logged both, fold them into one (crew marks win over radio times, notes are kept)."""
        with self.lock:
            a = [c for c in self.calls.values() if c.get("incident_id") == src_incident]
            b = [c for c in self.calls.values() if c.get("incident_id") == dst_incident]
            if not a:
                return False
            for c in a:
                c["incident_id"] = dst_incident
            if b:
                keep = b[0]
                for c in a:
                    for k, v in c["times"].items():
                        cur = keep["times"].get(k)
                        if not cur or (v.get("src") == "marked" and cur.get("src") != "marked"):
                            keep["times"][k] = v
                    for k in ("notes", "responders"):
                        if c.get(k) and c[k] not in (keep.get(k) or ""):
                            keep[k] = ((keep.get(k) or "") + ("\n" if keep.get(k) else "") + c[k]).strip()
                    for k in ("outcome", "title", "location"):
                        if c.get(k) and not keep.get(k):
                            keep[k] = c[k]
                    keep["created"] = min(keep["created"], c["created"])
                    self.calls.pop(c["id"], None)
            self._save()
            return True

    def delete(self, cid):
        with self.lock:
            ok = self.calls.pop(cid, None) is not None
            if ok:
                self._save()
            return ok

    def sync_from_board(self, snapshot):
        """Refresh auto times on calls linked to board incidents. Returns True if anything changed."""
        incs = {i["id"]: i for i in snapshot.get("incidents", []) + snapshot.get("closed", [])}
        changed = False
        with self.lock:
            for c in self.calls.values():
                inc = incs.get(c.get("incident_id"))
                if inc and self._apply_incident(c, inc):
                    changed = True
                c.pop("needs_resync", None)
            if changed:
                self._save()
        return changed

    def suggestions(self, snapshot, alerts=(), hours=12):
        """Harvard calls heard on the radio that aren't in the log yet."""
        cutoff = _now_ms() - hours * 3600_000
        linked = {c.get("incident_id") for c in self.calls.values()}
        out = []
        for inc in snapshot.get("incidents", []) + snapshot.get("closed", []):
            if inc.get("harvard") and inc["id"] not in linked and inc["last"] >= cutoff:
                out.append({"incident_id": inc["id"], "title": inc.get("complaint") or "Harvard call",
                            "location": inc.get("address") or inc.get("place") or "Location unclear",
                            "harvard": inc["harvard"], "opened": inc["opened"], "status": inc.get("status"),
                            "closed": bool(inc.get("closed"))})
        return sorted(out, key=lambda x: -x["opened"])

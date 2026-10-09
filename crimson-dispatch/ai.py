"""
AI dispatch reader: asks Claude (through the `claude -p` command line) to read the radio transcripts
of one call and return structured facts: units, address, complaint, acuity, Harvard or not, summary.

Design rules
  * Never in the alarm path: keyword alerts fire immediately; Claude's answer arrives seconds later
    and only refines the board card / alert / logged call.
  * Only public radio transcripts are sent: never crew notes or anything typed on the dashboard.
  * Fails soft: if `claude` is missing, not logged in, slow or over budget, everything else keeps working.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

SCHEMA = {
    "type": "object",
    "properties": {
        "is_call": {"type": "boolean"},
        "units": {"type": "array", "items": {"type": "string"}},
        "address": {"type": ["string", "null"]},
        "place": {"type": ["string", "null"]},
        "town": {"type": ["string", "null"]},
        "complaint": {"type": ["string", "null"]},
        "patient": {"type": ["string", "null"]},
        "acuity": {"type": ["string", "null"], "enum": ["high", "low", None]},
        "acuity_reason": {"type": ["string", "null"]},
        "harvard": {"type": "boolean"},
        "harvard_location": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
        "same_call_as": {"type": ["string", "null"]},
    },
    "required": ["is_call", "units", "address", "complaint", "acuity", "harvard", "summary", "confidence"],
    "additionalProperties": False,
}

SYSTEM = """You read automatic speech-to-text transcripts of Cambridge, Massachusetts public-safety radio \
for Crimson EMS, a Harvard student EMS crew. You get the transmissions that seem to belong to ONE call \
and return the facts as JSON. Use null when the radio doesn't say; never invent details.

Radio facts:
- Pro EMS is the ambulance service. Its units: "Ambulance N" / "Pro N" (BLS) and "Paramedic N" (ALS). \
Dispatch often calls a unit by number only: "15, sign on and respond, ..." means Ambulance 15. \
"Pro base" / "Pro dispatch" is the dispatcher, not a unit.
- Cambridge Fire units: Engine N, Ladder N (often heard as "truck"), Rescue N, Squad N, Car N, Division N.
- "Fire Alarm" is the call sign of Cambridge fire dispatch ("Squad 2 to Fire Alarm"), not a fire alarm.
- "HUPD" is Harvard University Police; "private response" / "HUPD private response" means HUPD asked for EMS \
on Harvard property. Common mishearings: "Pharmatic", "Pyramid", "Permanic", "Paramount", "Pardon my" = Paramedic; \
"Bill and 15", "Annual 4", "Williams 14" = Ambulance N; "superperson" = sick person; "E.T.H." = ETOH; \
"4-1 Jackson Street" = 41 Jackson Street; "sign and respond"/"sign on" = dispatch.

Fields:
- is_call: true if this is a real dispatch or traffic about a real call; false for chatter, radio checks, noise.
- units: canonical names, e.g. ["Ambulance 15", "Paramedic 7", "Engine 6"]. Only units actually named or addressed; a number right before a street name is a house number, not a unit ("3, Marcella Street" = 3 Marcella Street).
- address: street address or intersection, cleaned up ("41 Jackson Street", "Mass Ave & Dundee Road"). \
place: building / landmark ("Adams House, Westmorly Hall", "Harvard MBTA"). town if not Cambridge.
- complaint: short label, 1-4 words ("Fall", "ETOH", "Difficulty breathing", "Overdose", "Psych", "Sick person", \
"Lift assist", "Transfer", "Fire alarm", "Motor vehicle crash").
- patient: age / sex if said ("19 F", "62 F", "84 M").
- acuity: "high" = likely time-critical or ALS (cardiac arrest, unresponsive/altered, breathing problem, chest pain, \
seizure, stroke, overdose, allergic reaction, major trauma, OB, suicidal, diabetic emergency, explicit ALS/paramedic \
dispatch); "low" = fall without red flags, lift assist, intox who is alert, sick person, minor injury, psych, \
transfer, BLS; null if the radio doesn't say enough. acuity_reason: a few words.
- harvard: true only if the call is at a Harvard location or an HUPD / Harvard private response. \
harvard_location: which (from the list below or as heard).
- summary: one plain sentence, at most 20 words, e.g. "Ambulance 15 to Adams House (Westmorly) for 19 F ETOH, HUPD on scene."
- confidence: 0-1, how sure you are this reading is right given the transcript quality.
- same_call_as: you may also be shown OTHER open calls (with ids). If this call is clearly the SAME incident as one of them (same patient/scene: e.g. Fire and Pro EMS both sent to the same address or landmark, "Harvard MBTA" = "1400 Mass Ave", a re-dispatch or added unit for the same patient), give that call's id. Different patients, different addresses, or merely the same unit moving between calls are NOT the same call. Landmarks and \
addresses can name the same place ("Harvard MBTA" / "Harvard Square T station" = 1400 Mass Ave; \
"Holyoke Center"/"Smith Center" = 1350 Mass Ave). If unsure, null.

Harvard locations to recognise: {harvard}
"""


def _harvard_list(config):
    terms = [r["term"] for r in config.get("rules", []) if r.get("level") == "high"]
    return ", ".join(sorted(set(terms)))[:3000] or "Harvard Yard, the Harvard Houses, HUPD"


class ClaudeReader:
    def __init__(self, config_getter, on_result, log, model="haiku", claude_path=None,
                 max_per_hour=60, debounce_s=15, timeout_s=90, others_getter=None):
        self.config_getter, self.on_result, self.log = config_getter, on_result, log
        self.others_getter = others_getter        # inc -> list of other recent calls, for duplicate detection
        self.model, self.max_per_hour, self.debounce_s, self.timeout_s = model, max_per_hour, debounce_s, timeout_s
        self.claude = claude_path or shutil.which("claude") or next(
            (p for p in ("/opt/homebrew/bin/claude", "/usr/local/bin/claude", os.path.expanduser("~/.claude/local/claude"),
                         os.path.expanduser("~/.npm-global/bin/claude"), os.path.expanduser("~/.local/bin/claude"))
             if os.path.exists(p)), None)
        self.status = "starting" if self.claude else "off: `claude` command not found"
        self.lock = threading.Lock()
        self.pending = {}            # incident id -> (due_time, payload)
        self.runs = {}               # incident id -> number of runs
        self.last_sig = {}           # incident id -> transcript signature already analysed
        self.recent = []             # timestamps of runs (rate limit)
        self.legacy = False          # older claude without --json-schema / --tools
        self.workdir = tempfile.mkdtemp(prefix="crimson-ai-")   # empty dir: no CLAUDE.md, no project files
        self.cost = 0.0
        if self.claude:
            threading.Thread(target=self._loop, name="claude-ai", daemon=True).start()

    # ---------------------------------------------------------------- public
    def enabled(self):
        return self.status.startswith(("ready", "starting", "busy"))

    def queue(self, incident):
        """Ask for a (re)read of this call once its radio traffic settles."""
        if not self.claude or self.status.startswith("off"):
            return
        lines = [e for e in incident.get("timeline", []) if not e.get("note")]
        if not any(e.get("dispatch") for e in lines):
            return                                    # only calls that had a dispatch
        sig = "|".join(e["text"] for e in lines[:14]) + "|m" + str(len(incident.get("merged_from") or []))
        with self.lock:
            if self.last_sig.get(incident["id"]) == sig or self.runs.get(incident["id"], 0) >= 4:
                return
            self.pending[incident["id"]] = (time.time() + self.debounce_s, incident, sig)

    # ---------------------------------------------------------------- worker
    def _loop(self):
        ok = self._selftest()
        self.status = "ready" if ok else self.status
        while True:
            time.sleep(1)
            if not ok:
                ok = self._selftest() if int(time.time()) % 300 == 0 else False
                if ok:
                    self.status = "ready"
                continue
            job = None
            with self.lock:
                now = time.time()
                due = [(t, iid) for iid, (t, _, _) in self.pending.items() if t <= now]
                if due:
                    _, iid = min(due)
                    _, inc, sig = self.pending.pop(iid)
                    job = (iid, inc, sig)
            if not job:
                continue
            self.recent = [t for t in self.recent if time.time() - t < 3600]
            if len(self.recent) >= self.max_per_hour:
                self.status = f"ready (hourly limit of {self.max_per_hour} reached, paused)"
                continue
            iid, inc, sig = job
            self.status = "busy"
            try:
                res = self._ask(inc)
                self.recent.append(time.time())
                with self.lock:
                    self.runs[iid] = self.runs.get(iid, 0) + 1
                    self.last_sig[iid] = sig
                if res:
                    self.on_result(iid, res)
                self.status = "ready"
            except Exception as e:
                self.status = f"ready (last error: {type(e).__name__}: {str(e)[:80]})"
                self.log(f"AI read failed for call {iid}: {type(e).__name__}: {e}")

    def _selftest(self):
        try:
            out = self._run("Reply with the single word OK.", system="Reply with the single word OK.", schema=None)
            return bool(out)
        except Exception as e:
            msg = str(e)
            if "login" in msg.lower() or "auth" in msg.lower() or "api key" in msg.lower():
                self.status = "off: run `claude` once in Terminal and log in"
            else:
                self.status = f"off: {msg[:120]}"
            self.log("AI reader unavailable:", self.status)
            return False

    def _run(self, prompt, system, schema):
        base = [self.claude, "-p", "--output-format", "json", "--model", self.model]
        full = base + ["--tools", "", "--no-session-persistence", "--system-prompt", system]
        if schema:
            full += ["--json-schema", json.dumps(schema)]
        cmd = base + ["--append-system-prompt", system] if self.legacy else full
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout_s,
                           cwd=self.workdir, env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        if p.returncode != 0 and not self.legacy and re.search(r"unknown option|unrecognized|invalid option", p.stderr, re.I):
            self.legacy = True                         # older Claude Code: retry with the basic flags
            return self._run(prompt, system, schema)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "claude exited with an error").strip().splitlines()[-1][:300])
        d = json.loads(p.stdout)
        if d.get("is_error"):
            raise RuntimeError(str(d.get("result") or d.get("subtype"))[:300])
        self.cost += float(d.get("total_cost_usd") or 0)
        if d.get("structured_output") is not None:
            return d["structured_output"]
        text = d.get("result") or ""
        if schema:
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        return text

    def _ask(self, inc):
        cfg = self.config_getter()
        lines = [e for e in inc.get("timeline", []) if not e.get("note")][:14]
        tx = "\n".join(f"[{time.strftime('%H:%M:%S', time.localtime(e['time'] / 1000))} {e.get('talkgroup_name', '')}] {e['text']}"
                       for e in lines)
        others = []
        if self.others_getter:
            try:
                others = self.others_getter(inc)[:8]
            except Exception:
                others = []
        other_txt = ""
        if others:
            other_txt = "\n\nOTHER open calls right now (for same_call_as):\n" + "\n".join(
                f"- id {o['id']}: opened {time.strftime('%H:%M', time.localtime(o['opened'] / 1000))} · "
                f"{o.get('where') or 'location unclear'} · {o.get('what') or ''} · units {', '.join(o.get('units') or []) or '-'}"
                + (f" · summary: \"{o['summary']}\"" if o.get("summary") else "")
                + (f" · dispatch heard: \"{o['heard']}\"" if o.get("heard") else "") for o in others)
        prompt = ("Transmissions for one call (oldest first). Some lines may belong to other calls; ignore those.\n\n"
                  + tx + other_txt + ("\n\nReturn only the JSON object." if self.legacy else ""))
        system = SYSTEM.replace("{harvard}", _harvard_list(cfg))
        if self.legacy:
            system += "\nReturn ONLY a JSON object with keys: " + ", ".join(SCHEMA["properties"]) + "."
        res = self._run(prompt, system, SCHEMA)
        if not isinstance(res, dict):
            return None
        res["at"] = int(time.time() * 1000)
        return res

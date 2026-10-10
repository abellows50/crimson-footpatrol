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
        "status": {"type": ["string", "null"],
                   "enum": ["dispatched", "responding", "on scene", "transporting", "at hospital", "clear", None]},
        "transport_level": {"type": ["string", "null"], "enum": ["ALS", "BLS", None]},
        "hospital": {"type": ["string", "null"]},
        "outcome": {"type": ["string", "null"]},
        "remove_lines": {"type": "array", "items": {"type": "string"}},
        "add_lines": {"type": "array", "items": {"type": "string"}},
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

- status: where the call stands at the LAST line that belongs to it: dispatched, responding, on scene, transporting, at hospital, or clear (also for cancelled / refusal / AMA / no patient). null if unclear.
- transport_level: "ALS" or "BLS" if the patient was transported and the radio says which ("BLS to the Mount"); NOT the dispatch level ("ALS, sign on and respond" is the dispatch). hospital: where the patient went ("Mount Auburn", "MGH" for "the General", "Cambridge Hospital", "Emerson", "Spaulding", "Lahey" for "the lady"...). outcome: "Refusal / AMA", "Cancelled", "No patient found" or null.
- remove_lines: ids (like "L3") of the call's lines that are NOT about this call (another unit's traffic, chatter).
- add_lines: ids (like "N5") of NEARBY radio lines that clearly ARE about this call and are missing from it.

How the radio works: a unit calls ("Pro base, paramedic 9" / "Ambulance 4 to Pro") or is called ("17, go ahead"), then the SAME unit gives its report in the next 1-3 keyups, usually without repeating its name ("Transporting BLS to the Mount"). An unlabelled report belongs to whoever called in just before it on that channel; if someone else called in in between, it is theirs. Only move lines when the thread makes it clear; when unsure, leave them.
Common garbles: MIT's ambulance "MIT 8" = "M-I-T-A", "MITA", "NYC"; Paramedic = "Primark", "Paramount", "Permanente"; Ambulance = "Annual", "Ambient".

Harvard locations to recognise: {harvard}
"""


def _harvard_list(config):
    terms = [r["term"] for r in config.get("rules", []) if r.get("level") == "high"]
    return ", ".join(sorted(set(terms)))[:3000] or "Harvard Yard, the Harvard Houses, HUPD"


class ClaudeReader:
    def __init__(self, config_getter, on_result, log, model="haiku", claude_path=None,
                 max_per_hour=60, debounce_s=15, timeout_s=90, others_getter=None, nearby_getter=None):
        self.config_getter, self.on_result, self.log = config_getter, on_result, log
        self.others_getter = others_getter        # inc -> list of other recent calls, for duplicate detection
        self.nearby_getter = nearby_getter        # inc -> radio heard around this call that isn't on it
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
        sig = "|".join(e["text"] for e in lines[-20:]) + "|m" + str(len(incident.get("merged_from") or []))
        with self.lock:
            if self.last_sig.get(incident["id"]) == sig or self.runs.get(incident["id"], 0) >= 8:
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

    # ---------------------------------------------------------------- "Ask the radio"
    ASK_SYSTEM = (
        "You answer questions from a Harvard student EMS crew (Crimson EMS) about Cambridge, MA radio traffic "
        "(Pro EMS ambulances, Cambridge Fire). You are given machine transcripts of the radio (often garbled: "
        "use context, e.g. 'Fort Haverhouse' is probably Pforzheimer House) and the auto-built call board. "
        "Answer ONLY from that data; if it isn't there, say so plainly. Never invent units, addresses or times. "
        "Be brief and concrete: 1-3 sentences or a short list; skip side details nobody asked about. Write times in 12-hour "
        "form (e.g. 4:59 PM) even though the data uses 24-hour HH:MM:SS. Mention when a "
        "transcript is unclear or your reading is a guess. 'Pro N' / 'Ambulance N' / 'Paramedic N' are Pro EMS "
        "ambulances; Engine / Ladder / Squad / Rescue are Cambridge Fire. Put the exact transcript times "
        "(HH:MM:SS as written in the data) of the lines you relied on in cited_times. "
        "How the radio works: a unit calls (\"Ambulance 4 to Pro base\" / \"Pro base, ambulance 4\"), Pro base answers "
        "(\"go ahead\" / \"answering\"), then the SAME unit gives its report in the next 1-3 keyups, usually without "
        "repeating its name. So an unlabelled report (\"We are taking one patient, BLS to the General\") belongs to "
        "whoever called in just before it. Follow such threads across the whole window, including much later updates "
        "about an earlier call (on scene, transporting ALS/BLS, hospital, clear, refusal/AMA). Common transcription "
        "garbles: MIT ambulances (\"MIT 8\") come out as \"M-I-T-A\", \"MITA\", \"NYC\", \"My Pia\", \"M.I.T. A\", "
        "\"MIT, A2\"; \"Pro\" as \"Pearl\", \"Pro's\", \"Brow\"; \"Ambulance\" as \"Annual\", \"Ambient\", "
        "\"Williams\"; \"Paramedic\" as \"Paramount\", \"Primark\", \"Permanente\", \"Pyramid\". \"The General\" / "
        "\"MGH\" = Mass General Hospital; \"the Mount\" = Mount Auburn Hospital; \"the Emerson\" = Emerson Hospital. If"
        " you connect garbled lines to a unit, say it's an inference and show the line.")
    ASK_SCHEMA = {"type": "object", "properties": {
        "answer": {"type": "string"},
        "cited_times": {"type": "array", "items": {"type": "string"}, "maxItems": 8}},
        "required": ["answer", "cited_times"]}

    def ask_radio(self, question, context, history=()):
        """Free-form question about the radio. Returns {answer, cited_times}. Raises on failure."""
        if not self.claude or self.status.startswith("off"):
            raise RuntimeError("AI is off: " + self.status)
        now = time.time()
        with self.lock:
            self.recent = [t for t in self.recent if now - t < 3600]
            if len(self.recent) >= self.max_per_hour * 2:
                raise RuntimeError("Too many AI requests this hour; try again in a few minutes.")
            self.recent.append(now)
        convo = "".join(f"Earlier question: {h.get('q', '')[:300]}\nYour answer: {h.get('a', '')[:600]}\n\n"
                        for h in list(history)[-4:])
        prompt = f"{context}\n\n{convo}Question: {question[:500]}"
        out = self._run(prompt, self.ASK_SYSTEM, self.ASK_SCHEMA)
        if isinstance(out, str):
            out = {"answer": out, "cited_times": []}
        return out or {"answer": "No answer.", "cited_times": []}

    def _ask(self, inc):
        cfg = self.config_getter()
        lines = [e for e in inc.get("timeline", []) if not e.get("note")][-20:]
        hm = lambda ms: time.strftime('%H:%M:%S', time.localtime(ms / 1000))
        ids = {}
        rows = []
        for k, e in enumerate(lines, 1):
            ids[f"L{k}"] = e.get("audio_url")
            rows.append(f"L{k} [{hm(e['time'])} {e.get('talkgroup_name', '')}] {e['text']}")
        tx = "\n".join(rows)
        near = []
        if self.nearby_getter:
            try:
                near = self.nearby_getter(inc)[:45]
            except Exception:
                near = []
        if near:
            nrows = []
            for k, r in enumerate(near, 1):
                ids[f"N{k}"] = r.get("audio_url")
                nrows.append(f"N{k} [{hm(r['time'])} {r.get('talkgroup_name', '')}] {r['text']}"
                             + (f"   (now on another call: {r['on_call']})" if r.get("on_call") else ""))
            tx += ("\n\nNEARBY radio on the same channels (not on this call; for add_lines and to follow threads):\n"
                   + "\n".join(nrows))
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
        # line ids -> audio urls the board understands
        res["remove_audio"] = [ids[x] for x in (res.get("remove_lines") or []) if str(x).startswith("L") and ids.get(x)]
        res["add_audio"] = [ids[x] for x in (res.get("add_lines") or []) if str(x).startswith("N") and ids.get(x)]
        return res

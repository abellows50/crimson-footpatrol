"""
Demo mode: a second dashboard (default http://localhost:8081) that plays back a past stretch of radio
"as if it were live", for demos and training.

* Uses transcripts already in logs/calls-*.jsonl. Calls in the window that aren't in the logs (the monitor
  was off) are fetched from OpenMHz and transcribed with the live Whisper first, live traffic always going
  first; those transcripts are cached in logs/demo-cache.jsonl for next time. Everything then runs through the
  CURRENT pipeline: junk filter, Harvard matcher, alarms, call board, acuity, merging, Our calls.
* Times are shifted so the calls look like they're happening now (at 1x, 2x, 5x or 10x speed).
* Completely separate from the live monitor: its own alerts, board and call log, no phone pushes,
  nothing written to the real logs (only logs/replay-demo-*.jsonl, reset each run).
"""
import datetime as dt
import glob
import json
import os
import threading
import time


MAX_MISSING = 600            # don't try to transcribe more than this many missing clips for one demo
CACHE = "demo-cache.jsonl"


def _now_ms():
    return int(time.time() * 1000)


class DemoFetcher:
    """Hands the clip URL back as the 'audio' so the transcriber can look the text up."""
    name = "demo (recorded transcripts)"

    def get_json(self, url):
        return {"calls": []}

    def get_bytes(self, url):
        return url.encode()


class DemoTranscriber:
    name = "demo: recorded transcripts"

    def __init__(self):
        self.text = {}

    def transcribe(self, path, prompt):
        with open(path, "rb") as f:
            return self.text.get(f.read().decode(), "")


class DemoController:
    SPEEDS = (1, 2, 5, 10, 30)

    def __init__(self, monitor, log_dir, log, live=None):
        self.m, self.log_dir, self.log = monitor, log_dir, log
        self.live = live                      # the real monitor: its OpenMHz connection and Whisper fill gaps
        self.transcriber = DemoTranscriber()
        self.m.transcriber = self.transcriber
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self.state = {"state": "idle", "speed": 5, "from": None, "to": None, "done": 0, "total": 0,
                      "at": None, "label": "", "prep_done": 0, "prep_total": 0, "note": "",
                      "clock": None}   # replay clock: {"src": ms, "wall": ms, "speed": x, "frozen": ms|None}

    # ---------------------------------------------------------------- data
    def _records(self, start_ms, end_ms):
        recs = {}
        files = (sorted(glob.glob(os.path.join(self.log_dir, "calls-*.jsonl")))
                 + sorted(glob.glob(os.path.join(self.log_dir, "replay-2*-calls.jsonl")))      # past Replay runs
                 + [os.path.join(self.log_dir, CACHE)])
        for f in files:
            try:
                with open(f) as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if start_ms <= r.get("time", 0) < end_ms and r.get("id"):
                            recs[r["id"]] = r
            except OSError:
                pass
        return sorted(recs.values(), key=lambda r: r["time"])

    def _list_openmhz(self, start_ms, end_ms):
        """Every call OpenMHz has for the monitored talkgroups in [start, end), paging backwards from the end."""
        found, cursor = {}, end_ms
        for _ in range(200):
            batch = self.live._api("calls/older", {"time": cursor})
            if not batch:
                break
            times = []
            for c in batch:
                t = int(dt.datetime.fromisoformat(c["time"].replace("Z", "+00:00")).timestamp() * 1000)
                times.append(t)
                if start_ms <= t < end_ms:
                    found[c["_id"]] = (t, c)
            oldest = min(times)
            if oldest >= cursor or oldest < start_ms:
                break
            cursor = oldest
        return [c for _, c in sorted(found.values(), key=lambda x: x[0])]

    def _fill_gaps(self, start_ms, end_ms, have):
        """Transcribe calls OpenMHz has but our logs don't. Returns new records (also cached to disk)."""
        if not self.live or not getattr(self.live, "transcriber", None):
            with self.lock:
                self.state["note"] = "Whisper isn't ready on the live monitor, so only logged calls will play."
            return []
        try:
            listed = self._list_openmhz(start_ms, end_ms)
        except Exception as e:
            with self.lock:
                self.state["note"] = f"Couldn't reach OpenMHz ({type(e).__name__}), so only logged calls will play."
            self.log(f"Demo: OpenMHz listing failed: {type(e).__name__}: {e}")
            return []
        missing = [c for c in listed if c["_id"] not in have]
        if len(missing) > MAX_MISSING:
            with self.lock:
                self.state["note"] = f"{len(missing)} clips were missing; transcribing the first {MAX_MISSING}."
            missing = missing[:MAX_MISSING]
        with self.lock:
            self.state.update(state="preparing" if missing else self.state["state"], prep_done=0, prep_total=len(missing))
        self._publish()
        if missing:
            self.log(f"Demo: {len(missing)} calls in that window aren't in the logs; transcribing them with Whisper first")
        prompt = self.live.config.get().get("whisper_prompt", "")
        tg_names = self.live.tg_names()
        out = []
        for i, c in enumerate(missing):
            if self.stop_flag.is_set():
                return out
            while self.live.work.qsize() and not self.stop_flag.is_set():
                time.sleep(0.5)                          # live calls always go first
            text, err = "", None
            try:
                path = self.live._download(c.get("url"))
                try:
                    text = self.live.transcriber.transcribe(path, prompt)
                finally:
                    os.unlink(path)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            t = int(dt.datetime.fromisoformat(c["time"].replace("Z", "+00:00")).timestamp() * 1000)
            tg = str(c.get("talkgroupNum"))
            rec = {"id": c["_id"], "time": t, "talkgroup": tg, "talkgroup_name": tg_names.get(tg, f"TG {tg}"),
                   "len": c.get("len"), "audio_url": c.get("url"), "text": text, "raw_text": text, "error": err,
                   "hits": [], "source": "demo-whisper"}
            out.append(rec)
            try:
                with open(os.path.join(self.log_dir, CACHE), "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass
            with self.lock:
                self.state["prep_done"] = i + 1
            if (i + 1) % 3 == 0 or i + 1 == len(missing):
                self._publish()
        return out

    def moments(self, limit=25):
        """Past Harvard alarms worth demoing ('start 3 minutes before this')."""
        out = []
        try:
            with open(os.path.join(self.log_dir, "alerts.jsonl")) as f:
                for line in f:
                    try:
                        a = json.loads(line)
                    except ValueError:
                        continue
                    if a.get("test"):
                        continue
                    out.append({"time": a["time"], "level": a["level"], "terms": [h["term"] for h in a["hits"]][:3],
                                "text": (a.get("text") or "")[:140], "acuity": a.get("acuity")})
        except OSError:
            pass
        out.sort(key=lambda x: (x["level"] != "high", -x["time"]))
        seen, res = set(), []
        for x in out:                                   # one per 10-minute stretch
            k = x["time"] // 600_000
            if k not in seen:
                seen.add(k)
                res.append(x)
        return sorted(res[:limit], key=lambda x: -x["time"])

    def coverage(self):
        """First / last logged transcript, so the page can offer a sensible range."""
        files = sorted(glob.glob(os.path.join(self.log_dir, "calls-*.jsonl")))
        return {"days": [os.path.basename(f)[6:16] for f in files]}

    # ---------------------------------------------------------------- control
    def status(self):
        with self.lock:
            return dict(self.state)

    def _publish(self):
        self.m.hub.publish("demo_status", self.status())

    def start(self, start_ms, end_ms, speed=5, label=""):
        self.stop()
        if end_ms <= start_ms:
            raise ValueError("'To' must be after 'From'.")
        if end_ms - start_ms > 6 * 3600_000:
            raise ValueError("Pick a window of 6 hours or less.")
        if start_ms > _now_ms():
            raise ValueError("That window is in the future.")
        speed = speed if speed in self.SPEEDS else 5
        self.stop_flag.clear()
        self.pause_flag.clear()
        with self.lock:
            self.state.update(state="preparing", speed=speed, done=0, total=0, at=None, label=label, note="",
                              prep_done=0, prep_total=0, clock=None, **{"from": start_ms, "to": end_ms})
        self._publish()
        self.thread = threading.Thread(target=self._prepare_and_run, args=(start_ms, end_ms, speed),
                                       name="demo", daemon=True)
        self.thread.start()
        return self.status()

    def _prepare_and_run(self, start_ms, end_ms, speed):
        have = {r["id"]: r for r in self._records(start_ms, end_ms)}
        for r in self._fill_gaps(start_ms, end_ms, have):
            have[r["id"]] = r
        if self.stop_flag.is_set():
            return
        recs = sorted(have.values(), key=lambda r: r["time"])
        if not recs:
            with self.lock:
                self.state.update(state="error", note=self.state.get("note") or
                                  "No radio found for that window (nothing logged, and OpenMHz had nothing either).")
            self._publish()
            return
        self.m.reset_demo()
        self.transcriber.text = {r["audio_url"]: (r.get("raw_text") or r.get("text") or "") for r in recs}
        with self.lock:
            self.state.update(state="playing", done=0, total=len(recs))
            speed = self.state["speed"]
        self.m.hub.publish("demo_reset", self.status())
        self.log(f"Demo: playing {len(recs)} calls from {dt.datetime.fromtimestamp(start_ms / 1000):%m-%d %H:%M} at {speed}x")
        self._run(recs, start_ms, speed)

    def _set_clock(self, src_ms, wall_s, paused_s, speed, frozen=None):
        """Anchor for the page's replay clock: replay time = src + (now - wall) * speed."""
        with self.lock:
            self.state["clock"] = {"src": int(src_ms), "wall": int((wall_s + paused_s) * 1000),
                                   "speed": speed, "frozen": frozen}

    def _clock_now(self):
        c = self.state.get("clock")
        if not c:
            return None
        return c["frozen"] if c.get("frozen") is not None else int(c["src"] + (_now_ms() - c["wall"]) * c["speed"])

    def pause(self, on=True):
        (self.pause_flag.set if on else self.pause_flag.clear)()
        with self.lock:
            if self.state["state"] in ("playing", "paused"):
                self.state["state"] = "paused" if on else "playing"
                c = self.state.get("clock")
                if c and on and c.get("frozen") is None:
                    c["frozen"] = self._clock_now()
        self._publish()
        return self.status()

    def set_speed(self, speed):
        with self.lock:
            if speed in self.SPEEDS:
                self.state["speed"] = speed
        self._publish()
        return self.status()

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.stop_flag.set()
            self.pause_flag.clear()
            self.thread.join(timeout=10)
        with self.lock:
            if self.state["state"] in ("playing", "paused", "preparing"):
                self.state["state"] = "stopped"
                c = self.state.get("clock")
                if c and c.get("frozen") is None:
                    c["frozen"] = self._clock_now()
        self._publish()
        return self.status()

    # ---------------------------------------------------------------- playback
    def _run(self, recs, start_ms, speed):
        wall0, src0, paused_total = time.time(), recs[0]["time"], 0.0
        self._set_clock(src0, wall0, paused_total, speed)
        self._publish()
        for i, r in enumerate(recs):
            # wait until this call's moment comes round (scaled by speed; pauses don't count)
            while True:
                if self.stop_flag.is_set():
                    return
                if self.pause_flag.is_set():
                    t0 = time.time()
                    while self.pause_flag.is_set() and not self.stop_flag.is_set():
                        time.sleep(0.2)
                    paused_total += time.time() - t0
                    self._set_clock(src0, wall0, paused_total, speed)
                    self._publish()
                    continue
                with self.lock:
                    sp = self.state["speed"]
                if sp != speed:                 # speed changed mid-run: re-anchor the clock here
                    cur = min(r["time"], src0 + (time.time() - wall0 - paused_total) * 1000 * speed)
                    wall0, src0, paused_total, speed = time.time(), cur, 0.0, sp
                    self._set_clock(src0, wall0, paused_total, speed)
                    self._publish()
                due = (r["time"] - src0) / 1000 / speed
                if time.time() - wall0 - paused_total >= due:
                    break
                time.sleep(min(0.25, max(0.02, due - (time.time() - wall0 - paused_total))))
            shown = _now_ms()                   # the call "happens now"
            call = {"_id": f"demo-{r['id']}", "time": dt.datetime.fromtimestamp(shown / 1000, dt.timezone.utc).isoformat(),
                    "talkgroupNum": r.get("talkgroup"), "url": r.get("audio_url"), "len": r.get("len")}
            try:
                with self.m.lock:
                    self.m.status["last_poll_ok"] = shown
                self.m.process(call, False)
            except Exception as e:
                self.log(f"Demo: skipped a call ({type(e).__name__}: {e})")
            with self.lock:
                self.state["done"], self.state["at"] = i + 1, r["time"]
            if (i + 1) % 3 == 0 or i + 1 == len(recs):
                self._publish()
        with self.lock:
            self.state["state"] = "finished"
            if self.state.get("clock"):
                self.state["clock"]["frozen"] = recs[-1]["time"]
        self._publish()
        self.log("Demo: finished")

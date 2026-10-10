#!/usr/bin/env python3
"""
Crimson EMS dispatch alert server.

Polls OpenMHz (Cambridge CoMIRS site) for new Pro EMS / Cambridge Fire transmissions,
transcribes each clip with Whisper, flags Harvard locations and "HUPD / private response"
phrases, and pushes alerts to a live dashboard (and optionally ntfy / GroupMe / a webhook).

OpenMHz requests (call lists + audio clips) go through a headless Chromium driven by
Playwright with playwright-stealth applied, so they look like a normal browser session.

Setup:
    pip install playwright playwright-stealth faster-whisper
    python3 -m playwright install chromium

Run:  python3 server.py --ntfy-topic crimson-ems-<something-random>
Open: http://localhost:8080  (or http://<this-computer's-IP>:8080 from phones on the same network)
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import glob
import json
import os
import queue
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from matcher import Matcher, looks_like_dispatch, LEVEL_RANK
from board import Board, call_kind, parse as parse_radio
from ai import ClaudeReader
from demo import DemoController, DemoFetcher
from acuity import classify as classify_acuity
from textfilter import TextFilter
from ourcalls import CallLog, MILESTONES, OUTCOMES, SOURCES
from feedback import FeedbackStore
from geo import Geocoder, travel

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOG_DIR = os.path.join(HERE, "logs")
UA = "CrimsonEMS-Dispatch-Alert/1.0 (+student EMS dispatch monitor)"  # used for push channels only

CONTEXT_WINDOW_S = 45        # transmissions on one talkgroup within this window are matched together
ALERT_DEDUPE_S = 180         # same location within this window updates the existing alert
LATE_UPLOAD_SLACK_MS = 90_000  # re-ask for calls up to 90 s older than the cursor (out-of-order uploads)
HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching",
                  "so", "okay", "uh", "um", "music"}


def now_ms():
    return int(time.time() * 1000)


def parse_iso_ms(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _norm_words(s):
    import re
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()


def is_prompt_echo(text, prompt):
    """True when a short transcript is just a run of words copied from the Whisper prompt."""
    import re
    p = f" {' '.join(_norm_words(prompt or ''))} "
    parts = [_norm_words(x) for x in re.split(r"[.!?,;]+", text)]
    parts = [x for x in parts if x]
    if not parts or p == "  " or sum(map(len, parts)) > 24 or any(len(x) > 6 for x in parts):
        return False
    # every sentence/phrase is a verbatim piece of the prompt (catches "HUPD private response. HUPD private
    # response." and regurgitated lists like "Mass Ave, Mount Auburn Street, Plympton Street, Quincy Street,")
    return all(f" {' '.join(x)} " in p for x in parts)


def print_recent_calls(calls, tg_names, n=3):
    """Print the newest n calls OpenMHz returned, so you can confirm the feed works."""
    recent = sorted(calls, key=lambda c: c["time"])[-n:]
    if not recent:
        log("OpenMHz returned 0 calls for these talkgroups (feed reachable, just quiet).")
        return
    log(f"Last {len(recent)} call(s) from OpenMHz:")
    for c in reversed(recent):
        t = dt.datetime.fromtimestamp(parse_iso_ms(c["time"]) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        tg = str(c.get("talkgroupNum"))
        print(f"    {t}  [{tg} {tg_names.get(tg, '')}]  {c.get('len', '?')}s  {c.get('url')}", flush=True)


# ---------------------------------------------------------------------------- config

class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with open(path) as f:
            self.data = json.load(f)

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def update(self, new):
        allowed = {"talkgroups", "rules", "address_ranges", "whisper_prompt", "base"}
        with self.lock:
            for k in allowed & set(new):
                self.data[k] = new[k]
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
            return json.loads(json.dumps(self.data))


# ---------------------------------------------------------------------------- HTTP fetchers (OpenMHz)

class HTTPStatusError(Exception):
    def __init__(self, status, url):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


class UrllibFetcher:
    """Plain urllib fetcher (the original behavior). Used with --no-stealth."""
    name = "urllib"

    def get_json(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)

    def get_bytes(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()


class StealthFetcher:
    """
    Fetches through a real headless Chromium with playwright-stealth applied.

    Playwright's sync API is bound to the thread that created it, but the poll loop and
    the work loop run on different threads. So one dedicated browser thread owns the
    Playwright objects and every request is handed to it as a job on a queue.
    """
    BLOCK_STATUSES = (403, 429, 503)

    def __init__(self, warmup_url, headless=True, timeout_s=30, channel=None):
        self.warmup_url = warmup_url
        self.channel = channel or None   # e.g. "chrome" = use the installed Google Chrome
        self.headless = headless
        self.timeout_ms = int(timeout_s * 1000)
        self.jobs = queue.Queue()
        self.fatal = None
        self.started = threading.Event()
        self._pw = self._browser = self._context = self._page = None
        self._per_page_stealth = None   # set when only playwright-stealth 1.x is installed
        self.name = "playwright-stealth (starting)"
        threading.Thread(target=self._run, name="stealth-browser", daemon=True).start()

    # ---- public API (safe to call from any thread)
    def get_json(self, url):
        return json.loads(self._submit(self._get_text, url))

    def get_bytes(self, url):
        return self._submit(self._get_bytes, url)

    def _submit(self, fn, *args):
        self.started.wait(60)
        if self.fatal:
            raise RuntimeError(f"stealth browser unavailable: {self.fatal}")
        fut = concurrent.futures.Future()
        self.jobs.put((fn, args, fut))
        return fut.result(timeout=300)

    # ---- browser thread
    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        except Exception as e:
            self.fatal = (f"{type(e).__name__}: {e}  "
                          "(pip install playwright playwright-stealth && python3 -m playwright install chromium)")
            log("Stealth browser failed to start:", self.fatal)
            self.started.set()
            return
        self.started.set()
        while True:
            fn, args, fut = self.jobs.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                if self._page is None or self._page.is_closed():
                    self._launch()
                fut.set_result(fn(*args))
            except HTTPStatusError as e:
                fut.set_exception(e)
            except Exception as e:
                # Browser crashed / target closed / timeout: start fresh on the next job.
                fut.set_exception(e)
                log(f"Stealth browser error ({type(e).__name__}); relaunching on next request.")
                self._teardown()

    def _launch(self):
        self._teardown()
        log(f"Launching stealth browser ({self.channel or 'Playwright Chromium'})...")
        # Persistent profile: cookies (including a passed browser check) survive restarts.
        profile = os.path.join(HERE, ".browser-profile")
        self._context = self._pw.chromium.launch_persistent_context(
            profile, headless=self.headless, channel=self.channel,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1366, "height": 850}, locale="en-US", timezone_id="America/New_York")
        version = self._apply_stealth(self._context)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        if self._per_page_stealth:
            self._per_page_stealth(self._page)
        self._page.set_default_timeout(self.timeout_ms)
        self.name = f"{self.channel or 'Chromium'} + {version}"
        self._warm()
        log(f"Stealth browser ready ({self.name}).")

    def _apply_stealth(self, context):
        try:
            from playwright_stealth import Stealth          # playwright-stealth >= 2.0
            Stealth().apply_stealth_sync(context)
            return "playwright-stealth 2.x"
        except ImportError:
            pass
        try:
            from playwright_stealth import stealth_sync     # playwright-stealth 1.x (per page)
            self._per_page_stealth = stealth_sync
            return "playwright-stealth 1.x"
        except ImportError:
            log("WARNING: playwright-stealth not installed; using plain Playwright.")
            return "plain Playwright (no stealth)"

    def _warm(self):
        """Load the public OpenMHz page first so cookies / any bot challenge are handled
        the way a real visitor's browser would handle them."""
        try:
            log(f"Warm-up: loading {self.warmup_url}")
            resp = self._page.goto(self.warmup_url, wait_until="domcontentloaded")
            self._page.wait_for_timeout(2500)
            log(f"Warm-up: HTTP {resp.status if resp else '?'}, title {self._page.title()!r}")
            if self._challenged():
                wait_s = 20 if self.headless else 180
                if self.headless:
                    log("Warm-up: OpenMHz is showing a browser check; waiting up to 20 s for it to clear...")
                else:
                    log("Warm-up: OpenMHz is showing a browser check. If it asks, complete it in the "
                        f"Chrome window (waiting up to {wait_s} s)...")
                deadline = time.time() + wait_s
                while self._challenged() and time.time() < deadline:
                    self._page.wait_for_timeout(1000)
                if self._challenged():
                    log("Warm-up: browser check did NOT clear. Try running with --headful once.")
                else:
                    log(f"Warm-up: check passed, title {self._page.title()!r}")
        except Exception as e:
            log(f"Warm-up load of {self.warmup_url} failed: {type(e).__name__}: {e}")

    def _challenged(self):
        try:
            return "just a moment" in self._page.title().lower()
        except Exception:
            return False

    def _teardown(self):
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self._browser = self._context = self._page = None

    def _get_text(self, url, retried=False):
        resp = self._page.goto(url, wait_until="domcontentloaded")
        status = resp.status if resp else 0
        if status != 200:
            log(f"OpenMHz API returned HTTP {status} (title {self._page.title()!r})")
        if status in self.BLOCK_STATUSES:
            # Possibly an interstitial JS challenge: give it time to clear and redirect.
            deadline = time.time() + 20
            while time.time() < deadline:
                self._page.wait_for_timeout(1000)
                try:
                    body = self._page.inner_text("body")
                    json.loads(body)
                    return body
                except Exception:
                    continue
            if not retried:
                self._warm()
                return self._get_text(url, retried=True)
            raise HTTPStatusError(status, url)
        if status >= 400:
            raise HTTPStatusError(status, url)
        return resp.text()

    def _get_bytes(self, url):
        # context.request shares the browser context's cookies and user agent.
        r = self._context.request.get(url, timeout=self.timeout_ms)
        if r.ok:
            return r.body()
        if r.status not in self.BLOCK_STATUSES:
            raise HTTPStatusError(r.status, url)
        # Fall back to an in-page fetch (real browser network stack).
        b64 = self._page.evaluate(
            """async (u) => {
                const r = await fetch(u);
                if (!r.ok) return {status: r.status};
                const buf = new Uint8Array(await r.arrayBuffer());
                let s = ''; const CH = 0x8000;
                for (let i = 0; i < buf.length; i += CH) s += String.fromCharCode.apply(null, buf.subarray(i, i + CH));
                return {status: 200, data: btoa(s)};
            }""", url)
        if b64.get("status") != 200:
            raise HTTPStatusError(b64.get("status"), url)
        import base64
        return base64.b64decode(b64["data"])


# ---------------------------------------------------------------------------- transcription

ENHANCE = os.environ.get("NO_ENHANCE") != "1"      # audio enhancement layer (see enhance.py); --no-enhance turns it off
ENHANCE_STATS = {"clips": 0, "denoised": 0, "tones": 0, "fallback_used": 0}


def condition_audio(audio, sr=16000):
    """Plain clean-up only: radio voice band-pass, steady level, padding (no enhancement)."""
    import enhance
    return enhance.condition(audio)


def prepare_audio(audio):
    """What Whisper hears first. Returns (audio, plain_or_None): plain_or_None is the un-enhanced version
    to fall back to when the enhanced clip comes out unclear (None when enhancement changed nothing)."""
    import enhance
    if not ENHANCE:
        return enhance.condition(audio), None
    st = {}
    x = enhance.enhance(audio, stats=st)
    ENHANCE_STATS["clips"] += 1
    changed = st.get("denoised") or st.get("tones")
    ENHANCE_STATS["denoised"] += 1 if st.get("denoised") else 0
    ENHANCE_STATS["tones"] += 1 if st.get("tones") else 0
    return x, (enhance.condition(audio) if changed else None)


class LocalWhisper:
    def __init__(self, model_name):
        from faster_whisper import WhisperModel  # noqa: imported lazily so --help works without it
        log(f"Loading Whisper model '{model_name}' (first run downloads it)...")
        self.model = WhisperModel(model_name, device="auto", compute_type="int8")
        self.name = f"local faster-whisper ({model_name})"
        log("Whisper ready.")

    @staticmethod
    def _decode(path):
        """Decode audio to 16 kHz mono float32 ourselves. faster-whisper's own decoder passes
        `metadata_errors` to av.open(), which newer PyAV versions no longer accept."""
        import av
        import numpy as np
        chunks = []
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        with av.open(path) as container:
            for frame in container.decode(audio=0):
                for f in resampler.resample(frame):
                    chunks.append(f.to_ndarray().reshape(-1))
            for f in resampler.resample(None):
                chunks.append(f.to_ndarray().reshape(-1))
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32) / 32768.0

    def _pass(self, audio, prompt, beam, vad, use_prompt):
        kw = dict(language="en", beam_size=beam, condition_on_previous_text=False,
                  initial_prompt=(prompt or None) if use_prompt else None,
                  no_speech_threshold=0.6, log_prob_threshold=-1.0, compression_ratio_threshold=2.4)
        if vad:
            # gentler than the default so weak radio speech isn't cut out
            kw.update(vad_filter=True, vad_parameters={"threshold": 0.35, "min_silence_duration_ms": 500,
                                                        "speech_pad_ms": 300})
        segments, _info = self.model.transcribe(audio, **kw)
        keep = []
        for seg in segments:
            # Whisper "fills in" static and squelch with guesses (often words from the prompt).
            # Drop segments it is itself unsure are speech, very low-confidence ones, and repetition loops.
            if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
                continue
            if seg.avg_logprob < -1.3 or seg.compression_ratio > 2.4:
                continue
            keep.append(seg)
        text = " ".join(x.text.strip() for x in keep).strip()
        weight = sum(max(1, len(x.text)) for x in keep)
        conf = sum(x.avg_logprob * max(1, len(x.text)) for x in keep) / weight if keep else -9.0
        return text, conf

    def transcribe(self, path, prompt):
        audio = self._decode(path)
        if audio.size < 1600:   # under 0.1 s: nothing to transcribe
            return ""
        audio, plain = prepare_audio(audio)
        text, conf = self._pass(audio, prompt, beam=5, vad=True, use_prompt=True)
        if conf < -0.6 or not text:
            # Unclear clip: retry without the voice detector (it can cut weak speech) and without the
            # vocabulary prompt (it pulls Whisper toward prompt words on noise), with a wider beam, and
            # on the un-enhanced audio if enhancement changed it. Keep whichever result Whisper is most confident in.
            tries = [(audio, False, True), (audio, True, False)] + ([(plain, True, True)] if plain is not None else [])
            for a, vad, use_prompt in tries:
                t2, c2 = self._pass(a, prompt, beam=8, vad=vad, use_prompt=use_prompt)
                if t2 and c2 > conf + 0.05:
                    text, conf = t2, c2
                    if a is plain:
                        ENHANCE_STATS["fallback_used"] += 1
        return text


class OpenAIWhisper:
    def __init__(self, api_key, model):
        self.key, self.model = api_key, model
        self.name = f"OpenAI API ({model})"

    def transcribe(self, path, prompt):
        boundary = uuid.uuid4().hex
        with open(path, "rb") as f:
            audio = f.read()
        parts = []
        for k, v in (("model", self.model), ("language", "en"), ("prompt", prompt or "")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="clip.mp3"\r\n'
                     f'Content-Type: audio/mpeg\r\n\r\n'.encode() + audio + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions", data=b"".join(parts), method="POST",
            headers={"Authorization": f"Bearer {self.key}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("text", "").strip()


# ---------------------------------------------------------------------------- push channels

class Pusher:
    def __init__(self, args):
        self.args = args

    def enabled(self):
        a = self.args
        return any([a.ntfy_topic, a.groupme_bot_id, a.webhook_url])

    def send(self, alert, kind="alert"):
        threading.Thread(target=self._send, args=(alert, kind), daemon=True).start()

    def _post(self, url, data, headers):
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **headers}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()

    def _send(self, alert, kind):
        a = self.args
        if kind == "alert":
            terms = ", ".join(h["term"] for h in alert["hits"])
            title = ("HARVARD CALL: " if alert["level"] == "high" else "Possible Harvard call: ") + terms
            if alert.get("acuity") == "high":
                title = "HIGH ACUITY " + title
            elif alert.get("acuity") == "low":
                title += " (low acuity)"
            body = f'[{alert["talkgroup_name"]}] "{alert["text"]}"'
            prio = "5" if alert["level"] == "high" else "3"
            tags = "rotating_light,ambulance" if alert["level"] == "high" else "warning"
        else:
            title, body, prio, tags = alert["title"], alert["text"], "4", "warning"
        if a.ntfy_topic:
            try:
                hdrs = {"Title": title.encode("ascii", "ignore").decode(), "Priority": prio, "Tags": tags}
                if a.public_url:
                    hdrs["Click"] = a.public_url
                if kind == "alert" and alert.get("audio_url"):
                    hdrs["Actions"] = f"view, Play audio, {alert['audio_url']}"
                self._post(f"{a.ntfy_server.rstrip('/')}/{a.ntfy_topic}", body.encode(), hdrs)
            except Exception as e:
                log("ntfy push failed:", e)
        if a.groupme_bot_id:
            try:
                text = f"{title}\n{body}" + (f"\n{a.public_url}" if a.public_url else "")
                self._post("https://api.groupme.com/v3/bots/post",
                           json.dumps({"bot_id": a.groupme_bot_id, "text": text[:990]}).encode(),
                           {"Content-Type": "application/json"})
            except Exception as e:
                log("GroupMe push failed:", e)
        if a.webhook_url:
            try:
                payload = {"text": f"{title}\n{body}", "alert": alert if kind == "alert" else None}
                self._post(a.webhook_url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
            except Exception as e:
                log("Webhook push failed:", e)


# ---------------------------------------------------------------------------- live event hub (SSE)

class Hub:
    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    def count(self):
        with self.lock:
            return len(self.clients)


# ---------------------------------------------------------------------------- monitor

class Monitor:
    def __init__(self, args, config, hub, pusher, fetcher):
        self.args, self.config, self.hub, self.pusher = args, config, hub, pusher
        self.fetcher = fetcher
        self.matcher = Matcher(config.get())
        self.textfilter = self._make_textfilter()
        self.transcriber = None
        self.work = queue.Queue()
        self.seen = collections.OrderedDict()
        self.calls = collections.deque(maxlen=300)
        self.alerts = collections.deque(maxlen=100)
        self.context = collections.defaultdict(collections.deque)
        self.cursor_ms = None
        self.replay = None
        self.board = Board()
        self._logged_recs = {}       # last few hours of the call log (filled at startup)
        self.ai = None               # ClaudeReader for the live monitor (never for replays)
        self.feedback = None         # FeedbackStore: crew corrections, learned fixes (shared by live / demo / replay)
        self.geo = None              # Geocoder (shared)
        self.calllog = None          # set for the live monitor only (not replays)
        self.lock = threading.Lock()
        self.status = {"started": now_ms(), "last_poll_ok": None, "last_poll_error": None,
                       "last_call_ms": None, "transcriber": "loading...", "feed_alarm_sent": False,
                       "calls_processed": 0, "fetcher": getattr(fetcher, "name", "?"), "asr_sec": None}
        self._asr_times = collections.deque(maxlen=30)
        os.makedirs(LOG_DIR, exist_ok=True)

    # -- helpers
    def reload_rules(self):
        self.matcher = Matcher(self.config.get())
        self.textfilter = self._make_textfilter()

    def _make_textfilter(self):
        """Radio vocabulary from config + street names heard in the last few days of logs."""
        tf = TextFilter(self.config.get())
        texts = []
        for f in sorted(glob.glob(os.path.join(LOG_DIR, "calls-*.jsonl")))[-4:]:
            try:
                with open(f) as fh:
                    for line in fh:
                        try:
                            texts.append(json.loads(line).get("text") or "")
                        except ValueError:
                            pass
            except OSError:
                pass
        tf.learn_streets(texts)
        return tf

    def tg_names(self):
        return self.config.get().get("talkgroups", {})

    def snapshot(self):
        with self.lock:
            st = dict(self.status)
            st["queue"] = self.work.qsize()
            st["clients"] = self.hub.count()
            st["push"] = self.pusher.enabled()
            st["fetcher"] = getattr(self.fetcher, "name", "?")
            st["ai"] = self.ai.status if self.ai else "off"
            return {"calls": list(self.calls)[-150:], "alerts": list(self.alerts), "board": self.board.snapshot(),
                    "status": st, "talkgroups": self.tg_names()}

    def _set_status(self, **kw):
        with self.lock:
            self.status.update(kw)
        self.hub.publish("status", self.snapshot()["status"])

    def _log_path(self, kind):
        tag = getattr(self, "log_tag", None)
        if tag:
            return os.path.join(LOG_DIR, f"replay-{tag}-{kind}.jsonl")
        return os.path.join(LOG_DIR, f"calls-{time.strftime('%Y-%m-%d')}.jsonl" if kind == "calls" else "alerts.jsonl")

    def _write_log(self, rec):
        with open(self._log_path("calls"), "a") as f:
            f.write(json.dumps(rec) + "\n")

    # -- OpenMHz
    def _api(self, path, params):
        url = f"{self.args.api_base.rstrip('/')}/{self.config.get()['system']}/{path}"
        tgs = ",".join(self.tg_names().keys())
        q = {"filter-type": "talkgroup", "filter-code": tgs, **params}
        return self.fetcher.get_json(url + "?" + urllib.parse.urlencode(q)).get("calls", [])

    def _startup_calls(self, cutoff_ms=None):
        """Recent calls at power-on; pages further back if the first page doesn't reach the rewind cutoff."""
        calls = {c["_id"]: c for c in self._api("calls", {})}
        if cutoff_ms and calls:
            for _ in range(6):
                oldest = min(parse_iso_ms(c["time"]) for c in calls.values())
                if oldest <= cutoff_ms:
                    break
                more = self._api("calls/older", {"time": oldest})
                new = [c for c in more if c["_id"] not in calls]
                if not new:
                    break
                for c in new:
                    calls[c["_id"]] = c
        return sorted(calls.values(), key=lambda c: c["time"])

    def poll_loop(self):
        backoff = self.args.poll
        while True:
            try:
                if self.cursor_ms is None:
                    rewind_ms = max(0, int(getattr(self.args, "rewind", 0) or 0)) * 60_000
                    calls = self._startup_calls(now_ms() - rewind_ms if rewind_ms else None)
                    if calls:
                        self.cursor_ms = parse_iso_ms(calls[-1]["time"])
                    else:
                        self.cursor_ms = now_ms() - 60_000
                    for c in calls:
                        self.seen[c["_id"]] = 1
                    if rewind_ms:
                        # Power-on rewind: everything from the last N minutes goes through the full pipeline
                        # (feed, board, Claude, and alerts, marked "while the monitor was off").
                        cutoff = now_ms() - rewind_ms
                        window = [c for c in calls if parse_iso_ms(c["time"]) >= cutoff]
                        known = self._logged_recs
                        reuse = [dict(known[c["_id"]], backfill=True) for c in window if c["_id"] in known]
                        fresh = [c for c in window if c["_id"] not in known]
                        with self.lock:
                            for r in reuse:                      # already transcribed before a quick restart
                                self.calls.append(r)
                        for c in fresh:
                            self.work.put((c, "rewind"))
                        log(f"Connected to OpenMHz. Rewinding the last {rewind_ms // 60_000} min: {len(window)} calls "
                            f"({len(fresh)} to transcribe, {len(reuse)} already transcribed). Harvard calls in that window will alert.")
                    else:
                        backfill = calls[-self.args.backfill:] if self.args.backfill else []
                        for c in backfill:
                            self.work.put((c, True))
                        log(f"Connected to OpenMHz. {len(calls)} recent calls; backfilling {len(backfill)} (no alarms).")
                    print_recent_calls(calls, self.tg_names())
                else:
                    calls = self._api("calls/newer", {"time": self.cursor_ms - LATE_UPLOAD_SLACK_MS})
                    calls.sort(key=lambda c: c["time"])
                    for c in calls:
                        if c["_id"] in self.seen:
                            continue
                        self.seen[c["_id"]] = 1
                        self.cursor_ms = max(self.cursor_ms, parse_iso_ms(c["time"]))
                        self.work.put((c, False))
                    while len(self.seen) > 5000:
                        self.seen.popitem(last=False)
                self._set_status(last_poll_ok=now_ms(), last_poll_error=None, feed_alarm_sent=False)
                backoff = self.args.poll
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                log("Poll error:", err)
                self._set_status(last_poll_error={"at": now_ms(), "msg": err})
                last_ok = self.status["last_poll_ok"] or self.status["started"]
                if now_ms() - last_ok > 10 * 60_000 and not self.status["feed_alarm_sent"]:
                    self.pusher.send({"title": "Crimson dispatch monitor: feed DOWN",
                                      "text": f"Can't reach OpenMHz for 10+ min ({err}). Alerts are NOT working."},
                                     kind="system")
                    self._set_status(feed_alarm_sent=True)
                backoff = min(backoff * 2, 60)
            time.sleep(backoff)

    def _download(self, url):
        last = None
        for attempt in range(3):
            try:
                data = self.fetcher.get_bytes(url)
                suffix = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".mp3"
                fd, path = tempfile.mkstemp(suffix=suffix)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                return path
            except Exception as e:
                last = e
                time.sleep(2 + attempt * 3)
        raise last

    # -- processing
    def work_loop(self):
        while True:
            call, backfill = self.work.get()
            try:
                self.process(call, backfill)
            except Exception:
                log("Processing error:\n" + traceback.format_exc())

    def process(self, call, backfill):
        tg = str(call.get("talkgroupNum"))
        tg_name = self.tg_names().get(tg, f"TG {tg}")
        t_ms = parse_iso_ms(call["time"])
        url = call.get("url")
        text, err = "", None
        try:
            path = self._download(url)
            try:
                t0 = time.time()
                text = self.transcriber.transcribe(path, self.config.get().get("whisper_prompt", ""))
                self._asr_times.append(time.time() - t0)
                with self.lock:
                    self.status["asr_sec"] = round(sum(self._asr_times) / len(self._asr_times), 2)
            finally:
                os.unlink(path)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            log(f"Transcribe failed for {url}: {err}")
        if text.lower().strip(" .!?,") in HALLUCINATIONS:
            text = ""
        asr_text, fixed = None, None
        if text and self.feedback:
            new, used = self.feedback.apply_fixes(text)     # mishearings the crew has corrected 2+ times
            if used:
                asr_text, fixed, text = text, used, new
        raw_text, filtered = None, None
        if text and not self.args_no_filter():
            ok, why = self.textfilter.check(text, call.get("len"))
            if not ok:
                # Not radio traffic (mic click, tones, static, Whisper inventing words): keep it for
                # reference but show it as unclear, and keep it out of alerts and the board.
                raw_text, filtered, text = text, why, ""
        suspect = None
        # Only lines that would raise a Harvard alert are checked for prompt echo: ordinary short radio
        # ("Transporting.", "Squad 2.") also appears in the vocabulary hint and must still reach the board.
        if text and self.matcher.match(text) and is_prompt_echo(text, self.config.get().get("whisper_prompt", "")):
            # e.g. "HUPD private response." or "Pforzheimer." on a burst of static: Whisper may have
            # copied the prompt. It could also be a real one-line repeat of a location, so we still
            # show it, but only as a medium ("possible") alert: amber on the dashboard, no phone push.
            suspect = "prompt echo"

        # rolling per-talkgroup context so a dispatch split over several keyups still matches
        ctx = self.context[tg]
        while ctx and t_ms - ctx[0][0] > CONTEXT_WINDOW_S * 1000:
            ctx.popleft()
        hits = self.matcher.match(text)
        if suspect:
            for h in hits:
                h["level"], h["suspect"] = "medium", suspect
        prev_terms = set().union(*[x[2] for x in ctx]) if ctx else set()
        if text and not suspect:
            ctx.append((t_ms, text, {h["term"] for h in hits}))
        context_text = " ".join(x[1] for x in ctx)
        if len(ctx) > 1 and text and not suspect:
            # Only keep matches that exist *because* words were split across keyups
            # (e.g. "...respond to Kirkland" / "House, for a fall"), not ones already
            # matched in an earlier transmission.
            seen_terms = {h["term"] for h in hits} | prev_terms
            for h in self.matcher.match(" ".join(x[1] for x in list(ctx)[-2:])):
                if h["term"] not in seen_terms:
                    h["from_context"] = True
                    hits.append(h)
            hits.sort(key=lambda h: -LEVEL_RANK[h["level"]])

        rec = {"id": call.get("_id"), "time": t_ms, "talkgroup": tg, "talkgroup_name": tg_name,
               "len": call.get("len"), "audio_url": url, "text": text, "error": err,
               "hits": hits, "dispatch": looks_like_dispatch(context_text), "backfill": bool(backfill),
               "rewind": backfill == "rewind",
               "suspect": suspect, "filtered": filtered, "raw_text": raw_text}
        if fixed:
            rec["asr_text"], rec["fixed"] = asr_text, fixed
        with self.lock:
            self.calls.append(rec)
            self.status["last_call_ms"] = max(self.status["last_call_ms"] or 0, t_ms)
            self.status["calls_processed"] += 1
        self._write_log(rec)
        self.hub.publish("call", rec)
        try:
            if self.board.ingest(rec):
                self._publish_board()
                if self.ai:
                    inc = self.board.incident_for_tg(rec["talkgroup_name"])
                    if inc:
                        self.ai.queue(inc)
        except Exception:
            log("Board error:\n" + traceback.format_exc())
        if text:
            log(f"[{tg_name}] {text}" + (f"   <-- {[h['term'] for h in hits]}" if hits else ""))
        if hits and (not backfill or backfill == "rewind"):
            # EMS only: a Harvard fire alarm / odor / elevator call doesn't alarm; an actual fire at Harvard does
            kind = self.board.kind_of(rec["audio_url"])
            if kind is None:
                p = parse_radio(context_text, tg_name)
                kind = call_kind(p["complaint"], p["units"], [context_text])
            if kind == "fire":
                log(f"(Harvard fire call that isn't an actual fire: no alert) {[h['term'] for h in hits]}")
            else:
                self.raise_alert(rec, hits, context_text)

    def raise_alert(self, rec, hits, context_text):
        level = "high" if any(h["level"] == "high" for h in hits) else "medium"
        with self.lock:
            for a in reversed(self.alerts):
                if a.get("test") or a.get("dismissed") or rec["time"] - a["last_time"] > ALERT_DEDUPE_S * 1000:
                    continue
                if self._same_incident(hits, a["hits"], rec["time"] - a["last_time"]):
                    # same incident: fold this transmission in
                    a["transmissions"].append({"time": rec["time"], "text": rec["text"],
                                               "audio_url": rec["audio_url"], "talkgroup_name": rec["talkgroup_name"]})
                    a["last_time"] = rec["time"]
                    known = {h["term"] for h in a["hits"]}
                    a["hits"] += [h for h in hits if h["term"] not in known]
                    acu, why = classify_acuity(" ".join(x["text"] or "" for x in a["transmissions"]))
                    if acu and (a.get("acuity") != "high"):
                        a["acuity"], a["acuity_why"] = acu, why
                    upgraded = level == "high" and a["level"] == "medium"
                    if upgraded:
                        a["level"] = "high"
                        a["acked_by"] = None
                        a["text"] = rec["text"] or a["text"]
                        a["audio_url"] = rec["audio_url"] or a["audio_url"]
                    self.hub.publish("alert_update", a)
                    if upgraded:
                        self.hub.publish("alert", a)
                        if self._should_push(a):
                            self.pusher.send(a)
                    return
            alert = {"id": uuid.uuid4().hex[:10], "created": now_ms(), "time": rec["time"],
                     "while_off": bool(rec.get("rewind")),
                     "last_time": rec["time"], "level": level, "hits": hits,
                     "text": rec["text"] or context_text, "context": context_text,
                     "talkgroup_name": rec["talkgroup_name"], "audio_url": rec["audio_url"],
                     "dispatch": rec["dispatch"], "acked_by": None, "acked_at": None,
                     "acuity": classify_acuity(context_text)[0], "acuity_why": classify_acuity(context_text)[1],
                     "transmissions": [{"time": rec["time"], "text": rec["text"], "audio_url": rec["audio_url"],
                                        "talkgroup_name": rec["talkgroup_name"]}]}
            self.alerts.append(alert)
        log(f"*** {level.upper()} ALERT: {[h['term'] for h in hits]}")
        self.hub.publish("alert", alert)
        with open(self._log_path("alerts"), "a") as f:
            f.write(json.dumps(alert) + "\n")
        if self._should_push(alert):
            self.pusher.send(alert)

    @staticmethod
    def _same_incident(new_hits, old_hits, gap_ms):
        """Fold a transmission into an open alert only when it names the same place."""
        generic = {"HUPD / private", "General"}
        new_loc = {h["term"] for h in new_hits if h.get("category") not in generic}
        old_loc = {h["term"] for h in old_hits if h.get("category") not in generic}
        if new_loc:
            return bool(new_loc & old_loc)
        if old_loc:
            # e.g. "HUPD on scene" right after "Weld Hall..." on the same alert window
            return gap_ms < 60_000 and any(h.get("category") == "HUPD / private" for h in new_hits)
        return gap_ms < 60_000 and bool({h["term"] for h in new_hits} & {h["term"] for h in old_hits})

    def start_replay(self, start, end):
        start_ms, end_ms = parse_local_ms(start), parse_local_ms(end)
        validate_window(start_ms, end_ms)
        with self.lock:
            if self.replay and self.replay.running():
                raise RuntimeError("a replay is already running; cancel it first")
            self.replay = ReplayJob(self, start_ms, end_ms)
            job = self.replay
        threading.Thread(target=job.run, name="replay", daemon=True).start()
        return job

    def _seed_board(self, hours=3):
        """Rebuild the call board from the last few hours of the call log, so a restart doesn't wipe it."""
        cutoff = now_ms() - hours * 3600_000
        recs = {}
        for day in (time.time() - 86400, time.time()):
            path = os.path.join(LOG_DIR, f"calls-{time.strftime('%Y-%m-%d', time.localtime(day))}.jsonl")
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("time", 0) >= cutoff and r.get("id"):
                            recs[r["id"]] = r
            except OSError:
                pass
        n = 0
        for r in sorted(recs.values(), key=lambda r: r["time"]):
            try:
                n += bool(self.board.ingest(r))
            except Exception:
                pass
        self._logged_recs = recs
        self.board.tick()
        if recs:
            snap = self.board.snapshot()
            log(f"Call board: rebuilt from {len(recs)} logged transmissions "
                f"({len(snap['incidents'])} active, {len(snap['closed'])} recently closed).")
        self._publish_board()

    def board_tick_loop(self):
        """Close idle incidents on the live board even when the radio is quiet."""
        while True:
            time.sleep(60)
            try:
                if self.board.tick():
                    self._publish_board()
            except Exception:
                log("Board tick error:\n" + traceback.format_exc())

    # ---------------------------------------------------------------- our calls (the calls we respond to)
    def _handle_merges(self):
        """Duplicate board cards were merged: logged calls follow, and Claude re-reads the combined card."""
        for src, dst, why in self.board.pop_merges():
            log(f"Merged duplicate call {src} into {dst} ({why})")
            if self.calllog:
                self.calllog.relink(src, dst)
            if self.ai:
                inc = self.board.get(dst)
                if inc:
                    self.ai.queue(inc)

    BOARD_FIXES = os.path.join(LOG_DIR, "board-fixes.json")

    def load_board_fixes(self):
        try:
            with open(self.BOARD_FIXES) as f:
                self.board.fixes = json.load(f)
        except (OSError, ValueError):
            pass

    def board_fix(self, iid, audio_url, to_iid=None, by=None):
        """Crew says a transmission was wrongly put in a call: take it out (or move it to the right call)."""
        out = self.board.detach(iid, audio_url, to_iid)
        if not out:
            return None
        src, dst = out
        if not getattr(self, "log_tag", None):             # live board only: remember it across restarts
            try:
                tmp = self.BOARD_FIXES + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.board.fixes, f)
                os.replace(tmp, self.BOARD_FIXES)
            except OSError:
                pass
        if self.feedback:
            self.feedback.record_board(src, audio_url, dst, by)
        self._publish_board()
        if self.ai:
            for inc in (src, dst):
                if inc and not inc.get("dismissed"):
                    self.ai.queue(inc)
        line = next((e["text"] for e in (dst or src)["timeline"] if e.get("audio_url") == audio_url), "")
        log(f"Board fix by {by or 'someone'}: \"{line[:60]}\" " + (f"moved to {dst['id']}" if dst else f"removed from {src['id']}"))
        return {"from": src, "to": dst}

    def radio_context(self, hours=6, max_lines=1400):
        """Text for 'Ask the radio': the call board plus recent transcripts (radio only: no crew notes)."""
        now = now_ms()
        since = now - hours * 3600_000
        recs = {}
        if not getattr(self, "log_tag", None):          # live: today's / yesterday's logs cover more than memory
            for f in sorted(glob.glob(os.path.join(LOG_DIR, "calls-*.jsonl")))[-2:]:
                try:
                    with open(f) as fh:
                        for line in fh:
                            try:
                                r = json.loads(line)
                            except ValueError:
                                continue
                            if r.get("time", 0) >= since and r.get("id"):
                                recs[r["id"]] = r
                except OSError:
                    pass
        with self.lock:
            for r in self.calls:
                if r.get("time", 0) >= since:
                    recs[r.get("id") or r.get("audio_url")] = r
        lines = []
        for r in sorted(recs.values(), key=lambda r: r["time"]):
            t = r.get("text") or ""
            if not t and r.get("raw_text"):
                t = f"(unclear) {r['raw_text']}"
            if t:
                lines.append((r["time"], f"{dt.datetime.fromtimestamp(r['time'] / 1000):%H:%M:%S} [{r.get('talkgroup_name', '')}] {t}"))
        lines = lines[-max_lines:]
        b = self.board.snapshot()
        def inc_line(i, closed):
            units = ", ".join(f"{u['unit']} ({u['status']})" for u in i["units"]) or "no unit named"
            where = " / ".join(x for x in [i.get("address"), i.get("place"), i.get("town")] if x) or "location unclear"
            return (f"- {'CLOSED ' if closed else ''}{i.get('complaint') or 'Call'} at {where}; units: {units}; "
                    f"status {i.get('status')}; opened {dt.datetime.fromtimestamp(i['opened'] / 1000):%H:%M}"
                    + (f"; HARVARD ({', '.join(i.get('harvard_terms') or [])})" if i.get("harvard") else "")
                    + (f"; summary: {i['ai_summary']}" if i.get("ai_summary") else ""))
        board = [inc_line(i, False) for i in b["incidents"]] + [inc_line(i, True) for i in b["closed"][:15]]
        alerts = [f"- {dt.datetime.fromtimestamp(a['time'] / 1000):%H:%M:%S} {a['level']} Harvard alert: "
                  f"{', '.join(h['term'] for h in a['hits'])}" + (" (dismissed / false alarm)" if a.get("dismissed") else "")
                  for a in list(self.alerts)[-15:] if not a.get("test")]
        txt = (f"Current time: {dt.datetime.now():%A %b %d, %H:%M:%S} (local)\n\n"
               f"CALL BOARD (auto-built, may be incomplete):\n" + ("\n".join(board) or "(empty)") +
               "\n\nHARVARD ALERTS:\n" + ("\n".join(alerts) or "(none)") +
               f"\n\nRADIO TRANSCRIPTS, last {hours} h, oldest first ({len(lines)} lines):\n" + "\n".join(l for _, l in lines))
        return txt, [(t, l) for t, l in lines], recs

    def other_calls_for_ai(self, inc):
        snap = self.board.snapshot()
        out = []
        for o in snap["incidents"]:
            if o["id"] == inc["id"] or abs(o["opened"] - inc["opened"]) > 45 * 60_000:
                continue
            out.append({"id": o["id"], "opened": o["opened"],
                        "where": " · ".join(x for x in [o.get("address"), o.get("place")] if x),
                        "what": o.get("complaint"), "units": [u["unit"] for u in o.get("units", [])],
                        "summary": o.get("ai_summary"),
                        "heard": next((e["text"] for e in o.get("timeline", []) if e.get("dispatch")), "")[:200]})
        return out

    def _publish_board(self):
        self._handle_merges()
        snap = self.board.snapshot()
        self.hub.publish("board", snap)
        self._acuity_from_board(snap)
        if self.calllog:
            self.calllog.sync_from_board(snap)
            self.hub.publish("ourcalls", self.calls_payload(snap))

    def dismiss_alert(self, alert_id, by=None):
        with self.lock:
            a = next((x for x in self.alerts if x["id"] == alert_id), None)
            if not a:
                return None
            a["dismissed"], a["dismissed_at"] = (by or "someone")[:40], now_ms()
        self.hub.publish("alert_update", a)
        return a

    def dismiss_alerts_for_incident(self, inc, by=None):
        """A board call was dismissed as not real: take its alerts out of the Harvard alerts sidebar too."""
        urls = {e.get("audio_url") for e in inc.get("timeline", []) if e.get("audio_url")}
        hit = []
        with self.lock:
            for a in self.alerts:
                if not a.get("dismissed") and urls & {x.get("audio_url") for x in a.get("transmissions", [])}:
                    a["dismissed"], a["dismissed_at"] = (by or "someone")[:40], now_ms()
                    hit.append(a)
        for a in hit:
            self.hub.publish("alert_update", a)
        return len(hit)

    def nearby_for_ai(self, inc):
        """Radio on the call's channels around its time that isn't on this card (for the AI check)."""
        lines = [e for e in inc.get("timeline", []) if not e.get("note")]
        if not lines:
            return []
        on = {e.get("audio_url") for e in lines}
        tgs = {e.get("talkgroup_name") for e in lines}
        t0, t1 = lines[0]["time"] - 120_000, max(e["time"] for e in lines) + 15 * 60_000
        where = {}
        b = self.board.snapshot()
        for i in b["incidents"] + b["closed"]:
            for e in i["timeline"]:
                if e.get("audio_url"):
                    where[e["audio_url"]] = f"{i.get('complaint') or 'call'} at {i.get('address') or i.get('place') or '?'}"
        out = []
        with self.lock:
            recs = list(self.calls)
        for r in recs:
            if r.get("talkgroup_name") in tgs and t0 <= r["time"] <= t1 and r.get("text") and r.get("audio_url") not in on:
                out.append({"time": r["time"], "talkgroup_name": r["talkgroup_name"], "text": r["text"],
                            "audio_url": r["audio_url"], "on_call": where.get(r["audio_url"])})
        return out[-45:]

    def _apply_ai_lines(self, iid, res):
        """The AI check says some radio is on the wrong card: move it (never against a crew correction)."""
        if (res.get("confidence") or 0) < 0.7:
            return
        moved = 0
        for url in res.get("remove_audio") or []:
            if not self.board.crew_placed(url) and self.board.detach(iid, url, record=False):
                moved += 1
        recs = {r.get("audio_url"): r for r in list(self.calls)}
        for url in res.get("add_audio") or []:
            if self.board.crew_placed(url):
                continue
            src = next((i for i in self.board.snapshot()["incidents"] if any(e.get("audio_url") == url for e in i["timeline"])), None)
            if src and src["id"] != iid:
                if self.board.detach(src["id"], url, to_iid=iid, record=False):
                    moved += 1
            elif not src and recs.get(url):
                if self.board.attach(iid, recs[url]):
                    moved += 1
        if moved:
            log(f"AI check moved {moved} radio line(s) on call {iid}")

    def apply_ai(self, iid, res):
        """Claude finished reading a call: update the board, its alerts, and raise a 'possible Harvard'
        alert if Claude found a Harvard location the keywords missed."""
        self._apply_ai_lines(iid, res)
        inc = self.board.apply_ai(iid, res)
        if not inc:
            return
        dup = res.get("same_call_as")
        if dup and dup != inc["id"] and (res.get("confidence") or 0) >= 0.6 and self.board.get(dup):
            merged = self.board.merge(inc["id"], dup, why="AI: same incident")
            if merged:
                inc = merged
        log(f"AI: {res.get('summary')}  [acuity {res.get('acuity')}, harvard {res.get('harvard')}, conf {res.get('confidence')}]")
        try:
            with open(os.path.join(LOG_DIR, f"ai-{time.strftime('%Y-%m-%d')}.jsonl"), "a") as f:
                f.write(json.dumps({"incident": iid, "at": now_ms(), "result": res,
                                    "transmissions": [e["text"] for e in inc.get("timeline", []) if not e.get("note")][:14]}) + "\n")
        except OSError:
            pass
        urls = {e.get("audio_url") for e in inc.get("timeline", []) if e.get("audio_url")}
        updated, linked = [], False
        with self.lock:
            for a in self.alerts:
                if a.get("test") or not (urls & {x.get("audio_url") for x in a.get("transmissions", [])}):
                    continue
                linked = True
                a["ai_summary"] = res.get("summary")
                if res.get("acuity") in ("high", "low"):
                    a["acuity"], a["acuity_why"] = res["acuity"], ["AI: " + (res.get("acuity_reason") or res["acuity"])]
                updated.append(a)
        for a in updated:
            self.hub.publish("alert_update", a)
        if inc.get("ai_found_harvard") and not linked and (res.get("confidence") or 0) >= 0.6:
            last = [e for e in inc["timeline"] if not e.get("note")][-1]
            rec = {"time": last["time"], "text": res.get("summary") or last["text"], "audio_url": last.get("audio_url"),
                   "talkgroup_name": last.get("talkgroup_name", ""), "dispatch": True}
            hits = [{"term": inc["ai_found_harvard"], "level": "medium", "category": "AI", "heard": "", "score": res.get("confidence")}]
            self.raise_alert(rec, hits, " ".join(e["text"] for e in inc["timeline"][-4:]))
        self._publish_board()

    def _acuity_from_board(self, snap):
        """Alerts whose own words didn't say (e.g. just "Adams House") take the acuity of their board call,
        which also knows who was sent (paramedic / ALS units)."""
        incs = snap["incidents"] + snap["closed"]
        updated = []
        with self.lock:
            for a in self.alerts:
                if a.get("acuity") or a.get("test"):
                    continue
                urls = {x.get("audio_url") for x in a.get("transmissions", []) if x.get("audio_url")}
                for inc in incs:
                    if inc.get("acuity") and urls & {e.get("audio_url") for e in inc.get("timeline", [])}:
                        a["acuity"], a["acuity_why"] = inc["acuity"], list(inc.get("acuity_why") or [])
                        updated.append(a)
                        break
        for a in updated:
            self.hub.publish("alert_update", a)

    def calls_payload(self, snap=None):
        snap = snap or self.board.snapshot()
        return {"calls": self.calllog.list(), "suggestions": [],      # only calls the crew responded to
                "milestones": MILESTONES, "outcomes": OUTCOMES, "sources": SOURCES}

    def _incident(self, iid):
        snap = self.board.snapshot()
        return next((i for i in snap["incidents"] + snap["closed"] if i["id"] == iid), None)

    def _alert_time_for(self, inc):
        """Earliest alarm raised for this incident's transmissions (or for its Harvard terms around then)."""
        urls = {e.get("audio_url") for e in inc.get("timeline", []) if e.get("audio_url")}
        terms = set(inc.get("harvard_terms") or [])
        best = None
        with self.lock:
            for a in self.alerts:
                if a.get("test"):
                    continue
                a_urls = {x.get("audio_url") for x in a.get("transmissions", [])}
                near = inc["opened"] - 120_000 <= a["time"] <= inc["last"] + 60_000
                if (a_urls & urls) or (near and terms & {h["term"] for h in a["hits"]}):
                    t = a.get("created") or a["time"]
                    best = t if best is None else min(best, t)
        return best

    def _incident_for_alert(self, alert):
        urls = {x.get("audio_url") for x in alert.get("transmissions", []) if x.get("audio_url")}
        snap = self.board.snapshot()
        for inc in snap["incidents"] + snap["closed"]:
            if urls & {e.get("audio_url") for e in inc.get("timeline", [])}:
                return inc
        return None

    def add_our_call(self, body):
        by = body.get("by")
        inc, alert_time = None, None
        if body.get("incident_id"):
            inc = self._incident(body["incident_id"])
            if not inc:
                raise ValueError("that call is no longer on the board")
            alert_time = self._alert_time_for(inc)
        elif body.get("alert_id"):
            with self.lock:
                alert = next((a for a in self.alerts if a["id"] == body["alert_id"]), None)
            if not alert:
                raise ValueError("unknown alert")
            inc = self._incident_for_alert(alert)
            alert_time = alert.get("created") or alert["time"]
        times = body.get("times") if isinstance(body.get("times"), dict) else None
        c = self.calllog.add(by=by, incident=inc, alert_time=alert_time, title=body.get("title"),
                             location=body.get("location"), mark=body.get("mark"), source=body.get("source"),
                             notes=body.get("notes"), acuity=body.get("acuity"), times=times,
                             after_the_fact=bool(body.get("after_the_fact")))
        if inc is None and body.get("alert_id"):
            with self.lock:
                alert = next((a for a in self.alerts if a["id"] == body["alert_id"]), None)
            if alert:
                c = self.calllog.update(c["id"], {"title": ", ".join(h["term"] for h in alert["hits"]),
                                                  "notes": f'Radio: "{alert["text"]}"'})
                if alert.get("acuity"):
                    with self.calllog.lock:
                        self.calllog.calls[c["id"]].update(acuity=alert["acuity"], acuity_why=alert.get("acuity_why", []))
                        self.calllog._save()
                    c = self.calllog.get(c["id"])
        self.hub.publish("ourcalls", self.calls_payload())
        return c

    def reset_demo(self):
        """Demo monitor only: wipe calls, alerts, board and the demo call log before a new playback."""
        with self.lock:
            self.calls.clear()
            self.alerts.clear()
            self.context.clear()
            self.seen.clear()
            self.status.update(calls_processed=0, last_call_ms=None, last_poll_ok=now_ms(), last_poll_error=None)
        self.board = Board()
        self.reload_rules()
        if self.calllog:
            with self.calllog.lock:
                self.calllog.calls = {}
                self.calllog._save()
        for kind in ("calls", "alerts"):
            try:
                open(self._log_path(kind), "w").close()
            except OSError:
                pass

    def args_no_filter(self):
        return bool(getattr(self.args, "no_filter", False))

    def _should_push(self, alert):
        return LEVEL_RANK[alert["level"]] >= LEVEL_RANK[self.args.push_level]

    def ack(self, alert_id, who):
        with self.lock:
            for a in self.alerts:
                if a["id"] == alert_id:
                    a["acked_by"], a["acked_at"] = (who or "someone")[:40], now_ms()
                    self.hub.publish("alert_update", a)
                    return a
        return None

    def test_alert(self, push):
        rec = {"time": now_ms(), "text": "TEST — Pro 2 respond to Weld Hall, Harvard Yard, 19 year old intox, HUPD private response.",
               "audio_url": None, "talkgroup_name": "TEST", "dispatch": True}
        hits = [{"term": "TEST ALERT", "level": "high", "category": "Test", "heard": "test", "score": 1.0}]
        level = "high"
        rec["acuity"], rec["acuity_why"] = classify_acuity(rec["text"])
        alert = {"id": uuid.uuid4().hex[:10], "created": now_ms(), "time": rec["time"], "last_time": rec["time"],
                 "level": level, "hits": hits, "text": rec["text"], "context": rec["text"], "test": True,
                 "talkgroup_name": "TEST", "audio_url": None, "dispatch": True, "acked_by": None, "acked_at": None,
                 "transmissions": [{"time": rec["time"], "text": rec["text"], "audio_url": None, "talkgroup_name": "TEST"}],
                 "acuity": rec["acuity"], "acuity_why": rec["acuity_why"]}
        with self.lock:
            self.alerts.append(alert)
        self.hub.publish("alert", alert)
        if push:
            self.pusher.send(alert)
        return alert

    def start(self):
        threading.Thread(target=self._start, daemon=True).start()

    def _start(self):
        # Start polling right away; don't make the feed wait for Whisper to load/download.
        # Calls that arrive meanwhile just queue up until the transcriber is ready.
        self._seed_board()
        threading.Thread(target=self.poll_loop, name="poll", daemon=True).start()
        threading.Thread(target=self.board_tick_loop, name="board", daemon=True).start()
        a = self.args
        try:
            self.transcriber = LockedTranscriber(make_transcriber(a))
        except Exception as e:
            log("Transcriber failed to load:", f"{type(e).__name__}: {e}")
            self._set_status(transcriber=f"FAILED: {type(e).__name__}: {e}")
            return
        self._set_status(transcriber=self.transcriber.name)
        self.work_loop()


# ---------------------------------------------------------------------------- HTTP

def make_handler(monitor, config, hub, args, demo=None):
    dashboard_path = os.path.join(HERE, "dashboard.html")

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _admin_ok(self):
            return not args.admin_key or self.headers.get("X-Admin-Key") == args.admin_key

        def do_GET(self):
            p = urllib.parse.urlparse(self.path).path
            if p in ("/", "/index.html"):
                with open(dashboard_path, "rb") as f:
                    body = f.read()
                if demo:
                    body = body.replace(b"</head>", b"<script>window.CRIMSON_DEMO = true;</script></head>", 1)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif p == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                q = hub.subscribe()
                try:
                    self.wfile.write(b"retry: 3000\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            msg = q.get(timeout=15)
                        except queue.Empty:
                            msg = ": ping\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    hub.unsubscribe(q)
            elif p == "/api/state":
                self._json(monitor.snapshot())
            elif p == "/api/config":
                cfg = config.get()
                cfg["admin_required"] = bool(args.admin_key)
                self._json(cfg)
            elif p == "/api/demo" and demo:
                self._json({"status": demo.status(), "moments": demo.moments(), "speeds": demo.SPEEDS})
            elif p == "/api/ourcalls":
                if not monitor.calllog:
                    return self._json({"error": "call log not available"}, 404)
                self._json(monitor.calls_payload())
            elif p == "/api/board":
                self._json(monitor.board.snapshot())
            elif p == "/api/geo":
                if not monitor.geo:
                    return self._json({"error": "maps not available"}, 404)
                qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                b = config.get().get("base") or {}
                base_addr = (b.get("address") or "").strip() or "Harvard Yard, Cambridge, MA"
                bh = monitor.geo.lookup(base_addr)
                base = {"lat": bh["lat"], "lon": bh["lon"], "label": b.get("label") or base_addr,
                        "is_default": not (b.get("address") or "").strip()} if bh else None
                dest = None
                if qs.get("inc"):
                    inc = monitor.board.get(qs["inc"])
                    if inc:
                        dest = monitor.geo.locate(inc.get("address"), inc.get("place"), inc.get("town"))
                elif qs.get("q"):
                    dest = monitor.geo.locate(qs["q"])
                out = {"base": base, "dest": dest}
                if base and dest:
                    out.update(travel(base, dest))
                self._json(out, 200 if dest else 404)
            elif p == "/api/clipaudio":
                qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                path = monitor.feedback.audio_path(qs.get("key", "")) if monitor.feedback else None
                if not path:
                    return self._json({"error": "not found"}, 404)
                with open(path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            elif p == "/api/feedback":
                fb = monitor.feedback
                if not fb:
                    return self._json({"error": "feedback not available"}, 404)
                self._json({"stats": fb.stats(config.get().get("whisper_prompt", "")), "clips": fb.clip_verdicts(),
                            "alerts": {k: r["verdict"] for k, r in fb.alerts.items()}})
            elif p == "/api/replay":
                job = monitor.replay
                self._json(job.info(full=True) if job else {"state": "none"})
            elif p == "/api/health":
                st = monitor.snapshot()["status"]
                ok = st["last_poll_ok"] and now_ms() - st["last_poll_ok"] < 120_000
                self._json({"ok": bool(ok), **st}, 200 if ok else 503)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            p = urllib.parse.urlparse(self.path).path
            try:
                body = self._body()
            except Exception as e:
                return self._json({"error": str(e)}, 400)
            if p == "/api/ack":
                a = monitor.ack(body.get("id"), body.get("by"))
                return self._json(a or {"error": "unknown alert"}, 200 if a else 404)
            if p == "/api/test":
                if body.get("push") and not self._admin_ok():
                    return self._json({"error": "admin key required to push a test"}, 403)
                return self._json(monitor.test_alert(bool(body.get("push"))))
            if demo and p in ("/api/config", "/api/replay"):
                return self._json({"error": "Not available in demo mode: use the live dashboard for this."}, 403)
            if demo and p.startswith("/api/demo/"):
                action = p.rsplit("/", 1)[1]
                try:
                    if action == "start":
                        def ms(v):
                            if isinstance(v, (int, float)):
                                return int(v)
                            return int(dt.datetime.strptime(str(v)[:16], "%Y-%m-%dT%H:%M").timestamp() * 1000)
                        out = demo.start(ms(body.get("from")), ms(body.get("to")), int(body.get("speed") or 5),
                                         str(body.get("label") or "")[:120])
                    elif action == "pause":
                        out = demo.pause(bool(body.get("on", True)))
                    elif action == "speed":
                        out = demo.set_speed(int(body.get("speed") or 5))
                    elif action == "stop":
                        out = demo.stop()
                    else:
                        return self._json({"error": "not found"}, 404)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                return self._json(out)
            if p == "/api/config":
                if not self._admin_ok():
                    return self._json({"error": "admin key required"}, 403)
                try:
                    for r in body.get("rules", []):
                        assert r["term"] and r.get("level", "high") in LEVEL_RANK
                    if "base" in body:
                        b0 = body["base"] if isinstance(body["base"], dict) else {}
                        body["base"] = {"address": str(b0.get("address") or "")[:200], "label": str(b0.get("label") or "")[:60]}
                    new = config.update(body)
                    monitor.reload_rules()
                    return self._json(new)
                except Exception as e:
                    return self._json({"error": f"bad config: {e}"}, 400)
            if p == "/api/replay":
                if not self._admin_ok():
                    return self._json({"error": "admin key required"}, 403)
                try:
                    job = monitor.start_replay(body.get("from"), body.get("to"))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                except RuntimeError as e:
                    return self._json({"error": str(e)}, 409)
                return self._json(job.info())
            if p == "/api/replay/cancel":
                if not self._admin_ok():
                    return self._json({"error": "admin key required"}, 403)
                if monitor.replay and monitor.replay.running():
                    monitor.replay.cancel()
                return self._json(monitor.replay.info() if monitor.replay else {"state": "none"})
            if p == "/api/board/merge":
                merged = monitor.board.merge(body.get("src"), body.get("dst"),
                                             why=f"merged by {(body.get('by') or 'someone')[:40]}")
                if merged:
                    monitor._publish_board()
                return self._json(merged or {"error": "couldn't merge those"}, 200 if merged else 400)
            if p == "/api/ask":
                reader = monitor.ai or getattr(monitor, "asker", None)
                if not reader:
                    return self._json({"error": "AI is off (start the server without --no-ai, with Claude Code installed)."}, 503)
                q = str(body.get("question") or "").strip()
                if not q:
                    return self._json({"error": "Ask a question"}, 400)
                hist = body.get("history") if isinstance(body.get("history"), list) else []
                t0 = time.time()
                ctx, lines, recs = monitor.radio_context(hours=min(24, max(1, int(body.get("hours") or 6))))
                try:
                    out = reader.ask_radio(q, ctx, hist)
                except Exception as e:
                    return self._json({"error": f"AI couldn't answer: {e}"}, 502)
                # turn the cited times into playable lines
                by_time = {}
                for r in recs.values():
                    by_time.setdefault(f"{dt.datetime.fromtimestamp(r['time'] / 1000):%H:%M:%S}", r)
                cites = []
                for ts in (out.get("cited_times") or [])[:8]:
                    r = by_time.get(str(ts).strip()[:8])
                    if r:
                        cites.append({"time": r["time"], "talkgroup_name": r.get("talkgroup_name"),
                                      "text": r.get("text") or r.get("raw_text") or "", "audio_url": r.get("audio_url")})
                log(f"Ask: {q[:80]!r} ({time.time() - t0:.0f} s)")
                return self._json({"answer": out.get("answer", ""), "cites": cites, "seconds": round(time.time() - t0, 1),
                                   "lines": len(lines)})
            if p in ("/api/feedback/clip", "/api/feedback/alert"):
                fb = monitor.feedback
                if not fb:
                    return self._json({"error": "feedback not available"}, 404)
                try:
                    if p.endswith("/clip"):
                        corrected = body.get("corrected")
                        out = fb.record_clip(body.get("id"), body.get("audio_url"), body.get("heard"),
                                             None if corrected is None else str(corrected), by=body.get("by"),
                                             talkgroup_name=body.get("talkgroup_name"), t=body.get("time"))
                        log(f"Feedback: {out['verdict']} \"{out['heard'][:60]}\"" + ("" if out["verdict"] == "right" else f" -> \"{out['text'][:60]}\""))
                        return self._json(out)
                    a = next((x for x in monitor.alerts if x["id"] == body.get("id")), None)
                    if not a:
                        return self._json({"error": "unknown alert"}, 404)
                    out = fb.record_alert(a, body.get("verdict"), by=body.get("by"))
                    a["verdict"] = out["verdict"]
                    if out["verdict"] == "false":
                        monitor.dismiss_alert(a["id"], body.get("by"))   # also publishes the update
                    else:
                        monitor.hub.publish("alert_update", a)
                    log(f"Feedback: alert {out['terms']} marked {'REAL' if out['verdict'] == 'real' else 'FALSE ALARM'}")
                    return self._json(out)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
            if p == "/api/board/detach":
                out = monitor.board_fix(body.get("id"), body.get("audio_url"), body.get("to") or None, body.get("by"))
                return self._json(out or {"error": "couldn't find that line on that call"}, 200 if out else 404)
            if p == "/api/alert/dismiss":
                a = monitor.dismiss_alert(body.get("id"), body.get("by"))
                return self._json(a or {"error": "unknown alert"}, 200 if a else 404)
            if p == "/api/board/dismiss":
                inc = monitor.board.get(body.get("id"))
                ok = monitor.board.dismiss(body.get("id"), body.get("by"))
                if ok:
                    if inc:
                        monitor.dismiss_alerts_for_incident(inc, body.get("by"))
                    monitor._publish_board()
                return self._json({"dismissed": ok}, 200 if ok else 404)
            if p.startswith("/api/ourcalls/"):
                if not monitor.calllog:
                    return self._json({"error": "call log not available"}, 404)
                action = p.rsplit("/", 1)[1]
                try:
                    if action == "add":
                        out = monitor.add_our_call(body)
                    elif action == "mark":
                        t = body.get("t")
                        out = monitor.calllog.mark(body.get("id"), body.get("key"),
                                                   None if t in (None, "") else ("now" if t == "now" else int(t)), body.get("by"))
                        monitor.calllog.sync_from_board(monitor.board.snapshot())   # bring back auto time if a mark was removed
                        out = monitor.calllog.get(out["id"])
                    elif action == "update":
                        out = monitor.calllog.update(body.get("id"), body.get("fields") or {})
                    elif action == "delete":
                        if not self._admin_ok():
                            return self._json({"error": "admin key required"}, 403)
                        gone = monitor.calllog.get(body.get("id"))
                        out = {"deleted": monitor.calllog.delete(body.get("id")), "call": gone}
                    elif action == "acuity":
                        out = monitor.calllog.set_acuity(body.get("id"), body.get("acuity"))
                    elif action == "restore":
                        out = monitor.calllog.restore(body.get("call") or {})
                    else:
                        return self._json({"error": "not found"}, 404)
                except KeyError:
                    return self._json({"error": "unknown call"}, 404)
                except (ValueError, TypeError) as e:
                    return self._json({"error": str(e)}, 400)
                hub.publish("ourcalls", monitor.calls_payload())
                return self._json(out)
            if p == "/api/match":   # try the matcher on arbitrary text from the dashboard
                return self._json({"hits": monitor.matcher.match(body.get("text", ""))})
            self._json({"error": "not found"}, 404)

    return H


class NullPusher:
    def enabled(self):
        return False

    def send(self, *a, **k):
        pass


class _ReplayHub:
    """Event hub for a replay's private Monitor: its events reach dashboards as replay_*,
    so they never mix with the live feed or trigger the live alarm."""
    def __init__(self, hub):
        self.hub = hub

    def publish(self, event, data):
        if event != "status":
            self.hub.publish("replay_" + event, data)

    def count(self):
        return self.hub.count()


class LockedTranscriber:
    """One transcription at a time, shared by the live feed and replays."""
    def __init__(self, inner):
        self.inner, self.name, self.lock = inner, inner.name, threading.Lock()

    def transcribe(self, path, prompt):
        with self.lock:
            return self.inner.transcribe(path, prompt)


class MLXWhisper:
    """Whisper on the Mac's GPU via Apple's MLX (pip install mlx-whisper). Much faster than
    faster-whisper on Apple Silicon, which makes large models like large-v3-turbo practical."""

    def __init__(self, model_path):
        import mlx_whisper  # noqa: imported lazily so the rest works without it
        import numpy as np
        self.mx, self.path = mlx_whisper, model_path
        self.name = f"mlx-whisper GPU ({os.path.basename(os.path.normpath(model_path))})"
        log(f"Loading {self.name}...")
        mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32), path_or_hf_repo=model_path,
                               language="en", verbose=None)          # load + warm up
        log("Whisper ready.")

    def _pass(self, audio, prompt, use_prompt):
        r = self.mx.transcribe(
            audio, path_or_hf_repo=self.path, language="en", verbose=None,
            condition_on_previous_text=False,
            initial_prompt=(prompt or None) if use_prompt else None,
            temperature=(0.0, 0.2, 0.4, 0.6),
            no_speech_threshold=0.6, logprob_threshold=-1.0, compression_ratio_threshold=2.4)
        keep = []
        for seg in r.get("segments", []):
            nsp, lp, cr = seg.get("no_speech_prob", 0), seg.get("avg_logprob", 0), seg.get("compression_ratio", 1)
            if nsp > 0.6 and lp < -0.8:
                continue
            if lp < -1.3 or cr > 2.4:
                continue
            keep.append(seg)
        text = " ".join(x["text"].strip() for x in keep).strip()
        weight = sum(max(1, len(x["text"])) for x in keep)
        conf = sum(x["avg_logprob"] * max(1, len(x["text"])) for x in keep) / weight if keep else -9.0
        return text, conf

    def transcribe(self, path, prompt):
        audio = LocalWhisper._decode(path)
        if audio.size < 1600:
            return ""
        audio, plain = prepare_audio(audio)
        text, conf = self._pass(audio, prompt, use_prompt=True)
        if conf < -0.6 or not text:
            # unclear: try without the vocabulary prompt (it pulls Whisper toward prompt words on noise),
            # and on the un-enhanced audio if enhancement changed it; keep the most confident
            tries = [(audio, False)] + ([(plain, True)] if plain is not None else [])
            for a, use_prompt in tries:
                t2, c2 = self._pass(a, prompt, use_prompt=use_prompt)
                if t2 and c2 > conf + 0.05:
                    text, conf = t2, c2
                    if a is plain:
                        ENHANCE_STATS["fallback_used"] += 1
        return text


def is_mlx_model(path):
    return os.path.isdir(path) and any(os.path.exists(os.path.join(path, f))
                                       for f in ("weights.safetensors", "weights.npz"))


def default_model():
    """The big model on the Mac GPU when install.command has downloaded it, else small.en (faster-whisper)."""
    mlx = os.path.join(HERE, "models", "large-v3-turbo-mlx")
    return mlx if is_mlx_model(mlx) else "small.en"


def make_transcriber(args):
    if args.openai_key:
        return OpenAIWhisper(args.openai_key, args.openai_model)
    if args.engine == "mlx" or (args.engine == "auto" and is_mlx_model(args.model)):
        return MLXWhisper(args.model)
    return LocalWhisper(args.model)


REPLAY_TZ = "America/New_York"
MAX_REPLAY_HOURS = 6


def parse_local_ms(s):
    """'YYYY-MM-DD HH:MM' (or the dashboard's 'YYYY-MM-DDTHH:MM') in Eastern time -> epoch ms."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(REPLAY_TZ)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(dt.datetime.strptime(str(s).strip(), fmt).replace(tzinfo=tz).timestamp() * 1000)
        except ValueError:
            pass
    raise ValueError(f'bad time {s!r}; use "YYYY-MM-DD HH:MM" (Eastern), e.g. "2026-10-03 01:00"')


def fmt_local(ms):
    from zoneinfo import ZoneInfo
    return dt.datetime.fromtimestamp(ms / 1000, ZoneInfo(REPLAY_TZ)).strftime("%m-%d %H:%M:%S")


def validate_window(start_ms, end_ms):
    if end_ms <= start_ms:
        raise ValueError("end must be after start")
    if end_ms - start_ms > MAX_REPLAY_HOURS * 3600_000:
        raise ValueError(f"window is longer than {MAX_REPLAY_HOURS} hours")
    if start_ms > now_ms():
        raise ValueError("start is in the future")


class ReplayJob:
    """Pull a past window from OpenMHz and run it through transcription + matching exactly like
    live, with its own calls/alerts/context, no pushes, and logs in logs/replay-<start>-*.jsonl."""

    def __init__(self, monitor, start_ms, end_ms):
        self.monitor = monitor
        self.id = uuid.uuid4().hex[:8]
        self.start_ms, self.end_ms = start_ms, end_ms
        self.state, self.total, self.done, self.error = "fetching", 0, 0, None
        self.cancelled = False
        self.created, self.finished = now_ms(), None
        from zoneinfo import ZoneInfo
        self.tag = dt.datetime.fromtimestamp(start_ms / 1000, ZoneInfo(REPLAY_TZ)).strftime("%Y%m%d-%H%M")
        self.child = Monitor(monitor.args, monitor.config, _ReplayHub(monitor.hub), NullPusher(), monitor.fetcher)
        self.child.log_tag = self.tag

    def running(self):
        return self.finished is None

    def info(self, full=False):
        recs = list(self.child.calls)
        d = {"id": self.id, "state": self.state, "start": self.start_ms, "end": self.end_ms,
             "total": self.total, "done": self.done, "error": self.error, "tag": self.tag,
             "created": self.created, "finished": self.finished,
             "transcribed": sum(1 for r in recs if r["text"]),
             "failed": sum(1 for r in recs if r["error"]),
             "alerts": list(self.child.alerts)}
        if full:
            d["calls"] = recs
        return d

    def _publish(self):
        self.monitor.hub.publish("replay_status", self.info())

    def cancel(self):
        self.cancelled = True

    def _fetch(self):
        log(f"Replay {self.tag}: fetching calls {fmt_local(self.start_ms)} -> {fmt_local(self.end_ms)} Eastern")
        found, cursor = {}, self.end_ms
        for page in range(500):
            if self.cancelled:
                break
            batch = self.monitor._api("calls/older", {"time": cursor})
            if not batch:
                break
            times = [parse_iso_ms(c["time"]) for c in batch]
            for c, t in zip(batch, times):
                if self.start_ms <= t < self.end_ms:
                    found[c["_id"]] = c
            oldest = min(times)
            log(f"  page {page + 1}: {len(batch)} calls, back to {fmt_local(oldest)}; {len(found)} in window")
            self.total = len(found)
            self._publish()
            if oldest >= cursor or oldest < self.start_ms:
                break
            cursor = oldest
        return sorted(found.values(), key=lambda c: c["time"])

    def run(self, live_first=True):
        try:
            calls = self._fetch()
            self.total = len(calls)
            if calls and not self.cancelled:
                if self.monitor.transcriber is None:
                    self.state = "waiting for Whisper"
                    self._publish()
                while self.monitor.transcriber is None and not self.cancelled:
                    if str(self.monitor.status.get("transcriber", "")).startswith("FAILED"):
                        raise RuntimeError("transcriber failed to load")
                    time.sleep(1)
                self.child.transcriber = self.monitor.transcriber
                self.child.feedback = self.monitor.feedback
                self.state = "transcribing"
                self._publish()
            for c in calls:
                if self.cancelled:
                    break
                # live traffic always goes first
                while live_first and self.monitor.work.qsize() and not self.cancelled:
                    time.sleep(0.5)
                try:
                    self.child.process(c, backfill=False)
                except Exception:
                    log("Replay processing error:\n" + traceback.format_exc())
                self.done += 1
                if self.done % 10 == 0:
                    log(f"Replay {self.tag}: {self.done}/{self.total}")
                self._publish()
            self.state = "cancelled" if self.cancelled else "done"
        except Exception as e:
            self.state, self.error = "error", f"{type(e).__name__}: {e}"
            log(f"Replay {self.tag} failed: {self.error}")
        self.finished = now_ms()
        self._publish()
        log(f"Replay {self.tag}: {self.state} ({self.done}/{self.total} calls, {len(self.child.alerts)} alerts)")

    def summary_text(self):
        i = self.info()
        alerts = i["alerts"]
        out = ["", "================ REPLAY SUMMARY ================",
               f"Window:      {fmt_local(self.start_ms)} -> {fmt_local(self.end_ms)} Eastern",
               f"Status:      {self.state}" + (f" ({self.error})" if self.error else ""),
               f"Calls:       {self.total}  (transcribed {i['transcribed']}, "
               f"silent/empty {self.done - i['transcribed'] - i['failed']}, failed {i['failed']})",
               f"Alerts:      {len(alerts)}  (high {sum(a['level'] == 'high' for a in alerts)}, "
               f"medium {sum(a['level'] == 'medium' for a in alerts)})"]
        for a in alerts:
            out.append(f"  {fmt_local(a['time'])}  {a['level'].upper():6}  [{a['talkgroup_name']}]  "
                       f"{', '.join(h['term'] for h in a['hits'])}")
            out.append(f'      "{a["text"]}"')
        out.append(f"Logs:        logs/replay-{self.tag}-calls.jsonl"
                   + (f", logs/replay-{self.tag}-alerts.jsonl" if alerts else ""))
        return "\n".join(out)


def run_replay(args, config, fetcher):
    """Command-line replay (--replay-from/--replay-to): same engine as the dashboard's Replay."""
    try:
        start_ms, end_ms = parse_local_ms(args.replay_from), parse_local_ms(args.replay_to)
        validate_window(start_ms, end_ms)
    except ValueError as e:
        raise SystemExit(f"Replay: {e}")
    monitor = Monitor(args, config, Hub(), NullPusher(), fetcher)
    monitor.transcriber = make_transcriber(args)
    log("Audio enhancement: " + ("on (tone removal + noise reduction on noisy clips; --no-enhance to turn off)" if ENHANCE else "off"))
    job = ReplayJob(monitor, start_ms, end_ms)
    job.run(live_first=False)
    print(job.summary_text(), flush=True)


class QuietHTTPServer(ThreadingHTTPServer):
    """Don't print tracebacks when a browser/phone simply drops its connection."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    ap = argparse.ArgumentParser(description="Crimson EMS dispatch alert server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from other devices on the network")
    ap.add_argument("--model", default=os.environ.get("WHISPER_MODEL") or default_model(),
                    help="model name or folder: small.en, models/small.en, or an MLX folder like models/large-v3-turbo-mlx")
    ap.add_argument("--engine", choices=["auto", "faster", "mlx"], default=os.environ.get("WHISPER_ENGINE", "auto"),
                    help="auto = mlx-whisper (Mac GPU) when --model is an MLX folder, else faster-whisper")
    ap.add_argument("--openai-key", default=os.environ.get("OPENAI_API_KEY"),
                    help="use OpenAI's transcription API instead of local Whisper")
    ap.add_argument("--openai-model", default=os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1"))
    ap.add_argument("--poll", type=float, default=4.0, help="seconds between OpenMHz polls")
    ap.add_argument("--demo-port", type=int, default=int(os.environ.get("DEMO_PORT", 8081)),
                    help="port for the demo dashboard that replays recorded radio as if live (0 = off)")
    ap.add_argument("--rewind", type=int, default=int(os.environ.get("REWIND_MIN", 10)),
                    help="at power-on, run the last N minutes of radio through everything, alerts included (0 = off)")
    ap.add_argument("--backfill", type=int, default=10, help="with --rewind 0: show the last N calls at startup (never alarms)")
    ap.add_argument("--api-base", default=os.environ.get("OPENMHZ_API", "https://api.openmhz.com"))
    ap.add_argument("--no-stealth", action="store_true", default=os.environ.get("NO_STEALTH") == "1",
                    help="fetch OpenMHz with plain urllib instead of a stealth Playwright browser")
    ap.add_argument("--browser-channel", default=os.environ.get("BROWSER_CHANNEL"),
                    help='use an installed browser instead of Playwright\'s Chromium: "chrome" or "msedge"')
    ap.add_argument("--headful", action="store_true", default=os.environ.get("HEADFUL") == "1",
                    help="show the Chromium window (useful for debugging a blocked/challenged session)")
    ap.add_argument("--warmup-url", default=os.environ.get("OPENMHZ_WARMUP_URL"),
                    help="page the stealth browser opens first (default: https://openmhz.com/system/<system>)")
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC"),
                    help="ntfy topic for phone push (pick something long and random)")
    ap.add_argument("--ntfy-server", default=os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
    ap.add_argument("--groupme-bot-id", default=os.environ.get("GROUPME_BOT_ID"))
    ap.add_argument("--webhook-url", default=os.environ.get("WEBHOOK_URL"),
                    help="POST JSON {text, alert} here (Slack/Discord-compatible 'text')")
    ap.add_argument("--push-level", choices=["high", "medium"], default=os.environ.get("PUSH_LEVEL", "high"),
                    help="minimum alert level sent to push channels (dashboard always shows both)")
    ap.add_argument("--public-url", default=os.environ.get("PUBLIC_URL"),
                    help="dashboard URL to include in push notifications")
    ap.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY"),
                    help="if set, required to edit keywords or send test pushes from the dashboard")
    ap.add_argument("--replay-from", metavar='"YYYY-MM-DD HH:MM"',
                    help="replay past calls from this Eastern time (no pushes, no dashboard), then exit")
    ap.add_argument("--replay-to", metavar='"YYYY-MM-DD HH:MM"', help="end of the replay window (Eastern)")
    ap.add_argument("--no-ai", action="store_true", default=os.environ.get("NO_AI") == "1",
                    help="don't use Claude (`claude -p`) to read dispatches")
    ap.add_argument("--ai-model", default=os.environ.get("AI_MODEL", "haiku"),
                    help="Claude model for reading dispatches: haiku (fast, default), sonnet (smarter, slower)")
    ap.add_argument("--ai-max-per-hour", type=int, default=int(os.environ.get("AI_MAX_PER_HOUR", 60)),
                    help="safety cap on Claude calls per hour")
    ap.add_argument("--claude-path", default=os.environ.get("CLAUDE_PATH"),
                    help="path to the claude command if it isn't on PATH")
    ap.add_argument("--stock-model", action="store_true",
                    help="ignore a fine-tuned model from train_whisper.py and use --model as given")
    ap.add_argument("--no-enhance", action="store_true", default=os.environ.get("NO_ENHANCE") == "1",
                    help="turn off the audio enhancement layer (tone removal + noise reduction) before Whisper")
    ap.add_argument("--no-filter", action="store_true",
                    help="show every transcript, even ones that don't look like radio traffic")
    ap.add_argument("--check", action="store_true",
                    help="just fetch and print the last 3 calls from OpenMHz, then exit (no Whisper, no server)")
    args = ap.parse_args()
    active = os.path.join(HERE, "models", "active-model.txt")      # written by train_whisper.py when a fine-tune wins
    if not args.stock_model and os.path.exists(active) and not args.openai_key:
        try:
            with open(active) as f:
                p = f.read().strip()
            p = p if os.path.isabs(p) else os.path.join(HERE, p)
            if is_mlx_model(p):
                log(f"Using the fine-tuned Whisper model {os.path.relpath(p, HERE)} (--stock-model to use {args.model})")
                args.model = p
        except OSError:
            pass
    global ENHANCE
    ENHANCE = not args.no_enhance

    config = Config(CONFIG_PATH)
    if args.no_stealth:
        fetcher = UrllibFetcher()
    else:
        warmup = args.warmup_url or f"https://openmhz.com/system/{config.get()['system']}"
        fetcher = StealthFetcher(warmup, headless=not args.headful, channel=args.browser_channel)

    if args.check:
        tgs = config.get().get("talkgroups", {})
        url = (f"{args.api_base.rstrip('/')}/{config.get()['system']}/calls?"
               + urllib.parse.urlencode({"filter-type": "talkgroup", "filter-code": ",".join(tgs)}))
        log("Fetching", url)
        try:
            print_recent_calls(fetcher.get_json(url).get("calls", []), tgs)
        except Exception as e:
            log("FAILED:", f"{type(e).__name__}: {e}")
            raise SystemExit(1)
        return
    if args.replay_from or args.replay_to:
        if not (args.replay_from and args.replay_to):
            raise SystemExit("--replay-from and --replay-to must be used together")
        return run_replay(args, config, fetcher)

    hub = Hub()
    pusher = Pusher(args)
    monitor = Monitor(args, config, hub, pusher, fetcher)
    monitor.calllog = CallLog(os.path.join(LOG_DIR, "our-calls.json"))
    monitor.feedback = FeedbackStore(LOG_DIR, download=monitor._download, log=log)
    monitor.geo = Geocoder(os.path.join(LOG_DIR, "geocache.json"), log=log)
    monitor.load_board_fixes()
    if monitor.feedback.fixes:
        log(f"Learned fixes from crew corrections: {len(monitor.feedback.fixes)}")
    if not args.no_ai:
        monitor.ai = ClaudeReader(config.get, monitor.apply_ai, log, model=args.ai_model,
                                  claude_path=args.claude_path, max_per_hour=args.ai_max_per_hour,
                                  others_getter=monitor.other_calls_for_ai, nearby_getter=monitor.nearby_for_ai)
        log(f"AI dispatch reader: {monitor.ai.status} (model {args.ai_model}, `{monitor.ai.claude or 'claude'}`)")
    monitor.start()

    if args.demo_port:
        try:
            demo_hub = Hub()
            dm = Monitor(args, config, demo_hub, NullPusher(), DemoFetcher())
            dm.log_tag = "demo"
            dm.calllog = CallLog(os.path.join(LOG_DIR, "demo-our-calls.json"))
            dm.feedback = monitor.feedback
            dm.geo = monitor.geo
            dm.asker = monitor.ai               # "Ask the radio" in the demo uses the live AI reader
            dm.status.update(transcriber="demo: recorded transcripts", fetcher="demo")
            dm.demo = DemoController(dm, LOG_DIR, log, live=monitor)
            dsrv = QuietHTTPServer((args.host, args.demo_port), make_handler(dm, config, demo_hub, args, demo=dm.demo))
            threading.Thread(target=dsrv.serve_forever, name="demo-http", daemon=True).start()
            log(f"Demo dashboard (recorded radio played back as if live): http://localhost:{args.demo_port}")
        except OSError as e:
            log(f"Demo dashboard not started ({e}); use --demo-port to pick another port")

    srv = QuietHTTPServer((args.host, args.port), make_handler(monitor, config, hub, args))
    log(f"Dashboard: http://localhost:{args.port}")
    log("OpenMHz fetcher: " + ("urllib" if args.no_stealth else "Playwright + stealth"))
    log("Monitoring talkgroups: " + ", ".join(f"{k} {v}" for k, v in config.get()["talkgroups"].items()))
    if not pusher.enabled():
        log("No push channel configured (--ntfy-topic / --groupme-bot-id / --webhook-url): dashboard alerts only.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
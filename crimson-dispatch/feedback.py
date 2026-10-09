"""
Right / Wrong feedback from the crew, and what the system learns from it.

* Radio lines: "Right" or a corrected transcript. The clip's audio is saved next to the corrected text
  (logs/training/audio/<id>.mp3 + logs/training/clips.jsonl): that is the training set for
  train_whisper.py.
* Alerts: "Real call" or "False alarm" (logs/training/alerts.jsonl), to measure and tune the matcher.
* Learned fixes: when the crew corrects the same mishearing at least twice ("Fort Haverhouse" ->
  "Pforzheimer House"), it is fixed automatically in new transcripts from then on.

Only public radio audio and its transcript are stored here; nothing about patients.
"""
import difflib
import json
import os
import re
import shutil
import threading
import time

_TOK = re.compile(r"[A-Za-z0-9']+")
# never auto-replace these on their own: too common to be a reliable mishearing
COMMON = set("""a an the and or but of to in on at for from by with is are was were be it this that we you i he she they
me my our your his her its yes no ok okay so just now go one two three four five six seven eight nine ten""".split())


def _now_ms():
    return int(time.time() * 1000)


def words(s):
    return _TOK.findall((s or "").lower())


def wer(ref, hyp):
    r, h = words(ref), words(hyp)
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[-1] / len(r)


def phrase_fixes(heard, corrected):
    """Word-level replacements between what Whisper heard and the correction: [("fort haverhouse", "pforzheimer house")]."""
    a, b_orig = words(heard), _TOK.findall(corrected or "")
    b = [w.lower() for w in b_orig]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "replace" and i2 - i1 <= 4 and j2 - j1 <= 4:
            out.append((" ".join(a[i1:i2]), " ".join(b_orig[j1:j2])))   # keep the crew's capitalisation
    return out


class FeedbackStore:
    def __init__(self, log_dir, download=None, log=print, min_repeats=2):
        self.dir = os.path.join(log_dir, "training")
        self.audio_dir = os.path.join(self.dir, "audio")
        os.makedirs(self.audio_dir, exist_ok=True)
        self.clips_path = os.path.join(self.dir, "clips.jsonl")
        self.alerts_path = os.path.join(self.dir, "alerts.jsonl")
        self.download, self.log, self.min_repeats = download, log, min_repeats
        self.lock = threading.Lock()
        self.clips, self.alerts = self._load(self.clips_path), self._load(self.alerts_path)
        self._rebuild_fixes()

    # ---------------------------------------------------------------- storage
    @staticmethod
    def _load(path):
        out = {}
        try:
            with open(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        out[r["key"]] = r                     # latest verdict for a clip / alert wins
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
        return out

    def _append(self, path, rec):
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    @staticmethod
    def clip_key(rec_id, audio_url):
        k = (rec_id or "").replace("demo-", "") or os.path.basename(audio_url or "")
        return re.sub(r"[^A-Za-z0-9_.-]", "_", k)[:80]

    # ---------------------------------------------------------------- radio lines
    def record_clip(self, rec_id, audio_url, heard, corrected=None, by=None, talkgroup_name=None, t=None):
        """corrected=None means 'Right' (the transcript was correct)."""
        key = self.clip_key(rec_id, audio_url)
        if not key:
            raise ValueError("unknown clip")
        heard = (heard or "").strip()[:1000]
        corrected = None if corrected is None else str(corrected).strip()[:1000]
        ok = corrected is None or words(corrected) == words(heard)
        rec = {"key": key, "id": rec_id, "audio_url": audio_url, "heard": heard,
               "text": heard if ok else corrected, "verdict": "right" if ok else "wrong",
               "wer": 0.0 if ok else round(wer(corrected, heard), 3), "by": (by or "")[:40],
               "talkgroup_name": talkgroup_name, "time": t, "at": _now_ms(),
               "audio": self.clips.get(key, {}).get("audio")}
        with self.lock:
            self.clips[key] = rec
            self._append(self.clips_path, rec)
            self._rebuild_fixes()
        if not rec["audio"] and audio_url and self.download:
            threading.Thread(target=self._save_audio, args=(key, audio_url), daemon=True).start()
        return rec

    def _save_audio(self, key, url):
        try:
            tmp = self.download(url)
            ext = os.path.splitext(tmp)[1] or ".mp3"
            dst = os.path.join(self.audio_dir, key + ext)
            shutil.move(tmp, dst)
            with self.lock:
                rec = dict(self.clips.get(key) or {}, audio=os.path.relpath(dst, self.dir))
                if rec.get("key"):
                    self.clips[key] = rec
                    self._append(self.clips_path, rec)
        except Exception as e:
            self.log(f"Feedback: couldn't save the audio for {key}: {type(e).__name__}: {e}")

    # ---------------------------------------------------------------- alerts
    def record_alert(self, alert, verdict, by=None):
        if verdict not in ("real", "false"):
            raise ValueError("verdict must be real or false")
        rec = {"key": alert["id"], "verdict": verdict, "by": (by or "")[:40], "at": _now_ms(),
               "level": alert.get("level"), "terms": [h["term"] for h in alert.get("hits", [])],
               "text": alert.get("text"), "time": alert.get("time"), "talkgroup_name": alert.get("talkgroup_name")}
        with self.lock:
            self.alerts[rec["key"]] = rec
            self._append(self.alerts_path, rec)
        return rec

    def alert_verdict(self, alert_id):
        r = self.alerts.get(alert_id)
        return r and r["verdict"]

    def clip_verdicts(self):
        with self.lock:
            return {k: {"verdict": r["verdict"], "text": r["text"]} for k, r in self.clips.items()}

    # ---------------------------------------------------------------- learned fixes
    def _rebuild_fixes(self):
        votes, confirmed = {}, set()
        for r in self.clips.values():
            if r["verdict"] == "right":
                # a phrase that was heard correctly elsewhere must never be "fixed"
                w = words(r["heard"])
                for n in (1, 2, 3):
                    confirmed.update(" ".join(w[i:i + n]) for i in range(len(w) - n + 1))
                continue
            for wrong, right in phrase_fixes(r["heard"], r["text"]):
                votes.setdefault(wrong, {}).setdefault(right, 0)
                votes[wrong][right] += 1
        fixes = {}
        for wrong, opts in votes.items():
            right, n = max(opts.items(), key=lambda kv: kv[1])
            if n < self.min_repeats or n < 0.67 * sum(opts.values()):
                continue
            if wrong in confirmed or wrong in COMMON or len(wrong) < 4:
                continue
            fixes[wrong] = right
        self.fixes = fixes
        self._fix_re = (re.compile(r"\b(" + "|".join(re.escape(w).replace(r"\ ", r"[\s,.-]+")
                                                      for w in sorted(fixes, key=len, reverse=True)) + r")\b", re.I)
                        if fixes else None)

    def apply_fixes(self, text):
        """Returns (fixed_text, [(wrong, right), ...])."""
        if not text or not self._fix_re:
            return text, []
        used = []

        def sub(m):
            key = " ".join(words(m.group(0)))
            right = self.fixes.get(key)
            if not right:
                return m.group(0)
            used.append((key, right))
            return right[:1].upper() + right[1:] if m.group(0)[:1].isupper() else right
        return self._fix_re.sub(sub, text), used

    # ---------------------------------------------------------------- stats
    def stats(self, prompt=""):
        with self.lock:
            clips, alerts = list(self.clips.values()), list(self.alerts.values())
        n = len(clips)
        right = sum(1 for c in clips if c["verdict"] == "right")
        ref_words = sum(len(words(c["text"])) for c in clips) or 1
        errs = sum(c["wer"] * len(words(c["text"])) for c in clips)
        prompt_words = set(words(prompt))
        new_words = {}
        for c in clips:
            if c["verdict"] == "wrong":
                for w in set(words(c["text"])) - set(words(c["heard"])) - prompt_words - COMMON:
                    if not w.isdigit() and len(w) > 2:
                        new_words[w] = new_words.get(w, 0) + 1
        real = sum(1 for a in alerts if a["verdict"] == "real")
        false_terms = {}
        for a in alerts:
            if a["verdict"] == "false":
                for t in a["terms"]:
                    false_terms[t] = false_terms.get(t, 0) + 1
        return {
            "clips_reviewed": n, "clips_right": right, "clips_wrong": n - right,
            "word_accuracy": round(100 * (1 - errs / ref_words), 1) if n else None,
            "audio_saved": sum(1 for c in clips if c.get("audio")),
            "training_ready": sum(1 for c in clips if c.get("audio") and c["text"]),
            "alerts_reviewed": len(alerts), "alerts_real": real, "alerts_false": len(alerts) - real,
            "false_alarm_terms": sorted(false_terms.items(), key=lambda kv: -kv[1])[:10],
            "learned_fixes": sorted(self.fixes.items()),
            "suggested_words": [w for w, _ in sorted(new_words.items(), key=lambda kv: -kv[1])[:15]],
        }

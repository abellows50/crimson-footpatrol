# Crimson EMS · Dispatch Watch

This app listens to Cambridge Pro EMS and Cambridge Fire radio traffic on OpenMHz and transcribes each transmission. It alerts the crew when a call names a Harvard location or says something like "HUPD… private response."

```
OpenMHz (Cambridge CoMIRS site) ──► server.py ──► Whisper transcript ──► matcher
                                            │                              │
                                            ├──► dashboard (any browser on the network): alarm + flash + notification
                                            └──► phone push: ntfy app / GroupMe bot / webhook
```

## Quick start (Mac)

1. Put this folder on the station computer. It needs to stay on and connected. Install [Google Chrome](https://www.google.com/chrome/) if it isn't there (OpenMHz only lets a real browser in).
2. Double-click **install.command** (if macOS blocks it: right-click → Open). It checks Python, sets up the app, and on Apple Silicon downloads the Whisper large-v3-turbo model (~1.6 GB). Run it again any time something seems broken, or after moving the folder.
3. Double-click **start.command** and open **http://localhost:8080**. Click **Turn on alarm sound**, then press **Test alarm**.
4. Crew on the same Wi-Fi can open the `http://<ip>:8080` address printed in the terminal.
5. In the dashboard: **Settings → Map & base** to set where your crew waits (for distances and walk times).

Getting better over time: press **✓** or **Fix** on radio lines and **Real call / False alarm** on alerts. Repeated corrections are fixed automatically, and once there are ~200 reviewed clips, quit the server and double-click **train.command** to train Whisper on your own radio (see **More → Accuracy & training**).

## Phone alerts (recommended)

Install the free **ntfy** app on each phone. Copy `settings.env.example` to `settings.env`, set `NTFY_TOPIC` to a long random name, and subscribe to that topic in the app. High alerts arrive as priority-5 (urgent) notifications that include the transcript and a "Play audio" button. Turn on ntfy's "Override Do Not Disturb" option for the topic if you want them to ring through.

You can also set `GROUPME_BOT_ID` to post to your crew GroupMe, or `WEBHOOK_URL` for Slack or Discord.

## What triggers an alert

| Level | Examples | What happens |
|---|---|---|
| **High** | HUPD, "private response", Harvard police, Yard dorms (Weld, Canaday…), all 12 Houses + Dudley, the Quad, Widener, Annenberg, Science Center, Smith Center/HUHS, Law School, `1200–1600 Mass Ave`, `1–120 Mt Auburn St`, `1033 Mass Ave` | Loud repeating alarm until someone acks, red flash, browser notification, phone push |
| **Medium** | "Harvard" alone (could be Harvard St), Harvard Square, Plympton/Linden/Holyoke/DeWolfe/Oxford/Quincy St… | Amber "possible" card plus a chime. Not pushed to phones unless `PUSH_LEVEL=medium` |

* Matching is fuzzy, so "Elliot House", "Canady Hall" and "private responce" still hit. "H U P D" gets collapsed to HUPD.
* A location split across two keyups ("…respond to Kirkland" / "House for a fall") is caught by combining consecutive transmissions on the same talkgroup.
* Follow-up transmissions about the same place within 3 minutes join the open alert instead of re-alarming.
* **Acknowledge** shows everyone who's taking the call.

Edit everything (terms, aliases, levels, address ranges, talkgroups) under **Settings** in the dashboard, or in `config.json`. Use **Try the matcher** to check how a sentence would be scored.

## Talkgroups monitored (OpenMHz system `cambridge`)

1227 Pro EMS · 1239 Pro Intercept · 1051 CFD Ch 1 (Dispatch) · 1059 CFD EMS · 1053 CFD Citywide · 1229 / 1231 Cambridge EMS. Crimson EMS's own TG 1201 is left off on purpose so your own traffic doesn't set off alarms.

## Options

`python3 server.py --help` lists every option. The common ones:

* `--model` picks the speech model: by default `models/large-v3-turbo-mlx` (Mac GPU) when install.command has downloaded it, else `small.en`.
* `--openai-key …` sends clips to OpenAI's transcription API instead of transcribing locally.
* `--admin-key …` requires a password to edit rules from the dashboard.
* `--port 8080`, `--poll 4` (seconds between polls).

Logs go to `logs/` (never committed to git: they can contain patient notes): `logs/calls-YYYY-MM-DD.jsonl` (every transcript) and `logs/alerts.jsonl`. They're handy for tuning the keyword list after a few shifts.

## Limits (please read)

* **Delay:** OpenMHz posts each transmission after it ends, so expect roughly 10–60 s behind live radio, plus a few seconds to transcribe.
* **Feed availability:** The Cambridge OpenMHz feed is run by a volunteer and can go offline. The dashboard warns when it can't reach OpenMHz, and when no traffic has arrived for 45+ minutes. If the feed is unreachable for 10 minutes, it also sends a "feed DOWN" push.
* **Transcription errors:** Radio audio is rough and Whisper will miss things. Treat this as an extra heads-up, never as your only way of getting dispatched.
* **Usage:** This uses OpenMHz's public listener API at a polite rate (one request every 4 s). Please don't crank `--poll` down, and consider supporting OpenMHz.

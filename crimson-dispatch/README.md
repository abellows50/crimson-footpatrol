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

**Analyze** (More ▾ → Analyze, or http://localhost:8080/analyze) runs any stretch of Pro EMS radio (TG 1227) through the pipeline in the background and charts dispatches and Harvard calls by hour of day and day of week (OpenMHz only keeps audio for a limited time, so very old days come back empty). Clips already transcribed are reused, and results build up across runs. Tick **Only near Harvard College** to count only the Harvard calls whose place (Harvard address, street corner or named Harvard building) is within a 10-minute walk of Adams House (Russell Hall, 26 Plympton St; same walking estimate as the dashboard), e.g. leaving out the Quad, the Law School and Allston; calls whose place can't be pinned down are left out and counted on the tile. Each call shows its walk time from Adams House, and the dispatch list can be filtered the same way. Tick **Leave out calls from HUHS** to rule out calls at Harvard University Health Services (Smith Campus Center, 75 Mount Auburn St) from the Harvard counts. **Chief complaints** breaks the calls down by what they were for (as heard on the radio, including Whisper's usual garbles such as "safe person" for sick person), for all dispatches and for Harvard calls; click a complaint to list its dispatches, and both lists can be sorted by chief complaint. **Where Harvard calls happen** puts the Harvard calls on an OpenStreetMap map (one circle per place, sized by the number of calls; click a circle for its calls and their radio), with the 10-minute walk around Adams House drawn in; it follows the same checkboxes and chart filters (it needs internet access for the map tiles). Days with no dispatches at all are gaps in the feed, not quiet days, so they're left out of the counts, averages and charts (the "Time analyzed" tile says how many). When the rules change, Analyze re-scores everything it has analyzed by itself; **Your rulings vs. the current rules** lists any of your Harvard / Not Harvard rulings the current rules disagree with, with the reason, and **Follow the new rules** shows today's verdict for that call right away, even before the re-score finishes.

Getting better over time: press **✓** or **Fix** on radio lines and **Real call / False alarm** on alerts. Repeated corrections are fixed automatically, and once there are ~200 reviewed clips, quit the server and double-click **train.command** to train Whisper on your own radio (see **More → Accuracy & training**).

## Phone alerts (recommended)

Install the free **ntfy** app on each phone. Copy `settings.env.example` to `settings.env`, set `NTFY_TOPIC` to a long random name, and subscribe to that topic in the app. High alerts arrive as priority-5 (urgent) notifications that include the transcript and a "Play audio" button. Turn on ntfy's "Override Do Not Disturb" option for the topic if you want them to ring through.

You can also set `GROUPME_BOT_ID` to post to your crew GroupMe, or `WEBHOOK_URL` for Slack or Discord.

## What triggers an alert

A call is a **Harvard call** when it says so, or when its address is somewhere HUPD would normally respond:

| Level | When | What happens |
|---|---|---|
| **High** | **Always:** "HUPD" in any spelling Whisper uses (HWPD, "3HU, PD", Harvard police / PD) or Harvard's own private ambulance contract ("private response / private call for Harvard", "it'll be a private for Harvard", "respond for Harvard"). **By place:** a named Harvard place (Houses and their halls, Yard dorms, libraries, Smith Center/HUHS, Law School, final clubs, the Crimson, Lampoon, Hillel…); a **Harvard address** (any Harvard-owned building or land in Cambridge, rentals included; student-organization buildings); the **sidewalk in front of one** on the same side of the street (even a number the city doesn't list); an **intersection with a Harvard corner** (Mass Ave & Holyoke St) | Loud repeating alarm until someone acks, red flash, browser notification, phone push |
| **Medium** (possible) | A Harvard street, "Harvard" or "Harvard Square" with no address that can be pinned down; Harvard-leased space in a shared building (1280 Mass Ave), partly Harvard-owned buildings, a shop inside a club building, a lot line between Harvard and someone else; "private response" with no address; a very short line that only repeats Whisper's vocabulary hint ("HUPD private response." on static) | Amber "possible" card plus a chime. Not pushed to phones unless `PUSH_LEVEL=medium` |

Not Harvard (no alert): other owners' buildings, even in the Square or across the street from the Yard (the Coop at 1400 Mass Ave, CVS at 6 JFK St), the Harvard MBTA headhouse (1401 Mass Ave, in the middle of the street), Harvard Street / Harvard Vanguard / Harvard Pilgrim, and anything in another town ("into Arlington", "in Watertown", "mutual aid Belmont": 75 Mount Auburn St in Watertown isn't HUHS). The Allston/Boston campus isn't matched by address, but HUPD / "private response for Harvard" there still alarm.

* **Where "Harvard address" comes from:** the City of Cambridge's property records (owner of every parcel), Harvard's own campus map (building names, its addresses, and space it leases), street centerlines and parcel outlines for "in front of" and corners, and a hand-checked list of student-organization buildings (`data/harvard-curated.json`). `python3 parcels.py --update` rebuilds `data/cambridge-addresses.json` from the live sources in about a minute; do it every month or two (clubs move, Harvard buys buildings). `python3 parcels.py "12 Holyoke St" "Mass Ave and Holyoke St"` shows what it decides and why.
* **Alarm filters** (Settings › Harvard places, off until ticked): *No loud alarm for calls from HUHS* and *Loud alarm only for Harvard calls within a 10-min walk of Adams House*. They apply to the live dashboard, replays, the demo and phone pushes; a filtered Harvard call still shows as an amber "possible" card with the reason, never silently dropped.
* **When it's wrong:** **Settings › Harvard places** is the crew's own list (Harvard / Possible / Not Harvard; addresses like `15-31 Mill St`, corners like `Mill St & Plympton St`, or names). It beats the records. On the Analyze page, an opened dispatch has **Always Harvard / Never Harvard** buttons that add its address to that list.
* Addresses are read the way dispatch says them: twice, often digit by digit ("13, 12, Mass Ave" = 1312; "140, 1" = 1401; "number 8, Harvard Yard"), and the two readings vote. Corrections ("sorry, 1337 Cambridge Street") win.
* Matching is fuzzy, so "Elliot House", "Candidate Hall" (Canaday), "Carrier House" (Currier) and "75 on Auburn Street" still hit.
* A location split across two keyups ("…respond to Kirkland" / "House for a fall") is caught by combining consecutive transmissions on the same talkgroup.
* Follow-up transmissions about the same place within 3 minutes join the open alert instead of re-alarming.
* **Acknowledge** shows everyone who's taking the call.

Edit the rules (terms, aliases, levels, the crew's Harvard places, talkgroups) under **Settings** in the dashboard, or in `config.json`. Use **Try the matcher** to see how a sentence would be scored and why (which address it heard and what the records say). `python3 harvard_check.py` checks the rules against labeled examples and every Harvard address (`--rulings` also compares them with the crew's rulings on the Analyze page).

## Talkgroups monitored (OpenMHz system `cambridge`)

1227 Pro EMS · 1239 Pro Intercept · 1051 CFD Ch 1 (Dispatch) · 1059 CFD EMS · 1053 CFD Citywide · 1229 / 1231 Cambridge EMS. Crimson EMS's own TG 1201 is left off on purpose so your own traffic doesn't set off alarms.

## Options

`python3 server.py --help` lists every option. The common ones:

* `--model` picks the speech model: by default `models/large-v3-turbo-mlx` (Mac GPU) when install.command has downloaded it, else `small.en`.
* `--openai-key …` sends clips to OpenAI's transcription API instead of transcribing locally.
* `--admin-key …` requires a password to edit rules from the dashboard.
* `--port 8080`, `--poll 4` (seconds between polls).
* `--long` (or `--long 5`) turns on long mode. Only transmissions longer than 3 s (or the number you give) are downloaded and transcribed, and short keyups are skipped. Alerts, the call board and the AI work as usual on what's left. The trade-off: a Harvard location said in a short keyup on its own will be missed.

Logs go to `logs/` (never committed to git: they can contain patient notes): `logs/calls-YYYY-MM-DD.jsonl` (every transcript) and `logs/alerts.jsonl`. They're handy for tuning the keyword list after a few shifts.

## Limits (please read)

* **Delay:** OpenMHz posts each transmission after it ends, so expect roughly 10–60 s behind live radio, plus a few seconds to transcribe.
* **Feed availability:** The Cambridge OpenMHz feed is run by a volunteer and can go offline. The dashboard warns when it can't reach OpenMHz, and when no traffic has arrived for 45+ minutes. If the feed is unreachable for 10 minutes, it also sends a "feed DOWN" push.
* **Transcription errors:** Radio audio is rough and Whisper will miss things. Treat this as an extra heads-up, never as your only way of getting dispatched.
* **Usage:** This uses OpenMHz's public listener API at a polite rate (one request every 4 s). Please don't crank `--poll` down, and consider supporting OpenMHz.

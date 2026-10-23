"""
Does the matcher call the right things Harvard?   python3 harvard_check.py [--all] [--rulings]

1. Labeled cases (synthetic sentences in dispatch style; no real patient details in this file).
2. Every Harvard address in data/cambridge-addresses.json, said the way dispatch says it, must alarm (recall), and
   a sample of nearby non-Harvard addresses must not (false positives).   (--all: every address, not a sample)
3. --rulings: agreement with the crew's Harvard / not-Harvard rulings from the Analyze page (logs/, local only).

Exit code 1 if any labeled case or recall check fails.
"""
import json
import os
import random
import sys
import time

import matcher
import parcels

HERE = os.path.dirname(os.path.abspath(__file__))

# (text, expected): "high" = Harvard call (alarm), "possible" = amber, "none" = no alert
CASES = [
    # --- automatic: HUPD / Harvard's private calls, whatever the address
    ("Paramedic 6, private response, HUPD requesting, 60 Plympton Street", "high"),
    ("Ambulance 9, H U P D on scene requesting an ambulance, Brattle Street and JFK Street", "high"),
    ("Paramedic 15, private call, 3HU, PD, respond, 70 North Harvard Street", "high"),
    ("Harvard police are with the party at the Science Center plaza", "high"),
    ("Paramedic 8, private response for Harvard, 19 Elliott Street, main entrance", "high"),
    ("Ambulance 17, private call from Harvard, 93 Banks Street, near the MCR room", "high"),
    ("Alright, received when you are, it'll be a private for Harvard, 7 St. Paul Street", "high"),
    ("Paramedic 8, respond for Harvard in Boston, 53 Harvard Way, room 704", "high"),
    ("Paramedic 20, private response for Harvard and Allston, 25 Harvard Way, parking lot", "high"),
    ("21, private response, into Watertown, 480 Pleasant Street, abdominal pain", "none"),
    ("Ambulance 10, private response, Youville House, 1573 Cambridge Street, unit 140, a fall", "none"),
    ("Paramedic 9, private response, 240 Albany Street, inside, vomiting", "none"),
    ("Paramedic 16, private response, Cambridge Rehab, 8 Dana Street, room 218", "none"),
    ("Thank you, it's a private response, address to follow", "possible"),
    ("1611 Cambridge Street, apartment 245, this is Harvard Vanguard Medical, sick person", "none"),
    # --- Harvard buildings by address (city property records)
    ("Paramedic 6, sign on and respond, 60 Plympton Street, second floor, intoxicated", "high"),
    ("Ambulance 4, respond to 64 Linnaean Street, the M entry, fifth floor", "high"),
    ("Paramedic 7, take number 8, Harvard Yard, back pain", "high"),
    ("Ambulance 16, 1 Oxford Street, the library, well-being check", "high"),
    ("Paramedic 10, 20 Quincy Street, intoxicated", "high"),
    ("Paramedic 7, 60 Garden Street, possible dislocated shoulder", "high"),
    ("Paramedic 11, 79 JFK Street, the lobby, syncope", "high"),
    ("Ambulance 2, 32 Mill Street, the medical assist", "high"),
    # Harvard-owned rentals count
    ("Ambulance 12, sign on and respond, J.P. Licks, 13, 12, Mass Ave, sick person", "high"),
    ("Paramedic 7, sign and respond, 34, Mount Auburn Street, unit 203", "high"),
    ("Paramedic 6, 75 Mount Auburn Street, third floor, psych eval", "high"),
    ("Paramedic 9, 75 on Auburn Street, the HHS, leg laceration", "high"),
    ("1350 Mass Ave, first floor, sick person", "high"),
    # --- not Harvard: other owners' buildings, even in the Square or across from the Yard
    ("Sign on and respond, Harvard MBTA, 1401 Mass Ave, northbound platform, chest pain", "none"),
    ("Paramedic 7, 1400 Mass Ave, the Harvard Coop, man down", "none"),
    ("Ambulance 6, sign on and respond, 6 JFK Street, the CVS, sick person", "none"),
    # Harvard leases offices upstairs in this shared building: amber, not a loud alarm
    ("Paramedic 8, 1238 Mass Ave, across from the Hong Kong, intoxicated", "possible"),
    ("20, sign and respond, 2 Mount Auburn Street, unit 509, lifeline no response", "none"),
    ("Ambulance 14, 984 Memorial Drive, unit 604, a fall", "none"),
    ("5, sign on and respond, Sheraton Commander Hotel, 16 Garden Street, a fall", "none"),
    ("Ambulance 6, 255 Garden Street, unit 12, psych", "none"),
    ("Paramedic 1, sign on and respond, 360 Mount Auburn Street, unit 311, back pain", "none"),
    ("Paramedic 6, can I have you head over to the area of 292 Harvard Street", "none"),
    ("6, sign and respond, 287 Harvard Street, unit 62, seizure", "none"),
    ("Mount Auburn Hospital, 330 Mount Auburn Street, the ER, transfer", "none"),
    # other towns: Cambridge addresses don't apply
    ("Paramedic 9 into Arlington, 1398 Mass Ave, urgent care, difficulty breathing", "none"),
    ("In Watertown, number 75, Mount Auburn Street, abdominal pain, call on with Watertown Fire", "none"),
    ("Mutual aid Belmont, 115 Mill Street, McLean, difficulty breathing", "none"),
    ("Paramedic 6 into Arlington, 13, 95, Mass Ave, Sunrise Assisted Living, room 229", "none"),
    # --- in front of a Harvard building, on the same side of the street (numbers the city doesn't list)
    ("Paramedic 4, sign on and respond, 1385 Mass Ave, outside the Yard, a fall", "high"),
    # --- intersections with a Harvard corner
    ("Holyoke Street and Mass Ave, a sick person, 77-year-old male", "high"),
    ("Ambulance 17, sign on and respond, Brattle Street and JFK Street, sick person", "none"),
    # --- vague: possible
    ("Ambulance 4, sign on and respond to Mount Auburn Street, for a fall", "possible"),
    ("When you enter the city, head towards Harvard Square and cover", "possible"),
    # --- named Harvard places
    ("Pro 2 respond to Canaday Hall in the Yard for a 19 year old intox", "high"),
    ("Paramedic 6, Quincy House, Stone Hall, second floor, male party", "high"),
    ("Engine 1 responding with an ALS, Winthrop Standish House, for the medical assist", "high"),
    # --- things that are not Harvard places at all
    ("All of them go ahead", "none"),
    ("Paramedic 12, can you start your way up to the Emerson, going home to Harvard", "none"),
    ("Now, Harvard Hospital, stand 6, going to Good Shepherd in Newton", "none"),
    ("Paramedic 11, sign and respond, Lesley University Hall, 1815 Mass Ave, 4th floor, seizure", "none"),
    ("Headed up to Emerson Hospital, going to Boston Hills Road in Sudbury, chair car call", "none"),
    # --- found by the adversarial review (Oct 2026)
    # Mather House: 10 Cowperthwaite St isn't on the city's address list; Harvard lines both sides of the street
    ("Number 10, Copperweight Street. 10 Copperweight Street, apartment 407.", "high"),
    ("7, we're responding 10, Copperway Street.", "high"),
    ("Pro 5, respond to 7 Cowperthwaite Street, 7 Cowperthwaite Street.", "high"),
    # everyday words that are not Harvard halls ("severe fall" / Sever Hall, "home, all set" / Holmes Hall)
    ("Ambulance 4, going home. All set. Available.", "none"),
    ("Paramedic 4, sign on and respond, 255 Garden Street, unit 12, 88-year-old female, severe fall, head strike.", "none"),
    ("Paramedic 9, 360 Mount Auburn Street, child call, breathing problem.", "none"),
    ("Engine 6 to Fire Alarm, investigated, nothing found, all units are clear.", "none"),
    ("Ambulance 4, transfer, going to Wellesley Hills, 100 Washington Street, chair car.", "none"),
    # street names that are everyday words: a hospital floor, a direction, a walker
    ("Paramedic 1, take Spaulding of Cambridge, 4 South. Spaulding of Cambridge, 4 South, diabetic issue.", "none"),
    ("Paramedic 4, car versus pedestrian, vehicle traveling south on JFK Street.", "possible"),
    ("Engine 1, respond for a dumpster fire, dumpster at Mount Auburn Street behind the restaurant.", "possible"),
    # town words that are Harvard Houses or Cambridge streets; real other towns
    ("Paramedic 6, sign on and respond to Quincy and Mass Ave, bicyclist down", "high"),
    ("Paramedic 6, respond to the Quincy dining hall, 58 Plympton Street, 58 Plympton Street, choking", "high"),
    ("Paramedic 8, 75 Mount Auburn Street, fourth floor, they want her to go to Mass General in Boston", "high"),
    ("Paramedic 8, 1 Oxford Street, 1 Oxford Street, engine 1 and the Somerville ambulance, fall", "high"),
    ("Ambulance 9, 12 Bow Street, Charlestown, female, intoxicated.", "none"),
    ("Paramedic 9, Watertown's 75 Mount Auburn Street, abdominal pain.", "none"),
    # a call sign before a corner; read-outs that need another reading of the number
    ("6, Mass Ave and Holyoke Street, man down", "high"),
    ("21, 60 Plympton Street, intox", "high"),
    ("Paramedic 6, 12-14 Plympton Street, sick person", "high"),
    ("Paramedic 7, fifteen eighty-five Mass Ave, the lobby, fall", "high"),
    ("Paramedic 6, Mass Ave, corner of Holyoke Street, a fall", "high"),
    # police wording that isn't HUPD; private-call wording that is or isn't Harvard's
    ("Paramedic 6, sign on and respond, Prospect at Harvard, police on scene, assault victim.", "possible"),
    ("Paramedic 4, sign on and respond, 1394 Mass Ave, a male who PD has in custody, laceration.", "none"),
    ("Ambulance 2, ah, PD is on scene, 360 Mount Auburn Street, unit 3, psych.", "none"),
    ("Paramedic 4, 2 Mount Auburn Street, unit 509, a Harvard University student, intoxicated.", "none"),
    ("Paramedic 6, private response, it's gonna be for Harvard, Brattle Street, a fall", "high"),
    ("Ambulance 12, respond in Boston for Harvard, Harvard Athletics, 65 North Harvard Street, gate 8", "high"),
    ("Paramedic 12, going home to Harvard. Private call, Youville House, 1573 Cambridge Street, unit 140.", "none"),
    ("Ambulance 10, private call, Linnaean Street, the assisted living, 85-year-old, fall.", "possible"),
    # a Harvard name used as a landmark or a street
    ("Paramedic 5, sign on and respond, 1394 Mass Ave, across from Harvard Yard, male, seizure.", "possible"),
    ("Paramedic 6, number 25 Holworthy, 25 Holworthy Street, unit 1, a fall.", "none"),
    ("Ambulance 5, 74 Boylston Street, 74 Boylston Street, unknown medical.", "none"),
    # property records: assessor addresses, a partly-owned building, a corner fixed by the intersection layer
    ("Pro 5, respond to 47 Concord Avenue, 4, 7 Concord Avenue.", "high"),
    ("Paramedic 6, 0 Arrow Street, zero Arrow Street, the theater, fall", "high"),
    ("Pro 5, respond to 393 Mount Auburn Street for a fall.", "none"),
    ("Pro 5, respond to 12 Palmer Street for an unresponsive party.", "possible"),
    ("Pro 5, respond to Beacon Street and Kirkland Street.", "none"),
    ("Pro 5, respond to 41 Quincy Street.", "none"),
    ("17, sign and respond, 2, Peabody, Karis, number 2, Peabody, Karis, a fall", "high"),
]

STYLES = [
    "Paramedic 6, sign on and respond, {n} {s}, for a fall",
    "Ambulance 12, {n}, {s}. {n}, {s}, 19-year-old male, intoxicated",
    "Pro medic 21, take number {n} {s}, third floor",
]


def level_of(hits):
    if any(h["level"] == "high" for h in hits):
        return "high"
    return "possible" if hits else "none"


def spoken(street):
    """'mount auburn street' -> 'Mount Auburn Street', 'massachusetts avenue' -> 'Mass Ave'."""
    if street.startswith("massachusetts avenue"):
        return "Mass Ave"
    return " ".join(w.upper() if w == "jfk" else w.capitalize() for w in street.split())


def main():
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    # the rules and the records, without the crew's own Always / Never Harvard list (which is meant to override them)
    m = matcher.Matcher(dict(cfg, crew_places=[], not_harvard_places=[]))
    fails = 0
    print("== labeled cases")
    for text, want in CASES:
        got = level_of(m.match(text))
        if got != want:
            fails += 1
            print(f"  FAIL want {want:8} got {got:8} | {text}\n       hits: {[(h['term'], h['level'], h.get('note', '')) for h in m.match(text)]}")
    print(f"  {len(CASES) - fails}/{len(CASES)} correct")

    loc = parcels.load()
    if loc.ok:
        harv = [k for k, v in loc.table.items() if v[0] in parcels.HARVARD]
        nonh = [k for k, v in loc.table.items() if v[0] == "N"]
        rnd = random.Random(7)
        sample = harv if "--all" in sys.argv else rnd.sample(harv, min(300, len(harv)))
        print(f"== recall: {len(sample)} of {len(harv)} Harvard addresses, said {len(STYLES)} ways")
        missed = []
        t0 = time.time()
        for key in sample:
            n, st = key.split(" ", 1)
            for style in STYLES:
                text = style.format(n=n, s=spoken(st))
                if level_of(m.match(text)) != "high":
                    missed.append((key, text))
                    break
        dt = time.time() - t0
        for key, text in missed[:40]:
            print(f"  MISSED {key:34} | {text}")
        print(f"  {len(sample) - len(missed)}/{len(sample)} alarm ({dt / max(1, len(sample) * len(STYLES)) * 1000:.1f} ms per call)")
        fails += len(missed)
        # false positives: non-Harvard addresses on streets that also have Harvard addresses
        hstreets = {k.split(" ", 1)[1] for k in harv}
        near = [k for k in nonh if k.split(" ", 1)[1] in hstreets]
        sample = near if "--all" in sys.argv else rnd.sample(near, min(400, len(near)))
        fps = []
        for key in sample:
            n, st = key.split(" ", 1)
            text = STYLES[0].format(n=n, s=spoken(st))
            if level_of(m.match(text)) == "high":
                fps.append((key, [h["term"] for h in m.match(text) if h["level"] == "high"]))
        for key, terms in fps[:40]:
            print(f"  FALSE ALARM {key:34} | {terms}")
        print(f"== false positives: {len(fps)}/{len(sample)} non-Harvard addresses on Harvard streets alarm"
              " (some may be legitimately in front of Harvard property)")
    else:
        print("(no data/cambridge-addresses.json: run python3 parcels.py --update)")

    if "--rulings" in sys.argv:
        rulings_report(m)
    sys.exit(1 if fails else 0)


def rulings_report(m):
    """How today's rules compare with the crew's rulings on the Analyze page (local logs only). Rulings follow a call
    by its radio clips, as on the Analyze page, so they still match after a re-score gives the alerts new ids."""
    logs = os.path.join(HERE, "logs")
    try:
        dq = json.load(open(os.path.join(logs, "analyze-not-harvard.json")))
        alerts = json.load(open(os.path.join(logs, "analyze-alerts.json")))
    except (OSError, ValueError):
        print("(no Analyze rulings in logs/)")
        return
    by_url, by_id = {}, {}
    for k in ("alerts", "dispatches", "harvard_alerts", "harvard_dispatches"):
        kind = "confirmed" if k.startswith("harvard_") else "removed"
        for rid, r in dq.get(k, {}).items():
            e = (r.get("at", 0), kind)
            by_id[rid] = max(by_id.get(rid, e), e)
            for u in r.get("urls") or []:
                by_url[u] = max(by_url.get(u, e), e)
    rows = {"removed": {"high": 0, "possible": 0, "none": 0}, "confirmed": {"high": 0, "possible": 0, "none": 0}}
    disagree = []
    for aid, a in alerts.items():
        txs = a.get("transmissions") or [{"text": a.get("text"), "audio_url": a.get("audio_url")}]
        cands = [by_url[x["audio_url"]] for x in txs if x.get("audio_url") in by_url] + \
            ([by_id[aid]] if aid in by_id else [])
        if not cands:
            continue
        kind = max(cands)[1]
        # each transmission on its own and with the one before it, as the pipeline sees them
        texts = [x.get("text") or "" for x in txs]
        texts += [f"{p}. {q}" for p, q in zip(texts, texts[1:])] + [a.get("context") or ""]
        levels = [level_of(m.match(t)) for t in texts if t]
        lv = "high" if "high" in levels else "possible" if "possible" in levels else "none"
        rows[kind][lv] += 1
        if (kind == "removed" and lv == "high") or (kind == "confirmed" and lv != "high"):
            terms = sorted({h["term"] for t in texts if t for h in m.match(t) if h["level"] == "high"})
            disagree.append((kind, lv, terms, " ".join((a.get("context") or a.get("text") or "").split())[:110]))
    print("== crew rulings vs today's rules (matched by radio clip)")
    for kind, c in rows.items():
        print(f"  {kind:10} high={c['high']:4} possible={c['possible']:4} none={c['none']:4}")
    for kind, lv, terms, text in disagree:
        print(f"  you said {'NOT ' if kind == 'removed' else ''}Harvard, rules say {lv}: {terms} | {text}")


if __name__ == "__main__":
    main()

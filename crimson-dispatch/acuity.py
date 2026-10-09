"""
High vs low acuity from dispatch wording.

High acuity = likely time-critical / ALS: arrest, unresponsive, breathing problems, chest pain, seizure,
stroke, overdose, allergic reaction, major trauma, OB, suicidal, diabetic emergency, or an ALS dispatch.
Low acuity = fall, lift assist / unable to ambulate, intox (alert), sick person, pain, minor injury, psych,
assault (no weapon), transfer, BLS.
The strongest reason wins; returns (None, []) when nothing in the text says either way.
"""
import re

HIGH = [
    (r"cardiac arrest|\bcpr\b|not breathing|no pulse|pulseless|\bcode (blue|99)\b|\bdoa\b|agonal|\bin arrest\b", "arrest / not breathing"),
    (r"unconscious|unresponsive|not responding to|passed out|loss of consciousness|altered mental|\bams\b|not alert|syncop|fainted|\bunconsc", "unresponsive / altered"),
    (r"difficulty breathing|trouble breathing|short(ness)? of breath|\bsob\b|respiratory distress|can'?t breathe|choking|physical breathing|breathing difficult|difficulty breath", "breathing problem"),
    (r"chest pain|chest pains|chest pressure|\bcardiac\b|heart attack|\bstemi\b", "chest pain / cardiac"),
    (r"\bseiz\w*|\bseizing\b", "seizure"),
    (r"\bstroke\b|\bcva\b|facial droop|slurred speech|one[- ]sided weakness", "stroke symptoms"),
    (r"overdose|\bover dose\b|\bod\b|narcan|opioid|heroin|fentanyl", "overdose"),
    (r"anaphyla\w*|allergic reaction|epi ?pen|throat (is )?swelling", "allergic reaction"),
    (r"severe bleeding|uncontrolled bleeding|hemorrhag\w*|arterial|\bstab(bed|bing|s)?\b|gunshot|\bshot\b|\bgsw\b|impaled|amputat\w*|major trauma|rollover|ejected|pedestrian struck|ped struck|struck by (a )?(car|vehicle|bus|truck|train)", "major trauma"),
    (r"pregnan\w*|\bin labor\b|childbirth|imminent delivery|\bob\b|giving birth", "OB / pregnancy"),
    (r"suicid\w*|self[- ]harm|attempted suicide|suicide attempt|\bjumper\b", "suicidal / self-harm"),
    (r"hypoglyc\w*|low blood sugar|diabetic\w*.{0,40}(unresponsive|altered|not alert|confus)", "diabetic emergency"),
    (r"\bals\b|paramedic intercept|priority (1|one)\b|lights and sirens|hot response", "ALS dispatch"),
]
LOW = [
    (r"lift assist|unable to (ambulate|get up|walk)|can'?t get up|help (getting|get) up", "lift assist"),
    (r"\bfall\b|\bfell\b|\bfallen\b|slip(ped)? and fall|\bfall(ing)? down\b", "fall"),
    (r"intox\w*|\betoh\b|e\.?t\.?o\.?h|\be\.t\.h\b|alcohol|drunk", "intoxication"),
    (r"sick person|sick per|superperson|super person|general illness|not feeling well|nausea|vomit\w*|dizz\w*|\bweakness\b|\bflu\b|fever", "sick person"),
    (r"abdominal pain|stomach pain|back pain|\bpain\b|painful|headache", "pain"),
    (r"\bminor\b|laceration|\bcut\b|sprain|ankle|wrist|injur\w*|bleeding", "minor injury"),
    (r"\bpsych\w*|emotional|anxiety|panic attack|section 12", "psych"),
    (r"\bassault\w*|\bfight\b|stage for police|station for police", "assault / police staging"),
    (r"\btransfer\b|interfacility|discharge", "transfer"),
    (r"medical alarm|life ?line|\bbls\b|priority (3|three)\b", "BLS dispatch"),
]
# complaint labels the board produces from the same text
LABEL_HIGH = {"Cardiac arrest", "Chest pain", "Difficulty breathing", "Unconscious / syncope", "Seizure", "Stroke",
              "Overdose", "Allergic reaction"}
LABEL_LOW = {"Fall", "Fall / head strike", "Intoxication (ETOH)", "Sick / dizziness", "Abdominal pain", "Psych",
             "Bleeding / laceration", "Assault / trauma", "Transfer", "Pain"}
_H = [(re.compile(r, re.I), why) for r, why in HIGH]
_L = [(re.compile(r, re.I), why) for r, why in LOW]


def classify(text):
    """-> ('high' | 'low' | None, [reasons])"""
    t = text or ""
    hi = [why for rx, why in _H if rx.search(t)]
    if hi:
        return "high", hi[:3]
    lo = [why for rx, why in _L if rx.search(t)]
    if lo:
        return "low", lo[:3]
    return None, []


def classify_incident(texts, units=(), complaint=None):
    """Acuity for a whole call: explicit wording first, then the complaint label, then who was sent."""
    a, why = classify(" ".join(texts))
    if a:
        return a, why
    if complaint in LABEL_HIGH:
        return "high", [complaint.lower()]
    if complaint in LABEL_LOW:
        return "low", [complaint.lower()]
    us = list(units)
    if any(u.startswith(("Paramedic ", "ALS ")) for u in us):
        return "high", ["paramedic / ALS unit sent"]
    if any(u.startswith(("BLS ",)) for u in us):
        return "low", ["BLS unit sent"]
    return None, []

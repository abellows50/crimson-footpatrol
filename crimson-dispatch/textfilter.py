"""
Reject transcripts that don't sound like radio traffic.

Whisper invents short phrases on mic clicks, alert tones and static ("Crazy.", "Beep, beep, beep.",
"Don't kill us, Luna.", "Thank you for inviting me."). Real Cambridge Fire / Pro EMS traffic uses a
small vocabulary: unit names, numbers, status words, streets, places and medical terms. A short
transcript has to contain some of that vocabulary, and not much else, to be shown as text.
Rejected transcripts are kept (as `raw_text`) but shown as "unclear" and ignored by alerts and the board.
"""
import re

# Words that carry radio meaning. Streets, places and rule terms from config.json are added at runtime.
RADIO_WORDS = """
pro paramedic paramedics medic medics ambulance ambulances engine engines ladder truck squad rescue car deputy chief
division battalion tower als bls base dispatch dispatcher fire alarm alarms police pd hupd mit cpd state trooper
unit units company companies crew command incident
responding respond responded response answering answer answered received receive copy copies copied clear cleared
clearing available service quarters location scene transporting transport transported transports en route enroute
off on onscene hospital mount mt auburn general emerson mass cambridge brigham beth israel bi childrens tufts mgh cha
whidden spaulding lahey somerville watertown belmont arlington concord weston waltham lexington newton boston
brookline lincoln medford everett winchester wellesley needham bedford
cancel cancelled canceled disregard stand down staging stage standby standing affirmative negative roger
ahead sign signed channel operational op ops patient patients party male female year years old yo minutes minute
min mile miles point medical assist assistance fall fell chest pain breathing difficulty unconscious unresponsive
intox intoxicated etoh alcohol transfer pickup priority cardiac arrest lift seizure stroke overdose od narcan allergic
reaction diabetic sick person bleeding laceration injury injured head strike assault psych mva mvc motor vehicle
accident crash pedestrian struck smoke odor gas leak co detector water elevator activation box working structure
street st avenue ave road rd drive dr place pl square sq court ct way lane ln terrace parkway park highway turnpike
building floor apartment apt room hall house dorm yard library center school station church
harvard university campus private hupd huhs quad radcliffe kirkland adams eliot dunster lowell leverett winthrop mather
quincy cabot currier pforzheimer pfoho weld canaday thayer wigglesworth hollis stoughton holworthy grays matthews
mower straus lionel memorial science annenberg widener lamont houghton
one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen
nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand zero
alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo
sierra tango uniform victor whiskey xray yankee zulu
destination eta er ed emergency ville transport transports bound relay notify advise advised request requesting requested need needs needed update updated check checking
headed heading returning return back inside outside front rear side corner intersection between near
crossing cross lot garage entrance exit lobby stairs stairwell basement roof
male female juvenile adult elderly conscious alert breathing vomiting dizzy dizziness weakness abdominal
ems ems1 cfd pfd mfd tones tone page paged paging
""".split()

# Ordinary function words: they don't count for or against a transcript.
FILLER = set("""
a an the and or but of to in on at for from by with into onto over under up down out off is are was were be been
being am it its it's this that these those there here we we're we'll we've us our you you're your yours i i'm i'll
i've me my he she they them their his her him what where when who how why which if then than so just now also
all any some can could would should will shall may might do does did done have has had get got go going gonna
let let's please thanks thank right alright all right yeah yep yup ok okay good great oh yes no sir maam ma'am
go see well sure hello hi hey bye fine perfect
""".split())

# Whole transcripts that are known Whisper inventions on noise
JUNK_RE = re.compile(
    r"^\W*(beep\W*)+$|thank(s| you) for (watching|listening|inviting|having|coming|joining)|"
    r"subscribe|like and share|see you (next time|in the next)|transcri(bed|ption) by|"
    r"^\W*(music|applause|laughter|silence|inaudible|static|noise|bleep|blank|\.\.\.)\W*$|"
    r"^\W*[!?.,]+\W*$", re.I)

_TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:\.\d+)?", re.I)


class TextFilter:
    def __init__(self, config=None):
        self.words = set(RADIO_WORDS)
        if config:
            self.add_config(config)

    def add_config(self, config):
        blobs = [config.get("whisper_prompt", "")]
        for r in config.get("rules", []):
            blobs.append(r.get("term", ""))
            blobs.extend(r.get("aliases", []))
        for rg in config.get("address_ranges", []):
            blobs.extend([rg.get("street", ""), rg.get("label", "")])
            blobs.extend(rg.get("aliases", []))
        blobs.extend(config.get("talkgroups", {}).values())
        blobs.extend(config.get("extra_radio_words", []))
        for b in blobs:
            for w in _TOKEN.findall(b or ""):
                w = w.lower()
                if len(w) > 1 and w not in FILLER:
                    self.words.add(w)

    def learn_streets(self, texts):
        """Add street names seen in real traffic ('26 Gurney Street' -> 'gurney')."""
        rx = re.compile(r"\b\d{1,5}[\s,]+((?:[A-Z][a-z]+\s+){1,2})(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Place|"
                        r"Court|Way|Lane|Terrace|Park|Square)\b")
        for t in texts:
            for m in rx.finditer(t or ""):
                for w in m.group(1).split():
                    if w.lower() not in FILLER:
                        self.words.add(w.lower())

    def check(self, text, clip_len=None):
        """Return (ok, reason). ok=False means: hide as unclear."""
        t = (text or "").strip()
        if not t:
            return True, None
        if JUNK_RE.search(t):
            return False, "noise"
        toks = [x.lower() for x in _TOKEN.findall(t)]
        if not toks:
            return False, "noise"
        known = sum(1 for w in toks if w[0].isdigit() or w in self.words)
        filler = sum(1 for w in toks if w in FILLER and w not in self.words)
        unknown = len(toks) - known - filler
        n = len(toks)
        if n <= 3:
            # very short: must be mostly radio words ("Clear.", "On location.", "17.", "Paramedic 9, Pro.")
            if known == 0 or unknown > known:
                return False, "not radio traffic"
        elif n <= 8:
            if known == 0 or unknown > 2 * known + 1:
                return False, "not radio traffic"
        else:
            # long: real speech; only drop if it has essentially nothing radio-related
            if known <= 1 and unknown > 0.6 * n:
                return False, "not radio traffic"
        return True, None

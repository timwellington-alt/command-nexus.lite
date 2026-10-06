"""
Nickname mapping table for cross-system name matching.

Used by the reconciliation job and any remaining lookup code.
Bidirectional: "becky" -> "rebecca" and "rebecca" -> {"becky", "becca"}.
"""

# Nickname → formal name
NICKNAMES: dict[str, str] = {
    "mike": "michael", "kenny": "kenneth", "ken": "kenneth",
    "randy": "randall", "dave": "david", "bob": "robert",
    "bobby": "robert", "bobbi": "bobbie", "bobbie": "bobbi",
    "becky": "rebecca", "becca": "rebecca", "andi": "andrea",
    "andy": "andrew", "bill": "william", "billy": "william",
    "jim": "james", "jimmy": "james", "joe": "joseph",
    "joey": "joseph", "tom": "thomas", "tommy": "thomas",
    "dan": "daniel", "danny": "daniel", "dick": "richard",
    "rick": "richard", "rich": "richard", "rob": "robert",
    "robby": "robert", "steve": "steven", "steph": "stephanie",
    "tony": "anthony", "chris": "christopher", "ed": "edward",
    "ted": "theodore", "teddy": "theodore", "pat": "patricia",
    "patty": "patricia", "kathy": "katherine", "kate": "katherine",
    "katie": "katherine", "cathy": "catherine", "liz": "elizabeth",
    "beth": "elizabeth", "betty": "elizabeth", "sue": "susan",
    "suzy": "susan", "sam": "samuel", "sammy": "samuel",
    "nick": "nicholas", "zack": "zachary", "zach": "zachary",
    "matt": "matthew", "matty": "matthew", "charlie": "charles",
    "chuck": "charles", "jack": "john", "johnny": "john",
    "jerry": "gerald", "larry": "lawrence", "lenny": "leonard",
    "len": "leonard", "barb": "barbara", "libby": "elizabeth",
    "debbie": "deborah", "deb": "deborah", "jenny": "jennifer",
    "pam": "pamela", "cindy": "cynthia", "mandy": "amanda",
    "sandy": "sandra", "peggy": "margaret", "maggie": "margaret",
    "meg": "margaret", "vicky": "victoria", "tina": "christina",
    "alex": "alexander", "al": "albert", "wally": "walter",
    "walt": "walter", "ray": "raymond", "ron": "ronald",
    "ronny": "ronald", "don": "donald", "donny": "donald",
    "doug": "douglas", "greg": "gregory", "jeff": "jeffrey",
    "ben": "benjamin", "fred": "frederick", "frank": "francis",
    "hank": "henry", "harry": "harold", "art": "arthur",
    "amy": "amelia", "wes": "wesley", "buck": "ronald",
    "tammi": "tammy", "jeannie": "jeanne", "jeanne": "jeannie",
    "jamey": "james", "traci": "tracy", "tracy": "traci",
    # Additional nicknames discovered during staff matching audits
    "gabby": "gabrielle", "gabi": "gabrielle", "gabe": "gabriel",
    "abby": "abigail", "abbi": "abigail", "abbie": "abigail",
    "eddie": "edward",
    "natty": "natalie", "nat": "natalie",
    "lizzy": "elizabeth", "lizzie": "elizabeth", "eliza": "elizabeth",
    "jenn": "jennifer", "jenni": "jennifer",
    "maddie": "madeline", "maddy": "madelyn",
    "alisha": "alison", "ali": "alison",
    "kat": "kathryn", "kit": "katherine",
    "izzy": "isabella", "bella": "isabella",
    "josh": "joshua", "nate": "nathan",
    "zac": "zachary",
    "wil": "william", "willy": "william",
    "fran": "francis", "franny": "frances",
    "kim": "kimberly", "kimmy": "kimberly",
    "jax": "jackson", "jaxon": "jackson",
    "lou": "louis", "louie": "louis",
    "cate": "catherine",
    "vince": "vincent", "vinny": "vincent",
    "theo": "theodore",
    "em": "emily", "emmy": "emily", "emmie": "emily",
    "gray": "grayson",
    "mags": "margaret",
    # Melissa family — surfaced during Missi/Missy Wolfenbarker
    # Paxton mismatch on 2026-08-06.
    "missy": "melissa", "missi": "melissa", "mel": "melissa",
    "mellie": "melissa",
    # Discovered during 2026-08-11 room-roster reconciliation audit:
    # PES rosters have a mix of formal + informal spellings the earlier
    # table missed. All bidirectional via FORMAL_TO_NICKS pass 2 below.
    "madalyn": "madelyn",              # spelling variant — Zickafoose
    "madeline": "madelyn",
    "jennie": "jennifer",              # Jennie ↔ Jennifer (Jenny already mapped)
    "rachel": "rachael", "rachael": "rachel",  # spelling variant — Irwin
}

# Formal name → set of nicknames (bidirectional).
# Two passes so siblings link through their shared formal name:
#   pass 1 — nickname ↔ formal (direct edges)
#   pass 2 — every nickname of a formal also links to every other
#            nickname of the same formal (Missi ↔ Missy through Melissa)
FORMAL_TO_NICKS: dict[str, set[str]] = {}
for _nick, _formal in NICKNAMES.items():
    FORMAL_TO_NICKS.setdefault(_formal, set()).add(_nick)
    FORMAL_TO_NICKS.setdefault(_nick, set()).add(_formal)
for _formal, _nicks in list(FORMAL_TO_NICKS.items()):
    # Skip the entries that are themselves nicknames — only expand
    # sibling sets on the formal-name key so the transitive closure
    # is limited to one hop and can't accidentally chain unrelated
    # names.
    if _formal in NICKNAMES:
        continue
    for _n in _nicks:
        FORMAL_TO_NICKS.setdefault(_n, set()).update(_nicks - {_n})


def get_nickname_variants(first_name: str) -> set[str]:
    """Return all name variants for a given first name (including the name itself)."""
    name = first_name.strip().lower().split()[0] if first_name else ""
    if not name:
        return set()
    return FORMAL_TO_NICKS.get(name, set()) | {name}

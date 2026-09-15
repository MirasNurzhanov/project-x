"""Server-side screening for user-generated text.

One function, `classify()`, and one rule for callers: run it before the row is
visible to anybody else. It is deliberately the only place any wordlist lives -
the alternative, a check inlined in whichever view someone remembered, is how a
project ends up filtering Swift captions and not story text.

What this is and is not
-----------------------
It is a **denylist for the categories App Review's 1.2 is actually about**:
sexual content involving minors, solicitation, slurs and dehumanising hate
speech, and credible threats. It is not a profanity filter. Swearing is not
objectionable content, and an app that rejects "damn" while missing a slur has
built the wrong thing and annoyed its users doing it.

Two outcomes, because one is not enough:

    BLOCK   refuse the write. For material there is no version of which is
            acceptable - a moderator reviewing it later is not a remedy,
            because publication already happened.

    FLAG    accept the write but quarantine it, so it is stored, invisible, and
            in the queue. For material that is *usually* prohibited but has
            legitimate uses - the same word can be a slur or a quotation, and a
            filter confident enough to refuse either way is wrong regularly.

Evasion
-------
`normalise()` is where most of the real work is. A denylist matched against raw
text stops nobody: the same string arrives spaced out, with digits for letters,
with a zero-width joiner in the middle, or with the vowels doubled. Normalising
first means one pattern covers all of those, which is both more effective and
far shorter than enumerating spellings.

It will not stop a determined adversary, and it is not supposed to. It stops
casual abuse before publication and routes the rest to a human, which is what
"a method for filtering objectionable content" means in practice.
"""
import re
import unicodedata
from dataclasses import dataclass, field

from django.conf import settings

from .reasons import Reason

# ─── Outcomes ─────────────────────────────────────────────────────────────────
ALLOW = 'allow'
FLAG = 'flag'
BLOCK = 'block'


@dataclass(frozen=True)
class Verdict:
    """What `classify` decided, and enough of why to act on it.

    `reason` is a `moderation.reasons.Reason`, so an automated flag files a
    report in exactly the same vocabulary a human report uses and the two sort
    into the same buckets in the queue.
    """

    action: str = ALLOW
    reason: str = ''
    #: Which rules fired. Names, never the matched text: this ends up in log
    #: lines and admin columns, and reproducing the material there would put it
    #: somewhere new rather than keeping it out.
    matched: tuple = field(default_factory=tuple)

    @property
    def blocked(self):
        return self.action == BLOCK

    @property
    def flagged(self):
        return self.action == FLAG

    @property
    def allowed(self):
        return self.action == ALLOW

    def message(self):
        """What to tell the author of blocked text.

        Deliberately vague about *which* rule fired. A precise error is a
        free oracle for tuning around the filter, and somebody posting this
        material does not need help finding the wording that gets through.
        """
        return (
            'This breaks the Approx Community Guidelines and cannot be posted. '
            'Approx has zero tolerance for objectionable content.'
        )


# ─── Normalisation ────────────────────────────────────────────────────────────
#: Characters people substitute for letters. Kept small and unambiguous - the
#: temptation is to map every lookalike in Unicode, but each mapping is also a
#: way to create a false positive out of ordinary text.
_LEET = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '8': 'b',
    '@': 'a', '$': 's', '!': 'i', '|': 'i', '+': 't',
})

#: Zero-width and directional marks, which are invisible and therefore the
#: cheapest possible evasion: one of these inside a word defeats any pattern.
_INVISIBLE = re.compile(r'[​-‏‪-‮⁠-⁤﻿]')

#: Three or more of the same letter collapse to one. "heeeeey" -> "hey".
_REPEATS = re.compile(r'(.)\1{2,}')


def normalise(text):
    """Fold text down to the form the patterns are written against.

    Returns a pair: the spaced form and the joined form. Both are needed and
    they catch different evasions - the spaced form preserves word boundaries so
    a pattern can require them, and the joined form is what catches text
    deliberately broken up with separators.
    """
    if not text:
        return '', ''
    text = _INVISIBLE.sub('', str(text))
    # Preserve scripts and their meaningful marks. Only Latin accents are
    # folded for the existing English patterns; e.g. Cyrillic й must remain й.
    text = unicodedata.normalize('NFKC', text.casefold())
    text = ''.join(
        ''.join(part for part in unicodedata.normalize('NFKD', c) if not unicodedata.combining(part))
        if 'LATIN' in unicodedata.name(c, '') else c for c in text
    )
    text = text.translate(_LEET)
    # Unicode marks outside the Latin combining range (Arabic/Indic, etc.)
    # belong to their words too; punctuation/emoji remain separators.
    text = ''.join(c if unicodedata.category(c)[0] in 'LNM' else ' ' for c in text)
    spaced = ' '.join(text.split())
    spaced = _REPEATS.sub(r'\1', spaced)
    joined = spaced.replace(' ', '')
    return spaced, joined


# ─── Rules ────────────────────────────────────────────────────────────────────
# Each rule is (name, pattern, reason). The name is what gets logged; the
# pattern is matched against the *normalised* forms above, so it should be
# written in plain lowercase letters with no punctuation.
#
# `\b` works on the spaced form. Patterns that must also survive an author
# stripping the spaces out are listed in the JOINED sets below instead, where
# boundaries do not exist and the pattern has to be specific enough not to
# match inside an innocent word. That is a real hazard - "analyse" contains a
# word people would like to filter - and it is why the joined lists are short.

#: Refuse outright. No version of this is publishable.
_BLOCK_SPACED = [
    # Sexual content involving minors. The single category with no benign
    # reading and the one App Review names explicitly.
    ('csam.explicit', r'\b(child|kid|kids|minor|minors|underage|preteen|pre teen)\s+'
                      r'(porn|pornography|nudes|nude|sex|sexual|xxx)\b', Reason.MINOR_SAFETY),
    ('csam.solicit', r'\b(loli|lolicon|shotacon|jailbait|cp\s+trade|pedo|paedo)\b',
     Reason.MINOR_SAFETY),
    ('csam.age', r'\b(1[0-4]|[3-9])\s*(yo|y o|yr|yrs|year old|years old)\s+'
                 r'(nude|nudes|sex|sexy|naked|porn)\b', Reason.MINOR_SAFETY),
    ('minor.grooming', r'\bdont\s+tell\s+(your\s+)?(mom|mum|dad|parents|mother|father)\b',
     Reason.MINOR_SAFETY),

    # Sexual solicitation for payment, which is prohibited regardless of the
    # ages involved and is the most common thing reported on a proximity app.
    ('solicit.paid', r'\b(escort|escorts|hookup for cash|sex for (money|cash)|'
                     r'paid sex|selling nudes|buy my nudes|onlyfans link)\b', Reason.NUDITY),

    # Credible threats. Requires a target pronoun, so "I want to kill this
    # playlist" does not match and "i will kill you" does.
    ('threat.direct', r'\b(i(m| am| will|ll)?\s+(going to\s+)?(kill|murder|stab|shoot|rape))\s+'
                      r'(you|u|him|her|them|your)\b', Reason.VIOLENCE),
    ('threat.address', r'\b(i know where you live|im coming to your house|'
                       r'ill find you and)\b', Reason.VIOLENCE),
]

#: Refuse outright, matched against the joined form so separators do not help.
#: Short list on purpose - see the note above about substring collisions.
_BLOCK_JOINED = [
    ('csam.joined', r'childporn|childp0rn|kiddieporn|underagesex|lolicon|jailbait',
     Reason.MINOR_SAFETY),
]

#: Publish nothing; store and quarantine for review. Material that is usually
#: prohibited but has readings a filter cannot distinguish - reporting a slur,
#: quoting one, reclaiming one.
_FLAG_SPACED = [
    ('hate.dehumanising', r'\b(subhuman|vermin|cockroaches)\b\s*'
                          r'|(\ball\s+\w+\s+should\s+(die|be killed|be gassed)\b)', Reason.HATE),
    ('hate.exterminate', r'\b(gas the|exterminate the|kill all)\s+\w+', Reason.HATE),
    ('self_harm.instruction', r'\b(kill yourself|kys|go die|hang yourself|'
                              r'you should end it)\b', Reason.SELF_HARM),
    ('harassment.doxx', r'\b(heres (her|his|their) address|posting (her|his|their) '
                        r'(address|number|workplace))\b', Reason.PRIVACY),
    ('scam.payment', r'\b(send me \$?\d+|wire (me )?(money|cash)|gift card code|'
                     r'crypto (investment|doubler))\b', Reason.SPAM),
]


def _extra(name, default=()):
    """Deployment-supplied additions, so ops can respond to an abuse wave
    without waiting for a release. Each entry is a bare regex string."""
    return tuple(getattr(settings, name, default) or default)


def _compile(rules):
    return tuple((name, re.compile(pattern), reason) for name, pattern, reason in rules)


_COMPILED_BLOCK_SPACED = _compile(_BLOCK_SPACED)
_COMPILED_BLOCK_JOINED = _compile(_BLOCK_JOINED)
_COMPILED_FLAG_SPACED = _compile(_FLAG_SPACED)


def _extra_compiled(setting_name, reason):
    return tuple(
        (f'custom.{i}', re.compile(p), reason)
        for i, p in enumerate(_extra(setting_name))
    )


def classify(text):
    """Screen one piece of user text.

    Cheap enough to call on every write: two regex passes over a string that is
    at most a few hundred characters, no I/O, no network. Blocking rules are
    evaluated before flagging ones so the stronger verdict always wins.
    """
    if not text or not str(text).strip():
        return Verdict()

    spaced, joined = normalise(text)
    if not spaced:
        return Verdict()

    block_rules = _COMPILED_BLOCK_SPACED + _extra_compiled(
        'MODERATION_EXTRA_BLOCK', Reason.OTHER)
    for name, pattern, reason in block_rules:
        if pattern.search(spaced):
            return Verdict(BLOCK, reason, (name,))

    for name, pattern, reason in _COMPILED_BLOCK_JOINED:
        if pattern.search(joined):
            return Verdict(BLOCK, reason, (name,))

    flag_rules = _COMPILED_FLAG_SPACED + _extra_compiled(
        'MODERATION_EXTRA_FLAG', Reason.OTHER)
    matched = tuple(
        name for name, pattern, _ in flag_rules if pattern.search(spaced)
    )
    if matched:
        reason = next(
            r for name, pattern, r in flag_rules if name == matched[0]
        )
        return Verdict(FLAG, reason, matched)

    return Verdict()


def classify_many(*texts):
    """Screen several fields as one unit - a caption and its place name, say.

    Returns the strongest verdict across all of them, which is the only useful
    answer: a post is publishable or it is not, and the field that failed is a
    detail the caller already knows.
    """
    worst = Verdict()
    order = {ALLOW: 0, FLAG: 1, BLOCK: 2}
    for text in texts:
        verdict = classify(text)
        if order[verdict.action] > order[worst.action]:
            worst = verdict
        if worst.blocked:
            break
    return worst

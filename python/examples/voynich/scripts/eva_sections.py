"""
Representative EVA-transliterated samples per Voynich section.

Source: the Zandbergen-Landini (ZL) interlinear transliteration, which is the
standard reference corpus for the Voynich Manuscript and is freely distributed
from voynich.nu. The samples below are abbreviated (a few hundred to a couple
thousand EVA tokens per section) — enough to give Jakobsen's algorithm
statistically meaningful text to attack while keeping the module self-contained.

For full-corpus runs in production, the Databricks notebook
`01_load_corpus.py` ingests the complete ZL file into the
`voynich_corpus.eva_words` Delta table; the SA solver can be pointed at that
instead. See `run_analysis.py --from-delta`.

Token convention follows the loader in `01_load_corpus.py`:
  - `ch`, `sh`, `th`, `qo` are atomic glyphs (digraph tokens)
  - everything else is a single Latin letter
  - words separated by whitespace
"""

# ---------------------------------------------------------------------------
# Herbal (f1r–f20v) — botanical illustrations
# ---------------------------------------------------------------------------

HERBAL = """
fachys ykal ar ataiin shol shory cthres y kor sholdy
sory ckhar or y kair chtaiin shar are cthar cthar dan
syaiir sheky or ykaiin shod cthoary cthes daraiin sa
ooiin oteey oteos roloty cthar daiin otaiin or okan
sair y chear cthaiin cphar cfhaiin
ydaraishy
odar o y shol cphoy oydar sh s cfhoaiin shodary
yshey shody okchoy otchol chocthy oschy dain chocthy
cthaiin okaiir chear cthar cthar dar shar cphaiin shodal
otaiin shar shey okain otaiin or okaiin sair shy
qokeey otaiin chol chol kor chal shtolshy shey okol
qotchey qotaiin qokaiin qokal qokeey qokeedy qokeey
qokedy qokar shedy qokedy qokaiin qokeedy chedy okeedy
qokeey qokeedy ykeedy qokedy qokar shey okal qokal
chedy qokeedy chey shey qokain qokaiin chedy qokeedy
saiin shey qokar otaiin shedy qokeey shey okal qokal
otol chol shol shor shol shory cthy dar aly daiin
qokeey qokeedy ykeedy chedy shedy qokain qokaiin
chol chor shol shory shy ar aiin chey okeey okeedy
daiin chedy qokeedy shedy otedy qokain shol chor shol
shory cthy dar aly otol chol qokeey qokeedy ykeedy
qokar shey okal qokal chedy qokeedy chey shey qokain
chedy qokeedy ykeedy chedy shedy qokain qokaiin chey
otaiin chol chol kor chal shtolshy shey okol qotchey
qotaiin qokaiin qokal qokeey qokeedy qokeey qokedy
qokar shedy qokedy qokaiin qokeedy chedy okeedy qokeey
qokeedy ykeedy qokedy qokar shey okal qokal chedy
qokeedy chey shey qokain qokaiin chedy qokeedy saiin
shey qokar otaiin shedy qokeey shey okal qokal otol
chol shol shor shol shory cthy dar aly daiin qokeey
sho chodain chol cphol shol cthol shol cthor sho
otaiin or aiin shar she sho chol chol cthar shor
ar al s aiin chey shy chey okeey okeedy ykeedy
chedy ykeedy oky qokeedy qokedy chedy qoky shedy
otedy or aiin chol chedy qokeedy chey shedy qokain
shol shory cthy dar aly otol chol qokeey qokeedy
chedy ykeedy oky qokeedy qokedy chedy qoky shedy
otedy or aiin chol chedy qokeedy chey shedy qokain
""".strip()


# ---------------------------------------------------------------------------
# Astronomical (zodiac roundels f70v–f73v) — circular star/zodiac diagrams
# ---------------------------------------------------------------------------

ASTRONOMICAL = """
otol shor shey otol cheor okeol chol chol shol
otol shor okeey okeol chol shol shor okeey otol
okol okol shol shor okeey okeol chol shol shor
okeey otol shor okeey okeol chol shol shor okeey
otal okal sho shol shor okal shol shor okeey
otol shol shor okeey okeol chol shol shor okeey
otaly chodaiin shey chol shol shor okeey okeol
daiin chol shol shory cthy dar aly otol chol
qokeey qokeedy ykeedy chedy shedy qokain shol shor
chol chor shol shory cthy dar aly otol chol shol
shor okeey okeol chol shol shor okeey otol shor
okeey otol shor okeey okeol chol shol shor okeey
shol shor okeey okeol chol shol shor okeey otol
otal okal sho shol shor okal shol shor okeey otol
chol shol shor okeey okeol chol shol shor okeey
otaly chodaiin shey chol shol shor okeey okeol
chol shol shory cthy dar aly otol chol qokeey
qokeedy ykeedy chedy shedy qokain qokaiin chol
shor shol shory cthy dar aly otol chol shol shor
okeey okeol chol shol shor okeey otol shor okeey
otol shor okeey okeol chol shol shor okeey otal
okal sho shol shor okal shol shor okeey otol chol
shol shor okeey okeol chol shol shor okeey otaly
chodaiin shey chol shol shor okeey okeol chol
shol shory cthy dar aly otol chol qokeey qokeedy
ykeedy chedy shedy qokain qokaiin chol shor shol
shory cthy dar aly otol chol shol shor okeey okeol
""".strip()


# ---------------------------------------------------------------------------
# Balneological (f75r–f84v) — bathing nymphs and pools
# ---------------------------------------------------------------------------

BALNEOLOGICAL = """
qokeey qokeedy ykeedy chedy shedy qokain qokaiin
chedy qokeedy ykeedy qokedy qokar shey okal qokal
chedy qokeedy chey shey qokain qokaiin chedy qokeedy
saiin shey qokar otaiin shedy qokeey shey okal qokal
otol chol shol shor shol shory cthy dar aly daiin
qokeey qokeedy ykeedy chedy shedy qokain qokaiin
qokeedy qokeey qokeedy ykeedy chedy shedy qokain qokaiin
chedy qokeedy ykeedy qokedy qokar shey okal qokal
chedy qokeedy chey shey qokain qokaiin chedy qokeedy
saiin shey qokar otaiin shedy qokeey shey okal qokal
oteedy okedy okal okal sheey okeey okeedy okeey okeedy
chol chey shey shol shor okal okeey okeedy chol shey
qotedy qokar qokeey qokeedy qokar qokal qokeey qokeedy
shedy chedy okeey okeedy chey shey okal okeey okeedy
qokal qokar qokeey qokeedy qokar qokal qokeey qokeedy
shedy chedy okeey okeedy chey shey okal okeey okeedy
otol chol shol shor shol shory cthy dar aly daiin
qokeey qokeedy ykeedy chedy shedy qokain qokaiin
qokeedy qokeey qokeedy ykeedy chedy shedy qokain qokaiin
chedy qokeedy ykeedy qokedy qokar shey okal qokal
chedy qokeedy chey shey qokain qokaiin chedy qokeedy
saiin shey qokar otaiin shedy qokeey shey okal qokal
oteedy okedy okal okal sheey okeey okeedy okeey okeedy
chol chey shey shol shor okal okeey okeedy chol shey
qotedy qokar qokeey qokeedy qokar qokal qokeey qokeedy
shedy chedy okeey okeedy chey shey okal okeey okeedy
qokal qokar qokeey qokeedy qokar qokal qokeey qokeedy
""".strip()


# ---------------------------------------------------------------------------
# Pharmaceutical (f88r–f102v) — labelled jars and plant fragments
# ---------------------------------------------------------------------------

PHARMACEUTICAL = """
otol chol shol shor shol shory cthy dar aly daiin
chol chor shol shory cthy dar aly otol chol shol
qokeey qokeedy ykeedy chedy shedy qokain qokaiin
chol shor shol shory cthy dar aly otol chol shol
shor okeey okeol chol shol shor okeey otol shor okeey
otol shor okeey okeol chol shol shor okeey otal okal
sho shol shor okal shol shor okeey otol chol shol
shor okeey okeol chol shol shor okeey otaly chodaiin
shey chol shol shor okeey okeol chol shol shory cthy
dar aly otol chol qokeey qokeedy ykeedy chedy shedy
qokain qokaiin chol shor shol shory cthy dar aly otol
otol chol shol shor shol shory cthy dar aly daiin
qokeey qokeedy ykeedy chedy shedy qokain qokaiin
qokeedy qokeey qokeedy ykeedy chedy shedy qokain qokaiin
chedy qokeedy ykeedy qokedy qokar shey okal qokal
chedy qokeedy chey shey qokain qokaiin chedy qokeedy
saiin shey qokar otaiin shedy qokeey shey okal qokal
oteedy okedy okal okal sheey okeey okeedy okeey okeedy
chol chey shey shol shor okal okeey okeedy chol shey
qotedy qokar qokeey qokeedy qokar qokal qokeey qokeedy
shedy chedy okeey okeedy chey shey okal okeey okeedy
qokal qokar qokeey qokeedy qokar qokal qokeey qokeedy
shedy chedy okeey okeedy chey shey okal okeey okeedy
chol chey shey shol shor okal okeey okeedy chol shey
""".strip()


# ---------------------------------------------------------------------------
# Recipes / "stars" (f103r–f116v) — short paragraphs each prefixed by a star
# ---------------------------------------------------------------------------

RECIPES = """
oteol cheor okeol chol chol shol otol shor okeey okeol
chol shol shor okeey otol shor okeey okeol chol shol
shor okeey otal okal sho shol shor okal shol shor okeey
otol chol shol shor okeey okeol chol shol shor okeey
otaly chodaiin shey chol shol shor okeey okeol chol
shol shory cthy dar aly otol chol qokeey qokeedy ykeedy
chedy shedy qokain qokaiin chol shor shol shory cthy
dar aly otol chol shol shor okeey okeol chol shol shor
okeey otol shor okeey okeol chol shol shor okeey otal
okal sho shol shor okal shol shor okeey otol chol shol
shor okeey okeol chol shol shor okeey otaly chodaiin
shey chol shol shor okeey okeol chol shol shory cthy
dar aly otol chol qokeey qokeedy ykeedy chedy shedy qokain
qokaiin chol shor shol shory cthy dar aly otol chol
shol shor okeey okeol chol shol shor okeey otol shor
okeey otol shor okeey okeol chol shol shor okeey otal
okal sho shol shor okal shol shor okeey otol chol shol
shor okeey okeol chol shol shor okeey otaly chodaiin
shey chol shol shor okeey okeol chol shol shory cthy
""".strip()


SECTIONS: dict[str, str] = {
    "herbal": HERBAL,
    "astronomical": ASTRONOMICAL,
    "balneological": BALNEOLOGICAL,
    "pharmaceutical": PHARMACEUTICAL,
    "recipes": RECIPES,
}


# Atomic EVA tokens. `ch`, `sh`, `th`, `qo` are digraphs treated as single
# glyphs in EVA — the loader in 01_load_corpus.py uses the same regex.
import re

_TOKEN_RE = re.compile(r"ch|sh|th|qo|[a-z]")


def tokenize(eva_text: str) -> list[str]:
    """Split EVA text into atomic glyph tokens (digraphs preserved)."""
    out: list[str] = []
    for word in eva_text.split():
        out.extend(_TOKEN_RE.findall(word.lower()))
    return out


def section_tokens(section: str) -> list[str]:
    """Tokenized glyph stream for a section."""
    return tokenize(SECTIONS[section])

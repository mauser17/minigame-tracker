"""
Minigame Tracker (GUI) — OCR success/fail/reward counter for the Sols RNG
summer minigame event.

- Single widened OCR region over the notification stack (bottom-right corner)
- Hardcoded detection keywords — no per-user keyword setup needed
- Requires the "Minigame" header plus the distinguishing word ("exited" vs
  "cleared") before counting anything, so background noise can't be misread
- Captured region is converted to high-contrast black/white before OCR, using
  the brightest colour channel (not luminance, which dims the BLUE reward
  text), a per-frame adaptive threshold (so a biome changing the screen
  brightness doesn't blind it) and a 2x upscale. Verified reading both
  notification types across a 0.3x-1.6x brightness sweep of real captures.
  Test Capture shows exactly that image plus a diagnostics readout
- Event detection lives in a pure, unit-testable EventDetector state machine:
  after an event is logged, the notification must be absent for several
  CONSECUTIVE reads before another event can be counted, so fade-out frames
  and one-frame OCR blips can't re-trigger the same notification
- Rewards are matched structurally: first the family ("summer random box" vs
  "boost module"), then the distinguishing word, so a word-wrapped OR
  OCR-corrupted reward line still counts correctly without garbage matching
- Detects success, then settles the reward as soon as several readings agree
  (no "reward wait" setting: a clock was the wrong tool for it)
- Start / Pause / Resume / Stop, with a full end-of-session report
- Header with a status dot (gray idle, green running, amber paused, red
  stopped) and a session timer counting ACTIVE tracking time: it stops while
  paused, continues on resume, freezes on stop and resets on the next start
- Discord: a neutral "Tracker started" embed on Start, per-event embeds (green
  success with that run's reward, red fail), an embed session summary at Stop,
  and a Test Webhook button. Every one carries the current uptime
- Dark theme from one embedded stylesheet, with Start/Pause/Stop colour-coded
  to match the status dot
- Optional inactivity alert: if no result is detected for a configurable
  number of minutes of ACTIVE tracking, one Discord message with an @ping
  (user id, role id, @everyone or @here) warns that it may be stuck
- Every event and every detector state transition is appended to
  minigame_tracker.log next to this script for diagnosing issues
- Config (region, webhook, timing) saves to minigame_tracker_config.json next
  to this script, so each user who runs it configures their own once

Packaged: MinigameTracker.exe needs no setup at all; Tesseract is inside it.
Rebuild it with `pyinstaller tracker.spec` (see README.md).

Setup (running from source):
    pip install PyQt6 mss pytesseract requests pillow
    Install Tesseract OCR (Windows): https://github.com/UB-Mannheim/tesseract/wiki
    It is found automatically in C:/Program Files/Tesseract-OCR, otherwise on PATH.

Run:    python tracker.py
Tests:  python test_tracker.py   (pure logic only; no screen, Qt or Tesseract needed)
"""

from __future__ import annotations

import sys
import json
import time
import re
import hashlib
import difflib
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from typing import Callable, List, Optional

import mss
import pytesseract
from PIL import Image, ImageChops, ImageOps
import requests

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QLineEdit, QGroupBox, QMessageBox, QFormLayout, QTextEdit, QDoubleSpinBox,
    QFrame, QGridLayout, QSizePolicy, QTabWidget
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot, QRect, QTimer, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QFont, QIcon, QImage, QPixmap, QPolygonF

def _configure_tesseract():
    """Point pytesseract at a Tesseract it can run, and say which one.

    1. The packaged exe carries its own Tesseract, unpacked at launch into the
       temporary folder PyInstaller exposes as sys._MEIPASS. Using it means a
       tester needs nothing installed, and a different Tesseract on their PC
       can never be picked up by mistake.
    2. Running the .py: the standard Windows install location.
    3. Otherwise whatever is on PATH.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle:
        exe = Path(bundle) / "tesseract" / "tesseract.exe"
        if exe.exists():
            pytesseract.pytesseract.tesseract_cmd = str(exe)
            # Tesseract finds its language data through this variable. It is
            # inherited by the tesseract.exe that pytesseract starts.
            os.environ["TESSDATA_PREFIX"] = str(exe.parent / "tessdata")
            return f"bundled ({exe})"
    installed = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if installed.exists():
        pytesseract.pytesseract.tesseract_cmd = str(installed)
        return f"installed ({installed})"
    return "from PATH"


TESSERACT_SOURCE = _configure_tesseract()


def _app_dir():
    """Where settings and the log are kept.

    For the packaged exe this is the folder the exe sits in. A one-file build
    runs from a temporary folder that is DELETED when the app closes, so
    anything saved "next to the script" there would be lost every time: the
    tester would have to pick their region and webhook again on every launch.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource(name):
    """A file shipped with the app: inside the exe when packaged, otherwise
    next to the script."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


CONFIG_PATH = _app_dir() / "minigame_tracker_config.json"
LOG_PATH = _app_dir() / "minigame_tracker.log"

log = logging.getLogger("minigame_tracker")

# Old config files may still carry "poll_interval" and "reward_wait_seconds".
# They are ignored now (see REWARD_AGREEMENT_READS) and simply travel along
# harmlessly, so a config written by an older version still loads.
DEFAULT_CONFIG = {
    "region": {"top": 700, "left": 1400, "width": 500, "height": 350},
    "webhook_url": "",
    # 0 = inactivity alerts off. Above that, minutes of active tracking with
    # no result before a single alert is sent.
    "inactivity_minutes": 0.0,
    "ping_target": "",
}

# Both notification types share this header. Requiring it present before
# evaluating anything else is what keeps background noise from being misread.
HEADER_WORD = "minigame"
FAIL_WORD = "exited"
SUCCESS_WORD = "cleared"

# ---------------------------------------------------------------------------
# Detection tuning
# ---------------------------------------------------------------------------
# After an event is logged, the region must show NO notification for this many
# consecutive poll cycles before the detector will accept a new event. A single
# empty/garbled read mid-fade (an OCR "blip") therefore can't release the guard.
#
# Live testing showed blips of "a frame or two", so 3 sits right on the edge;
# 5 gives margin. At the default 0.2s poll that's 1.0s of sustained absence.
# Raising it costs nothing in practice — real results are minutes apart — so
# err high rather than low if duplicates ever reappear.
CLEAN_READS_REQUIRED = 5
# Safety net on top of the clean-read guard: a real minigame cycle takes
# minutes, so anything classified as a new event this soon after the last one
# is the same notification being re-read. Suppressed hits are logged as
# warnings so an early release would be visible in the log.
MIN_SECONDS_BETWEEN_EVENTS = 10.0
# How the reward is settled. There is no "reward wait" setting: waiting on a
# clock was the wrong tool. A fixed wait is either too short (a real session
# set 3s and lost rewards whose popup appeared at 3.2s) or slower than needed
# (waiting 12s for a reward that was already read three times).
#
# Instead the reward is settled by AGREEMENT. A success is reported as soon as
# this many readings agree on the same reward and amount, which usually takes
# under a second, and guards against the single-frame digit misreads the font
# produces ("1" read as "7" in ~9% of readings, 43% in Snowy).
REWARD_AGREEMENT_READS = 3
# A reading only counts when the text CHANGED. The capture loop re-uses the
# previous reading while the screen is unchanged, so without this the same
# frame would "agree" with itself and prove nothing.
#
# When the popup stops changing there is no more evidence to gather, so settle
# on what we have this long after the last new reading.
REWARD_SETTLE_SECONDS = 2.0
# The popup vanished: decide on what was seen after this many clear readings.
REWARD_GONE_READS = 3
# Backstop. Nothing readable by now: report the success with the reward marked
# unread. Measured over 756 real successes, the first readable reward frame
# arrived a median 3.2s after "cleared" and as late as 10.7s.
REWARD_WAIT_MAX_SECONDS = 12.0
# How often the screen is captured. Fixed: 0.2s is fast enough to catch every
# notification and slow enough to leave the game alone.
POLL_INTERVAL_SECONDS = 0.20

# ---------------------------------------------------------------------------
# OCR preprocessing tuning
# ---------------------------------------------------------------------------
# Tesseract reads small UI text far better when it's enlarged; 2x is plenty.
OCR_UPSCALE = 2
# The threshold separating "text" from "background" is chosen PER FRAME by
# Otsu's method rather than fixed, because biomes change how bright the whole
# screen is. A fixed cutoff cannot survive that: measured against a real
# capture, a cutoff tuned to look perfect on one biome stopped detecting
# anything once the screen dimmed by only 15%, while the per-frame threshold
# still read the notification correctly with the screen at 30% brightness.
#
# The risk of an adaptive threshold is that a frame containing no notification
# gets its scenery amplified into garbage text. That is harmless here, and
# verified so: classification requires the word "minigame" PLUS "exited" or
# "cleared", which garbage does not produce.
#
# Otsu alone is not enough, because it assumes the image splits into two
# comparable populations. Over a BRIGHT background (a green biome, a light
# scene) the background dominates and Otsu lands far too low, turning 40-60%
# of the region black. That is what makes the preview look "bold": strokes
# thicken until letters merge and Tesseract reads nothing from text that is
# plainly visible. Measured on real light-background captures, Otsu chose 99
# to 118 where the text actually needed 190 to 230.
#
# So the threshold is also constrained by how much of the region it is willing
# to call text. Lettering is a small fraction of any region; 6% was the best
# performing value over a sweep of real captures at several brightnesses.
OCR_TARGET_INK_FRACTION = 0.06
# Never threshold below this — on a flat, contrast-free frame Otsu returns a
# meaningless value, and this keeps that from turning the whole region black.
OCR_MIN_THRESHOLD = 60
# Guard for degenerate frames. With the ink constraint above this should not
# normally trigger; when it does, the frame is reported as UNREADABLE rather
# than as empty, so the detector never mistakes it for "the notification is
# gone" and releases its duplicate guard early.
OCR_MAX_INK_FRACTION = 0.40
# Extra Tesseract CLI flags (e.g. "--psm 6"). Empty = defaults.
TESSERACT_CONFIG = ""

# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------
REWARD_NAMES = [
    "Normal Summer Random Box",
    "Rare Summer Random Box",
    "Mega Summer Random Box",
    "Boost Module V1",
    "Boost Module V2",
]

# Reward matching is STRUCTURAL, in two steps, rather than one search for a
# keyword anywhere in the text:
#
#   1. Establish which FAMILY the reward belongs to, from words that are long
#      enough to survive OCR: "summer"/"random"/"box" for the three boxes,
#      "boost"/"module" for the two modules.
#   2. Only then look for the word that distinguishes members of that family.
#
# Step 1 is what makes step 2 safe to do loosely. Two real failures drove this:
#   - A real capture read "Rare" as "Rore", so exact matching dropped a reward
#     the player actually received.
#   - A real capture read "You've" as "You'v i", and a bare search for "v1"
#     counted it as a Boost Module V1 on a FAIL notification.
# Requiring family context first means a corrupted qualifier can be matched
# leniently without garbage ever reaching the qualifier step at all.
BOX_CONTEXT_WORDS = ("summer", "random", "box")
MODULE_CONTEXT_WORDS = ("boost", "module")
BOX_QUALIFIERS = {
    "normal": "Normal Summer Random Box",
    "rare": "Rare Summer Random Box",
    "mega": "Mega Summer Random Box",
}
# 0.72 separates OCR corruptions of the qualifiers (measured: "rore"/"rara"/
# "mego"/"meqa" all score 0.75, "normol" 0.83) from the real words that share
# letters with them ("manga" 0.67, "cleared" vs "rare" 0.55).
QUALIFIER_CUTOFF = 0.72

# The qualifier always sits directly before "Summer" ("got 3 Normal Summer
# Random Box"). Judged in THAT position, the word can be matched far more
# loosely, because nothing else in the capture competes for it.
#
# This is the main fix for Unrecognized rewards. The game's font makes OCR read
# "Normal" as "Nonnal" / "Nonnol" / "Nonmol" / "Nonnoal" ("rm" -> "nn"/"nm",
# "a" -> "o"). Those score 0.50-0.67 against "normal", under the global 0.72
# cutoff, so across 756 real successes 206 Normal boxes were lost this way.
# In position, the best of the three just has to clearly beat the other two;
# the worst real misread ("nonnol") scores 0.50 for normal and 0.00 for rare.
POSITIONAL_CUTOFF = 0.45
POSITIONAL_MARGIN = 0.20
SUMMER_MATCH = 0.70

# The lookbehind blocks an apostrophe before the "v", so a garbled "You've"
# rendered as "You'v i" can't read as V1.
# "u" is there because real captures read "Boost Module - V1" as "- VU"
# (46 readings, 7 whole rewards lost).
MODULE_VERSION_RE = re.compile(
    r"(?<![\"'`´’])\bv\s?(?P<digit>[12lizu])\b",
    re.IGNORECASE,
)
_VERSION_FIXES = {"1": "1", "l": "1", "i": "1", "u": "1", "2": "2", "z": "2"}

_WORD_RE = re.compile(r"[a-z0-9]+")

# Quantity: "got 2 ..." (with common digit misreads tolerated), or a number
# directly in front of the reward's first word ("3 Boost Module V1").
# A real capture rendered the "1" in "got 1 Rare" as "]", hence the brackets.
QTY_AFTER_GOT_RE = re.compile(r"got\s*([0-9lIO|\]\[!]{1,3})\b", re.IGNORECASE)
QTY_BEFORE_REWARD_RE = re.compile(
    r"\b(\d{1,3})\s+(?:normal|rare|mega|boost|module)\b", re.IGNORECASE
)
_DIGIT_FIXES = str.maketrans({
    "l": "1", "I": "1", "|": "1", "]": "1", "[": "1", "!": "1", "O": "0",
})
# Most of one reward a single run can give: 4, for every reward (set by the
# player). A larger number is a misread digit, not a quantity, and counts as
# having NO readable quantity, so it can never win the vote.
#
# Across 6,236 quantity readings in real sessions, OCR only ever produced 1-4
# plus 7 (the font's "1" misread), 9 and 39. It never produced 5, 6 or 8, so
# any cap from 4 to 6 blocks every misread seen. The one hard rule: the cap
# must stay below 7, or the common 1 -> 7 misread gets counted.
MAX_REWARD_QUANTITY = 4

# Discord embed colours: the app's own palette, so Discord and the window
# read as one product. Green, red and amber mean the same thing in both.
COLOR_SUCCESS = 0x4ADE80   # cleared    (app: running, Start)
COLOR_FAIL = 0xF87171      # failed     (app: stopped, Stop)
COLOR_ALERT = 0xFBBF24     # attention  (app: paused)
COLOR_STARTED = 0x818CF8   # neutral indigo, the app's accent
COLOR_INFO = 0x94A3B8      # calm slate for the end-of-session summary

# ---------------------------------------------------------------------------
# Dark theme
# ---------------------------------------------------------------------------
PALETTE = {
    "bg": "#1e1e2e",        # window background
    "panel": "#26273a",     # group boxes, header
    "input": "#1a1b28",     # text fields sit darker than the panel they're in
    "border": "#363850",
    "text": "#e5e7eb",
    "muted": "#9ca3af",
    "green": "#4ade80",     # running / start / success
    "amber": "#fbbf24",     # paused / pause
    "red": "#f87171",       # stopped / stop / fail
    "idle": "#6b7280",      # not started yet
    "accent": "#818cf8",    # neutral actions: region, capture, webhook
}

# Applied to the MAIN WINDOW, never to QApplication. An app-wide rule painting
# QWidget backgrounds would also paint RegionSelector, the translucent
# full-screen overlay used to pick the OCR region, turning it opaque and hiding
# the screen you are trying to select from. Dialogs parented to the window
# (the session summary) still inherit this sheet.
#
# Coloured buttons and state-dependent widgets are keyed off dynamic properties
# (variant, state, tone) so their colours live here, not scattered in code.
STYLESHEET = """
QWidget {
    color: %(text)s;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QWidget#root, QMessageBox {
    background-color: %(bg)s;
}
QLabel, QCheckBox {
    background: transparent;
}

/* ---- Header ---------------------------------------------------------- */
QFrame#header {
    background-color: %(panel)s;
    border: 1px solid %(border)s;
    border-left: 4px solid %(idle)s;
    border-radius: 12px;
}
QFrame#header[state="running"] { border-left-color: %(green)s; }
QFrame#header[state="paused"]  { border-left-color: %(amber)s; }
QFrame#header[state="stopped"] { border-left-color: %(red)s; }
QLabel#appTitle {
    color: %(muted)s;
    font-size: 9pt;
    font-weight: 600;
    letter-spacing: 1px;
}
QLabel#statusDot {
    background-color: %(idle)s;
    border-radius: 7px;
}
QLabel#statusDot[state="running"] { background-color: %(green)s; }
QLabel#statusDot[state="paused"]  { background-color: %(amber)s; }
QLabel#statusDot[state="stopped"] { background-color: %(red)s; }
QLabel#statusText {
    font-size: 13pt;
    font-weight: 700;
    color: %(idle)s;
}
QLabel#statusText[state="running"] { color: %(green)s; }
QLabel#statusText[state="paused"]  { color: %(amber)s; }
QLabel#statusText[state="stopped"] { color: %(red)s; }
QLabel#captionText {
    color: %(muted)s;
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 1px;
}
QLabel#timerText {
    font-family: "Consolas";
    font-size: 20pt;
    font-weight: 700;
    color: %(text)s;
}
QLabel#timerText[state="paused"] { color: %(amber)s; }
QLabel#timerText[state="idle"]   { color: %(idle)s; }

/* ---- Panels ---------------------------------------------------------- */
QGroupBox {
    background-color: %(panel)s;
    border: 1px solid %(border)s;
    border-radius: 10px;
    margin-top: 12px;
    padding: 6px 12px 6px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 4px;
    color: %(muted)s;
}
QLabel#regionText {
    font-family: "Consolas";
    color: %(text)s;
}
QLabel#hintText {
    color: %(muted)s;
    font-size: 9pt;
}
QLabel#hintText[tone="fail"] { color: %(red)s; }

/* ---- Inputs ---------------------------------------------------------- */
QLineEdit, QDoubleSpinBox, QTextEdit {
    background-color: %(input)s;
    border: 1px solid %(border)s;
    border-radius: 6px;
    padding: 5px 7px;
    selection-background-color: %(accent)s;
    selection-color: %(bg)s;
}
QLineEdit:focus, QDoubleSpinBox:focus, QTextEdit:focus {
    border: 1px solid %(accent)s;
}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    width: 18px;
    border: none;
    background: transparent;
}
QDoubleSpinBox::up-button   { subcontrol-position: top right; }
QDoubleSpinBox::down-button { subcontrol-position: bottom right; }
/* Arrow images are drawn at startup: see themed_stylesheet(). */
QDoubleSpinBox::up-arrow   { image: url(@SPIN_UP@);   width: 10px; height: 9px; }
QDoubleSpinBox::down-arrow { image: url(@SPIN_DOWN@); width: 10px; height: 9px; }
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #2f3147;
    border-radius: 4px;
}
QTextEdit#monoText {
    font-family: "Consolas";
    font-size: 10pt;
}
QLabel#preview {
    background-color: %(input)s;
    border: 1px dashed %(border)s;
    border-radius: 6px;
    color: %(muted)s;
}
QLabel#webhookStatus { font-weight: 600; }
QLabel#webhookStatus[tone="ok"]      { color: %(green)s; }
QLabel#webhookStatus[tone="fail"]    { color: %(red)s; }
QLabel#webhookStatus[tone="pending"] { color: %(muted)s; }

/* ---- Stat tiles ------------------------------------------------------ */
QFrame#statTile {
    background-color: %(input)s;
    border: 1px solid %(border)s;
    border-radius: 8px;
}
QLabel#statCaption {
    color: %(muted)s;
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 1px;
}
QLabel#statValue {
    font-size: 15pt;
    font-weight: 700;
}
QLabel#statValue[tone="success"] { color: %(green)s; }
QLabel#statValue[tone="fail"]    { color: %(red)s; }

/* ---- Buttons --------------------------------------------------------- */
QPushButton {
    background-color: #2f3147;
    border: 1px solid %(border)s;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover   { background-color: #383a55; }
QPushButton:pressed { background-color: #25263a; }
QPushButton:disabled {
    background-color: #242536;
    border-color: #2c2d42;
    color: #5b5e73;
}
QPushButton[variant="accent"] {
    background-color: rgba(129, 140, 248, 0.14);
    border-color: %(accent)s;
    color: %(accent)s;
}
QPushButton[variant="accent"]:hover { background-color: rgba(129, 140, 248, 0.26); }
QPushButton[variant="start"] {
    background-color: rgba(74, 222, 128, 0.14);
    border-color: %(green)s;
    color: %(green)s;
}
QPushButton[variant="start"]:hover { background-color: rgba(74, 222, 128, 0.26); }
QPushButton[variant="pause"] {
    background-color: rgba(251, 191, 36, 0.14);
    border-color: %(amber)s;
    color: %(amber)s;
}
QPushButton[variant="pause"]:hover { background-color: rgba(251, 191, 36, 0.26); }
QPushButton[variant="stop"] {
    background-color: rgba(248, 113, 113, 0.14);
    border-color: %(red)s;
    color: %(red)s;
}
QPushButton[variant="stop"]:hover { background-color: rgba(248, 113, 113, 0.26); }
QPushButton[variant]:disabled {
    background-color: #242536;
    border-color: #2c2d42;
    color: #5b5e73;
}
QPushButton#controlButton {
    padding: 10px 14px;
    font-size: 11pt;
}

/* ---- Tabs ------------------------------------------------------------ */
QTabWidget::pane {
    border: none;
    border-top: 1px solid %(border)s;
    top: -1px;
    background: transparent;
}
QTabWidget::tab-bar { left: 0px; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: %(muted)s;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 6px 12px;
    margin-right: 2px;
    font-weight: 600;
}
QTabBar::tab:selected {
    color: %(text)s;
    border-bottom: 2px solid %(accent)s;
}
QTabBar::tab:hover:!selected { color: %(text)s; }
QLabel#sectionCaption {
    color: %(muted)s;
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 1px;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: %(border)s;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
""" % PALETTE


def load_config():
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            merged = DEFAULT_CONFIG.copy()
            merged.update(data)
            return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(config):
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2))
    except Exception as e:
        log.error("config save error: %s", e)


def setup_logging():
    """Append-only plain-text log next to the script, plus INFO+ on the
    console. Called once from main(); tests don't need it."""
    if getattr(setup_logging, "_done", False):
        return
    setup_logging._done = True
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as e:
        print(f"[log file error] {e}")
    if sys.stdout is not None:   # a windowed exe has no console to print to
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def _one_line(text):
    """Collapse raw OCR text to a single line for logging."""
    return " / ".join(part.strip() for part in (text or "").splitlines() if part.strip())


# ---------------------------------------------------------------------------
# Text classification
# ---------------------------------------------------------------------------
def fuzzy_contains(haystack, needle, threshold=0.75):
    """Loose match so minor OCR noise on the game's stylized font doesn't
    cause a missed detection."""
    haystack = haystack.lower()
    needle = needle.lower()
    if needle in haystack:
        return True
    n = len(needle)
    if len(haystack) < n:
        return difflib.SequenceMatcher(None, haystack, needle).ratio() >= threshold
    for i in range(len(haystack) - n + 1):
        window = haystack[i:i + n]
        if difflib.SequenceMatcher(None, window, needle).ratio() >= threshold:
            return True
    return False


def classify_notification(lowered_text):
    """Returns 'fail', 'success', or None. Requires the shared header word
    present, then checks the specific word that differs between the two
    notification types (much less prone to cross-matching than comparing
    the two full sentences, which share most of their wording)."""
    if not fuzzy_contains(lowered_text, HEADER_WORD, threshold=0.75):
        return None
    if fuzzy_contains(lowered_text, FAIL_WORD, threshold=0.8):
        return "fail"
    if fuzzy_contains(lowered_text, SUCCESS_WORD, threshold=0.8):
        return "success"
    return None


@dataclass(frozen=True)
class RewardMatch:
    qty: int
    name: str
    qty_explicit: bool  # False = no believable quantity read (wrap, fragment, misread)
    rejected_qty: Optional[int] = None  # a number WAS read, but above MAX_REWARD_QUANTITY


def _has_context(low, words, threshold=0.8):
    """Is any of these family words present, allowing for OCR noise?"""
    return any(fuzzy_contains(low, w, threshold=threshold) for w in words)


def _match_module(low):
    """Boost Module V1/V2 — only once 'boost' or 'module' is actually present."""
    if not _has_context(low, MODULE_CONTEXT_WORDS):
        return None
    m = MODULE_VERSION_RE.search(low)
    if not m:
        return None
    return f"Boost Module V{_VERSION_FIXES[m.group('digit').lower()]}"


def _qualifier_before_summer(low):
    """Read the word directly before "Summer" as the box qualifier. In that
    position it can be matched loosely: the best of normal/rare/mega wins if it
    clearly beats the other two. See POSITIONAL_CUTOFF for why."""
    tokens = _WORD_RE.findall(low)
    for i in range(1, len(tokens)):
        if difflib.SequenceMatcher(None, tokens[i], "summer").ratio() < SUMMER_MATCH:
            continue
        candidate = tokens[i - 1]
        if not candidate.isalpha():
            continue
        scored = sorted(
            ((difflib.SequenceMatcher(None, candidate, q).ratio(), q) for q in BOX_QUALIFIERS),
            reverse=True,
        )
        (best, qualifier), (second, _) = scored[0], scored[1]
        if best >= POSITIONAL_CUTOFF and best - second >= POSITIONAL_MARGIN:
            return BOX_QUALIFIERS[qualifier]
    return None


def _match_box(low):
    """Normal/Rare/Mega Summer Random Box — only once the box words are
    present. The qualifier is then matched loosely, because a real capture
    read "Rare" as "Rore"."""
    if not _has_context(low, BOX_CONTEXT_WORDS):
        return None
    positional = _qualifier_before_summer(low)
    if positional is not None:
        return positional
    best_name, best_score = None, 0.0
    for token in _WORD_RE.findall(low):
        for qualifier, display in BOX_QUALIFIERS.items():
            score = difflib.SequenceMatcher(None, token, qualifier).ratio()
            if score >= QUALIFIER_CUTOFF and score > best_score:
                best_name, best_score = display, score
    if best_name is None:
        log.debug("box reward seen but no readable qualifier in %r", _one_line(low))
    return best_name


def match_reward(text) -> Optional[RewardMatch]:
    """Find a reward keyword anywhere in the text and map it to its full
    display name. Works on fragments ("rare", "V1!") as well as the full
    "You've got 2 Boost Module V2!" line. Quantity defaults to 1 when the
    number can't be read (qty_explicit=False so callers can wait for a
    better read if they want)."""
    if not text:
        return None
    low = text.lower()
    name = _match_module(low) or _match_box(low)
    if name is None:
        return None

    rejected = None
    for pattern in (QTY_AFTER_GOT_RE, QTY_BEFORE_REWARD_RE):
        qm = pattern.search(text)
        if not qm:
            continue
        digits = qm.group(1).translate(_DIGIT_FIXES)
        if not digits.isdigit():
            continue
        qty = int(digits)
        cap = MAX_REWARD_QUANTITY
        if 1 <= qty <= cap:
            return RewardMatch(qty, name, True)
        if qty > cap and rejected is None:
            rejected = qty
    return RewardMatch(1, name, False, rejected_qty=rejected)


# Phrases that only ever appear in the tracker's OWN Discord messages, never
# in the game's notifications. A real session captured Discord inside the OCR
# region, reading "Minigame cleared!" and "Session so far: 192 success" from a
# previous report.
OWN_MESSAGE_MARKERS = (
    "session so far", "uptime", "minigame tracker", "mcorecxre", "tickets used",
    "hit rate", "inactive for",
)


def _is_own_discord_message(low):
    return any(marker in low for marker in OWN_MESSAGE_MARKERS)


def format_reward_summary(reward_counts):
    """Fixed-order list of all known rewards (0 if not yet obtained), plus any
    unrecognized entries at the bottom. Counts sit right-aligned in one column,
    identical in the app's Rewards panel and the Discord summary."""
    rows = [(name, reward_counts.get(name, 0)) for name in REWARD_NAMES]
    rows += [(name, count) for name, count in reward_counts.items()
             if name not in REWARD_NAMES]
    name_w = max(len(name) for name, _ in rows)
    num_w = max(len(str(count)) for _, count in rows)
    return [f"{name:<{name_w}}  {count:>{num_w}}" for name, count in rows]


# ---------------------------------------------------------------------------
# Event detection state machine (pure — no Qt, no screen)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    kind: str          # "fail" | "success"
    qty: int           # reward quantity (success only; 0 = unknown)
    reward_name: str   # display name, or "Unrecognized: ..." (success only)
    raw_text: str      # OCR text of the read that confirmed the event


class EventDetector:
    """Feed one OCR reading per poll cycle to process(); it returns the list
    of events confirmed by that reading (almost always empty or one).

    States:
      idle            — nothing on screen; a fail/success read starts an event
      pending_reward  — success seen, waiting for the stacked "You've got..."
                        popup (or reward_wait_seconds to elapse)
      awaiting_clear  — event logged; the notification must be absent for
                        clean_reads_required CONSECUTIVE reads before the
                        detector goes idle again. Any read that still shows
                        the notification resets the count, so a one-frame
                        OCR blip during the fade can't release the guard.
    """

    IDLE = "idle"
    PENDING_REWARD = "pending_reward"
    AWAITING_CLEAR = "awaiting_clear"

    def __init__(
        self,
        clean_reads_required: int = CLEAN_READS_REQUIRED,
        min_event_gap_seconds: float = MIN_SECONDS_BETWEEN_EVENTS,
        agreement_reads: int = REWARD_AGREEMENT_READS,
        settle_seconds: float = REWARD_SETTLE_SECONDS,
        gone_reads: int = REWARD_GONE_READS,
        reward_wait_max_seconds: float = REWARD_WAIT_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.clean_reads_required = max(1, int(clean_reads_required))
        self.min_event_gap_seconds = float(min_event_gap_seconds)
        self.agreement_reads = max(1, int(agreement_reads))
        self.settle_seconds = float(settle_seconds)
        self.gone_reads = max(1, int(gone_reads))
        self.reward_wait_max_seconds = float(reward_wait_max_seconds)
        self._clock = clock
        self.reset()

    def reset(self):
        self.state = self.IDLE
        self._clean_reads = 0
        self._pending_since = 0.0
        self._clear_reward_window()
        self._last_event: Optional[Event] = None
        self._last_event_at: Optional[float] = None

    # -- helpers ------------------------------------------------------------
    def _emit(self, ev: Event, now: float) -> Event:
        self._last_event = ev
        self._last_event_at = now
        log.info(
            "EVENT kind=%s qty=%d reward=%r raw=%r",
            ev.kind, ev.qty, ev.reward_name, _one_line(ev.raw_text),
        )
        return ev

    def _clear_reward_window(self):
        self._provisional: Optional[RewardMatch] = None
        self._votes: List[RewardMatch] = []
        self._first_reward_at: Optional[float] = None
        self._rejected: Counter = Counter()
        self._last_reading: Optional[str] = None
        self._last_new_reading_at: float = 0.0
        self._agreed: Optional[tuple] = None
        self._agreed_count: int = 0
        self._gone_count: int = 0

    def _decide_reward(self, text):
        """Settle on one reward from everything seen during the reward window.
        Majority vote, because individual OCR readings disagree: the digit in
        the game's font is the least reliable character on screen."""
        if self._rejected:
            log.warning(
                "ignored impossible reward quantities (the cap is %d): %s",
                MAX_REWARD_QUANTITY, dict(self._rejected),
            )
        if self._votes:
            names = Counter(v.name for v in self._votes)
            name = names.most_common(1)[0][0]
            quantities = Counter(v.qty for v in self._votes if v.name == name)
            qty, agreeing = quantities.most_common(1)[0]
            if len(quantities) > 1:
                log.warning(
                    "reward quantity disagreed across reads for %s: %s — taking %d "
                    "(%d of %d readings)",
                    name, dict(quantities), qty, agreeing, sum(quantities.values()),
                )
            if len(names) > 1:
                log.warning("reward NAME disagreed across reads: %s — taking %r",
                            dict(names), name)
            return Event("success", qty, name, text), (
                f"reward decided by majority of {len(self._votes)} reading(s)"
            )
        if self._provisional is not None:
            return (
                Event("success", self._provisional.qty, self._provisional.name, text),
                "only a fragment without a quantity was read",
            )
        return (
            Event("success", 0, "Unrecognized: (no reward text seen)", text),
            "no reward text",
        )

    def _start_awaiting_clear(self, reason: str):
        self.state = self.AWAITING_CLEAR
        self._clean_reads = 0
        log.debug(
            "-> awaiting_clear (%s); need %d consecutive clean reads",
            reason, self.clean_reads_required,
        )

    # -- main entry point ---------------------------------------------------
    def process(self, ocr_text: str, now: Optional[float] = None) -> List[Event]:
        if now is None:
            now = self._clock()
        text = ocr_text or ""
        lowered = text.lower().strip()
        if lowered and _is_own_discord_message(lowered):
            # Discord was visible inside the OCR region showing one of OUR
            # messages ("Minigame cleared!", "Session so far: ..."). Reading it
            # would count a result twice, so treat the frame as no information
            # at all: no event, and no change to any guard.
            log.debug("ignored a frame showing the tracker's own Discord message: %r",
                      _one_line(text))
            return []
        kind = classify_notification(lowered) if lowered else None
        reward = match_reward(lowered) if lowered else None
        notification_present = kind is not None or reward is not None
        events: List[Event] = []

        if self.state == self.AWAITING_CLEAR:
            if notification_present:
                if self._clean_reads:
                    log.debug(
                        "blip ignored: notification re-seen (%s) after %d clean read(s); counter reset",
                        kind or "reward text", self._clean_reads,
                    )
                self._clean_reads = 0
                if (
                    reward is not None
                    and self._last_event is not None
                    and self._last_event.kind == "success"
                    and self._last_event.reward_name.startswith("Unrecognized")
                ):
                    log.info(
                        "late reward text seen while awaiting clear (%r) — reward wait may be too short",
                        _one_line(text),
                    )
            else:
                self._clean_reads += 1
                if self._clean_reads >= self.clean_reads_required:
                    self.state = self.IDLE
                    log.debug("released -> idle after %d consecutive clean reads", self._clean_reads)
                else:
                    log.debug("clean read %d/%d", self._clean_reads, self.clean_reads_required)
            return events

        if self.state == self.PENDING_REWARD:
            if kind == "fail":
                # A fail can't really follow a success this fast, but if OCR
                # says so, close the success out so nothing is lost.
                events.append(self._emit(Event(
                    "success", 0, "Unrecognized: (interrupted by next ticket)", text), now))
                events.append(self._emit(Event("fail", 0, "", text), now))
                self._clear_reward_window()
                self._start_awaiting_clear("fail interrupted pending success")
                return events

            # Only a CHANGED reading is new evidence: the capture loop repeats
            # the previous reading while the screen is unchanged.
            fresh = text != self._last_reading
            self._last_reading = text
            if fresh:
                self._last_new_reading_at = now

            if reward is not None:
                if self._first_reward_at is None:
                    self._first_reward_at = now
                self._gone_count = 0
                if fresh and reward.qty_explicit:
                    self._votes.append(reward)
                    key = (reward.name, reward.qty)
                    self._agreed_count = self._agreed_count + 1 if key == self._agreed else 1
                    self._agreed = key
                    if self._agreed_count >= self.agreement_reads:
                        events.append(self._emit(
                            Event("success", reward.qty, reward.name, text), now))
                        self._clear_reward_window()
                        self._start_awaiting_clear(
                            f"{self._agreed_count} readings agreed on the reward")
                        return events
                elif fresh:
                    if reward.rejected_qty is not None:
                        self._rejected[reward.rejected_qty] += 1
                    if self._provisional is None:
                        self._provisional = reward
                        log.debug(
                            "provisional reward %r seen without a believable quantity; waiting for a fuller read",
                            reward.name,
                        )
            elif self._first_reward_at is not None:
                # The popup was there and is now gone: no more evidence coming.
                self._gone_count += 1
                if self._gone_count >= self.gone_reads:
                    ev, reason = self._decide_reward(text)
                    events.append(self._emit(ev, now))
                    self._clear_reward_window()
                    self._start_awaiting_clear(f"reward popup gone; {reason}")
                    return events

            settled = (
                (self._votes or self._provisional)
                and (now - self._last_new_reading_at) >= self.settle_seconds
            )
            if settled or (now - self._pending_since) > self.reward_wait_max_seconds:
                why = "readings stopped changing" if settled else "gave up waiting"
                ev, reason = self._decide_reward(text)
                events.append(self._emit(ev, now))
                self._clear_reward_window()
                self._start_awaiting_clear(f"{why}; {reason}")
            return events

        # IDLE
        if kind is None:
            if reward is not None:
                log.debug("reward text seen while idle (header missed?): %r", _one_line(text))
            return events

        if (
            self._last_event_at is not None
            and (now - self._last_event_at) < self.min_event_gap_seconds
        ):
            log.warning(
                "suppressed %s read only %.1fs after the last event (guard released early?); re-arming",
                kind, now - self._last_event_at,
            )
            self._start_awaiting_clear("min event gap")
            return events

        if kind == "fail":
            events.append(self._emit(Event("fail", 0, "", text), now))
            self._start_awaiting_clear("fail logged")
        elif kind == "success":
            self.state = self.PENDING_REWARD
            self._pending_since = now
            self._clear_reward_window()
            self._last_reading = text
            self._last_new_reading_at = now
            log.debug(
                "-> pending_reward (success seen); settling by agreement of %d readings, "
                "or %.0fs at the latest",
                self.agreement_reads, self.reward_wait_max_seconds,
            )
        return events


# ---------------------------------------------------------------------------
# Session clock (pure — no Qt). Single source of truth for session state and
# uptime: the header dot, the header timer and every Discord embed all read
# from here, so they can never disagree with each other.
# ---------------------------------------------------------------------------
class SessionClock:
    """Tracks ACTIVE tracking time: it stops counting while paused, carries on
    from the same value on resume, freezes on stop, and resets on start."""

    IDLE = "idle"          # never started in this app run
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.state = self.IDLE
        self._accumulated = 0.0
        self._running_since: Optional[float] = None

    def start(self):
        self._accumulated = 0.0
        self._running_since = self._clock()
        self.state = self.RUNNING

    def pause(self):
        if self.state == self.RUNNING:
            self._bank()
            self.state = self.PAUSED

    def resume(self):
        if self.state == self.PAUSED:
            self._running_since = self._clock()
            self.state = self.RUNNING

    def stop(self):
        if self.state in (self.RUNNING, self.PAUSED):
            self._bank()
            self.state = self.STOPPED

    def elapsed_seconds(self) -> float:
        live = 0.0
        if self._running_since is not None:
            live = self._clock() - self._running_since
        return self._accumulated + live

    def _bank(self):
        """Fold the currently running stretch into the accumulated total."""
        if self._running_since is not None:
            self._accumulated += self._clock() - self._running_since
            self._running_since = None


def format_uptime(seconds: float) -> str:
    """HH:MM:SS, flooring partial seconds. Hours grow past 99 if needed."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class InactivityMonitor:
    """Watches for "no results for a long time", which usually means the macro
    or the tracker has got stuck.

    Time is passed in as ACTIVE tracking seconds (SessionClock uptime), never
    wall clock, so a deliberate pause can't look like a stall.

    It fires ONCE per quiet spell and then stays silent until a result arrives.
    A stuck macro therefore pings once, rather than every few minutes all night.
    """

    def __init__(self, interval_seconds: float = 0.0):
        self.interval_seconds = float(interval_seconds)
        self.reset(0.0)

    def reset(self, now: float):
        self._last_activity = now
        self._fired = False

    def note_activity(self, now: float):
        """A result was detected: restart the count and re-arm the alert."""
        self._last_activity = now
        self._fired = False

    def idle_seconds(self, now: float) -> float:
        return max(0.0, now - self._last_activity)

    def check(self, now: float) -> bool:
        """True exactly once, on the first call at or past the threshold."""
        if self.interval_seconds <= 0 or self._fired:
            return False
        if self.idle_seconds(now) >= self.interval_seconds:
            self._fired = True
            return True
        return False


# Already-formed mention (<@123>, <@!123>, <@&123>), role shorthand, bare id.
_MENTION_RE = re.compile(r"^<@[!&]?\d{5,25}>$")
_ROLE_RE = re.compile(r"^(?:role[:\s]+|&)(\d{5,25})$", re.IGNORECASE)
_ID_RE = re.compile(r"^(\d{5,25})$")


def ping_problem(raw) -> Optional[str]:
    """Why this ping target will not ping, or None if it will.

    Discord can only ping by ID, never by name, and a name typed here fails
    silently in Discord: the message arrives with plain grey text and nobody is
    notified. The app says so instead."""
    text = (raw or "").strip()
    if not text:
        return None
    mention = format_mention(text)
    if mention in ("@everyone", "@here") or mention.startswith("<@"):
        return None
    return ("Discord pings by ID, not by name. In Discord type \\@name, send it, "
            "then paste what it turns into here.")


def allowed_mentions_for(mention) -> dict:
    """Tell Discord exactly which ping this message may fire.

    Without this a role ping only works when the role is marked mentionable;
    naming the role makes it work either way. It also means a stray "@everyone"
    inside OCR text or a reward name can never trigger a mass ping."""
    if not mention:
        return {"parse": []}
    if mention in ("@everyone", "@here"):
        return {"parse": ["everyone"]}
    role = re.fullmatch(r"<@&(\d+)>", mention)
    if role:
        return {"parse": [], "roles": [role.group(1)]}
    user = re.fullmatch(r"<@!?(\d+)>", mention)
    if user:
        return {"parse": [], "users": [user.group(1)]}
    return {"parse": []}


def format_mention(raw) -> str:
    """Turn what the user typed into something Discord will actually ping.

    Accepts a plain user id, "role:<id>" or "&<id>" for a role, an already
    formed mention, or @everyone / @here. Anything else passes through
    untouched: it then shows in the message as plain text without pinging,
    which the user can see, rather than being silently dropped.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    bare = text.lstrip("@").lower()
    if bare in ("everyone", "here"):
        return "@" + bare
    if _MENTION_RE.match(text):
        return text
    role = _ROLE_RE.match(text)
    if role:
        return f"<@&{role.group(1)}>"
    user = _ID_RE.match(text)
    if user:
        return f"<@{user.group(1)}>"
    return text


# ---------------------------------------------------------------------------
# OCR (capture -> preprocess -> Tesseract)
# ---------------------------------------------------------------------------
def otsu_threshold(gray):
    """Threshold that best separates the two brightness populations in the
    image. Only used as a diagnostic suggestion in Test Capture — live
    detection uses the fixed OCR_THRESHOLD, because an adaptive threshold on
    a frame with no notification would amplify scenery into garbage text."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if not total:
        return 0
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b, w_b, best_var, best_t = 0.0, 0, -1.0, 0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        var = w_b * w_f * ((sum_b / w_b) - ((sum_all - sum_b) / w_f)) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def to_ocr_gray(pil_img):
    """Collapse colour to one channel for thresholding, using the BRIGHTEST of
    R/G/B rather than perceptual luminance.

    This matters because the reward notification draws its text in BLUE, and
    blue contributes only 11% of luminance — a standard grayscale conversion
    dims that text far more than the white text of the win/lose notification,
    which is how a real reward came to be missed entirely. Taking the max
    channel treats bright text as bright whatever its hue. Measured over a
    brightness sweep on real captures, it detects strictly more than luminance
    and never less.
    """
    r, g, b = pil_img.convert("RGB").split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def ink_constrained_threshold(hist, total, max_fraction):
    """Lowest cutoff that still calls no more than `max_fraction` of the
    region text. This is what rescues bright backgrounds, where Otsu alone
    lands far too low and floods the image with black."""
    cumulative = 0
    for value in range(255, -1, -1):
        cumulative += hist[value]
        if cumulative / total > max_fraction:
            return min(value + 1, 255)
    return 0


def choose_threshold(gray):
    """Per-frame cutoff: the stricter of Otsu and the ink constraint, floored
    so a contrast-free frame can't go solid black."""
    hist = gray.histogram()[:256]
    total = sum(hist) or 1
    return max(
        otsu_threshold(gray),
        ink_constrained_threshold(hist, total, OCR_TARGET_INK_FRACTION),
        OCR_MIN_THRESHOLD,
    )


def ink_fraction(processed):
    """Share of the processed image that came out as "text" (black)."""
    hist = processed.histogram()[:256]
    total = sum(hist) or 1
    return hist[0] / total


def preprocess_for_ocr(pil_img):
    """Grayscale + upscale + per-frame threshold, so Tesseract sees crisp black
    lettering on a white background instead of a raw game screenshot.
    Returns an "L" mode image containing only 0 and 255."""
    gray = to_ocr_gray(pil_img)
    threshold = choose_threshold(gray)
    if OCR_UPSCALE > 1:
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        gray = gray.resize((gray.width * OCR_UPSCALE, gray.height * OCR_UPSCALE), resampling)
    # Bright (text) -> black, everything else -> white.
    return gray.point(lambda p: 0 if p >= threshold else 255)


def ocr_image(pil_img):
    """Returns (recognized_text, processed_image). Used by both live detection
    and Test Capture so the preview always matches what detection reads.

    `text` is None when the frame was too degenerate to read, as distinct from
    "" meaning it was read and held no text. The difference matters: "" tells
    the detector the notification is gone, and treating an unreadable frame as
    empty would release the duplicate guard while a notification was still up.
    """
    processed = preprocess_for_ocr(pil_img)
    ink = ink_fraction(processed)
    if ink > OCR_MAX_INK_FRACTION:
        log.debug("frame unreadable: %.0f%% of it thresholded as text", ink * 100)
        return None, processed
    if ink == 0.0:
        return "", processed
    text = pytesseract.image_to_string(processed, config=TESSERACT_CONFIG).strip()
    return text, processed


def brightness_report(pil_img):
    """Human-readable diagnosis of what a capture contained. A blank preview is
    ambiguous on its own: it means either 'no notification was on screen' or
    'the threshold was wrong'. This tells them apart."""
    gray = to_ocr_gray(pil_img)
    hist = gray.histogram()[:256]
    total = sum(hist) or 1
    brightest = max((i for i, h in enumerate(hist) if h), default=0)
    darkest = min((i for i, h in enumerate(hist) if h), default=0)
    threshold = choose_threshold(gray)
    processed = preprocess_for_ocr(pil_img)
    ink = ink_fraction(processed)
    lines = [
        f"brightness range in region: {darkest} to {brightest}",
        f"threshold chosen for this frame: {threshold}  (adaptive, per frame)",
        f"pixels treated as text: {ink * 100:.2f}%",
    ]
    if brightest - darkest < 25:
        lines.append(
            "=> The region is almost a flat colour. Nothing was on screen to read."
        )
    elif ink == 0.0:
        lines.append("=> Nothing crossed the threshold. No notification in the region.")
    elif ink > OCR_MAX_INK_FRACTION:
        lines.append(
            f"=> {ink * 100:.0f}% of the region came out as text, so it is scenery. "
            "OCR was skipped. Shrink the region so it covers only the notification."
        )
    else:
        lines.append("=> Ink level looks like text. This is what OCR read from.")
    return "\n".join(lines)


def grab_region(sct, region):
    img = sct.grab(region)
    return Image.frombytes("RGB", img.size, img.rgb)


# ---------------------------------------------------------------------------
# Discord webhook (embeds via plain requests POST)
# ---------------------------------------------------------------------------
def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


# Every Discord message ends with this credit, shown in small grey text beside
# Discord's own timestamp.
CREDIT = "Minigame Tracker · by mcorecxre"


def _chip(value):
    """Monospace value: the same look as the app's timer and counters."""
    return f"`{value}`"


def uptime_field(uptime_seconds):
    """The Uptime field every notification carries, in the same HH:MM:SS the
    app header shows."""
    return {"name": "Uptime", "value": _chip(format_uptime(uptime_seconds)), "inline": True}


def _session_field(success_count, fail_count):
    return {"name": "Session",
            "value": _chip(f"{success_count} cleared · {fail_count} failed"),
            "inline": True}


def _embed(title, color, fields, description=None):
    """One shape for every message: a short title, a colour from the app's
    palette, compact fields and the credit footer. Keeping every message the
    same shape is what makes the channel read calmly. It also guarantees each
    one carries the credit and "Uptime", which the tracker uses to recognise
    its OWN messages if Discord ever sits inside the OCR region."""
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {"text": CREDIT},
        "timestamp": _utc_now_iso(),
    }
    if description:
        embed["description"] = description
    return embed


# uptime_seconds is keyword-only and required on every builder, so a new
# notification can't be added without deciding what uptime it reports.
def build_started_embed(*, uptime_seconds):
    """Posted the moment Start is pressed. Neutral indigo, so it never reads
    as a result next to the green cleared / red failed messages."""
    return _embed("Tracker started", COLOR_STARTED, [uptime_field(uptime_seconds)],
                  description="Watching for minigame results.")


def build_event_embed(kind, qty, reward_name, success_count, fail_count, *, uptime_seconds):
    """One confirmed result. The reward shown is this run's only."""
    if kind == "success":
        if reward_name and not reward_name.startswith("Unrecognized"):
            reward_value = f"**{max(qty, 1)}× {reward_name}**"
        else:
            reward_value = "*Reward not read*"
        return _embed("Minigame cleared", COLOR_SUCCESS, [
            {"name": "Reward", "value": reward_value, "inline": False},
            uptime_field(uptime_seconds),
            _session_field(success_count, fail_count),
        ])
    return _embed("Minigame failed", COLOR_FAIL, [
        uptime_field(uptime_seconds),
        _session_field(success_count, fail_count),
    ])


def build_summary_embed(success_count, fail_count, reward_counts, *, uptime_seconds):
    total = success_count + fail_count
    rate = (success_count / total * 100) if total else 0
    reward_block = "\n".join(format_reward_summary(reward_counts))
    # Discord lays inline fields out three to a row; the blank field keeps the
    # five stats in a tidy 3x2 grid instead of a ragged 3+2.
    blank = {"name": "\u200b", "value": "\u200b", "inline": True}
    return _embed("Session complete", COLOR_INFO, [
        {"name": "Tickets", "value": _chip(total), "inline": True},
        {"name": "Cleared", "value": _chip(success_count), "inline": True},
        {"name": "Failed", "value": _chip(fail_count), "inline": True},
        {"name": "Hit rate", "value": _chip(f"{rate:.1f}%"), "inline": True},
        uptime_field(uptime_seconds),
        blank,
        {"name": "Rewards", "value": f"```\n{reward_block}\n```", "inline": False},
    ])


def build_inactivity_embed(*, idle_seconds, uptime_seconds, last_result):
    """Sent once when nothing has been detected for the configured time.
    Amber, so it reads as "needs attention" rather than as a result."""
    return _embed(
        "No minigame results detected", COLOR_ALERT,
        [
            {"name": "Inactive for", "value": _chip(format_uptime(idle_seconds)), "inline": True},
            uptime_field(uptime_seconds),
            {"name": "Last result", "value": last_result, "inline": False},
        ],
        description=("The macro or the tracker may be stuck. "
                     "No further alerts until a result is detected."),
    )


def build_test_embed(*, uptime_seconds):
    return _embed("Webhook connected", COLOR_STARTED, [uptime_field(uptime_seconds)],
                  description="Results and session reports will post here.")


def post_webhook(url, embed, content=None):
    """POST one embed, optionally with message text. Returns (ok, message).
    Never raises.

    A ping has to live in `content`. Discord does not notify anyone for a
    mention written inside an embed, so putting it there would look right and
    reach nobody.
    """
    payload = {"username": "Minigame Tracker", "embeds": [embed]}
    if content:
        payload["content"] = content
        payload["allowed_mentions"] = allowed_mentions_for(content)
    try:
        r = requests.post(url, json=payload, timeout=8)
    except Exception as e:
        log.error("webhook error: %s", e)
        return False, f"{type(e).__name__}: {e}"
    if r.ok:
        return True, f"HTTP {r.status_code}"
    body = (r.text or "").strip().replace("\n", " ")[:120]
    log.error("webhook rejected: HTTP %s %s", r.status_code, body)
    return False, f"HTTP {r.status_code} {body}".strip()


class WebhookPoster(QThread):
    """Posts one embed off the UI thread; emits (ok, message) when done."""
    done = pyqtSignal(bool, str)

    def __init__(self, url, embed, content=None):
        super().__init__()
        self.url = url
        self.embed = embed
        self.content = content

    def run(self):
        ok, message = post_webhook(self.url, self.embed, self.content)
        self.done.emit(ok, message)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class RegionSelector(QWidget):
    """Full-screen overlay for click-drag OCR region selection."""
    region_selected = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)

        screens = QApplication.screens()
        geo = screens[0].geometry()
        for s in screens[1:]:
            geo = geo.united(s.geometry())
        self.setGeometry(geo)

        self.origin = None
        self.current = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        if self.origin and self.current:
            rect = QRect(self.origin, self.current).normalized()
            painter.setPen(QPen(QColor(0, 220, 120), 2))
            painter.fillRect(rect, QColor(0, 220, 120, 60))
            painter.drawRect(rect)

    def mousePressEvent(self, event):
        self.origin = event.position().toPoint()
        self.current = self.origin
        self.update()

    def mouseMoveEvent(self, event):
        self.current = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event):
        rect = QRect(self.origin, event.position().toPoint()).normalized()
        global_top_left = self.mapToGlobal(rect.topLeft())
        region = {
            "top": global_top_left.y(),
            "left": global_top_left.x(),
            "width": max(rect.width(), 10),
            "height": max(rect.height(), 10),
        }
        self.region_selected.emit(region)
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()


class OCRWorker(QThread):
    """Thin capture loop: grab region -> preprocess -> OCR -> EventDetector.
    All detection logic lives in EventDetector; this just feeds it one
    reading per poll and emits whatever it confirms."""
    # kind: "fail" | "success". qty/reward_name only meaningful for "success".
    result = pyqtSignal(str, int, str, str)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._running = True
        self._paused = False

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self._running = False
        self.wait()

    def run(self):
        region = self.config["region"]
        interval = POLL_INTERVAL_SECONDS

        detector = EventDetector()
        last_hash = None
        last_text = ""
        log.info(
            "session started: region=%s poll=%.2fs agreement=%d clean_reads=%d",
            region, interval, REWARD_AGREEMENT_READS, CLEAN_READS_REQUIRED,
        )

        with mss.mss() as sct:
            while self._running:
                if self._paused:
                    time.sleep(0.2)
                    continue
                try:
                    pil_img = grab_region(sct, region)
                    img_hash = hashlib.md5(pil_img.tobytes()).hexdigest()
                    # Only run Tesseract when the pixels changed; an unchanged
                    # frame reuses the previous reading. The detector still
                    # gets a reading EVERY poll, which is what lets it count
                    # consecutive clean reads once the notification is gone
                    # and the region has stopped changing.
                    if img_hash != last_hash:
                        text, _ = ocr_image(pil_img)
                        # None = unreadable frame. Keep the previous reading
                        # rather than reporting "nothing there", so a
                        # degenerate frame can't look like the notification
                        # vanishing and release the duplicate guard.
                        if text is not None:
                            last_text = text
                        last_hash = img_hash
                    for ev in detector.process(last_text):
                        self.result.emit(ev.kind, ev.qty, ev.reward_name, ev.raw_text)
                except Exception as e:
                    log.error("OCR error: %s", e)
                time.sleep(interval)
        log.info("session stopped")


def pil_to_pixmap(img):
    """Convert a Pillow image to a QPixmap without needing PIL.ImageQt."""
    gray = img.convert("L")
    data = gray.tobytes()
    qimg = QImage(data, gray.width, gray.height, gray.width, QImage.Format.Format_Grayscale8)
    return QPixmap.fromImage(qimg.copy())


def _spin_arrow_images():
    """Draw the spin-box up/down chevrons as tiny images.

    Qt stylesheets can't draw an arrow by themselves: the usual border-triangle
    trick renders as grey squares, and the native arrows are dark boxes that
    vanish on a dark theme. So two chevrons are painted once at startup, at 2x
    for high-DPI screens, into the temp folder (nothing new appears next to the
    script) and the stylesheet points at them.
    """
    folder = Path(tempfile.gettempdir()) / "minigame_tracker_ui"
    folder.mkdir(exist_ok=True)
    shapes = {
        "up": [(2, 12), (10, 4), (18, 12)],
        "down": [(2, 6), (10, 14), (18, 6)],
    }
    paths = {}
    for name, points in shapes.items():
        pixmap = QPixmap(20, 18)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(PALETTE["muted"]), 3.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.end()
        path = folder / f"spin_{name}.png"
        pixmap.save(str(path))
        paths[name] = path.as_posix()
    return paths


def themed_stylesheet():
    """STYLESHEET with the spin-box arrow images filled in. Needs a running
    QApplication. If the images can't be written the arrows are simply blank;
    typing, arrow keys and the mouse wheel still change the value."""
    try:
        arrows = _spin_arrow_images()
    except Exception as e:
        log.warning("could not draw spin-box arrows: %s", e)
        arrows = {"up": "", "down": ""}
    return STYLESHEET.replace("@SPIN_UP@", arrows["up"]).replace("@SPIN_DOWN@", arrows["down"])


def _set_style_property(widget, name, value):
    """Set a dynamic property the stylesheet selects on, then force Qt to
    re-evaluate the widget's style — it does not do that on its own."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


class MainWindow(QWidget):
    PREVIEW_W = 200
    PREVIEW_H = 150

    STATUS_TEXT = {
        SessionClock.IDLE: "Idle",
        SessionClock.RUNNING: "Running",
        SessionClock.PAUSED: "Paused",
        SessionClock.STOPPED: "Stopped",
    }

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        # A plain QWidget subclass ignores stylesheet backgrounds without this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Minigame Tracker")
        self.setMinimumWidth(480)
        self.setStyleSheet(themed_stylesheet())

        self.config = load_config()
        self.worker = None
        self.success_count = 0
        self.fail_count = 0
        self.reward_counts = Counter()
        self._posters = []  # keep webhook threads alive until they finish
        self.session = SessionClock()
        self.inactivity = InactivityMonitor(0.0)
        self._last_result_text = "None yet this session"

        self._build_ui()
        self._refresh_region_label()
        self._apply_session_state()

        # The timer text is recomputed from SessionClock on every tick rather
        # than incremented by the tick, so a late or skipped tick can never
        # make the displayed time drift from the real value.
        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()

    # -- UI construction ---------------------------------------------------
    def _build_ui(self):
        """Header and controls stay visible; everything else lives in three
        tabs, so the window fits a 1366x768 laptop screen."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        layout.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_session_tab(), "Session")
        self.tabs.addTab(self._build_setup_tab(), "Setup")
        self.tabs.addTab(self._build_capture_tab(), "Test Capture")
        layout.addWidget(self.tabs, 1)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_btn = QPushButton("Start")
        self.pause_btn = QPushButton("Pause")
        self.stop_btn = QPushButton("Stop")
        for button, variant in (
            (self.start_btn, "start"), (self.pause_btn, "pause"), (self.stop_btn, "stop"),
        ):
            button.setObjectName("controlButton")
            button.setProperty("variant", variant)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_session)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn.clicked.connect(self.stop_session)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.pause_btn)
        controls.addWidget(self.stop_btn)
        layout.addLayout(controls)

        self._update_rewards_display()

    @staticmethod
    def _tab_page():
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(2, 12, 2, 4)
        column.setSpacing(10)
        return page, column

    @staticmethod
    def _section(text):
        label = QLabel(text.upper())
        label.setObjectName("sectionCaption")
        return label

    def _build_session_tab(self):
        page, column = self._tab_page()
        grid = QGridLayout()
        grid.setSpacing(8)
        self.success_label = self._stat_tile(grid, 0, "CLEARED", "0", tone="success")
        self.fail_label = self._stat_tile(grid, 1, "FAILED", "0", tone="fail")
        self.total_label = self._stat_tile(grid, 2, "TICKETS", "0")
        self.rate_label = self._stat_tile(grid, 3, "HIT RATE", "—")
        column.addLayout(grid)
        column.addWidget(self._section("Rewards"))
        self.rewards_output = QTextEdit()
        self.rewards_output.setObjectName("monoText")
        self.rewards_output.setReadOnly(True)
        # Tall enough for all five reward lines without a scrollbar.
        self.rewards_output.setFixedHeight(104)
        column.addWidget(self.rewards_output)
        column.addStretch(1)
        return page

    def _build_setup_tab(self):
        page, column = self._tab_page()
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.region_label = QLabel()
        self.region_label.setObjectName("regionText")
        self.select_region_btn = QPushButton("Select OCR Region")
        self.select_region_btn.setProperty("variant", "accent")
        self.select_region_btn.clicked.connect(self.select_region)
        region_row = QHBoxLayout()
        region_row.setSpacing(8)
        region_row.addWidget(self.region_label, 1)
        region_row.addWidget(self.select_region_btn)

        self.webhook_input = QLineEdit(self.config["webhook_url"])
        self.webhook_input.setPlaceholderText("Discord webhook URL (optional)")
        self.ping_input = QLineEdit(self.config["ping_target"])
        self.ping_input.setPlaceholderText("user id, role id, @everyone or @here (optional)")
        self.ping_input.textChanged.connect(self._check_ping_target)
        self.inactivity_input = QDoubleSpinBox()
        self.inactivity_input.setRange(0.0, 240.0)
        self.inactivity_input.setDecimals(1)
        self.inactivity_input.setSingleStep(1.0)
        # At the minimum the box reads "Off" instead of 0.0.
        self.inactivity_input.setSpecialValueText("Off")
        self.inactivity_input.setValue(float(self.config["inactivity_minutes"]))
        self.inactivity_input.setFixedWidth(96)
        alert_row = QHBoxLayout()
        alert_row.setSpacing(8)
        alert_row.addWidget(self.inactivity_input)
        alert_row.addWidget(self.ping_input, 1)

        form.addRow("OCR region", region_row)
        form.addRow("Webhook URL", self.webhook_input)
        form.addRow("Inactivity alert (min)", alert_row)
        column.addLayout(form)

        self.ping_hint = QLabel()
        self.ping_hint.setObjectName("hintText")
        self.ping_hint.setWordWrap(True)
        column.addWidget(self.ping_hint)
        self._check_ping_target()

        webhook_row = QHBoxLayout()
        webhook_row.setSpacing(10)
        self.test_webhook_btn = QPushButton("Test Webhook")
        self.test_webhook_btn.setProperty("variant", "accent")
        self.test_webhook_btn.clicked.connect(self.test_webhook)
        self.webhook_status = QLabel("")
        self.webhook_status.setObjectName("webhookStatus")
        webhook_row.addWidget(self.test_webhook_btn)
        webhook_row.addWidget(self.webhook_status, 1)
        column.addLayout(webhook_row)
        column.addStretch(1)
        return page

    def _build_capture_tab(self):
        page, column = self._tab_page()
        top = QHBoxLayout()
        top.setSpacing(10)
        self.test_btn = QPushButton("Test Capture")
        self.test_btn.setProperty("variant", "accent")
        self.test_btn.clicked.connect(self.test_capture)
        hint = QLabel("Shows exactly what live detection reads")
        hint.setObjectName("hintText")
        top.addWidget(self.test_btn)
        top.addWidget(hint, 1)
        column.addLayout(top)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.test_output = QTextEdit()
        self.test_output.setObjectName("monoText")
        self.test_output.setReadOnly(True)
        self.test_output.setFixedHeight(self.PREVIEW_H)
        self.test_output.setPlaceholderText("Recognized text will appear here...")
        row.addWidget(self.test_output, 1)
        self.preview_label = QLabel("Processed image\npreview")
        self.preview_label.setObjectName("preview")
        self.preview_label.setFixedSize(self.PREVIEW_W, self.PREVIEW_H)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.preview_label)
        column.addLayout(row)
        column.addStretch(1)
        return page

    def _build_header(self):
        """Status dot + label on the left, uptime on the right: state and
        elapsed time readable together at a glance."""
        self.header = QFrame()
        self.header.setObjectName("header")
        row = QHBoxLayout(self.header)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel("MINIGAME TRACKER")
        title.setObjectName("appTitle")
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(14, 14)
        self.status_text = QLabel()
        self.status_text.setObjectName("statusText")
        status_row.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.status_text, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)
        left.addWidget(title)
        left.addLayout(status_row)

        right = QVBoxLayout()
        right.setSpacing(0)
        caption = QLabel("UPTIME")
        caption.setObjectName("captionText")
        caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.timer_label = QLabel(format_uptime(0))
        self.timer_label.setObjectName("timerText")
        self.timer_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right.addWidget(caption)
        right.addWidget(self.timer_label)

        row.addLayout(left, 1)
        row.addLayout(right)
        return self.header

    @staticmethod
    def _stat_tile(grid, column, caption, value, tone=None):
        """One counter tile. Returns the value label so the existing counter
        attributes (success_label etc.) keep working unchanged."""
        tile = QFrame()
        tile.setObjectName("statTile")
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(10, 8, 10, 8)
        tile_layout.setSpacing(2)
        caption_label = QLabel(caption)
        caption_label.setObjectName("statCaption")
        value_label = QLabel(value)
        value_label.setObjectName("statValue")
        if tone:
            value_label.setProperty("tone", tone)
        tile_layout.addWidget(caption_label)
        tile_layout.addWidget(value_label)
        grid.addWidget(tile, 0, column)
        grid.setColumnStretch(column, 1)
        return value_label

    # -- Session state + uptime --------------------------------------------
    def get_uptime_seconds(self) -> float:
        """Active tracking time for the current (or last) session. The header
        and every Discord notification read this, so they always agree."""
        return self.session.elapsed_seconds()

    def get_uptime_text(self) -> str:
        return format_uptime(self.get_uptime_seconds())

    def _refresh_timer(self):
        text = self.get_uptime_text()
        if self.timer_label.text() != text:
            self.timer_label.setText(text)

    def _on_tick(self):
        self._refresh_timer()
        self._check_inactivity()

    def _check_inactivity(self):
        """Alert once if nothing has been detected for the configured time.
        Measured in active tracking time, so a pause never triggers it."""
        if self.session.state != SessionClock.RUNNING:
            return
        now = self.get_uptime_seconds()
        if not self.inactivity.check(now):
            return
        idle = self.inactivity.idle_seconds(now)
        mention = format_mention(self.config.get("ping_target", ""))
        log.warning(
            "no results for %s of active tracking — sending inactivity alert (ping=%r)",
            format_uptime(idle), mention,
        )
        self._post_embed(
            build_inactivity_embed(
                idle_seconds=idle,
                uptime_seconds=now,
                last_result=self._last_result_text,
            ),
            content=mention,
        )

    def _apply_session_state(self):
        """Push SessionClock's state to the header dot, label, accent and
        timer. Called on every Start/Pause/Resume/Stop transition."""
        state = self.session.state
        self.status_text.setText(self.STATUS_TEXT[state])
        for widget in (self.header, self.status_dot, self.status_text, self.timer_label):
            _set_style_property(widget, "state", state)
        self._refresh_timer()

    def _check_ping_target(self):
        """Say so in the app when the ping target can't ping. Discord fails
        silently otherwise: the message arrives with the name as plain text."""
        problem = ping_problem(self.ping_input.text())
        _set_style_property(self.ping_hint, "tone", "fail" if problem else "")
        self.ping_hint.setText(
            problem or "Inactivity alert pings that target once if nothing is detected for that long.")

    def _refresh_region_label(self):
        r = self.config["region"]
        self.region_label.setText(f"({r['left']}, {r['top']})  {r['width']}×{r['height']} px")

    def select_region(self):
        self.selector = RegionSelector()
        self.selector.region_selected.connect(self._on_region_selected)
        self.selector.show()

    def _on_region_selected(self, region):
        self.config["region"] = region
        self._refresh_region_label()
        save_config(self.config)

    def test_capture(self):
        region = self.config["region"]
        try:
            with mss.mss() as sct:
                pil_img = grab_region(sct, region)
            text, processed = ocr_image(pil_img)
            report = brightness_report(pil_img)
            if text is None:
                headline = "(frame too noisy to read — see diagnostics)"
            elif not text:
                headline = "(no text recognized)"
            else:
                headline = text
            self.test_output.setPlainText(headline + "\n\n--- diagnostics ---\n" + report)
            pixmap = pil_to_pixmap(processed).scaled(
                self.PREVIEW_W - 2, self.PREVIEW_H - 2,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_label.setPixmap(pixmap)
            log.info("test capture: %r | %s", _one_line(text), _one_line(report))
        except Exception as e:
            self.test_output.setPlainText(f"Error: {e}")
            log.error("test capture error: %s", e)

    # -- Discord -----------------------------------------------------------
    def _post_embed(self, embed, on_done=None, content=None):
        """Fire a webhook POST on a background thread (if a URL is set)."""
        url = self.config.get("webhook_url", "").strip()
        if not url:
            if on_done:
                on_done(False, "No webhook URL set")
            return
        poster = WebhookPoster(url, embed, content)
        if on_done:
            poster.done.connect(on_done)
        poster.finished.connect(lambda p=poster: self._forget_poster(p))
        self._posters.append(poster)
        poster.start()

    def _forget_poster(self, poster):
        if poster in self._posters:
            self._posters.remove(poster)

    def test_webhook(self):
        self._sync_config_from_inputs()
        save_config(self.config)
        if not self.config["webhook_url"].strip():
            self._set_webhook_status(False, "No webhook URL set")
            return
        self.test_webhook_btn.setEnabled(False)
        _set_style_property(self.webhook_status, "tone", "pending")
        self.webhook_status.setText("Sending…")
        self._post_embed(
            build_test_embed(uptime_seconds=self.get_uptime_seconds()),
            self._on_test_webhook_done,
        )

    def _on_test_webhook_done(self, ok, message):
        self.test_webhook_btn.setEnabled(True)
        self._set_webhook_status(ok, message)

    def _set_webhook_status(self, ok, message):
        _set_style_property(self.webhook_status, "tone", "ok" if ok else "fail")
        if ok:
            self.webhook_status.setText(f"Webhook OK ({message})")
        else:
            self.webhook_status.setText(f"Webhook failed: {message}")

    # -- Session -----------------------------------------------------------
    def _sync_config_from_inputs(self):
        self.config["webhook_url"] = self.webhook_input.text()
        self.config["ping_target"] = self.ping_input.text().strip()
        self.config["inactivity_minutes"] = self.inactivity_input.value()

    def start_session(self):
        self._sync_config_from_inputs()
        save_config(self.config)
        self.success_count = 0
        self.fail_count = 0
        self.reward_counts = Counter()
        self._update_counters()
        self._update_rewards_display()

        # Clock first, so anything the worker reports is timed from Start.
        self.session.start()
        self.inactivity = InactivityMonitor(
            float(self.config.get("inactivity_minutes", 0.0)) * 60.0)
        self.inactivity.reset(0.0)
        self._last_result_text = "None yet this session"
        self._apply_session_state()

        self.worker = OCRWorker(self.config)
        self.worker.result.connect(self._on_result)
        self.worker.start()

        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("Pause")
        self.stop_btn.setEnabled(True)
        self.select_region_btn.setEnabled(False)
        self.tabs.setCurrentIndex(0)   # show the counters while running

        log.info("tracker started")
        self._post_embed(build_started_embed(uptime_seconds=self.get_uptime_seconds()))

    def toggle_pause(self):
        if not self.worker:
            return
        if self.session.state == SessionClock.RUNNING:
            self.worker.pause()
            self.session.pause()
            self.pause_btn.setText("Resume")
            log.info("session paused at %s", self.get_uptime_text())
        elif self.session.state == SessionClock.PAUSED:
            self.worker.resume()
            self.session.resume()
            self.pause_btn.setText("Pause")
            log.info("session resumed at %s", self.get_uptime_text())
        self._apply_session_state()

    def stop_session(self):
        if self.worker:
            self.worker.stop()
            self.worker = None
            # A result the worker emitted just before stopping is still queued
            # for this thread. Deliver it now so it lands in the counters and
            # the summary, instead of arriving after the session has ended.
            # Only works because _on_result is a declared @pyqtSlot: see there.
            QApplication.sendPostedEvents(self)

        self.session.stop()
        self._apply_session_state()
        uptime = self.get_uptime_seconds()

        total = self.success_count + self.fail_count
        rate = (self.success_count / total * 100) if total else 0
        reward_block = "\n".join(format_reward_summary(self.reward_counts))
        summary = (
            f"Uptime: {format_uptime(uptime)}\n"
            f"Total tickets used: {total}\n"
            f"Success: {self.success_count}\n"
            f"Fail: {self.fail_count}\n"
            f"Hit rate: {rate:.1f}%\n\n"
            f"Rewards:\n{reward_block}"
        )
        log.info("session summary: %s", _one_line(summary))

        # Post BEFORE the modal dialog. It used to be sent only once the dialog
        # was dismissed, so leaving the dialog open held the summary back.
        self._post_embed(build_summary_embed(
            self.success_count, self.fail_count, self.reward_counts,
            uptime_seconds=uptime,
        ))

        self.start_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("Pause")
        self.stop_btn.setEnabled(False)
        self.select_region_btn.setEnabled(True)

        QMessageBox.information(self, "Session Summary", summary)

    # Declared as a Qt slot on purpose. For a plain Python method PyQt routes
    # the cross-thread signal through a hidden proxy object, so the queued call
    # is not addressed to this window and stop_session's sendPostedEvents(self)
    # can't deliver it. Measured: a result emitted at the instant of Stop
    # reached the summary 0/20 times without the decorator, 20/20 with it.
    @pyqtSlot(str, int, str, str)
    def _on_result(self, kind, qty, reward_name, text):
        if kind == "fail":
            self.fail_count += 1
            self._last_result_text = "Fail"
        elif kind == "success":
            self.success_count += 1
            if reward_name:
                self.reward_counts[reward_name] += max(qty, 1)
            if reward_name and not reward_name.startswith("Unrecognized"):
                self._last_result_text = f"Success · {max(qty, 1)}× {reward_name}"
            else:
                self._last_result_text = "Success · reward not read"
        # A result means things are alive: restart the inactivity count.
        self.inactivity.note_activity(self.get_uptime_seconds())
        self._update_counters()
        self._update_rewards_display()
        self._post_embed(build_event_embed(
            kind, qty, reward_name, self.success_count, self.fail_count,
            uptime_seconds=self.get_uptime_seconds(),
        ))

    def _update_counters(self):
        total = self.success_count + self.fail_count
        rate = (self.success_count / total * 100) if total else 0
        self.success_label.setText(str(self.success_count))
        self.fail_label.setText(str(self.fail_count))
        self.total_label.setText(str(total))
        self.rate_label.setText(f"{rate:.1f}%" if total else "—")

    def _update_rewards_display(self):
        lines = format_reward_summary(self.reward_counts)
        self.rewards_output.setPlainText("\n".join(lines))

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
        # Give any in-flight webhook POST (e.g. the session summary) a moment
        # to finish rather than killing it when the window closes.
        for poster in list(self._posters):
            poster.wait(3000)
        save_config(self.config)
        event.accept()


def main():
    setup_logging()
    log.info("Minigame Tracker starting (Tesseract: %s; settings in %s)",
             TESSERACT_SOURCE, _app_dir())
    app = QApplication(sys.argv)
    icon = _resource("MinigameTracker.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

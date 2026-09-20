"""
Unit tests for the pure logic in tracker.py — the event-detection state
machine and the reward keyword matcher.

These deliberately avoid needing a live screen, a Qt event loop, or even a
working PyQt6 / mss / pytesseract / requests install: the modules tracker.py
imports at the top are stubbed out before the import below, so the tests run
anywhere Python does.

Run:  python test_tracker.py       (no pytest needed)
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------------------
# Stub the GUI / capture / OCR / network dependencies so importing tracker.py
# exercises only its pure logic.
# ---------------------------------------------------------------------------
def _stub_modules():
    def module(name):
        m = types.ModuleType(name)
        sys.modules.setdefault(name, m)
        return sys.modules[name]

    class _Any:
        """Stands in for any attribute access (Qt enums, classes, decorators)."""
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, item):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

    try:
        import PIL.Image  # noqa: F401
    except Exception:
        pil = module("PIL")
        img_mod = module("PIL.Image")
        ops_mod = module("PIL.ImageOps")
        pil.Image = img_mod
        pil.ImageOps = ops_mod

    for name in ("mss", "pytesseract", "requests"):
        try:
            __import__(name)
        except Exception:
            m = module(name)
            m.__getattr__ = lambda item: _Any()  # type: ignore[attr-defined]
            if name == "pytesseract":
                inner = module("pytesseract.pytesseract")
                inner.tesseract_cmd = ""
                m.pytesseract = inner

    # PyQt6 is stubbed unconditionally: even when installed it may fail to load
    # its DLLs in a headless/CI context, and none of it is needed here.
    qt = module("PyQt6")
    for sub, names in (
        ("QtWidgets", ["QApplication", "QWidget", "QLabel", "QPushButton",
                       "QVBoxLayout", "QHBoxLayout", "QLineEdit", "QGroupBox",
                       "QMessageBox", "QFormLayout", "QTextEdit", "QDoubleSpinBox",
                       "QFrame", "QGridLayout", "QSizePolicy", "QTabWidget"]),
        ("QtCore", ["Qt", "QThread", "pyqtSignal", "QRect", "QTimer", "QPointF"]),
        ("QtGui", ["QPainter", "QColor", "QPen", "QFont", "QIcon", "QImage", "QPixmap",
                   "QPolygonF"]),
    ):
        full = f"PyQt6.{sub}"
        m = module(full)
        for n in names:
            setattr(m, n, _Any if n != "pyqtSignal" else (lambda *a, **k: _Any()))
        setattr(qt, sub, m)
    # A decorator must hand the method back unchanged, not swap it for _Any.
    sys.modules["PyQt6.QtCore"].pyqtSlot = lambda *a, **k: (lambda func: func)


_stub_modules()

import tracker  # noqa: E402
from tracker import EventDetector, match_reward  # noqa: E402


FAIL_TEXT = "Minigame\nYou've exited the minigame"
SUCCESS_TEXT = "Minigame\nYou've cleared the minigame!"
REWARD_TEXT = "You've got 2 Boost Module V2!"


class FakeClock:
    """Manual clock so time-dependent behaviour is deterministic."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def feed(detector, readings, clock=None, step=0.2):
    """Feed a sequence of OCR readings, one per poll cycle. Returns all events."""
    events = []
    for text in readings:
        events.extend(detector.process(text))
        if clock is not None:
            clock.advance(step)
    return events


def new_detector(clock, clean_reads=3):
    return EventDetector(clean_reads_required=clean_reads, clock=clock)


def reward_window(detector, clock, reading, polls=25, step=0.2):
    """Hold ONE unchanging reward popup on screen.

    Repeating the same reading is not new evidence, so this never triggers the
    agreement rule. It settles once the readings have stopped changing.
    """
    return feed(detector, [reading] * polls, clock, step=step)


def changing(reading, count):
    """The same reward read `count` times, each reading slightly different.

    Real frames never come out byte-identical while a popup fades, and only a
    CHANGED reading counts as new evidence.
    """
    return [reading + " " + "." * i for i in range(count)]


# ---------------------------------------------------------------------------
# Reward keyword matcher (Bug 2)
# ---------------------------------------------------------------------------
class TestRewardMatcher(unittest.TestCase):
    def test_full_strings_for_every_reward(self):
        cases = [
            ("You've got 1 Normal Summer Random Box!", 1, "Normal Summer Random Box"),
            ("You've got 2 Rare Summer Random Box!", 2, "Rare Summer Random Box"),
            ("You've got 1 Mega Summer Random Box!", 1, "Mega Summer Random Box"),
            ("You've got 3 Boost Module V1!", 3, "Boost Module V1"),
            ("You've got 2 Boost Module V2!", 2, "Boost Module V2"),
        ]
        for text, qty, name in cases:
            with self.subTest(text=text):
                m = match_reward(text.lower())
                self.assertIsNotNone(m, f"no match for {text!r}")
                self.assertEqual(m.name, name)
                self.assertEqual(m.qty, qty)
                self.assertTrue(m.qty_explicit)

    def test_word_wrapped_fragments_still_match(self):
        """The reward line wraps mid-phrase; whichever half is read must still
        identify the reward, because the family words and the qualifier both
        land in the same capture."""
        cases = [
            ("you've got 1 rare summer", "Rare Summer Random Box"),
            ("rare summer\nrandom box!", "Rare Summer Random Box"),
            ("summer random box!\nmega", "Mega Summer Random Box"),
            ("boost module v1", "Boost Module V1"),
            ("you've got 1 boost\nmodule v2!", "Boost Module V2"),
            ("boost module\nv2!", "Boost Module V2"),
            ("normal\nsummer random box!", "Normal Summer Random Box"),
        ]
        for text, name in cases:
            with self.subTest(text=text):
                m = match_reward(text)
                self.assertIsNotNone(m, f"no match for {text!r}")
                self.assertEqual(m.name, name)
                self.assertGreaterEqual(m.qty, 1)

    def test_corrupted_qualifier_still_matches(self):
        """Regression from a real capture: "Rare" read as "Rore" and "Random"
        as "Randam". The reward was real and must not be lost."""
        m = match_reward("minigame\nyou've got] rore summer randam box!")
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "Rare Summer Random Box")
        self.assertEqual(m.qty, 1)
        for text, name in [
            ("you've got 2 mego summer random box!", "Mega Summer Random Box"),
            ("you've got 1 normol summer random box!", "Normal Summer Random Box"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(match_reward(text).name, name)

    def test_qualifier_needs_box_context(self):
        """A qualifier alone is not enough — garbage invents short words."""
        for noise in ["rare", "mega", "a normal day", "cleared the minigame"]:
            with self.subTest(noise=noise):
                self.assertIsNone(match_reward(noise))

    def test_fragment_without_quantity_is_not_explicit(self):
        m = match_reward("rare summer random box!")
        self.assertEqual(m.qty, 1)
        self.assertFalse(m.qty_explicit)

    def test_quantity_above_the_cap_is_a_misread_not_a_quantity(self):
        """The game never awards more than MAX_REWARD_QUANTITY. Real sessions
        read a "1" as "7", and also produced 32 and 9."""
        for text, bad in [
            ("you've got 7 normol summer rondom box!", 7),
            ("you've got 32 rare summer random box!", 32),
            ("you've got 9 boost module v1!", 9),
            ("you've got 5 mega summer random box!", 5),
        ]:
            with self.subTest(text=text):
                m = match_reward(text)
                self.assertIsNotNone(m, "the reward itself must still be recognised")
                self.assertFalse(m.qty_explicit)
                self.assertEqual(m.rejected_qty, bad)
                self.assertEqual(m.qty, 1)

    def test_quantities_up_to_the_cap_are_believable(self):
        """Every reward accepts 1 to 4."""
        self.assertEqual(tracker.MAX_REWARD_QUANTITY, 4)
        for text in ("you've got {n} normal summer random box!",
                     "you've got {n} rare summer random box!",
                     "you've got {n} mega summer random box!",
                     "you've got {n} boost module v1!",
                     "you've got {n} boost module v2!"):
            for n in (1, 2, 3, 4):
                with self.subTest(text=text, n=n):
                    m = match_reward(text.format(n=n))
                    self.assertTrue(m.qty_explicit)
                    self.assertEqual(m.qty, n)
                    self.assertIsNone(m.rejected_qty)

    def test_cap_stays_below_the_known_misread(self):
        """The font's "1" is misread as "7" (335 real readings). Whatever the
        cap is, it must stay below 7 or that misread gets counted."""
        self.assertLess(tracker.MAX_REWARD_QUANTITY, 7)
        for text in ("you've got 7 normal summer random box!",
                     "you've got 7 boost module v1!"):
            with self.subTest(text=text):
                m = match_reward(text)
                self.assertFalse(m.qty_explicit)
                self.assertEqual(m.rejected_qty, 7)

    def test_real_normal_misreads_are_recognised(self):
        """Regression from 756 real successes: the game font makes OCR read
        "Normal" as these. Each was a lost reward before positional matching."""
        for word in ("nonnal", "nonnol", "nonmol", "nonnoal", "normol", "narmol",
                     "nonmal", "nonmaol", "nommal", "narmal", "nonmnal", "nomnal",
                     "mormal", "harmal", "nomol", "norma"):
            with self.subTest(word=word):
                m = match_reward(f"minigame\nyou've got 2 {word} summer rondom box!")
                self.assertIsNotNone(m, word)
                self.assertEqual(m.name, "Normal Summer Random Box")
                self.assertEqual(m.qty, 2)

    def test_real_rare_and_mega_misreads_are_recognised(self):
        for word, name in (("rore", "Rare Summer Random Box"),
                           ("rara", "Rare Summer Random Box"),
                           ("mego", "Mega Summer Random Box"),
                           ("megs", "Mega Summer Random Box")):
            with self.subTest(word=word):
                self.assertEqual(
                    match_reward(f"you've got 1 {word} summer random box!").name, name)

    def test_positional_match_survives_a_wrapped_line(self):
        m = match_reward("minigame\nyou've got 3 nonnol\nsummer rondom box!")
        self.assertEqual(m.name, "Normal Summer Random Box")
        self.assertEqual(m.qty, 3)

    def test_positional_match_does_not_guess_on_garbage(self):
        """A word before 'Summer' that resembles none of the three clearly
        must not be forced into one."""
        for text in ("you've got] homa\nsummed rondon box!",
                     "you've got lipo\nsunes rondon box!",
                     "you've got 2 xyz summer random box!"):
            with self.subTest(text=text):
                self.assertIsNone(match_reward(text), text)

    def test_v1_read_as_vu(self):
        """Real captures read "Boost Module - V1" as "- VU" 46 times."""
        m = match_reward("minigame\nyou've got 1 boost module - vu")
        self.assertIsNotNone(m)
        self.assertEqual(m.name, "Boost Module V1")
        self.assertEqual(m.qty, 1)

    def test_quantity_before_reward_name(self):
        m = match_reward("got\n3 boost module v1!")
        self.assertEqual(m.name, "Boost Module V1")
        self.assertEqual(m.qty, 3)

    def test_ocr_digit_confusion_in_quantity(self):
        """'got l Rare...' — Tesseract reading 1 as a lowercase L."""
        m = match_reward("you've got l rare summer random box!")
        self.assertEqual(m.name, "Rare Summer Random Box")
        self.assertEqual(m.qty, 1)

    def test_ocr_digit_confusion_in_module_version(self):
        """V1 misread as 'Vl' / V2 misread as 'VZ' still map correctly."""
        self.assertEqual(match_reward("boost module vl").name, "Boost Module V1")
        self.assertEqual(match_reward("boost module vi").name, "Boost Module V1")
        self.assertEqual(match_reward("boost module vz").name, "Boost Module V2")

    def test_garbled_youve_is_not_a_boost_module(self):
        """Regression, seen in a real capture: OCR split "You've" into
        "You'v i", which matched as V1 and inflated that bucket on fails."""
        real_capture = (
            "qast qorred / wenn / wn fes neens, 27 / we apaito am / "
            "inigame / ea / m / you'v i / e exited the "
        )
        self.assertIsNone(match_reward(real_capture))

    def test_v1_v2_need_boost_or_module_context(self):
        """Two-character keywords are cheap for OCR noise to invent, so they
        only count when the reading also shows what they belong to."""
        for noise in ["ea / m / v i", "wn fes v l neens", "vz", "a v2 b"]:
            with self.subTest(noise=noise):
                self.assertIsNone(match_reward(noise))
        # ...but a genuine reward reading still matches.
        self.assertEqual(match_reward("boost module\nv i").name, "Boost Module V1")
        self.assertEqual(match_reward("you've got 2 boost module v2!").name, "Boost Module V2")

    def test_apostrophe_before_v_is_rejected(self):
        for text in ["you'v i", "it'v l", "you'v2"]:
            with self.subTest(text=text):
                self.assertIsNone(match_reward(text + " boost module"))

    def test_notification_text_is_not_a_reward(self):
        """Near-miss noise must not be matched. 'cleared' contains 'eare',
        which a fuzzy matcher would happily call 'rare'."""
        for text in [
            "minigame\nyou've cleared the minigame!",
            "minigame\nyou've exited the minigame",
            "",
            "   ",
            "random unrelated chat text",
            "declared winner",
            "rarely",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(match_reward(text), f"false match on {text!r}")

    def test_all_reward_names_are_reachable(self):
        """Every reward the UI can display must be producible by the matcher,
        so a typo in one list can't silently create a dead bucket."""
        produced = set()
        for text in [
            "you've got 1 normal summer random box!",
            "you've got 1 rare summer random box!",
            "you've got 1 mega summer random box!",
            "you've got 1 boost module v1!",
            "you've got 1 boost module v2!",
        ]:
            m = match_reward(text)
            self.assertIsNotNone(m, text)
            produced.add(m.name)
        self.assertEqual(produced, set(tracker.REWARD_NAMES))


# ---------------------------------------------------------------------------
# Event detection state machine (Bug 1)
# ---------------------------------------------------------------------------
class TestEventDetector(unittest.TestCase):
    def test_clean_single_fail(self):
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [""] * 3 + [FAIL_TEXT] + [""] * 5, clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "fail")

    def test_clean_single_success_with_reward(self):
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [""] * 2 + [SUCCESS_TEXT] * 5, clock)
        events += reward_window(d, clock, SUCCESS_TEXT + "\n" + REWARD_TEXT)
        events += feed(d, [""] * 5, clock)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, "success")
        self.assertEqual(ev.qty, 2)
        self.assertEqual(ev.reward_name, "Boost Module V2")

    def test_fade_with_blips_does_not_duplicate(self):
        """The exact shape of Bug 1: the notification fades, OCR drops it for
        a frame or two mid-fade, then reads it again. Must stay ONE event."""
        clock = FakeClock()
        d = new_detector(clock)
        readings = (
            [FAIL_TEXT] * 4      # notification up
            + [""]               # blip
            + [FAIL_TEXT] * 3    # still there, fading
            + ["", ""]           # two clean reads — not enough to release
            + [FAIL_TEXT] * 2    # blip again: re-seen
            + ["", "", "", ""]   # finally gone
        )
        events = feed(d, readings, clock)
        self.assertEqual(len(events), 1, [e.kind for e in events])
        self.assertEqual(d.state, EventDetector.IDLE)

    def test_garbled_blip_midfade_does_not_duplicate(self):
        """A garbled (non-empty, unclassifiable) read is also a clean read,
        but a run of them mixed with re-reads must not produce a duplicate."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 3, clock)
        events += reward_window(d, clock, SUCCESS_TEXT + "\n" + REWARD_TEXT)
        events += feed(d, ["m1n1gam3", "", "wm.", SUCCESS_TEXT, "", ""], clock)
        events += feed(d, [SUCCESS_TEXT] * 2 + [""] * 6, clock)
        self.assertEqual(len(events), 1, [(e.kind, e.reward_name) for e in events])

    def test_two_separate_events_far_apart(self):
        """Two genuine results minutes apart MUST both be counted."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [FAIL_TEXT] * 5 + [""] * 5, clock)
        self.assertEqual(len(events), 1)

        clock.advance(180)  # a real minigame cycle: minutes
        events += feed(d, [""] * 3 + [FAIL_TEXT] * 5 + [""] * 5, clock)
        self.assertEqual(len(events), 2, [e.kind for e in events])
        self.assertTrue(all(e.kind == "fail" for e in events))

    def test_two_separate_successes_far_apart(self):
        clock = FakeClock()
        d = new_detector(clock)
        def one_run():
            out = feed(d, [SUCCESS_TEXT] * 3, clock)
            out += reward_window(d, clock, "you've got 1 rare summer random box!")
            return out + feed(d, [""] * 6, clock)

        events = one_run()
        clock.advance(200)
        events += one_run()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.reward_name == "Rare Summer Random Box" for e in events))
        self.assertTrue(all(e.qty == 1 for e in events))

    def test_repeated_reward_reads_count_once(self):
        """The 2x Boost Module V2 case that logged +4: the reward popup stays
        on screen across many polls and must be counted exactly once."""
        clock = FakeClock()
        d = new_detector(clock)
        readings = (
            [SUCCESS_TEXT] * 3
            + [SUCCESS_TEXT + "\n" + REWARD_TEXT] * 12   # reward popup lingers
            + [REWARD_TEXT] * 6                          # success box gone first
            + [""] * 6
        )
        events = feed(d, readings, clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].qty, 2)
        self.assertEqual(events[0].reward_name, "Boost Module V2")

    def test_wrapped_reward_during_pending_still_matches(self):
        """Word-wrapped reward line: the quantity is on the half that got cut
        off, so it falls back to 1 once the reward wait elapses."""
        clock = FakeClock()
        d = new_detector(clock)
        wrapped = SUCCESS_TEXT + "\nrare summer\nrandom box!"
        events = feed(d, [SUCCESS_TEXT] * 2 + [wrapped] * 2, clock)
        self.assertEqual(events, [])
        clock.advance(6)  # reward wait elapses
        events = feed(d, [wrapped], clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reward_name, "Rare Summer Random Box")
        self.assertEqual(events[0].qty, 1)

    def test_fuller_reward_read_beats_provisional_fragment(self):
        """A fragment seen first, then a complete line: use the complete one."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT, SUCCESS_TEXT + "\nboost module v1"], clock)
        self.assertEqual(events, [])
        events = reward_window(d, clock, SUCCESS_TEXT + "\nyou've got 3 boost module v1!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reward_name, "Boost Module V1")
        self.assertEqual(events[0].qty, 3)

    def test_success_without_reward_notification(self):
        """Rare case: the reward popup never appears. Must not block forever."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 3, clock)
        self.assertEqual(events, [])
        clock.advance(tracker.REWARD_WAIT_MAX_SECONDS + 1)
        events = feed(d, [SUCCESS_TEXT], clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "success")
        self.assertTrue(events[0].reward_name.startswith("Unrecognized"))

    def test_single_clean_read_does_not_release_guard(self):
        clock = FakeClock()
        d = new_detector(clock, clean_reads=3)
        feed(d, [FAIL_TEXT], clock)
        self.assertEqual(d.state, EventDetector.AWAITING_CLEAR)
        feed(d, [""], clock)
        self.assertEqual(d.state, EventDetector.AWAITING_CLEAR)
        feed(d, [""], clock)
        self.assertEqual(d.state, EventDetector.AWAITING_CLEAR)
        feed(d, [""], clock)
        self.assertEqual(d.state, EventDetector.IDLE)

    def test_clean_read_counter_resets_on_reappearance(self):
        clock = FakeClock()
        d = new_detector(clock, clean_reads=3)
        feed(d, [FAIL_TEXT, "", ""], clock)
        self.assertEqual(d.state, EventDetector.AWAITING_CLEAR)
        feed(d, [FAIL_TEXT], clock)       # blip over: counter must reset
        feed(d, ["", ""], clock)          # only 2 clean reads again
        self.assertEqual(d.state, EventDetector.AWAITING_CLEAR)
        feed(d, [""], clock)
        self.assertEqual(d.state, EventDetector.IDLE)

    def test_min_gap_suppresses_an_early_release(self):
        """Belt-and-braces: even if the guard released early, a second read
        of the same notification seconds later is not counted."""
        clock = FakeClock()
        d = new_detector(clock, clean_reads=1)  # deliberately too lax
        events = feed(d, [FAIL_TEXT] + [""] * 2, clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(d.state, EventDetector.IDLE)
        clock.advance(2)  # 2s later — far too soon to be a real second run
        events += feed(d, [FAIL_TEXT], clock)
        self.assertEqual(len(events), 1, "duplicate slipped past the min-gap guard")

    def test_shipped_constant_tolerates_a_multi_frame_blip(self):
        """Same as above but using the value tracker.py actually ships, so the
        constant can't be lowered to an unsafe number without a test failing."""
        clock = FakeClock()
        d = EventDetector(clock=clock)  # shipped clean-read count
        self.assertGreaterEqual(tracker.CLEAN_READS_REQUIRED, 3)
        blip = [""] * (tracker.CLEAN_READS_REQUIRED - 1)
        readings = (
            [FAIL_TEXT] * 4
            + blip + [FAIL_TEXT] * 2      # longest tolerable blip, then re-seen
            + blip + [FAIL_TEXT]          # and again
            + [""] * (tracker.CLEAN_READS_REQUIRED + 2)
        )
        events = feed(d, readings, clock)
        self.assertEqual(len(events), 1, [e.kind for e in events])
        self.assertEqual(d.state, EventDetector.IDLE)

    def test_shipped_constant_still_allows_a_later_real_event(self):
        clock = FakeClock()
        d = EventDetector(clock=clock)
        events = feed(d, [FAIL_TEXT] * 4 + [""] * (tracker.CLEAN_READS_REQUIRED + 2), clock)
        clock.advance(180)
        events += feed(d, [SUCCESS_TEXT] * 3, clock)
        events += reward_window(d, clock, SUCCESS_TEXT + "\n" + REWARD_TEXT)
        self.assertEqual([e.kind for e in events], ["fail", "success"])

    def test_minority_quantity_misread_loses_the_vote(self):
        """Regression from a real session: one reading said "got 7 Mega" and
        the next said "got 1 Mega", and the 7 was logged. The digit is the
        least reliable character on screen, so the majority wins."""
        clock = FakeClock()
        d = new_detector(clock)
        good = "minigame\nyou've got 1 mega summer random box!"
        bad = "minigame\nyou've got 7 megs summer rondom box!"
        events = feed(d, [SUCCESS_TEXT] * 2, clock)
        # The bad reading arrives FIRST, as it did live.
        events += feed(d, [bad] + [good] * 24, clock)
        events += feed(d, [good], clock)
        self.assertEqual(len(events), 1, [(e.qty, e.reward_name) for e in events])
        self.assertEqual(events[0].reward_name, "Mega Summer Random Box")
        self.assertEqual(events[0].qty, 1, "the single misread digit won the vote")

    def test_unanimous_quantity_is_kept(self):
        """Voting must not flatten genuine multi-item rewards to 1."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 2, clock)
        events += reward_window(d, clock, "minigame\nyou've got 3 boost module v1!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].qty, 3)
        self.assertEqual(events[0].reward_name, "Boost Module V1")

    def test_lone_impossible_quantity_falls_back_to_one(self):
        """Regression from the log at 22:14: the only reading with a number
        said "got 7 Normol". It must not become seven boxes."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 20, clock)
        events += reward_window(d, clock, "minigame\nyou've got 7 normol summer rondom box!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reward_name, "Normal Summer Random Box")
        self.assertEqual(events[0].qty, 1)

    def test_vote_window_stays_open_after_a_late_first_reward_reading(self):
        """The popup appeared ~1.5s before the window would close, and the
        readable frames only came afterwards. Those later frames must vote."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 18, clock)  # first reward read at ~3.6s
        events += feed(d, ["minigame\nyou've got 7 normol summer rondom box!"] * 9, clock)
        self.assertEqual(events, [], "decided before the minimum vote time had passed")
        events += feed(d, ["minigame\nyou've got 2 normal summer random box!"] * 12, clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].qty, 2, "the readable later frames were not counted")

    def test_genuine_three_beats_a_misread_seven(self):
        """The cap must not flatten real multiples: 3 is possible, 7 is not."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 2, clock)
        events += feed(d, ["minigame\nyou've got 7 rare summer random box!"] * 3, clock)
        events += reward_window(d, clock, "minigame\nyou've got 3 rare summer random box!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reward_name, "Rare Summer Random Box")
        self.assertEqual(events[0].qty, 3)

    def test_slow_reward_popup_is_still_caught(self):
        """The first readable reward frame came as late as 10.7s after
        "cleared" in real sessions. Even with a 3s reward wait setting, a
        reward that shows up at ~9s must be counted."""
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 45, clock)         # 9s, nothing readable
        self.assertEqual(events, [], "gave up before the popup appeared")
        events += reward_window(d, clock, "minigame\nyou've got 2 nonnol summer rondom box!")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reward_name, "Normal Summer Random Box")
        self.assertEqual(events[0].qty, 2)

    def test_gives_up_after_the_maximum_wait(self):
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [SUCCESS_TEXT] * 55, clock)          # 11s: still hoping
        self.assertEqual(events, [])
        clock.advance(2)                                      # past 12s
        events = feed(d, [SUCCESS_TEXT], clock)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].reward_name.startswith("Unrecognized"))

    def test_own_discord_message_in_the_region_is_ignored(self):
        """Regression: Discord was visible in the OCR region and showed our own
        "Minigame cleared!" report. That must never count as a result."""
        clock = FakeClock()
        d = new_detector(clock)
        own = ("hee mp ee\n1x normal summer 07:08:13\nrandom box\n"
               "session so far: 192 success - 53 fail\nminigame cleared!\n"
               "reward uptime\n2x rare summer random box")
        events = feed(d, [own] * 40, clock)
        self.assertEqual(events, [])
        self.assertEqual(d.state, EventDetector.IDLE)

    def test_own_discord_message_does_not_release_the_guard(self):
        clock = FakeClock()
        d = new_detector(clock, clean_reads=3)
        feed(d, [FAIL_TEXT], clock)
        feed(d, ["session so far: 3 success - 1 fail"] * 10, clock)
        self.assertEqual(d.state, EventDetector.AWAITING_CLEAR,
                         "a Discord frame was counted as a clean read")

    def test_agreement_settles_the_reward_quickly(self):
        """Three readings that agree end it: no waiting on a clock."""
        clock = FakeClock()
        d = new_detector(clock)
        feed(d, [SUCCESS_TEXT], clock)
        started = clock.t
        events = feed(d, changing("minigame\nyou've got 2 rare summer random box!", 3), clock)
        self.assertEqual(len(events), 1, "did not settle on three agreeing readings")
        self.assertEqual((events[0].reward_name, events[0].qty), ("Rare Summer Random Box", 2))
        self.assertLess(clock.t - started, 1.0, "took too long for three agreeing readings")

    def test_the_same_frame_repeated_is_not_agreement(self):
        """The capture loop repeats the last reading while the screen is
        unchanged. Counting that as agreement would prove nothing, so an
        unchanging popup settles on time instead."""
        clock = FakeClock()
        d = new_detector(clock)
        feed(d, [SUCCESS_TEXT], clock)
        reading = "minigame\nyou've got 2 rare summer random box!"
        events = feed(d, [reading] * 5, clock)          # 1s of the identical frame
        self.assertEqual(events, [], "the same frame counted as agreement")
        events = feed(d, [reading] * 8, clock)          # past the settle time
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].qty, 2)

    def test_a_vanished_popup_settles_it(self):
        clock = FakeClock()
        d = new_detector(clock)
        feed(d, [SUCCESS_TEXT], clock)
        feed(d, ["minigame\nyou've got 1 mega summer random box!"], clock)
        events = feed(d, [SUCCESS_TEXT] * tracker.REWARD_GONE_READS, clock)
        self.assertEqual(len(events), 1, "popup disappeared but nothing was reported")
        self.assertEqual(events[0].reward_name, "Mega Summer Random Box")

    def test_a_misread_frame_cannot_win_by_repeating(self):
        """One bad reading repeated must not beat the readings that agree."""
        clock = FakeClock()
        d = new_detector(clock)
        feed(d, [SUCCESS_TEXT], clock)
        feed(d, ["minigame\nyou've got 3 rare summer random box!"] * 6, clock)
        events = feed(d, changing("minigame\nyou've got 1 rare summer random box!", 3), clock)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].qty, 1, "the repeated frame won")

    def test_reward_text_alone_while_idle_is_not_an_event(self):
        clock = FakeClock()
        d = new_detector(clock)
        events = feed(d, [REWARD_TEXT] * 3, clock)
        self.assertEqual(events, [])

    def test_realistic_long_session(self):
        """Fail, success+reward, fail — with fades and blips throughout."""
        clock = FakeClock()
        d = new_detector(clock)
        events = []

        events += feed(d, [""] * 5, clock)
        events += feed(d, [FAIL_TEXT] * 6 + ["", FAIL_TEXT, "", "", ""] + [""] * 3, clock)
        clock.advance(240)

        events += feed(d, [""] * 4, clock)
        events += feed(d, [SUCCESS_TEXT] * 8, clock)
        events += reward_window(
            d, clock, SUCCESS_TEXT + "\nyou've got 1 mega summer random box!")
        events += feed(d, ["", "you've got 1 mega summer random box!", "", "", "", ""], clock)
        clock.advance(260)

        events += feed(d, [""] * 4 + [FAIL_TEXT] * 7 + ["", "", FAIL_TEXT, "", "", "", ""], clock)

        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["fail", "success", "fail"], kinds)
        self.assertEqual(events[1].reward_name, "Mega Summer Random Box")
        self.assertEqual(events[1].qty, 1)


# ---------------------------------------------------------------------------
# Discord embed payload shape
# ---------------------------------------------------------------------------
def _fields(embed):
    return {f["name"]: f["value"] for f in embed.get("fields", [])}


class TestSessionClock(unittest.TestCase):
    """Active-tracking-time semantics: pause stops the count, resume carries
    on from the same value, stop freezes, start resets."""

    def setUp(self):
        self.clock = FakeClock()
        self.s = tracker.SessionClock(clock=self.clock)

    def test_idle_reads_zero(self):
        self.assertEqual(self.s.state, tracker.SessionClock.IDLE)
        self.assertEqual(self.s.elapsed_seconds(), 0)

    def test_runs_while_running(self):
        self.s.start()
        self.clock.advance(5)
        self.assertAlmostEqual(self.s.elapsed_seconds(), 5)
        self.assertEqual(self.s.state, tracker.SessionClock.RUNNING)

    def test_pause_stops_counting_and_resume_continues(self):
        self.s.start()
        self.clock.advance(5)
        self.s.pause()
        self.clock.advance(100)          # long pause must not count
        self.assertAlmostEqual(self.s.elapsed_seconds(), 5)
        self.assertEqual(self.s.state, tracker.SessionClock.PAUSED)
        self.s.resume()
        self.clock.advance(3)
        self.assertAlmostEqual(self.s.elapsed_seconds(), 8)

    def test_stop_freezes_final_value(self):
        self.s.start()
        self.clock.advance(42)
        self.s.stop()
        self.clock.advance(1000)
        self.assertAlmostEqual(self.s.elapsed_seconds(), 42)
        self.assertEqual(self.s.state, tracker.SessionClock.STOPPED)

    def test_stop_while_paused_keeps_banked_time(self):
        self.s.start()
        self.clock.advance(7)
        self.s.pause()
        self.clock.advance(50)
        self.s.stop()
        self.assertAlmostEqual(self.s.elapsed_seconds(), 7)

    def test_start_resets(self):
        self.s.start()
        self.clock.advance(30)
        self.s.stop()
        self.s.start()
        self.assertAlmostEqual(self.s.elapsed_seconds(), 0)
        self.clock.advance(2)
        self.assertAlmostEqual(self.s.elapsed_seconds(), 2)

    def test_repeated_pause_resume_never_double_counts(self):
        self.s.start()
        for _ in range(4):
            self.clock.advance(1)
            self.s.pause()
            self.s.pause()               # redundant pause is a no-op
            self.clock.advance(9)
            self.s.resume()
            self.s.resume()              # redundant resume is a no-op
        self.assertAlmostEqual(self.s.elapsed_seconds(), 4)

    def test_pause_and_resume_do_nothing_when_idle_or_stopped(self):
        self.s.pause()
        self.s.resume()
        self.assertEqual(self.s.state, tracker.SessionClock.IDLE)
        self.s.start()
        self.clock.advance(3)
        self.s.stop()
        self.s.resume()
        self.clock.advance(10)
        self.assertEqual(self.s.state, tracker.SessionClock.STOPPED)
        self.assertAlmostEqual(self.s.elapsed_seconds(), 3)

    def test_format_uptime(self):
        cases = [
            (0, "00:00:00"), (0.99, "00:00:00"), (59.9, "00:00:59"),
            (60, "00:01:00"), (3661, "01:01:01"), (86399, "23:59:59"),
            (360000, "100:00:00"), (-5, "00:00:00"),
        ]
        for seconds, text in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(tracker.format_uptime(seconds), text)


class TestInactivityMonitor(unittest.TestCase):
    """Time is ACTIVE tracking seconds, so a pause can never look like a stall."""

    def test_disabled_when_interval_is_zero(self):
        m = tracker.InactivityMonitor(0)
        self.assertFalse(m.check(10_000))

    def test_does_not_fire_before_the_threshold(self):
        m = tracker.InactivityMonitor(600)
        for t in (0, 100, 599, 599.9):
            self.assertFalse(m.check(t), t)

    def test_fires_once_at_the_threshold(self):
        m = tracker.InactivityMonitor(600)
        self.assertTrue(m.check(600))
        for t in (601, 900, 5_000):
            self.assertFalse(m.check(t), "alert repeated without any result")

    def test_a_result_re_arms_it(self):
        m = tracker.InactivityMonitor(600)
        self.assertTrue(m.check(600))
        m.note_activity(700)
        self.assertFalse(m.check(1_200), "fired before a fresh full interval")
        self.assertTrue(m.check(1_300), "did not re-arm after a result")

    def test_a_result_restarts_the_count(self):
        m = tracker.InactivityMonitor(600)
        for t in (100, 200, 300):
            m.note_activity(t)
            self.assertFalse(m.check(t + 599))
        self.assertTrue(m.check(900))

    def test_idle_seconds(self):
        m = tracker.InactivityMonitor(600)
        m.note_activity(120)
        self.assertEqual(m.idle_seconds(300), 180)
        self.assertEqual(m.idle_seconds(60), 0, "never negative")


class TestMentionFormatting(unittest.TestCase):
    def test_bare_id_becomes_a_user_mention(self):
        self.assertEqual(tracker.format_mention("123456789012345678"),
                         "<@123456789012345678>")

    def test_role_forms(self):
        for raw in ("role:123456789012345678", "role 123456789012345678",
                    "&123456789012345678"):
            with self.subTest(raw=raw):
                self.assertEqual(tracker.format_mention(raw), "<@&123456789012345678>")

    def test_everyone_and_here(self):
        for raw in ("@everyone", "everyone", "EVERYONE"):
            with self.subTest(raw=raw):
                self.assertEqual(tracker.format_mention(raw), "@everyone")
        for raw in ("@here", "here", "Here"):
            with self.subTest(raw=raw):
                self.assertEqual(tracker.format_mention(raw), "@here")

    def test_already_formed_mentions_pass_through(self):
        for raw in ("<@123456789012345678>", "<@!123456789012345678>",
                    "<@&123456789012345678>"):
            with self.subTest(raw=raw):
                self.assertEqual(tracker.format_mention(raw), raw)

    def test_blank_means_no_ping(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(tracker.format_mention(raw), "")

    def test_unrecognised_text_is_left_visible(self):
        """A display name cannot ping on Discord. Passing it through means the
        user can SEE that it did nothing, instead of it vanishing silently."""
        self.assertEqual(tracker.format_mention("@SomeUser"), "@SomeUser")


class TestPingTargets(unittest.TestCase):
    """Regression: a ping target of "mcorecxre" arrived in Discord as plain
    grey text and notified nobody, with nothing in the app to say why."""

    def test_a_name_is_reported_as_unusable(self):
        for raw in ("mcorecxre", "@mcorecxre", "Miko#1234", "some name"):
            with self.subTest(raw=raw):
                problem = tracker.ping_problem(raw)
                self.assertIsNotNone(problem, raw)
                self.assertIn("ID", problem)

    def test_usable_targets_report_no_problem(self):
        for raw in ("", "   ", None, "123456789012345678", "<@123456789012345678>",
                    "<@&123456789012345678>", "role:123456789012345678",
                    "@everyone", "@here", "here"):
            with self.subTest(raw=raw):
                self.assertIsNone(tracker.ping_problem(raw))

    def test_allowed_mentions_name_the_exact_target(self):
        """Without this a role ping only works if the role is mentionable."""
        self.assertEqual(tracker.allowed_mentions_for("<@&123456789012345678>"),
                         {"parse": [], "roles": ["123456789012345678"]})
        self.assertEqual(tracker.allowed_mentions_for("<@123456789012345678>"),
                         {"parse": [], "users": ["123456789012345678"]})
        self.assertEqual(tracker.allowed_mentions_for("<@!123456789012345678>"),
                         {"parse": [], "users": ["123456789012345678"]})
        self.assertEqual(tracker.allowed_mentions_for("@everyone"), {"parse": ["everyone"]})
        self.assertEqual(tracker.allowed_mentions_for("@here"), {"parse": ["everyone"]})

    def test_nothing_is_pingable_by_default(self):
        """A stray "@everyone" inside OCR text must never fire a mass ping."""
        for raw in ("", None, "mcorecxre", "@everyone is here"):
            with self.subTest(raw=raw):
                self.assertEqual(tracker.allowed_mentions_for(raw), {"parse": []})


class TestWebhookPayload(unittest.TestCase):
    class _Response:
        status_code, text, ok = 204, "", True

    def _capture(self, **kwargs):
        sent = {}

        def fake_post(url, json=None, timeout=None):
            sent["url"], sent["json"] = url, json
            return self._Response()

        original = tracker.requests.post
        tracker.requests.post = fake_post
        try:
            ok, _ = tracker.post_webhook(
                "https://example.invalid/hook", {"title": "x"}, **kwargs)
        finally:
            tracker.requests.post = original
        self.assertTrue(ok)
        return sent["json"]

    def test_ping_goes_in_content_not_the_embed(self):
        """A mention inside an embed notifies nobody on Discord."""
        payload = self._capture(content="<@123456789012345678>")
        self.assertEqual(payload["content"], "<@123456789012345678>")
        self.assertEqual(payload["embeds"], [{"title": "x"}])
        self.assertEqual(payload["allowed_mentions"],
                         {"parse": [], "users": ["123456789012345678"]})

    def test_a_role_ping_is_allowed_explicitly(self):
        payload = self._capture(content="<@&987654321098765432>")
        self.assertEqual(payload["allowed_mentions"],
                         {"parse": [], "roles": ["987654321098765432"]})

    def test_no_content_key_without_a_ping(self):
        for kwargs in ({}, {"content": ""}, {"content": None}):
            with self.subTest(kwargs=kwargs):
                self.assertNotIn("content", self._capture(**kwargs))


class TestEmbeds(unittest.TestCase):
    def test_success_embed_shows_this_runs_reward_only(self):
        e = tracker.build_event_embed("success", 2, "Boost Module V2", 5, 3, uptime_seconds=75)
        self.assertEqual(e["color"], tracker.COLOR_SUCCESS)
        self.assertEqual(e["fields"][0]["name"], "Reward")
        self.assertIn("2", e["fields"][0]["value"])
        self.assertIn("Boost Module V2", e["fields"][0]["value"])

    def test_fail_embed_is_red_with_no_reward_field(self):
        e = tracker.build_event_embed("fail", 0, "", 5, 4, uptime_seconds=75)
        self.assertEqual(e["color"], tracker.COLOR_FAIL)
        self.assertNotIn("Reward", _fields(e))

    def test_every_notification_carries_uptime(self):
        """Uptime must be visible on every message, in the header's format."""
        embeds = {
            "started": tracker.build_started_embed(uptime_seconds=0),
            "success": tracker.build_event_embed(
                "success", 1, "Rare Summer Random Box", 1, 0, uptime_seconds=3725),
            "fail": tracker.build_event_embed("fail", 0, "", 1, 1, uptime_seconds=3725),
            "summary": tracker.build_summary_embed(1, 1, {}, uptime_seconds=3725),
            "test": tracker.build_test_embed(uptime_seconds=3725),
            "inactivity": tracker.build_inactivity_embed(
                idle_seconds=900, uptime_seconds=3725, last_result="Fail"),
        }
        for name, embed in embeds.items():
            with self.subTest(embed=name):
                self.assertIn("Uptime", _fields(embed))
        self.assertIn("00:00:00", _fields(embeds["started"])["Uptime"])
        for name in ("success", "fail", "summary", "test", "inactivity"):
            self.assertIn("01:02:05", _fields(embeds[name])["Uptime"])

    def test_uptime_is_required_on_every_builder(self):
        """Keyword-only and required, so a new notification can't quietly
        ship without deciding what uptime it reports."""
        with self.assertRaises(TypeError):
            tracker.build_started_embed()
        with self.assertRaises(TypeError):
            tracker.build_event_embed("fail", 0, "", 0, 1)
        with self.assertRaises(TypeError):
            tracker.build_summary_embed(0, 0, {})
        with self.assertRaises(TypeError):
            tracker.build_test_embed()
        with self.assertRaises(TypeError):
            tracker.build_inactivity_embed(idle_seconds=1, last_result="x")

    def test_inactivity_embed_is_an_alert_not_a_result(self):
        e = tracker.build_inactivity_embed(
            idle_seconds=1_800, uptime_seconds=3_600, last_result="Success · 1× Mega")
        self.assertEqual(e["color"], tracker.COLOR_ALERT)
        self.assertNotIn(e["color"], (tracker.COLOR_SUCCESS, tracker.COLOR_FAIL))
        fields = _fields(e)
        self.assertIn("00:30:00", fields["Inactive for"])
        self.assertIn("01:00:00", fields["Uptime"])
        self.assertEqual(fields["Last result"], "Success · 1× Mega")

    def test_started_embed_is_neutral_not_a_result(self):
        e = tracker.build_started_embed(uptime_seconds=0)
        self.assertEqual(e["title"], "Tracker started")
        self.assertNotIn(e["color"], (tracker.COLOR_SUCCESS, tracker.COLOR_FAIL))

    def test_summary_embed_has_all_totals(self):
        counts = {"Boost Module V1": 3, "Rare Summer Random Box": 1}
        e = tracker.build_summary_embed(3, 1, counts, uptime_seconds=5400)
        values = {f["name"]: f["value"] for f in e["fields"]}
        for expected in ["Tickets", "Cleared", "Failed", "Hit rate", "Uptime", "Rewards"]:
            self.assertIn(expected, values)
        self.assertEqual(values["Tickets"], "`4`")
        self.assertEqual(values["Hit rate"], "`75.0%`")
        self.assertIn("01:30:00", values["Uptime"])
        self.assertIn("Boost Module V1", values["Rewards"])
        # Every known reward is listed even at zero
        for name in tracker.REWARD_NAMES:
            self.assertIn(name, values["Rewards"])

    def _all_embeds(self):
        return {
            "started": tracker.build_started_embed(uptime_seconds=0),
            "cleared": tracker.build_event_embed(
                "success", 2, "Rare Summer Random Box", 4, 1, uptime_seconds=600),
            "cleared, reward not read": tracker.build_event_embed(
                "success", 0, "Unrecognized: (no reward text seen)", 4, 1, uptime_seconds=600),
            "failed": tracker.build_event_embed("fail", 0, "", 4, 2, uptime_seconds=600),
            "summary": tracker.build_summary_embed(4, 2, {}, uptime_seconds=600),
            "inactivity": tracker.build_inactivity_embed(
                idle_seconds=900, uptime_seconds=600, last_result="Fail"),
            "webhook test": tracker.build_test_embed(uptime_seconds=0),
        }

    def test_every_message_carries_the_credit(self):
        self.assertIn("mcorecxre", tracker.CREDIT)
        for name, e in self._all_embeds().items():
            with self.subTest(embed=name):
                self.assertEqual(e["footer"]["text"], tracker.CREDIT)
                self.assertIn("timestamp", e)

    def test_colours_match_the_app_palette(self):
        """Discord and the window use the same colours for the same meaning."""
        hexa = lambda key: int(tracker.PALETTE[key].lstrip("#"), 16)
        self.assertEqual(tracker.COLOR_SUCCESS, hexa("green"))
        self.assertEqual(tracker.COLOR_FAIL, hexa("red"))
        self.assertEqual(tracker.COLOR_ALERT, hexa("amber"))
        self.assertEqual(tracker.COLOR_STARTED, hexa("accent"))

    def test_the_tracker_still_recognises_its_own_messages(self):
        """If Discord sits inside the OCR region, the tracker must know these
        are its own messages. "Minigame cleared" alone would otherwise look like
        a real result, so the redesign must keep the markers the guard uses."""
        for name, e in self._all_embeds().items():
            parts = [e.get("title", ""), e.get("description", "")]
            for f in e["fields"]:
                parts += [f["name"], f["value"]]
            parts.append(e["footer"]["text"])
            text = "\n".join(parts).lower()
            with self.subTest(embed=name):
                self.assertTrue(tracker._is_own_discord_message(text), text)

    def test_reward_list_is_one_aligned_column(self):
        lines = tracker.format_reward_summary({"Normal Summer Random Box": 350,
                                                "Boost Module V2": 7})
        self.assertEqual(len(lines), len(tracker.REWARD_NAMES))
        self.assertEqual(len({len(line) for line in lines}), 1, lines)
        self.assertTrue(lines[0].endswith("350"))
        self.assertTrue(lines[1].rstrip().endswith("0"))


class TestSettingsThatWereRemoved(unittest.TestCase):
    def test_no_reward_wait_or_poll_interval_settings(self):
        """Both were replaced: the reward is settled by agreement, and the
        poll rate is fixed. Old config files still load."""
        self.assertNotIn("reward_wait_seconds", tracker.DEFAULT_CONFIG)
        self.assertNotIn("poll_interval", tracker.DEFAULT_CONFIG)
        self.assertEqual(tracker.POLL_INTERVAL_SECONDS, 0.20)

    def test_an_old_config_still_loads(self):
        import json
        import tempfile
        old = {"region": {"top": 1, "left": 2, "width": 3, "height": 4},
               "poll_interval": 0.5, "reward_wait_seconds": 3.0,
               "webhook_url": "https://example.invalid/hook"}
        path = Path(tempfile.mkdtemp()) / "minigame_tracker_config.json"
        path.write_text(json.dumps(old), encoding="utf-8")
        saved = tracker.CONFIG_PATH
        tracker.CONFIG_PATH = path
        try:
            cfg = tracker.load_config()
        finally:
            tracker.CONFIG_PATH = saved
        self.assertEqual(cfg["region"], old["region"])
        self.assertEqual(cfg["webhook_url"], old["webhook_url"])
        self.assertEqual(cfg["inactivity_minutes"], 0.0)   # new key gets its default


class TestPackagedPaths(unittest.TestCase):
    """The exe runs from a temporary folder deleted on exit, so it must keep
    settings next to itself and use the Tesseract it carries."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self._saved = {k: getattr(sys, k, None) for k in ("frozen", "_MEIPASS", "executable")}
        self._cmd = tracker.pytesseract.pytesseract.tesseract_cmd
        self._env = tracker.os.environ.get("TESSDATA_PREFIX")

    def tearDown(self):
        import shutil
        for k, v in self._saved.items():
            if v is None and hasattr(sys, k):
                delattr(sys, k)
            elif v is not None:
                setattr(sys, k, v)
        tracker.pytesseract.pytesseract.tesseract_cmd = self._cmd
        if self._env is None:
            tracker.os.environ.pop("TESSDATA_PREFIX", None)
        else:
            tracker.os.environ["TESSDATA_PREFIX"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_settings_live_next_to_the_exe(self):
        exe = self.tmp / "tester folder" / "MinigameTracker.exe"
        exe.parent.mkdir()
        exe.write_bytes(b"")
        sys.frozen = True
        sys.executable = str(exe)
        self.assertEqual(tracker._app_dir(), exe.parent.resolve())

    def test_script_keeps_settings_next_to_the_script(self):
        if hasattr(sys, "frozen"):
            del sys.frozen
        self.assertEqual(tracker._app_dir(), Path(tracker.__file__).resolve().parent)

    def test_packaged_exe_uses_its_own_tesseract(self):
        bundle = self.tmp / "_MEI1234"
        (bundle / "tesseract" / "tessdata").mkdir(parents=True)
        (bundle / "tesseract" / "tesseract.exe").write_bytes(b"")
        sys.frozen = True
        sys._MEIPASS = str(bundle)
        source = tracker._configure_tesseract()
        self.assertTrue(source.startswith("bundled"), source)
        self.assertEqual(tracker.pytesseract.pytesseract.tesseract_cmd,
                         str(bundle / "tesseract" / "tesseract.exe"))
        self.assertEqual(tracker.os.environ["TESSDATA_PREFIX"],
                         str(bundle / "tesseract" / "tessdata"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

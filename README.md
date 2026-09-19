# Minigame Tracker

Watches the Roblox notification corner during the Sol's RNG summer minigame
and counts cleared and failed runs and the rewards each one gave. It reports
each result, and a session summary, to a Discord webhook.

By mcorecxre.

## Running the exe

1. Put `MinigameTracker.exe` in **its own folder**, for example on the
   Desktop. It saves its settings (`minigame_tracker_config.json`) and its
   log (`minigame_tracker.log`) next to itself, so avoid `Program Files`.
2. Double-click it. Nothing else needs installing: Python, the window toolkit
   and the Tesseract OCR engine are all inside the exe.
3. The exe isn't code-signed, so Windows may show "Windows protected your PC"
   the first time. Click **More info**, then **Run anyway**.
4. First launch takes a few seconds while it unpacks itself.

In the app:

1. **Setup** tab: **Select OCR Region** and drag a box over the corner where
   the minigame notifications appear. Make it tall enough for the result
   notification AND the reward notification that stacks with it.
2. Optional: paste a Discord webhook URL and press **Test Webhook**.
3. **Test Capture** tab: with a notification on screen, press **Test
   Capture** to see exactly what the tracker reads.
4. Press **Start**.

Keep Discord out of the OCR region. The tracker ignores its own Discord
messages, but it's cleaner not to capture them at all.

## Running from source

```
pip install PyQt6 mss pytesseract requests pillow
python tracker.py
```

Tesseract must be installed: https://github.com/UB-Mannheim/tesseract/wiki

Tests (pure logic, no screen or Tesseract needed):

```
python test_tracker.py
```

## Rebuilding the exe

One-time setup, from this folder:

```
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install PyQt6 mss pytesseract requests pillow pyinstaller
```

Tesseract must be installed in `C:\Program Files\Tesseract-OCR` (or set
`TESSERACT_DIR` to its folder). The build copies only what `tesseract.exe`
actually needs, plus English language data.

Then, for every rebuild:

```
.venv\Scripts\pyinstaller tracker.spec
```

The result is `dist\MinigameTracker.exe` with `dist\NOTICE.txt` beside it.
The `build` folder is scratch space and can be deleted.

## Licenses

`NOTICE.txt` lists every bundled component and its license, with the full
Apache 2.0 (Tesseract) and BSD 2-Clause (Leptonica) texts. Ship it alongside
the exe.

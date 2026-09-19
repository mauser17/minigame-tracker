# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build recipe for MinigameTracker.exe.
#
#     pyinstaller tracker.spec
#
# Produces dist\MinigameTracker.exe: one file, no console window, with Python,
# PyQt6 and Tesseract (English only) all inside it, plus dist\NOTICE.txt.
# See README.md for the one-time setup.
#
# Tesseract is read from the normal install folder. Set TESSERACT_DIR to use
# another one.

import os
import shutil
from pathlib import Path

from PyInstaller.depend import bindepend

HERE = Path(SPECPATH)
TESSERACT_DIR = Path(os.environ.get("TESSERACT_DIR", r"C:\Program Files\Tesseract-OCR"))

if not (TESSERACT_DIR / "tesseract.exe").exists():
    raise SystemExit(
        f"tesseract.exe not found in {TESSERACT_DIR}. Install Tesseract "
        "(https://github.com/UB-Mannheim/tesseract/wiki) or set TESSERACT_DIR."
    )
if not (TESSERACT_DIR / "tessdata" / "eng.traineddata").exists():
    raise SystemExit(f"eng.traineddata missing from {TESSERACT_DIR / 'tessdata'}.")


def tesseract_files():
    """tesseract.exe plus exactly the DLLs it loads, found by walking its
    imports, and the English language data only.

    The Tesseract folder holds 50+ DLLs, many only for its training tools
    (a 30 MB Unicode library among them). Walking the real import chain keeps
    the exe lean, and keeps working when a Tesseract update changes the set.
    """
    local = {p.name.lower(): p for p in TESSERACT_DIR.glob("*.dll")}
    needed, queue = {}, [TESSERACT_DIR / "tesseract.exe"]
    while queue:
        for item in bindepend.get_imports(str(queue.pop())):
            name = os.path.basename(str(item[0] if isinstance(item, (tuple, list)) else item)).lower()
            if name in local and name not in needed:
                needed[name] = local[name]
                queue.append(local[name])
    files = [(str(TESSERACT_DIR / "tesseract.exe"), "tesseract")]
    files += [(str(p), "tesseract") for p in sorted(needed.values())]
    files.append((str(TESSERACT_DIR / "tessdata" / "eng.traineddata"), "tesseract/tessdata"))
    print(f"[tracker.spec] bundling tesseract.exe + {len(needed)} DLLs + eng.traineddata")
    return files


a = Analysis(
    [str(HERE / "tracker.py")],
    pathex=[str(HERE)],
    binaries=[],
    # Shipped as plain files (not analysed as binaries), so they land together
    # in one "tesseract" folder where tesseract.exe finds its own DLLs.
    datas=tesseract_files() + [
        (str(HERE / "NOTICE.txt"), "."),
        (str(HERE / "MinigameTracker.ico"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # Never imported by the tracker. pytesseract only *tries* numpy/pandas and
    # works without them, so leaving them out saves a lot of size.
    excludes=["tkinter", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MinigameTracker",
    icon=str(HERE / "MinigameTracker.ico"),
    console=False,      # a GUI app: no console window alongside it
    upx=False,          # UPX-packed exes trip antivirus false positives more often
    debug=False,
    strip=False,
    runtime_tmpdir=None,
)

# The license notice travels beside the exe as well as inside it.
shutil.copy2(HERE / "NOTICE.txt", Path(DISTPATH) / "NOTICE.txt")

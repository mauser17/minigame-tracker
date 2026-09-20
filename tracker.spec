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
    # works without them, so leaving them out saves a lot of size. The Pillow
    # entries are image formats the tracker never opens; Pillow loads its
    # format plugins in a try/except, so their absence is simply ignored.
    excludes=[
        "tkinter", "numpy", "pandas",
        "PIL.AvifImagePlugin", "PIL.ImageQt", "PIL.ImageTk", "PIL.ImageShow",
    ],
    noarchive=False,
)

# ---------------------------------------------------------------- slimming
# Qt ships a lot this app never touches. Measured on the first build: these
# accounted for ~36 MB of 167 MB before compression.
#   opengl32sw.dll  Qt's software 3D renderer, for OpenGL/Quick; a widgets app
#                   painted by the raster engine never loads it
#   Qt6Pdf          no PDF anywhere in the app
#   Qt6Network      networking goes through Python's requests, not Qt
#   Qt6Svg + the svg/jpeg/webp/tiff image plugins
#                   the only images are PNG (the spin-box arrows) and the .ico
UNUSED_QT = {
    "opengl32sw.dll", "qt6pdf.dll", "qt6network.dll", "qt6svg.dll",
    "qjpeg.dll", "qwebp.dll", "qtiff.dll", "qsvg.dll", "qsvgicon.dll",
    "qpdf.dll",
}


def _is_unused_qt(dest):
    return os.path.basename(dest).lower() in UNUSED_QT


def _is_qt_translation(dest):
    parts = dest.replace("\\", "/").lower().split("/")
    return "translations" in parts and parts[-1].endswith(".qm")


def _is_duplicate_tesseract(dest, source):
    """A Tesseract file that PyInstaller also copied to the TOP LEVEL.

    PyInstaller analyses the Tesseract DLLs we ship as data and adds its own
    copy of each one at the root, so every file ended up in the exe twice
    (26 MB wasted). Only the root copies are dropped: the ones under
    "tesseract/" are what tesseract.exe loads, and removing those leaves the
    exe with no OCR engine at all, silently falling back to a Tesseract
    installed on the user's PC. Python's own OpenSSL DLLs are named
    differently and come from elsewhere, so they are never touched.
    """
    if os.path.dirname(dest):          # keep anything inside a folder
        return False
    try:
        return os.path.commonpath([os.path.abspath(source), str(TESSERACT_DIR)]) == str(TESSERACT_DIR)
    except ValueError:                 # different drives
        return False


before = len(a.binaries) + len(a.datas)
a.binaries = [b for b in a.binaries
              if not _is_unused_qt(b[0]) and not _is_duplicate_tesseract(b[0], b[1])]
a.datas = [d for d in a.datas if not _is_unused_qt(d[0]) and not _is_qt_translation(d[0])]
print(f"[tracker.spec] slimming: dropped {before - len(a.binaries) - len(a.datas)} files "
      "(duplicated Tesseract DLLs, unused Qt modules, Qt translations)")

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

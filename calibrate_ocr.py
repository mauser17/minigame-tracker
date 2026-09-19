"""
OCR calibration helper — TEMPORARY diagnostic tool, not part of the app.

Run it, then trigger a minigame notification. It samples your configured OCR
region for a while, keeps the frames that look like they contain a
notification, and writes them to a calibration folder. It then sweeps a range
of preprocessing settings against the best frame and prints what Tesseract
reads for each, so the right threshold can be chosen from evidence instead of
guesswork.

Run:
    python calibrate_ocr.py            # samples for 60 seconds
    python calibrate_ocr.py 120        # samples for 120 seconds

Nothing is sent anywhere. Everything is written to ./ocr_calibration/.
"""

import sys
import time
from pathlib import Path

import mss
import pytesseract
from PIL import Image, ImageDraw, ImageOps

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

OUT = HERE / "ocr_calibration"
CONFIG_PATH = HERE / "minigame_tracker_config.json"

_default_tesseract = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if _default_tesseract.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_default_tesseract)


def load_region():
    import json
    data = json.loads(CONFIG_PATH.read_text())
    return data["region"]


def grab(sct, region):
    img = sct.grab(region)
    return Image.frombytes("RGB", img.size, img.rgb)


def brightness_histogram(gray):
    return gray.histogram()[:256]


def otsu_threshold(gray):
    """Classic Otsu: pick the level that best separates the two brightness
    populations. No numpy needed."""
    hist = brightness_histogram(gray)
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best_var, best_t = -1.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def binarize(gray, threshold, text_is_bright=True, upscale=2):
    g = gray
    if upscale > 1:
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        g = g.resize((g.width * upscale, g.height * upscale), resampling)
    if text_is_bright:
        return g.point(lambda p: 0 if p >= threshold else 255)
    return g.point(lambda p: 0 if p <= threshold else 255)


def score_frame(gray):
    """How much bright, text-like content is present. Notification text is the
    brightest thing in an otherwise dark corner."""
    hist = brightness_histogram(gray)
    bright = sum(hist[180:])
    very_bright = sum(hist[230:])
    return bright + very_bright * 2


def ocr(img):
    try:
        return pytesseract.image_to_string(img).strip()
    except Exception as e:
        return f"<OCR error: {e}>"


def label_strip(width, text, height=26):
    strip = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(strip)
    d.text((6, 6), text, fill=0)
    return strip


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    region = load_region()
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()

    print(f"Region: {region}")
    print(f"Sampling for {duration:.0f}s at 2 Hz — go trigger a minigame notification now.")
    print("(A fail counts too. Anything that puts a notification in the corner.)\n")

    frames = []
    deadline = time.time() + duration
    with mss.mss() as sct:
        baseline = score_frame(ImageOps.grayscale(grab(sct, region)))
        last_report = 0.0
        while time.time() < deadline:
            img = grab(sct, region)
            gray = ImageOps.grayscale(img)
            s = score_frame(gray)
            frames.append((s, img))
            now = time.time()
            if now - last_report >= 5:
                remaining = deadline - now
                best = max(f[0] for f in frames)
                print(f"  {remaining:4.0f}s left | current score {s:8d} | best so far {best:8d} | idle baseline {baseline:8d}")
                last_report = now
            time.sleep(0.5)

    frames.sort(key=lambda f: f[0], reverse=True)
    top = frames[:3]
    if not top or top[0][0] <= baseline * 1.05:
        print("\nNo frame looked brighter than the idle screen.")
        print("Either no notification appeared, or the region is not over the notification stack.")
        print("Saving the brightest frame anyway so the region itself can be checked.")

    for i, (s, img) in enumerate(top):
        img.save(OUT / f"frame{i}_raw.png")
        print(f"\nSaved frame{i}_raw.png (score {s})")

    best_img = top[0][1]
    gray = ImageOps.grayscale(best_img)
    auto = otsu_threshold(gray)
    hist = brightness_histogram(gray)
    total = sum(hist)
    print(f"\nBrightness profile of the best frame (otsu suggests {auto}):")
    for lo in range(0, 256, 32):
        share = sum(hist[lo:lo + 32]) / total * 100
        print(f"  {lo:3d}-{lo+31:3d} | {'#' * int(share / 2):<50} {share:5.1f}%")

    print("\n=== Preprocessing sweep on the best frame ===")
    variants = []
    for bright in (True, False):
        for t in (auto, 100, 130, 150, 170, 190, 210):
            variants.append((t, bright))
    seen = set()
    results = []
    for t, bright in variants:
        key = (t, bright)
        if key in seen:
            continue
        seen.add(key)
        proc = binarize(gray, t, text_is_bright=bright, upscale=2)
        text = ocr(proc)
        flat = " / ".join(l.strip() for l in text.splitlines() if l.strip())
        polarity = "bright=text" if bright else "dark=text"
        tag = f"t{t:03d}_{'bright' if bright else 'dark'}"
        proc.save(OUT / f"sweep_{tag}.png")
        hit = "minigame" in flat.lower()
        results.append((hit, t, polarity, flat))
        print(f"  thr {t:3d} {polarity:11s} {'HIT ' if hit else '    '} -> {flat[:90]!r}")

    print("\n=== Best candidates (read 'minigame' correctly) ===")
    hits = [r for r in results if r[0]]
    if hits:
        for _, t, polarity, flat in hits:
            print(f"  threshold {t}, {polarity}: {flat[:120]!r}")
    else:
        print("  None. Send me the frame*_raw.png files and I'll tune from the pixels.")

    print(f"\nAll images written to: {OUT}")
    print("Send me frame0_raw.png (and the sweep output above) and I'll set the constants.")


if __name__ == "__main__":
    main()

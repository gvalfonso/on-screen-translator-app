"""On-screen translator: Windows OCR -> Google translate -> click-through overlay.

Usage: python translator.py [target_lang] [ocr_lang] [monitor]
  e.g. python translator.py en ja     (needs the Japanese OCR pack installed)
       python translator.py en th 1   (Thai via Tesseract: winget install UB-Mannheim.TesseractOCR,
                                       models in ./tessdata; slow, so one monitor is best)
Ctrl+C in the console to quit.
"""
import asyncio, ctypes, difflib, functools, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
import ctypes.wintypes
import tkinter as tk

import mss, requests
from langdetect import detect, DetectorFactory
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
from winrt.windows.storage.streams import DataWriter
import pytesseract
from PIL import Image

TARGET = sys.argv[1] if len(sys.argv) > 1 else "en"
OCR_LANG = sys.argv[2] if len(sys.argv) > 2 else None
MONITOR = int(sys.argv[3]) if len(sys.argv) > 3 else 0  # 0 = all screens, 1/2/.. = one screen
INTERVAL = 0.1  # seconds between scans

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # tk + mss both in physical pixels

cache = {}       # text -> translation, or None if already in TARGET / failed
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"cache_{TARGET}.json")


def save_cache():
    data = {k: v for k, v in list(cache.items()) if v}  # only real translations; "not foreign" is cheap to redo
    with open(CACHE_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)
    os.replace(CACHE_FILE + ".tmp", CACHE_FILE)  # atomic: a crash mid-write never corrupts the cache
    return len(data)
pending = set()
drawn = []       # latest frame to draw: [(x, y, w, h, line_h, translation)]
detected = []    # untranslated blocks for feedback outlines: [(x, y, w, h, "pending" | "seen")]
OVERLAY_ALPHA = 0.85  # 1.0 = solid boxes, lower = more see-through
MIN_FONT_PX = 9  # fit-to-block never shrinks text below this; the box grows instead
CLICK_TO_TRANSLATE = True  # only Ctrl+Shift+clicked blocks get translated; False = translate all foreign text
frame_blocks = []  # latest OCR blocks, for click hit-testing
requested = set()  # texts the user clicked
SHOW_DETECTED = True# grey outline on every block OCR found (not foreign); False to hide
DetectorFactory.seed = 0  # deterministic detection
pool = ThreadPoolExecutor(2)  # free endpoint rate-limits; keep it gentle


def google_gtx(text):
    r = requests.get("https://translate.googleapis.com/translate_a/single", timeout=5,
                     params={"client": "gtx", "sl": "auto", "tl": TARGET, "dt": "t", "q": text})
    r.raise_for_status()
    j = r.json()
    return "".join(seg[0] for seg in j[0] if seg[0]), j[2]


def google_clients5(text):
    r = requests.get("https://clients5.google.com/translate_a/t", timeout=5,
                     params={"client": "dict-chrome-ex", "sl": "auto", "tl": TARGET, "q": text})
    r.raise_for_status()
    out, src = r.json()[0]
    return out, src


def mymemory(text):  # needs a source language; anonymous quota ~5000 chars/day
    src = detect(text)[:2]
    r = requests.get("https://api.mymemory.translated.net/get", timeout=5,
                     params={"q": text, "langpair": f"{src}|{TARGET}"})
    r.raise_for_status()
    j = r.json()
    if j.get("quotaFinished") or j.get("responseStatus") != 200:
        raise RuntimeError(f"mymemory: {j.get('responseDetails')}")
    return j["responseData"]["translatedText"], src


OLLAMA_MODEL = "gemma3:12b"  # local LLM via Ollama (ollama pull gemma3:12b); fixes OCR errors, gets slang
OLLAMA_PROMPT = (
    "You translate text captured from a screen by OCR into the language with ISO code '{target}'. "
    "The text may have OCR errors: missing or wrong accents/tone marks, stray symbols like | » ~, "
    "merged or split words. Silently fix obvious OCR errors, then translate naturally, keeping tone and slang. "
    "Never invent meaning that isn't there. Reply as JSON: source_lang = ISO 639-1 code of the language "
    "the text is mostly written in (judge by the script and words, even if garbled; 'none' if it is "
    "meaningless garbage or code), translation = the translation."
)
OLLAMA_SCHEMA = {"type": "object", "required": ["source_lang", "translation"],
                 "properties": {"source_lang": {"type": "string"}, "translation": {"type": "string"}}}


def ollama(text):
    r = requests.post("http://127.0.0.1:11434/api/chat", timeout=30, json={
        "model": OLLAMA_MODEL, "stream": False, "keep_alive": "30m", "options": {"temperature": 0},
        "format": OLLAMA_SCHEMA,  # structured output: the model picks the language, code decides to skip
        "messages": [{"role": "system", "content": OLLAMA_PROMPT.format(target=TARGET)},
                     {"role": "user", "content": text}]})
    r.raise_for_status()
    j = json.loads(r.json()["message"]["content"])
    src = j["source_lang"].lower()[:2]
    if j["source_lang"].lower() == "none" or not j["translation"].strip():
        return text, TARGET  # garbage -> cached as "no overlay"
    return j["translation"].strip(), src


PROVIDERS = [ollama, google_gtx, google_clients5, mymemory]  # tried in order; failing ones benched
cooldown = {}  # provider -> time it may be used again
COOLDOWN = 30  # seconds a failing provider is benched


def translate(text):
    try:
        while True:
            ready = [p for p in PROVIDERS if cooldown.get(p, 0) <= time.time()]
            if ready:
                break
            time.sleep(min(cooldown.values()) - time.time())  # all benched: wait for the first
        for p in ready:
            try:
                out, src = p(re.sub(r"(.)\1{2,}", r"\1\1", text))  # "เน้นนนนน"/"sooooo" -> 2 repeats; stops MT looping
                if len(out) > 3 * len(text) + 20:  # degenerate output ("Nnnnnn...") -> cap it
                    out = out[:3 * len(text) + 20] + "…"
            except Exception as e:
                print(f"\n{p.__name__} failed, benched {COOLDOWN}s:", str(e)[:80])
                cooldown[p] = time.time() + COOLDOWN
                continue
            cache[text] = None if src[:2] == TARGET[:2] or out.strip() == text.strip() else out
            if cache[text]:
                print(f"\n[{src} via {p.__name__}] {text[:60]!r} -> {out[:60]!r}")
            return
        # every ready provider failed: not cached -> retried on a later frame
    finally:
        pending.discard(text)


def wordiness(text):
    """Share of space-separated tokens that are real words (letters only, 2+ chars, edge punctuation
    ignored). OCR noise like '�r gra -IE -s;' scores low; real sentences score high."""
    toks = text.split()
    return sum(len(w := t.strip(".,!?;:'\"()¿¡«»“”…")) >= 2 and w.isalpha() for t in toks) / max(1, len(toks))


def is_target_lang(text):
    """Offline check so text already in TARGET never hits the API."""
    try:
        return detect(text)[:2] == TARGET[:2]
    except Exception:  # no letters to judge
        return True


def same_block(text):
    """A known foreign text this is just an OCR re-read of (moving/scrolling block), or None."""
    known = [k for k, v in cache.items() if v] + list(pending)
    # ponytail: linear scan over foreign texts only; index by first word if that list gets huge
    match = difflib.get_close_matches(text, known, n=1, cutoff=0.85)
    return match[0] if match else None


CJK = "\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uff00-\uffef"  # Thai, kana, kanji/hanzi, full-width
CJK_CHAR = re.compile(f"[{CJK}]")
CJK_GAP = re.compile(f"(?<=[{CJK}]) (?=[{CJK}])")  # OCR puts spaces between these; the scripts don't

# Languages Windows OCR can't read -> Tesseract (models in ./tessdata). Add more: {"lo": "lao+eng", ...}
TESS_LANGS = {"th": "tha+eng"}
USE_TESS = (OCR_LANG or "")[:2] in TESS_LANGS
# font px = OCR line height * this. Measured: Windows OCR line h == font px; Tesseract Thai boxes
# are taller (stacked vowel/tone marks), ~0.85 at caption sizes. Tune if overlays look too big/small.
FONT_SCALE = 0.85 if USE_TESS else 1.0
TESS_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
TESS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")


async def windows_lines(engine, img):
    """Windows OCR -> [(x, y, w, h, text)] per line."""
    dw = DataWriter()
    dw.write_bytes(img.raw)
    bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)
    out = []
    for line in (await engine.recognize_async(bmp)).lines:
        rs = [w.bounding_rect for w in line.words]
        x0, y0 = min(r.x for r in rs), min(r.y for r in rs)
        x1, y1 = max(r.x + r.width for r in rs), max(r.y + r.height for r in rs)
        out.append((x0, y0, x1 - x0, y1 - y0, CJK_GAP.sub("", line.text)))
    return out


def tesseract_lines(img):
    """Tesseract OCR -> [(x, y, w, h, text)] per line. Slower than Windows OCR (~0.5-2s/screen)."""
    pytesseract.pytesseract.tesseract_cmd = TESS_EXE
    d = pytesseract.image_to_data(Image.frombytes("RGB", img.size, img.rgb), lang=TESS_LANGS[OCR_LANG[:2]],
                                  config=f'--tessdata-dir "{TESS_DIR}"', output_type=pytesseract.Output.DICT)
    lines = {}
    for i, word in enumerate(d["text"]):
        if word.strip() and float(d["conf"][i]) > 50:  # drop low-confidence junk (icons, borders)
            key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
            lines.setdefault(key, []).append((d["left"][i], d["top"][i], d["width"][i], d["height"][i], word))
    out = []
    for ws in lines.values():
        for seg in split_gaps(sorted(ws)):
            x0, y0 = min(w[0] for w in seg), min(w[1] for w in seg)
            x1, y1 = max(w[0] + w[2] for w in seg), max(w[1] + w[3] for w in seg)
            out.append((x0, y0, x1 - x0, y1 - y0, CJK_GAP.sub("", " ".join(w[4] for w in seg))))
    return out


def split_gaps(words):
    """Tesseract chains words across the whole screen into one 'line'; cut it where the
    horizontal gap is wider than ~1.5x the text height (Windows OCR does this itself)."""
    h = max(w[3] for w in words)
    segs = [[words[0]]]
    for w in words[1:]:
        prev = segs[-1][-1]
        if w[0] - (prev[0] + prev[2]) > h * 1.5:
            segs.append([])
        segs[-1].append(w)
    return segs


def group_lines(lines):
    """Merge OCR lines [(x, y, w, h, text)] into paragraph blocks."""
    out = []
    for x, y, w, h, t in sorted(lines, key=lambda l: (l[1], l[0])):
        for b in out:
            bx, by, bw, bh, lh, bt = b
            near = 0 <= y - (by + bh) < lh * 0.8 and abs(h - lh) < lh * 0.4
            overlap = x < bx + bw and bx < x + w
            if near and overlap:
                nx, ny = min(bx, x), by
                b[:] = [nx, ny, max(bx + bw, x + w) - nx, y + h - ny, lh, bt + " " + t]
                break
        else:
            out.append([x, y, w, h, h, t])
    return merge_overlaps(out)


def merge_overlaps(blocks, pad=4):
    """Blocks whose boxes overlap (within pad px) are one block; text joined top-to-bottom, left-to-right.
    ponytail: O(n^2) per pass, fine for ~100s of blocks/frame."""
    blocks = sorted(blocks, key=lambda b: (b[1], b[0]))
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(blocks):
            for b in blocks[i + 1:]:
                if (a[0] - pad < b[0] + b[2] and b[0] - pad < a[0] + a[2]
                        and a[1] - pad < b[1] + b[3] and b[1] - pad < a[1] + a[3]):
                    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
                    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
                    a[:] = [x0, y0, x1 - x0, y1 - y0, min(a[4], b[4]), a[5] + " " + b[5]]  # a sorts first
                    blocks.remove(b)
                    merged = True
                    break
            if merged:
                break
    return blocks


def resolve(frame, prev):
    """Pick a translation per block. While OCR's re-read variant of a block is still
    translating, keep the overlay that was drawn at that spot last frame (stops flicker)."""
    out = []
    for x, y, w, h, lh, t in frame:
        tr = cache.get(t)
        if t not in cache:
            tr = next((p[5] for p in prev if p[0] < x + w and x < p[0] + p[2] and p[1] < y + h and y < p[1] + p[3]), None)
        if tr:
            out.append((x, y, w, h, lh, tr))
    return out


async def scan_loop():
    use_tess = USE_TESS
    engine = None if use_tess else (OcrEngine.try_create_from_language(Language(OCR_LANG)) if OCR_LANG
                                    else OcrEngine.try_create_from_user_profile_languages())
    if engine is None and not use_tess:
        sys.exit(f"No OCR engine for '{OCR_LANG}'. Install it: Settings > Language > add language (with OCR).")
    cjk = (OCR_LANG or "")[:2] in ("ja", "zh", "th")  # scripts without spaces between words
    global drawn, detected, frame_blocks
    saved, last_save = None, time.time()
    with mss.mss() as sct:
        mon = sct.monitors[MONITOR]
        while True:
            t0 = time.perf_counter()
            img = sct.grab(mon)
            if use_tess:
                frame = group_lines(await asyncio.to_thread(tesseract_lines, img))
            else:
                frame = group_lines(await windows_lines(engine, img))
            unique = {b[5] for b in frame}  # same text in several places = 1 block
            frame_blocks = frame  # tk thread hit-tests clicks against this
            for t in unique:
                if t in cache or t in pending:
                    continue
                if t in requested:  # user clicked it: translate, skip the "is it foreign?" guessing
                    pending.add(t)
                    pool.submit(translate, t)
                    continue
                if CLICK_TO_TRANSLATE:
                    # not clicked: only reuse a translation if this is a re-read of a known block (moving text)
                    # ponytail: fuzzy match per unclicked block per frame; memoize if scans get slow
                    if (m := same_block(t)) and m in cache:
                        cache[t] = cache[m]
                    continue
                too_short = sum(c.isalpha() for c in t) < 3 or (len(t.split()) < 3 and not cjk)
                wrong_script = cjk and not CJK_CHAR.search(t)  # th/ja/zh mode: misread Latin UI text is junk
                noisy = not CJK_CHAR.search(t) and wordiness(t) < 0.6  # OCR noise; LLMs "translate" it into fiction
                if too_short or wrong_script or noisy or is_target_lang(t):
                    # ponytail: <3 words skipped (detection is noise on UI labels like "Run"); lingua-py if foreign labels matter
                    cache[t] = None
                elif m := same_block(t):
                    if m in cache:
                        cache[t] = cache[m]  # reuse, no API call
                    # else: original still translating; resolve() keeps old overlay meanwhile
                else:
                    pending.add(t)
                    pool.submit(translate, t)
            drawn = resolve(frame, drawn)  # swap whole list; tk thread reads it
            detected = [(x, y, w, h, "pending" if t in pending or (t in requested and t not in cache) else "seen")
                        for x, y, w, h, _, t in frame if not cache.get(t)]
            shown = len(drawn)
            print(f"\rscan {(time.perf_counter() - t0) * 1000:4.0f}ms | {len(unique)} blocks | "
                  f"{shown} overlaid | {len(cache)} cached | {len(pending)} translating   ", end="", flush=True)
            if time.time() - last_save > 5:
                n = sum(1 for v in list(cache.values()) if v)
                if n != saved:
                    saved = save_cache()
                last_save = time.time()
            await asyncio.sleep(max(0, INTERVAL - (time.perf_counter() - t0)))


def make_overlay():
    root = tk.Tk()
    with mss.mss() as s:
        m = s.monitors[MONITOR]
    root.geometry(f"{m['width']}x{m['height']}+{m['left']}+{m['top']}")
    root.overrideredirect(True)
    root.attributes("-topmost", True, "-transparentcolor", "#010101", "-alpha", OVERLAY_ALPHA)
    canvas = tk.Canvas(root, bg="#010101", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    root.update()
    u32 = ctypes.windll.user32
    hwnd = u32.GetParent(root.winfo_id())
    u32.SetWindowLongW(hwnd, -20, u32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x20)  # layered + click-through
    if not u32.SetWindowDisplayAffinity(hwnd, 0x11):  # hide overlay from capture so we don't OCR ourselves
        print("WARNING: can't hide overlay from capture (needs Windows 10 2004+); expect flicker")

    probe_canvas = tk.Canvas(root)  # never packed: just for measuring wrapped text
    probe = probe_canvas.create_text(0, 0, anchor="nw")

    @functools.lru_cache(maxsize=4096)
    def fit_px(text, w, h, px):
        """Biggest font <= px (the original's size) whose wrapped text fits the w x h block.
        A line's box is ~1.33x font px, so allow one line of slack beyond h."""
        lo, hi = MIN_FONT_PX, max(MIN_FONT_PX, px)
        while lo < hi:  # binary search on font size
            mid = (lo + hi + 1) // 2
            probe_canvas.itemconfig(probe, text=text, width=w, font=("Segoe UI", -mid))
            x0, y0, x1, y1 = probe_canvas.bbox(probe)
            if y1 - y0 <= h + mid * 0.35 and x1 - x0 <= w + 4:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def redraw():
        canvas.delete("all")
        for x, y, w, h, state in detected:
            if state == "pending":
                canvas.create_rectangle(x - 2, y - 2, x + w + 2, y + h + 2, outline="#ff8c00", width=2)
                canvas.create_text(x, y - 3, text="translating…", anchor="sw", font=("Segoe UI", 9, "bold"), fill="#ff8c00")
            elif SHOW_DETECTED:
                canvas.create_rectangle(x - 1, y - 1, x + w + 1, y + h + 1, outline="#909090")
        for x, y, w, h, lh, tr in drawn:  # OCR coords == canvas coords (same monitor rect)
            px = fit_px(tr, max(w, 40), h, round(lh * FONT_SCALE))
            txt = canvas.create_text(x, y, text=tr, anchor="nw", width=max(w, 40),
                                     font=("Segoe UI", -px), fill="black")
            bx0, by0, bx1, by1 = canvas.bbox(txt)  # background covers original block AND wrapped translation
            bg = canvas.create_rectangle(min(x, bx0) - 2, min(y, by0) - 2, max(x + w, bx1) + 2, max(y + h, by1) + 2,
                                         fill="#fffbe6", outline="")
            canvas.tag_lower(bg, txt)
        u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13)  # re-assert topmost (no move/size/activate)
        root.after(50, redraw)

    # Ctrl+Shift+click detection. The overlay is click-through, so poll global key state instead of
    # receiving the click. ponytail: the click still reaches the app underneath; a WH_MOUSE_LL hook could eat it.
    pt, was_down = ctypes.wintypes.POINT(), [False]

    def poll_click():
        key = lambda vk: u32.GetAsyncKeyState(vk) & 0x8000
        down = bool(key(0x01) and key(0x11) and key(0x10))  # left button + Ctrl + Shift
        if down and not was_down[0]:
            u32.GetCursorPos(ctypes.byref(pt))
            t = block_at(frame_blocks, pt.x - m["left"], pt.y - m["top"])
            if t:
                requested.add(t)
                print(f"\nclicked: {t[:60]!r}")
        was_down[0] = down
        root.after(15, poll_click)

    redraw()
    if CLICK_TO_TRANSLATE:
        poll_click()
    return root


def block_at(blocks, x, y, pad=6):
    """Text of the smallest block containing (x, y), or None."""
    hits = [b for b in blocks if b[0] - pad <= x <= b[0] + b[2] + pad and b[1] - pad <= y <= b[1] + b[3] + pad]
    return min(hits, key=lambda b: b[2] * b[3])[5] if hits else None


if __name__ == "__main__":
    if "--test" in sys.argv:
        g = group_lines([(10, 10, 100, 20, "hello"), (10, 34, 90, 20, "world"), (500, 10, 50, 20, "far")])
        assert [b[5] for b in g] == ["hello world", "far"], g
        assert g[0][:4] == [10, 10, 100, 44], g
        TARGET = "en"
        assert is_target_lang("Show working tree status") and not is_target_lang("Hola amigos, como estan hoy?")
        cache.update({"hola": "hello", "hi": None})
        prev = resolve([[0, 0, 50, 20, 20, "hola"], [0, 50, 50, 20, 20, "hi"]], [])
        assert prev == [(0, 0, 50, 20, 20, "hello")], prev                       # cached hit; English skipped
        assert resolve([[3, 2, 50, 20, 20, "h0la"]], prev)[0][5] == "hello"      # OCR variant keeps old overlay
        assert resolve([[300, 300, 50, 20, 20, "h0la"]], prev) == []             # no overlap -> nothing yet
        cache["Hola amigos, como estan hoy?"] = "Hello friends, how are you today?"
        assert same_block("Hola amigos, zc6mo estan hoy?") == "Hola amigos, como estan hoy?"
        assert same_block("El gato duerme en la casa") is None
        def limited(text): raise RuntimeError("429")
        def works(text): return "the cat", "es"
        PROVIDERS[:] = [limited, works]
        translate("el gato")
        assert cache["el gato"] == "the cat" and cooldown[limited] > time.time() and works not in cooldown
        CACHE_FILE = os.path.join(os.environ.get("TEMP", "."), "translator_test_cache.json")
        assert save_cache() == 3 and json.load(open(CACHE_FILE, encoding="utf-8"))["hola"] == "hello"
        os.remove(CACHE_FILE)
        segs = split_gaps([(0, 0, 50, 20, "a"), (55, 0, 50, 20, "b"), (900, 0, 30, 20, "junk")])
        assert [[w[4] for w in s] for s in segs] == [["a", "b"], ["junk"]], segs
        m = merge_overlaps([[0, 0, 100, 20, 20, "one"], [80, 10, 100, 20, 20, "two"], [500, 500, 50, 20, 20, "far"]])
        assert [b[5] for b in m] == ["one two", "far"] and m[0][:4] == [0, 0, 180, 30], m
        assert CJK_GAP.sub("", "こ ん に ち は 世 界 Show working tree") == "こんにちは世界 Show working tree"
        assert wordiness("Hola amigos, zc6mo estan hoy? El gato duerme.") >= 0.6
        assert wordiness("Archivo Editar Ver Ayuda") == 1
        bl = [[0, 0, 500, 300, 20, "big"], [10, 10, 100, 20, 20, "small"]]
        assert block_at(bl, 20, 15) == "small" and block_at(bl, 400, 200) == "big" and block_at(bl, 900, 900) is None
        assert wordiness("�r gra -IE -s; � �ng- T bda-") < 0.6 and wordiness("I gr4 -IE -5;") < 0.6
        print("ok")
        sys.exit()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache.update(json.load(f))
        print(f"loaded {len(cache)} translations from {CACHE_FILE}")
    # warm up the local LLM (cold load ~30s); harmless if Ollama isn't running -> Google is used instead
    threading.Thread(target=lambda: requests.post("http://127.0.0.1:11434/api/generate", timeout=120,
                     json={"model": OLLAMA_MODEL, "keep_alive": "30m"}), daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(scan_loop()), daemon=True).start()
    try:
        make_overlay().mainloop()
    finally:
        print(f"\nsaved {save_cache()} translations to {CACHE_FILE}")

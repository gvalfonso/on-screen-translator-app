"""Snip translator: Ctrl+Alt+drag a box on screen -> a vision model on OpenRouter reads + translates it -> shown in the box.

Run:   pythonw translator.py      (no console; lives in the tray's hidden icons)
       python translator.py       (with console log)
Use:   Ctrl+Alt+drag             select a region to translate
       box buttons               copy translation / retry a failed one / x close
       Ctrl+Alt+click on a box   close it
       tray icon      target language, API key, opacity, start with Windows, quit
Needs: an OpenRouter API key (openrouter.ai/keys): tray > Set API key, or env OPENROUTER_API_KEY.
       Pick the model in tray > Model; add more to MODELS (any image-input model id from openrouter.ai/models).
"""
import base64, ctypes, ctypes.wintypes as wt, faulthandler, functools, hashlib, io, json, os, shutil, sys, threading, time, winreg
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk

import mss, pystray, requests
from PIL import Image, ImageDraw, ImageFont

__version__ = "1.5.0"  # single source of truth: build.py reads it for the exe + installer
APP_NAME = "SnipTranslator"
FROZEN = getattr(sys, "frozen", False)  # True when running as the built .exe
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HERE), APP_NAME)  # settings, key, log (install dir is read-only)
os.makedirs(DATA_DIR, exist_ok=True)
if sys.stdout is None:  # no console (pythonw / built exe): log to a file instead
    sys.stdout = sys.stderr = open(os.path.join(DATA_DIR, "translator.log"), "a", encoding="utf-8", buffering=1)
else:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console is cp1252: Thai/CJK would crash print
faulthandler.enable(sys.stderr)  # hard crashes (not just exceptions) get a traceback in the log too

ctypes.windll.shcore.SetProcessDpiAwareness(2)  # everything in physical pixels
u32 = ctypes.windll.user32

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
if not os.path.exists(SETTINGS_FILE) and os.path.exists(os.path.join(HERE, "settings.json")):
    shutil.copy(os.path.join(HERE, "settings.json"), SETTINGS_FILE)  # one-time move from the old project-folder location
settings = {"target": "English", "opacity": 0.9, "show_original": True}
if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings.update(json.load(f))
LANGUAGES = ["English", "Thai", "Japanese", "Chinese", "Korean", "Spanish", "French", "German", "Filipino"]
MIN_FONT_PX = 10  # translation shrinks to fit the box down to this; only below it does the card grow
# card style
FONT, PAD, GAP, HEADER_PX = "Segoe UI", 8, 10, 11
CARD, BORDER, ACCENT, MUTED, INK, DIVIDER = "#fffdf5", "#e2d6ae", "#c98a1b", "#8a7a55", "#1f1d1a", "#eadfbe"


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)


# ---------- translation (vision model reads the image itself) ----------
# OpenRouter: one key, many models (OpenAI-style chat endpoint).
PROVIDERS = {
    "OpenRouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "extra": {},
                   "keys_page": "openrouter.ai/keys", "credits_page": "openrouter.ai/credits"},
}
# tray > Model. Only models that read slang/loanwords right in testing (Thai "rewatch Haikyu" caption, Oct 2026);
# most cheap vision models misread it. Ids + prices from openrouter.ai/models; $ per 1M tokens in/out.
# Kept: 2/2 on meaning + loanword + emoji out of 31 models tested, and under $0.001 per box.
MODELS = {
    "anthropic/claude-haiku-5.5": "Claude Haiku 5.5 ($0.10 / $0.50)",          # ~3s, ~$0.0001/box
    "google/gemini-3.1-flash-lite": "Gemini 3.1 Flash Lite ($0.25 / $1.50)",  # fastest, ~1.5s, ~$0.0004/box
}
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
# Thinking off (faster, cheaper; this is reading + translating, not reasoning). Models that can't turn it off get
# their lowest effort (none of the current ones). Per openrouter.ai/api/v1/models, Oct 2026.
NO_THINKING = {"enabled": False}
THINKING_OVERRIDES = {}
PROMPT = ("Read the text in the image and translate it into {target}. "
          "Keep the original's tone and slang: casual stays casual, formal stays formal. "
          "Keep emojis, emoticons and punctuation (?!…~, repeated letters for emphasis) as in the original. "
          "If it is already in {target}, translation = the text unchanged. "
          "If the image has no text at all (e.g. it is blank or a plain colour), reply with lines = [] "
          "instead of making something up. "
          # one entry per line: some models (Gemini Flash Lite) otherwise stop after the first chat bubble
          "Reply as JSON: lines = a list with one entry per line or message of text, top to bottom (skip usernames, "
          "timestamps and interface labels; emojis count as text), each {{\"text\": the original, \"translation\": "
          "its translation}}; source_language = its language in English (e.g. Thai); font_px = the original text's "
          "font size in pixels of the image (roughly the height of one line of text), as an integer.")
cache = {}  # (image hash, target, model) -> {src, lang, text, font_px}; same pixels never hit the API twice
pool = ThreadPoolExecutor(2)


def model():
    m = settings.get("model")
    return m if m in MODELS else DEFAULT_MODEL


def reasoning_param():
    r = THINKING_OVERRIDES.get(model(), NO_THINKING)
    return {"reasoning": r} if r else {}


def provider():
    name = settings.get("provider")
    return name if name in PROVIDERS else next(iter(PROVIDERS))  # removed/unknown provider -> first one


def api_key(name=None):
    name = name or provider()
    return settings.get("api_keys", {}).get(name.lower()) or os.environ.get(f"{name.upper()}_API_KEY", "")


def join_lines(j):
    """Reply {lines: [{text, translation}, ...]} -> (original, translation), one line each.
    Also accepts the older flat {text, translation} shape."""
    lines = j.get("lines")
    if not isinstance(lines, list):
        return str(j.get("text") or "").strip(), str(j.get("translation") or "").strip()
    lines = [l for l in lines if isinstance(l, dict)]
    return ("\n".join(str(l.get("text") or "").strip() for l in lines).strip(),
            "\n".join(str(l.get("translation") or "").strip() for l in lines).strip())


def parse_json(text):
    """Model reply -> dict; tolerates ```json fences, notes before the JSON, and a [ {...} ] list some models add."""
    text = text.strip()
    if "```" in text:
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        j = json.loads(text)
    except json.JSONDecodeError:
        j = json.loads(text[text.index("{"):text.rindex("}") + 1])  # JSON after some prose
    return j[0] if isinstance(j, list) and j and isinstance(j[0], dict) else j


def data_url(im, fmt="PNG"):
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": 80} if fmt == "JPEG" else {}))
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


def translate_image(im):
    url = data_url(im)
    name, p = provider(), PROVIDERS[provider()]
    key = (hashlib.md5(url.encode()).hexdigest(), settings["target"], model())
    if key not in cache:
        if not api_key():
            raise RuntimeError(f"no {name} API key")
        prompt = PROMPT.format(target=settings["target"])
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]
        r = requests.post(p["url"], timeout=90, headers={"Authorization": f"Bearer {api_key()}"}, json={
            "model": model(), "response_format": {"type": "json_object"}, **p["extra"], **reasoning_param(),
            "messages": [{"role": "user", "content": content}]})
        if r.status_code >= 400:
            raise RuntimeError(f"{name} {r.status_code}: {r.text[:120]}")
        j = parse_json(r.json()["choices"][0]["message"]["content"])
        src, tr = join_lines(j)
        print(f"{src[:80]!r} -> {tr[:80]!r}")
        try:
            font_px = max(MIN_FONT_PX, min(72, int(j.get("font_px") or 0)))
        except (TypeError, ValueError):
            font_px = None  # no usable estimate: fill the box instead
        cache[key] = {"src": src, "lang": f"{j.get('source_language', '?')} → {settings['target']}",
                      "text": tr or "(no text found)", "font_px": font_px}
    return cache[key]


def friendly(e):
    """Short, actionable error text for a failed box."""
    s, name = str(e), provider()
    if "402" in s:
        return f"{name} credits are empty - top up at {PROVIDERS[name]['credits_page']}"
    if "429" in s:
        return f"{name} rate limit / quota hit - wait a bit, or switch model in the tray"
    if " 401" in s or " 403" in s or "API_KEY_INVALID" in s or "valid API key" in s:
        return f"{name} rejected the API key - tray > Set API key"
    if "API key" in s and s.startswith("no "):
        return f"No {name} API key - tray > Set API key (get one at {PROVIDERS[name]['keys_page']})"
    if isinstance(e, requests.ConnectionError):
        return f"Can't reach {name} - check your internet"
    return s[:90]


# ---------- boxes ----------
boxes = []  # dicts: x, y, w, h (canvas coords), im (the capture), src, lang, font_px, failed,
#            copied_until, text (None while translating)
DRAW_KEYS = ("x", "y", "w", "h", "text", "src", "lang", "font_px", "failed")  # what the overlay draws from
lock = threading.Lock()
ui_actions = []  # (kind, box) queued by the mouse hook, run on the tk thread (clipboard is tk's)


def start_box(x0, y0, x1, y1):
    x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
    shot = grab_without_overlay({"left": x + VX, "top": y + VY, "width": w, "height": h})
    box = {"x": x, "y": y, "w": w, "h": h, "im": Image.frombytes("RGB", shot.size, shot.rgb), "text": None}
    with lock:
        boxes.append(box)
    pool.submit(translate_box, box)


def translate_box(box):
    """Fill a box from its capture; on failure the box shows why, with a retry button."""
    try:
        r = translate_image(box["im"])
        box.update(src=r["src"], lang=r["lang"], font_px=r["font_px"], failed=False)
        box["text"] = r["text"]  # set last: it's the "done" flag the draw thread checks
    except Exception as e:
        print("translate failed:", e)
        box.update(src="", lang="failed - click ↻ to retry", font_px=None, failed=True)
        box["text"] = friendly(e)


def retry(box):
    box["text"] = None  # back to "translating…"
    pool.submit(translate_box, box)


overlay_hwnd = [None]


def grab_without_overlay(region):
    """Screenshot of the real screen: hide the overlay from capture just for this grab, so existing
    boxes and the selection outline don't end up in what gets translated."""
    hwnd = overlay_hwnd[0]
    if hwnd:
        u32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
        time.sleep(0.05)  # let the compositor apply it
    try:
        with mss.mss() as s:
            return s.grab(region)
    finally:
        if hwnd:
            u32.SetWindowDisplayAffinity(hwnd, 0)  # back to normal: shows in screenshots


BTN = 20  # header button size (px)


def close_rect(b):
    return b["x"] + b["w"] - BTN, b["y"], b["x"] + b["w"], b["y"] + BTN


def action_rect(b):
    """Second header button, left of x: copy (translated) or retry (failed)."""
    return b["x"] + b["w"] - 2 * BTN - 2, b["y"], b["x"] + b["w"] - BTN - 2, b["y"] + BTN


def box_at(x, y, part="box"):
    """Topmost box whose part ("box", "close" or "action") is under (x, y)."""
    with lock:
        for b in reversed(boxes):
            if part == "action" and b["text"] is None:
                continue  # no action button while translating
            x0, y0, x1, y1 = {"close": close_rect, "action": action_rect}.get(
                part, lambda b: (b["x"], b["y"], b["x"] + b.get("draw_w", b["w"]), b["y"] + b.get("draw_h", b["h"])))(b)
            if x0 <= x <= x1 and y0 <= y <= y1:
                return b


def remove(b):
    with lock:
        if b in boxes:
            boxes.remove(b)
            print("closed box")


# ---------- global mouse hook: Ctrl+Alt+drag selects; box buttons work (all swallowed) ----------
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


u32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
u32.CallNextHookEx.restype = LRESULT
u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
u32.SetWindowsHookExW.restype = ctypes.c_void_p
sel = {"start": None, "cur": None}  # drag in progress (canvas coords)
moving = {"box": None, "dx": 0, "dy": 0}  # box being dragged, and where on it it was grabbed
DOWN, UP, MOVE = 0x201, 0x202, 0x200


def on_mouse(n, msg, lp):
    """Runs on every mouse event system-wide: keep it tiny. Return True to swallow the event."""
    p = MSLLHOOKSTRUCT.from_address(lp).pt
    x, y = p.x - VX, p.y - VY
    # Ctrl+Alt (not plain Ctrl): Ctrl+click is used everywhere (Explorer multi-select, links, VS Code)
    combo = u32.GetAsyncKeyState(0x11) & 0x8000 and u32.GetAsyncKeyState(0x12) & 0x8000
    if msg == DOWN and (b := box_at(x, y, "close")):
        remove(b)  # the x button
        sel["swallow_up"] = True
        return True
    if msg == DOWN and (b := box_at(x, y, "action")):
        if b.get("failed"):
            retry(b)
        else:
            ui_actions.append(("copy", b))  # clipboard belongs to the tk thread
        sel["swallow_up"] = True
        return True
    if msg == DOWN and combo:
        sel["start"] = sel["cur"] = (x, y)
        return True
    if msg == DOWN and (b := box_at(x, y)):
        moving.update(box=b, dx=x - b["x"], dy=y - b["y"])  # plain drag on a box moves it
        return True
    if msg in (MOVE, UP) and moving["box"]:
        b = moving["box"]
        b["x"], b["y"] = x - moving["dx"], y - moving["dy"]  # UP too: land exactly at the release point
        if msg == UP:
            moving["box"] = None
            return True
    if msg == MOVE and sel["start"]:
        sel["cur"] = (x, y)
    if msg == UP and sel.get("swallow_up"):
        sel["swallow_up"] = False
        return True
    if msg == UP and sel["start"]:
        (x0, y0), sel["start"] = sel["start"], None
        if abs(x - x0) > 8 and abs(y - y0) > 8:
            threading.Thread(target=start_box, args=(x0, y0, x, y), daemon=True).start()  # off the hook thread
        elif b := box_at(x0, y0):
            remove(b)  # Ctrl+Alt+click (no drag) on a box closes it
        return True
    return False


def hook_thread():
    def proc(n, wp, lp):
        if n == 0 and on_mouse(n, wp, lp):
            return 1
        return u32.CallNextHookEx(None, n, wp, lp)
    cb = HOOKPROC(proc)  # keep a reference or it gets garbage-collected
    u32.SetWindowsHookExW(14, cb, None, 0)  # WH_MOUSE_LL
    msg = wt.MSG()
    while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # LL hooks need a message loop
        pass


# ---------- overlay ----------
with mss.mss() as _s:
    _v = _s.monitors[0]  # whole virtual desktop (all monitors)
VX, VY, VW, VH = _v["left"], _v["top"], _v["width"], _v["height"]


def make_overlay():
    root = tk.Tk()
    root.geometry(f"{VW}x{VH}+{VX}+{VY}")
    root.overrideredirect(True)
    root.attributes("-topmost", True, "-transparentcolor", "#010101", "-alpha", settings["opacity"])
    canvas = tk.Canvas(root, bg="#010101", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    root.update()
    hwnd = u32.GetParent(root.winfo_id())
    u32.SetWindowLongW(hwnd, -20, u32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x20 | 0x80)  # layered, click-through, no taskbar
    overlay_hwnd[0] = hwnd  # visible in screenshots; hidden only while we grab (grab_without_overlay)

    probe_canvas = tk.Canvas(root)  # never packed: measures wrapped text
    probe = probe_canvas.create_text(0, 0, anchor="nw")

    def measure(text, w, font):
        probe_canvas.itemconfig(probe, text=text, width=w, font=font)
        x0, y0, x1, y1 = probe_canvas.bbox(probe)
        return x1 - x0, y1 - y0

    @functools.lru_cache(maxsize=1024)
    def fit_px(src, text, w, h, cap):
        """Biggest translation font, at most cap (the original's size per the model), where
        (original at 60% size) + translation fit the w x h area."""
        lo, hi = MIN_FONT_PX, max(MIN_FONT_PX, min(h, cap or 48))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            tw, th = measure(text, w, (FONT, -mid))
            sw, sh = measure(src, w, (FONT, -src_px(mid), "italic")) if src else (0, -GAP)
            lo, hi = (mid, hi) if sh + GAP + th <= h and max(tw, sw) <= w + 4 else (lo, mid - 1)
        return lo

    def src_px(px):
        return max(9, round(px * 0.6))  # original text: 60% of translation

    def draw_card(b):
        """Header (languages, copy/retry + x buttons), original text small/italic, divider, translation."""
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        iw = max(w - 2 * PAD, 40)
        accent = "#c0392b" if b.get("failed") else ACCENT
        src = b["src"] if settings["show_original"] and b["src"] and b["src"] != b["text"] else ""
        header = "copied!" if time.time() < b.get("copied_until", 0) else b["lang"].upper()
        items = [canvas.create_text(x + PAD, y + PAD, anchor="nw", text=header, fill=accent,
                                    font=(FONT, -HEADER_PX, "bold"), width=max(iw - 2 * BTN, 20))]
        top = canvas.bbox(items[0])[3] + 4
        px = fit_px(src, b["text"], iw, max(h - (top - y) - PAD, MIN_FONT_PX), b.get("font_px"))
        if src:
            items.append(canvas.create_text(x + PAD, top, anchor="nw", text=src, width=iw, fill=MUTED,
                                            font=(FONT, -src_px(px), "italic")))
            top = canvas.bbox(items[-1])[3] + GAP // 2
            items.append(canvas.create_line(x + PAD, top, x + PAD + iw, top, fill=DIVIDER))
            top += GAP // 2
        items.append(canvas.create_text(x + PAD, top, anchor="nw", text=b["text"], width=iw, fill=INK, font=(FONT, -px)))
        x1 = max(x + w, max(canvas.bbox(i)[2] for i in items) + PAD)
        y1 = max(y + h, canvas.bbox(items[-1])[3] + PAD)
        canvas.tag_lower(canvas.create_rectangle(x, y, x1, y1, fill=CARD, outline=BORDER, width=1), items[0])
        canvas.tag_lower(canvas.create_rectangle(x, y, x + 3, y1, fill=accent, outline=""), items[0])  # accent stripe
        b["draw_w"], b["draw_h"] = x1 - x, y1 - y  # for hit-testing the whole card
        ax0, ay0, ax1, ay1 = action_rect(b)
        cx, cy = (ax0 + ax1) // 2, (ay0 + ay1) // 2
        if b.get("failed"):
            canvas.create_text(cx, cy + 1, text="↻", font=(FONT, 12, "bold"), fill=accent)
        else:  # copy icon: two overlapping pages, drawn so it never depends on a font glyph
            canvas.create_rectangle(cx - 5, cy - 6, cx + 2, cy + 2, outline=accent, width=1.5)
            canvas.create_rectangle(cx - 2, cy - 3, cx + 5, cy + 5, outline=accent, width=1.5, fill=CARD)

    last = {"state": None, "tick": 0}

    def redraw():
        root.after(30, redraw)
        last["tick"] += 1
        if last["tick"] % 15 == 0:  # ~every 0.5s: stay topmost (cheap, but no need every frame)
            u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13)
        while ui_actions:  # queued by the mouse hook
            kind, b = ui_actions.pop(0)
            if kind == "copy" and b["text"]:
                root.clipboard_clear()
                root.clipboard_append(b["text"])
                b["copied_until"] = time.time() + 1.5
        with lock:
            snapshot = list(boxes)
        now = time.time()
        state = ([tuple(b.get(k) for k in DRAW_KEYS) + (now < b.get("copied_until", 0),) for b in snapshot],
                 sel["start"] and (sel["start"], sel["cur"]), settings["opacity"], settings["show_original"])
        if state == last["state"]:
            return  # nothing changed: skip the rebuild (keeps the GIL free for the mouse hook)
        if not last["state"] or state[2] != last["state"][2]:
            root.attributes("-alpha", settings["opacity"])
        last["state"] = state
        canvas.delete("all")
        for b in snapshot:
            x, y, w, h = b["x"], b["y"], b["w"], b["h"]
            if b["text"] is None:
                canvas.create_rectangle(x, y, x + w, y + h, outline="#ff8c00", width=2)
                canvas.create_text(x, y - 3, text="translating…", anchor="sw", font=("Segoe UI", 9, "bold"), fill="#ff8c00")
            else:
                draw_card(b)
            cx0, cy0, cx1, cy1 = close_rect(b)
            canvas.create_text((cx0 + cx1) / 2, (cy0 + cy1) / 2, text="×", font=(FONT, 11, "bold"), fill=MUTED)
        if sel["start"]:
            (x0, y0), (x1, y1) = sel["start"], sel["cur"]
            canvas.create_rectangle(x0, y0, x1, y1, outline="#1e90ff", width=2, dash=(4, 2))

    redraw()
    return root


# ---------- tray ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_startup(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            if FROZEN:
                cmd = f'"{sys.executable}"'
            else:
                pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                cmd = f'"{pythonw}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            winreg.DeleteValue(k, APP_NAME)


def ask_key(root, provider):
    """Paste-your-key dialog (runs on the tk thread). Saved in settings.json (plain text, git-ignored)."""
    from tkinter import simpledialog
    win = tk.Toplevel(root)
    win.withdraw()
    win.attributes("-topmost", True)
    key = simpledialog.askstring(f"{provider} API key", f"Paste your {provider} API key:", parent=win, show="*")
    win.destroy()
    if key is not None:
        settings.setdefault("api_keys", {})[provider] = key.strip()
        save_settings()
        print(f"{provider} key saved" if key.strip() else f"{provider} key cleared")


def tray_icon(root):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), 12, fill="#1e90ff")
    try:
        cjk, latin = ImageFont.truetype("msyh.ttc", 30), ImageFont.truetype("arialbd.ttf", 28)
    except OSError:
        cjk = latin = ImageFont.load_default()
    d.text((6, 2), "文", fill="white", font=cjk)
    d.text((36, 28), "A", fill="white", font=latin)

    def setter(key, value):
        def f(icon, item):
            settings[key] = value
            save_settings()
        return f

    def checked(key, value):
        return lambda item: settings[key] == value

    def quit_(icon, item):
        icon.stop()
        root.after(0, root.destroy)

    SEP = pystray.Menu.SEPARATOR
    menu = pystray.Menu(
        pystray.MenuItem(f"Snip Translator v{__version__}  -  Ctrl+Alt+drag", None, enabled=False),
        SEP,  # what you get
        pystray.MenuItem("Translate to", pystray.Menu(*[
            pystray.MenuItem(l, setter("target", l), checked=checked("target", l), radio=True) for l in LANGUAGES])),
        pystray.MenuItem("Model", pystray.Menu(*[
            pystray.MenuItem(label, setter("model", m), checked=lambda item, m=m: model() == m, radio=True)
            for m, label in MODELS.items()])),
        SEP,  # how it looks
        pystray.MenuItem("Show original text", lambda icon, item: (settings.update(show_original=not settings["show_original"]),
                                                                  save_settings()),
                         checked=lambda item: settings["show_original"]),
        pystray.MenuItem("Opacity", pystray.Menu(*[
            pystray.MenuItem(f"{int(o * 100)}%", setter("opacity", o), checked=checked("opacity", o), radio=True)
            for o in (1.0, 0.9, 0.8, 0.7)])),
        SEP,  # setup
        pystray.MenuItem("Set OpenRouter API key", lambda icon, item: root.after(0, ask_key, root, "openrouter")),
        pystray.MenuItem("Start with Windows", lambda icon, item: set_startup(not startup_enabled()),
                         checked=lambda item: startup_enabled()),
        SEP,
        pystray.MenuItem("Quit", quit_),
    )
    icon = pystray.Icon(APP_NAME, img, f"Snip Translator v{__version__} (Ctrl+Alt+drag)", menu)
    icon.run_detached()
    return icon


if __name__ == "__main__":
    if "--test" in sys.argv:
        boxes[:] = [{"x": 10, "y": 10, "w": 200, "h": 100, "text": "hi"}]
        assert box_at(50, 50)["text"] == "hi" and box_at(300, 300) is None
        assert box_at(200, 15, "close") and not box_at(50, 50, "close")
        boxes[0]["draw_h"] = 300  # card grew past the selection: still hit-testable
        assert box_at(50, 250)["text"] == "hi"
        del boxes[0]["draw_h"]
        assert box_at(178, 15, "action") and not box_at(200, 15, "action")  # copy sits left of x
        boxes[0]["text"] = None
        assert not box_at(178, 15, "action")  # no copy/retry while translating
        assert friendly(RuntimeError("OpenRouter 402: x")).startswith("OpenRouter credits")
        settings["provider"], settings["model"] = "DeepSeek", "deepseek-flash"  # stale settings from older versions
        assert provider() == "OpenRouter" and model() == DEFAULT_MODEL
        assert reasoning_param() == {"reasoning": {"enabled": False}}
        THINKING_OVERRIDES["x/mandatory"] = {"effort": "low"}  # a model that can't turn thinking off
        MODELS["x/mandatory"] = settings["model"] = "x/mandatory"
        assert reasoning_param() == {"reasoning": {"effort": "low"}}
        del THINKING_OVERRIDES["x/mandatory"], MODELS["x/mandatory"]
        assert parse_json('[{"a": 1}]') == {"a": 1}  # some models wrap the object in a list
        del settings["provider"], settings["model"]
        assert parse_json('```json\n{"a": 1}\n```') == {"a": 1} and parse_json('{"a": 1}') == {"a": 1}
        assert parse_json('Reading it: two lines.\n```json\n{"a": 1}\n```') == {"a": 1}  # notes before the JSON
        assert parse_json('Sure! {"a": 1} hope that helps') == {"a": 1}
        assert join_lines({"lines": [{"text": "a", "translation": "A"}, {"text": "b", "translation": "B"}]}) == ("a\nb", "A\nB")
        assert join_lines({"lines": []}) == ("", "")  # blank image
        assert join_lines({"text": "a", "translation": "A"}) == ("a", "A")  # older flat shape
        remove(boxes[0])
        assert boxes == []
        print("ok")
        sys.exit()
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "SnipTranslatorSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS: already running (tray icon)
        print("already running")
        sys.exit()
    root = make_overlay()
    threading.Thread(target=hook_thread, daemon=True).start()
    tray_icon(root)
    print(f"running v{__version__}: Ctrl+Alt+drag to translate, tray icon for settings")
    try:
        root.mainloop()
    finally:
        print("exited normally")  # missing from the log = crashed (faulthandler traceback above) or was killed

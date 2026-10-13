"""Snip translator: Ctrl+drag a box on screen -> DeepSeek reads + translates it -> shown in the box.

Run:   pythonw translator.py      (no console; lives in the tray's hidden icons)
       python translator.py       (with console log)
Use:   Ctrl+drag      select a region to translate
       x / Ctrl+click on a box   close it
       tray icon      target language, API key, opacity, start with Windows, clear all, quit
Needs: a DeepSeek API key (platform.deepseek.com): tray > Set API key, or env DEEPSEEK_API_KEY.
"""
import base64, ctypes, ctypes.wintypes as wt, faulthandler, functools, hashlib, io, json, os, shutil, sys, threading, time, winreg
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk

import mss, pystray, requests
from PIL import Image, ImageDraw, ImageFont

__version__ = "1.0.0"  # single source of truth: build.py reads it for the exe + installer
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
MIN_FONT_PX = 16  # translation never smaller than this; the card grows downward instead
# card style
FONT, PAD, GAP, HEADER_PX = "Segoe UI", 8, 10, 11
CARD, BORDER, ACCENT, MUTED, INK, DIVIDER = "#fffdf5", "#e2d6ae", "#c98a1b", "#8a7a55", "#1f1d1a", "#eadfbe"


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=1)


# ---------- translation (DeepSeek, reads the image itself) ----------
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"  # sees images; per api-docs.deepseek.com, Oct 2026
PROMPT = ("Read the text in this image and translate it into natural {target}, keeping tone and slang. "
          "If it is already in {target}, translation = the text unchanged. "
          "Reply as JSON: text = the original text, source_language = its language in English (e.g. Thai), "
          "translation = the translation.")
cache = {}  # (image hash, target) -> {src, lang, text}; same pixels never hit the API twice
pool = ThreadPoolExecutor(2)


def api_key():
    return settings.get("api_keys", {}).get("deepseek") or os.environ.get("DEEPSEEK_API_KEY", "")


def translate_image(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    png = buf.getvalue()
    key = (hashlib.md5(png).hexdigest(), settings["target"])
    if key not in cache:
        if not api_key():
            raise RuntimeError("no DeepSeek API key - tray > Set API key")
        r = requests.post(API_URL, timeout=60, headers={"Authorization": f"Bearer {api_key()}"}, json={
            "model": MODEL, "temperature": 0, "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},  # on by default: slow for this
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT.format(target=settings["target"])},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]})
        if r.status_code >= 400:
            raise RuntimeError(f"DeepSeek {r.status_code}: {r.text[:120]}")
        j = json.loads(r.json()["choices"][0]["message"]["content"])
        print(f"{j.get('text', '')[:80]!r} -> {j.get('translation', '')[:80]!r}")
        cache[key] = {"src": j.get("text", "").strip(), "lang": f"{j.get('source_language', '?')} → {settings['target']}",
                      "text": j.get("translation", "").strip() or "(no text found)"}
    return cache[key]


# ---------- boxes ----------
boxes = []  # dicts: x, y, w, h (canvas coords), src, lang, text (text None while translating)
lock = threading.Lock()


def start_box(x0, y0, x1, y1):
    x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
    shot = grab_without_overlay({"left": x + VX, "top": y + VY, "width": w, "height": h})
    im = Image.frombytes("RGB", shot.size, shot.rgb)
    box ={"x": x, "y": y, "w": w, "h": h, "text": None}
    with lock:
        boxes.append(box)

    def work():
        try:
            r = translate_image(im)
            box.update(src=r["src"], lang=r["lang"])
            box["text"] = r["text"]  # set last: it's the "done" flag the draw thread checks
        except Exception as e:
            print("translate failed:", e)
            box.update(src="", lang="error")
            box["text"] = f"(failed: {str(e)[:60]} - is Ollama running?)"
    pool.submit(work)


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


def close_rect(b):
    return b["x"] + b["w"] - 18, b["y"], b["x"] + b["w"], b["y"] + 18


def box_at(x, y, close_only=False):
    with lock:
        for b in reversed(boxes):  # topmost first
            x0, y0, x1, y1 = close_rect(b) if close_only else (b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"])
            if x0 <= x <= x1 and y0 <= y <= y1:
                return b


def remove(b):
    with lock:
        if b in boxes:
            boxes.remove(b)
            print("closed box")


# ---------- global mouse hook: Ctrl+drag selects, clicks on x close (both swallowed) ----------
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
DOWN, UP, MOVE = 0x201, 0x202, 0x200


def on_mouse(n, msg, lp):
    """Runs on every mouse event system-wide: keep it tiny. Return True to swallow the event."""
    p = MSLLHOOKSTRUCT.from_address(lp).pt
    x, y = p.x - VX, p.y - VY
    ctrl = u32.GetAsyncKeyState(0x11) & 0x8000
    if msg == DOWN and (b := box_at(x, y, close_only=True)):
        remove(b)  # the x button
        sel["swallow_up"] = True
        return True
    if msg == DOWN and ctrl:
        sel["start"] = sel["cur"] = (x, y)
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
            remove(b)  # Ctrl+click (no drag) on a box closes it
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
    def fit_px(src, text, w, h):
        """Biggest translation font where (original at 60% size) + translation fit the w x h area."""
        lo, hi = MIN_FONT_PX, max(MIN_FONT_PX, min(h, 48))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            tw, th = measure(text, w, (FONT, -mid))
            sw, sh = measure(src, w, (FONT, -src_px(mid), "italic")) if src else (0, -GAP)
            lo, hi = (mid, hi) if sh + GAP + th <= h and max(tw, sw) <= w + 4 else (lo, mid - 1)
        return lo

    def src_px(px):
        return max(13, round(px * 0.6))  # original text: 60% of translation, readable floor

    def draw_card(b):
        """Header (languages + x), original text small/italic, divider, translation."""
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        iw = max(w - 2 * PAD, 40)
        src = b["src"] if settings["show_original"] and b["src"] and b["src"] != b["text"] else ""
        items = [canvas.create_text(x + PAD, y + PAD, anchor="nw", text=b["lang"].upper(), fill=ACCENT,
                                    font=(FONT, -HEADER_PX, "bold"))]
        top = y + PAD + HEADER_PX + 4
        px = fit_px(src, b["text"], iw, max(h - (top - y) - PAD, MIN_FONT_PX))
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
        canvas.tag_lower(canvas.create_rectangle(x, y, x + 3, y1, fill=ACCENT, outline=""), items[0])  # accent stripe

    last = {"state": None, "tick": 0}

    def redraw():
        root.after(30, redraw)
        last["tick"] += 1
        if last["tick"] % 15 == 0:  # ~every 0.5s: stay topmost (cheap, but no need every frame)
            u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13)
        with lock:
            snapshot = list(boxes)
        state = ([tuple(b.values()) for b in snapshot], sel["start"] and (sel["start"], sel["cur"]),
                 settings["opacity"], settings["show_original"])
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


def ask_key(root, provider="deepseek"):
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

    def clear(icon, item):
        with lock:
            boxes.clear()

    def quit_(icon, item):
        icon.stop()
        root.after(0, root.destroy)

    menu = pystray.Menu(
        pystray.MenuItem(f"Snip Translator v{__version__}  -  Ctrl+drag", None, enabled=False),
        pystray.MenuItem("Translate to", pystray.Menu(*[
            pystray.MenuItem(l, setter("target", l), checked=checked("target", l), radio=True) for l in LANGUAGES])),
        pystray.MenuItem("Opacity", pystray.Menu(*[
            pystray.MenuItem(f"{int(o * 100)}%", setter("opacity", o), checked=checked("opacity", o), radio=True)
            for o in (1.0, 0.9, 0.8, 0.7)])),
        pystray.MenuItem("Start with Windows", lambda icon, item: set_startup(not startup_enabled()),
                         checked=lambda item: startup_enabled()),
        pystray.MenuItem("Set DeepSeek API key", lambda icon, item: root.after(0, ask_key, root)),
        pystray.MenuItem("Show original text", lambda icon, item: (settings.update(show_original=not settings["show_original"]),
                                                                  save_settings()),
                         checked=lambda item: settings["show_original"]),
        pystray.MenuItem("Clear all boxes", clear),
        pystray.MenuItem("Quit", quit_),
    )
    icon = pystray.Icon(APP_NAME, img, f"Snip Translator v{__version__} (Ctrl+drag)", menu)
    icon.run_detached()
    return icon


if __name__ == "__main__":
    if "--test" in sys.argv:
        boxes[:] = [{"x": 10, "y": 10, "w": 200, "h": 100, "text": "hi"}]
        assert box_at(50, 50)["text"] == "hi" and box_at(300, 300) is None
        assert box_at(200, 15, close_only=True) and not box_at(50, 50, close_only=True)
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
    print(f"running v{__version__}: Ctrl+drag to translate, tray icon for settings")
    try:
        root.mainloop()
    finally:
        print("exited normally")  # missing from the log = crashed (faulthandler traceback above) or was killed

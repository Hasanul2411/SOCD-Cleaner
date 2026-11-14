"""SOCD cleaner for any controller (XInput, DInput, HID - anything SDL recognises).

Reads a physical controller, resolves opposing directions, and outputs either a
virtual Xbox 360 pad (ViGEmBus) or keyboard keys. See README.md.
"""
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
from collections import Counter
from ctypes import wintypes as w
from pathlib import Path

CFG_PATH = Path(sys.argv[0]).resolve().with_name("socd_config.json")
DB_PATH = CFG_PATH.with_name("gamecontrollerdb.txt")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DIRS = ("up", "down", "left", "right")
ARROW = {"up": "↑", "down": "↓", "left": "←", "right": "→"}
MODES = {"last": "Last input priority", "first": "First input priority",
         "neutral": "Neutral", "up": "Up priority"}
OUTPUTS = {"pad": "Virtual Xbox pad", "keyboard": "Keyboard keys"}
TARGETS = {"dpad": "D-pad", "stick": "Left stick", "both": "D-pad + left stick"}

# SDL GameController button indices, in order, then the two triggers as pseudo-buttons.
BUTTONS = ["A", "B", "X", "Y", "Back", "Guide", "Start", "L3", "R3", "LB", "RB",
           "D-pad up", "D-pad down", "D-pad left", "D-pad right",
           "Misc", "Paddle 1", "Paddle 2", "Paddle 3", "Paddle 4", "Touchpad", "LT", "RT"]
NSDL, LT, RT = 21, 21, 22
# XInput wButtons bit for each of the first 15 SDL buttons (the rest have no Xbox 360 equivalent).
XBITS = [0x1000, 0x2000, 0x4000, 0x8000, 0x0020, 0x0400, 0x0010, 0x0040, 0x0080,
         0x0100, 0x0200, 0x0001, 0x0002, 0x0004, 0x0008]

DEFAULTS = {"controller": "", "mode": "last", "output": "pad", "pad_target": "dpad", "enabled": True,
            "bind": {"up": 11, "down": 12, "left": 13, "right": 14},
            "keys": {"up": "w", "down": "s", "left": "a", "right": "d"}}


def clean(mode, neg, pos, last, vertical=False):
    """Resolve one axis. neg/pos: is up|left / down|right held; last: -1/+1, the side
    pressed most recently. Returns -1, 0 or +1."""
    if not (neg and pos):
        return pos - neg
    if mode == "last":
        return last
    if mode == "first":
        return -last
    if mode == "up" and vertical:
        return -1
    return 0  # neutral, and the horizontal axis of "up"


# --- keyboard output (scancodes, because games ignore virtual-key-only input) ---

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    # pad makes the struct as large as the real INPUT union (MOUSEINPUT is the biggest member)
    _fields_ = [("type", w.DWORD), ("ki", KEYBDINPUT), ("pad", ctypes.c_ubyte * 8)]


# name -> (scancode, extended flag)
NAMED_KEYS = {"up": (0x48, 1), "down": (0x50, 1), "left": (0x4B, 1), "right": (0x4D, 1),
              "space": (0x39, 0), "enter": (0x1C, 0), "tab": (0x0F, 0),
              "shift": (0x2A, 0), "ctrl": (0x1D, 0), "alt": (0x38, 0)}


def scancode(name):
    """Key name -> (scancode, extended flag). Accepts one character or a name from NAMED_KEYS."""
    name = name.strip().lower()
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    if len(name) == 1:
        u = ctypes.windll.user32
        sc = u.MapVirtualKeyW(u.VkKeyScanW(ord(name)) & 0xFF, 0)
        if sc:
            return sc, 0
    raise ValueError(f"Unknown key {name!r}: use one character or {'/'.join(NAMED_KEYS)}")


def send_key(name, down):
    sc, ext = scancode(name)
    inp = INPUT(type=1, ki=KEYBDINPUT(0, sc, 0x8 | ext | (0 if down else 0x2), 0, 0))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


# --- config ---

def check_cfg(cfg):
    """Raise ValueError if cfg would break the engine (hand-edited or corrupt file, bad UI input)."""
    try:
        ok = (cfg["mode"] in MODES and cfg["output"] in OUTPUTS and cfg["pad_target"] in TARGETS
              and isinstance(cfg["controller"], str) and isinstance(cfg["enabled"], bool)
              and all(type(cfg["bind"][d]) is int and 0 <= cfg["bind"][d] < len(BUTTONS) for d in DIRS))
        keys = [cfg["keys"][d] for d in DIRS]
    except (KeyError, TypeError):
        ok = False
    if not ok:
        raise ValueError("Settings are invalid")
    for k in keys:
        if not isinstance(k, str):
            raise ValueError("Settings are invalid")
        scancode(k)
    # compare scancodes, not names, so "W" and "w" count as the same key
    if len({cfg["bind"][d] for d in DIRS}) < 4 or len({scancode(k) for k in keys}) < 4:
        raise ValueError("Each direction needs its own button and its own key")


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(CFG_PATH.read_text())
        for k in cfg:
            if k in saved:
                cfg[k] = {**cfg[k], **saved[k]} if isinstance(cfg[k], dict) else saved[k]
        check_cfg(cfg)
    except (OSError, ValueError, TypeError):
        return json.loads(json.dumps(DEFAULTS))  # missing or corrupt config: start from defaults
    return cfg


def save_cfg(cfg):
    tmp = CFG_PATH.with_suffix(".tmp")  # write-then-rename so a crash can't leave half a file
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, CFG_PATH)


def autostart(turn_on=None):
    """Read (no argument) or set the per-user 'start with Windows' entry. No admin needed."""
    import winreg
    if turn_on is None:
        try:  # read-only open, so a locked-down registry can't break the tray menu
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, "SOCD Cleaner")
            return True
        except OSError:
            return False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if turn_on:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:  # pythonw: no console window at login
                cmd = f'"{sys.executable.replace("python.exe", "pythonw.exe")}" "{Path(sys.argv[0]).resolve()}"'
            winreg.SetValueEx(key, "SOCD Cleaner", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "SOCD Cleaner")
            except OSError:
                pass


# --- engine: polls the controller at ~1 kHz on its own thread ---

class Engine(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.reload = False
        self.running = True
        self.status = "Starting"
        self.names = []      # physical controllers currently visible, for the settings window
        self.pressed = None  # last button pressed, for binding capture
        self.view = ((), 0, 0)  # (raw held directions, cleaned x, cleaned y) for the live viewer
        self.kind = "xbox"   # controller family of the open controller: picks button names and icons

    def run(self):
        try:
            os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"  # we never have focus
            if DB_PATH.exists():  # optional community mappings for obscure DInput devices
                os.environ["SDL_GAMECONTROLLERCONFIG_FILE"] = str(DB_PATH)
            import pygame
            from pygame._sdl2 import controller
            pygame.init()
            controller.init()
        except Exception as e:
            self.status = f"Controller support failed to start: {e}"
            return

        ctl = pad = sent = None
        keys = dict.fromkeys(DIRS)  # dir -> key name currently held down
        held = dict.fromkeys(DIRS, False)
        last = {"x": 1, "y": 1}
        prev = [0] * len(BUTTONS)
        next_scan = 0
        before = None        # controller names seen just before our virtual pad was plugged in
        ours = Counter()     # names that appeared because of it (hidden from the settings list)
        rumble = [(0, 0)]    # motor levels the game last sent to the virtual pad
        rumbling = (0, 0)

        def on_rumble(client, target, large, small, led, user_data):
            rumble[0] = (large, small)  # called on a ViGEm thread; applied in the loop below

        def release():
            for d, name in keys.items():
                if name:
                    send_key(name, False)
                    keys[d] = None

        def scan():
            return [controller.name_forindex(i) or f"Controller {i}"
                    for i in range(controller.get_count()) if controller.is_controller(i)]

        try:
            while self.running:
                try:
                    pygame.event.pump()
                    if time.monotonic() >= next_scan:
                        next_scan = time.monotonic() + 1
                        found = Counter(scan())
                        if before is not None and pad:
                            ours, before = found - before, None
                        self.names = sorted((found - ours).elements())
                    cfg = self.cfg
                    if self.reload or (ctl and not ctl.attached()):
                        if not self.reload:
                            self.status = f"Controller disconnected - waiting for {cfg['controller'] or 'a controller'}"
                        self.reload = False
                        release()
                        if ctl and rumbling != (0, 0):
                            try:
                                ctl.rumble(0, 0, 0)
                            except Exception:
                                pass
                        # Drop the virtual pad before rescanning so we can never open our own output.
                        # shortcut: controllers are matched by name, so two identical pads can't be told apart; match by GUID if that matters
                        ctl = pad = sent = before = None
                        ours, rumble[0], rumbling = Counter(), (0, 0), (0, 0)
                        self.view = ((), 0, 0)
                        time.sleep(0.3)  # let Windows remove the virtual pad before we look again
                        next_scan = 0
                        continue
                    if ctl is None:
                        want = cfg["controller"]
                        idx = next((i for i in range(controller.get_count()) if controller.is_controller(i)
                                    and (not want or controller.name_forindex(i) == want)), None)
                        if idx is None:
                            if not self.status.startswith("Controller disconnected"):
                                self.status = f"Waiting for {want or 'a controller'}"
                            time.sleep(0.25)
                            continue
                        ctl = controller.Controller(idx)
                        prev = [1] * len(BUTTONS)  # buttons already held at connect are not new presses
                        self.kind = controller_kind(ctl.name)
                        self.status = f"Running on {ctl.name}"
                        if cfg["output"] == "pad":
                            try:
                                import vgamepad
                                before = Counter(scan())
                                pad = vgamepad.VX360Gamepad()
                                next_scan = time.monotonic() + 1  # give the virtual pad time to appear
                            except Exception as e:  # driver missing: keep reading so binding still works
                                before = None
                                self.status = f"No virtual pad (is ViGEmBus installed?): {e}"
                            if pad:
                                try:
                                    pad.register_notification(callback_function=on_rumble)
                                except Exception:
                                    pass  # rumble passthrough is optional
                        continue

                    btn = [ctl.get_button(b) for b in range(NSDL)]
                    lt, rt = ctl.get_axis(4), ctl.get_axis(5)
                    btn += [lt > 16000, rt > 16000]
                    for b, now in enumerate(btn):
                        if now and not prev[b]:
                            self.pressed = b
                    prev = btn

                    bind = cfg["bind"]
                    for d in DIRS:
                        h = bool(btn[bind[d]])
                        if h and not held[d]:
                            last["y" if d in ("up", "down") else "x"] = -1 if d in ("up", "left") else 1
                        held[d] = h
                    on = cfg["enabled"]
                    x = clean(cfg["mode"], held["left"], held["right"], last["x"]) if on else 0
                    y = clean(cfg["mode"], held["up"], held["down"], last["y"], vertical=True) if on else 0
                    self.view = (tuple(d for d in DIRS if held[d]), x, y)

                    if cfg["output"] == "keyboard":
                        active = {"up": y < 0, "down": y > 0, "left": x < 0, "right": x > 0}
                        for d in DIRS:
                            if keys[d] and (not active[d] or keys[d] != cfg["keys"][d]):
                                send_key(keys[d], False)  # released, or the key was rebound while held
                                keys[d] = None
                            if active[d] and not keys[d]:
                                keys[d] = cfg["keys"][d]
                                send_key(keys[d], True)
                    elif pad:
                        # Everything except the bound direction buttons passes straight through.
                        # When disabled nothing is consumed: the virtual pad mirrors the real one.
                        bound = set(bind.values()) if on else set()
                        mask = sum(XBITS[b] for b in range(len(XBITS)) if btn[b] and b not in bound)
                        lx, ly = ctl.get_axis(0), min(32767, -ctl.get_axis(1))  # SDL Y is down-positive
                        if cfg["pad_target"] in ("dpad", "both"):
                            mask |= (y < 0) * 1 | (y > 0) * 2 | (x < 0) * 4 | (x > 0) * 8
                        if cfg["pad_target"] in ("stick", "both") and (x or y):
                            lx, ly = x * 32767, -y * 32767
                        rep = (mask, 0 if LT in bound else max(0, lt) >> 7, 0 if RT in bound else max(0, rt) >> 7,
                               lx, ly, ctl.get_axis(2), min(32767, -ctl.get_axis(3)))
                        if rep != sent:
                            r = pad.report
                            (r.wButtons, r.bLeftTrigger, r.bRightTrigger,
                             r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY) = rep
                            pad.update()
                            sent = rep
                        if rumble[0] != rumbling:
                            rumbling = rumble[0]
                            try:
                                # shortcut: SDL caps one rumble call at ~65 s; re-send on a timer if a game holds rumble longer
                                ctl.rumble(rumbling[0] / 255, rumbling[1] / 255, 0xFFFF)
                            except Exception:
                                pass  # controller has no rumble
                    time.sleep(0.001)
                except Exception as e:  # e.g. controller yanked mid-read: report, then reconnect
                    self.status = f"Error: {e}"
                    self.reload = True
                    time.sleep(1)
        finally:
            release()  # never leave a key stuck down


# --- tray icon + settings window ---

# Design tokens. Dark, one accent; every text/background pair is at least 4.5:1.
BG, FIELD, BORDER = "#14141c", "#20202c", "#3a3a52"
FG, MUTED, OFF = "#e8e8f0", "#a0a3b5", "#262634"
ACCENT, AMBER, GOOD, BAD = "#ff3b4e", "#ffc233", "#3ddc84", "#ff7b7b"
FONT, FONT_BOLD, FONT_TITLE, FONT_SMALL = (("Segoe UI", 10), ("Segoe UI Semibold", 10),
                                           ("Segoe UI Semibold", 15), ("Segoe UI", 9))
GAP = 8  # spacing unit; all padding is a multiple of it

# Arrow pointing right, in a unit box; rotated to draw all eight notation directions.
ARROW_SHAPE = [(-0.5, -0.16), (0.05, -0.16), (0.05, -0.45), (0.5, 0), (0.05, 0.45), (0.05, 0.16), (-0.5, 0.16)]


# Per controller family: display name, names of the 23 bindable buttons, face-button glyphs
# (letter or shape + colour), and short labels for the non-face, non-d-pad buttons.
_DPAD = ["D-pad up", "D-pad down", "D-pad left", "D-pad right"]
_PADDLES = ["Paddle 1", "Paddle 2", "Paddle 3", "Paddle 4", "Touchpad"]
LAYOUTS = {
    "xbox": ("Xbox", BUTTONS,
             [("A", "#6ddc5a"), ("B", "#ff5a5a"), ("X", "#5aa0ff"), ("Y", "#ffd23c")],
             ["BK", "G", "ST", "L3", "R3", "LB", "RB", "M", "P1", "P2", "P3", "P4", "TP", "LT", "RT"]),
    "playstation": ("PlayStation",
                    ["Cross", "Circle", "Square", "Triangle", "Share", "PS", "Options", "L3", "R3", "L1", "R1"]
                    + _DPAD + ["Mic"] + _PADDLES + ["L2", "R2"],
                    [("cross", "#7aa7ff"), ("circle", "#ff6b6b"), ("square", "#f29be0"), ("triangle", "#5fe0b0")],
                    ["SH", "PS", "OP", "L3", "R3", "L1", "R1", "M", "P1", "P2", "P3", "P4", "TP", "L2", "R2"]),
    "switch": ("Nintendo Switch",
               ["A", "B", "X", "Y", "Minus", "Home", "Plus", "L stick", "R stick", "L", "R"]
               + _DPAD + ["Capture"] + _PADDLES + ["ZL", "ZR"],
               [("A", FG), ("B", FG), ("X", FG), ("Y", FG)],
               ["-", "H", "+", "LS", "RS", "L", "R", "C", "P1", "P2", "P3", "P4", "TP", "ZL", "ZR"]),
}


def controller_kind(name):
    """Guess the controller family from the name SDL reports.
    shortcut: name matching only - a PlayStation pad behind Steam Input / DS4Windows shows up as Xbox; add a manual override if that bites."""
    n = (name or "").lower()
    if any(k in n for k in ("ps3", "ps4", "ps5", "dualshock", "dualsense", "playstation", "sony")):
        return "playstation"
    if any(k in n for k in ("nintendo", "switch", "joy-con", "joycon", "pro controller")):
        return "switch"
    return "xbox"


def button_icon(kind, b, size=24):
    """Original glyph for button b in the given controller family, as a PIL image."""
    from PIL import Image, ImageDraw, ImageFont
    k = 4  # draw 4x then shrink, for smooth edges
    n = size * k
    img = Image.new("RGB", (n, n), FIELD)
    d = ImageDraw.Draw(img)

    def text(label, colour, px):
        try:
            font = ImageFont.truetype("segoeuib.ttf", int(px))
        except OSError:
            font = ImageFont.load_default()
        box = d.textbbox((0, 0), label, font=font)
        d.text(((n - box[0] - box[2]) / 2, (n - box[1] - box[3]) / 2), label, fill=colour, font=font)

    _, _, face, short = LAYOUTS[kind]
    if b < 4:
        glyph, col = face[b]
        d.ellipse((k, k, n - k, n - k), fill="#0c0c12", outline=col, width=int(1.5 * k))
        lo, hi, wd = n * 0.33, n * 0.67, int(2.2 * k)
        if glyph == "cross":
            d.line((lo, lo, hi, hi), fill=col, width=wd)
            d.line((lo, hi, hi, lo), fill=col, width=wd)
        elif glyph == "circle":
            d.ellipse((lo - k, lo - k, hi + k, hi + k), outline=col, width=wd)
        elif glyph == "square":
            d.rectangle((lo, lo, hi, hi), outline=col, width=wd)
        elif glyph == "triangle":
            pts = [(n / 2, lo - 2 * k), (hi + k, hi), (lo - k, hi)]
            d.line(pts + pts[:2], fill=col, width=wd, joint="curve")
        else:
            text(glyph, col, n * 0.58)
    elif 11 <= b <= 14:
        a = n / 3  # plus-shaped d-pad with the bound arm lit
        d.rectangle((a, 2 * k, 2 * a, n - 2 * k), fill=BORDER)
        d.rectangle((2 * k, a, n - 2 * k, 2 * a), fill=BORDER)
        dx, dy = [(0, -1), (0, 1), (-1, 0), (1, 0)][b - 11]
        cx, cy = n / 2 + dx * (a - k), n / 2 + dy * (a - k)
        d.rectangle((cx - a / 2, cy - a / 2, cx + a / 2, cy + a / 2), fill=AMBER)
    else:
        label = short[b - 4 if b < 11 else b - 8]
        d.rounded_rectangle((k / 2, n * 0.2, n - k / 2, n * 0.8), radius=3 * k, fill="#0c0c12", outline=MUTED, width=k)
        text(label, FG, n * (0.42 if len(label) < 3 else 0.34))
    return img.resize((size, size), Image.LANCZOS)


def check_icon(checked, size=18):
    from PIL import Image, ImageDraw
    k = 4
    n = size * k
    img = Image.new("RGB", (n, n), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((k, k, n - k, n - k), radius=3 * k, fill=ACCENT if checked else FIELD,
                        outline=ACCENT if checked else MUTED, width=k)
    if checked:
        d.line([(n * 0.26, n * 0.52), (n * 0.44, n * 0.70), (n * 0.76, n * 0.32)], fill="#000000",
               width=int(2.4 * k), joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def arrow_points(cx, cy, dx, dy, size):
    a = math.atan2(dy, dx)
    c, s = math.cos(a), math.sin(a)
    return [v for px, py in ARROW_SHAPE
            for v in (cx + (px * c - py * s) * size, cy + (px * s + py * c) * size)]


def main():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex = k32.CreateMutexW(None, False, "SOCDCleanerSingleInstance")  # held until the process exits
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: two copies would fight over the controller
        ctypes.windll.user32.MessageBoxW(None, "SOCD Cleaner is already running - look in the system tray.",
                                         "SOCD Cleaner", 0x40)
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    except (OSError, AttributeError):
        pass

    import tkinter as tk
    from tkinter import ttk
    import pystray
    from PIL import Image, ImageDraw

    first_run = not CFG_PATH.exists()
    eng = Engine(load_cfg())
    eng.start()

    root = tk.Tk()
    root.title("SOCD Cleaner")
    root.resizable(False, False)
    root.configure(bg=BG)
    root.protocol("WM_DELETE_WINDOW", root.withdraw)

    st = ttk.Style(root)
    st.theme_use("clam")  # the only built-in theme that honours custom colours on Windows
    st.configure(".", background=BG, foreground=FG, font=FONT, bordercolor=BORDER,
                 lightcolor=BG, darkcolor=BG, troughcolor=BG, focuscolor=AMBER)
    st.configure("Muted.TLabel", foreground=MUTED, font=FONT_SMALL)
    st.configure("Title.TLabel", font=FONT_TITLE)
    st.configure("TLabelframe", bordercolor=BORDER, padding=GAP * 1.5)
    st.configure("TLabelframe.Label", foreground=MUTED, font=FONT_BOLD)
    st.configure("TButton", background=FIELD, padding=(GAP, GAP // 2 + 1), lightcolor=FIELD, darkcolor=FIELD)
    st.map("TButton", background=[("pressed", BORDER), ("active", "#2c2c3c")],
           bordercolor=[("focus", AMBER)])
    st.configure("Accent.TButton", background=ACCENT, foreground="#000000", font=FONT_BOLD,
                 padding=(GAP * 2, GAP - 1), lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT)
    st.map("Accent.TButton", background=[("pressed", "#c92a3a"), ("active", "#ff6170")])
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=FG,
                 padding=GAP // 2)
    st.map("TCombobox", fieldbackground=[("disabled", BG), ("readonly", FIELD)],
           foreground=[("disabled", BORDER), ("readonly", FG)],
           selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
           bordercolor=[("focus", AMBER)], arrowcolor=[("disabled", BORDER)])
    st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, padding=GAP // 2)
    st.map("TEntry", fieldbackground=[("disabled", BG)], foreground=[("disabled", "#6b6e80")],
           bordercolor=[("focus", AMBER)])
    for opt, val in (("background", FIELD), ("foreground", FG), ("selectBackground", ACCENT),
                     ("selectForeground", "#000000")):
        root.option_add(f"*TCombobox*Listbox.{opt}", val)

    frm = ttk.Frame(root, padding=GAP * 2)
    frm.grid()

    cfg = eng.cfg
    bind = dict(cfg["bind"])
    capturing = [None]

    def key_of(names, shown):
        return next(k for k, v in names.items() if v == shown)

    def card(col, row, title):
        box = ttk.Labelframe(frm, text=title.upper())
        box.grid(row=row, column=col, sticky="new", pady=(GAP, 0), padx=(GAP * 2 if col else 0, 0))
        box.columnconfigure(1, weight=1)
        return box

    def combo(parent, row, label, choices, current):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=GAP // 2, padx=(0, GAP * 2))
        var = tk.StringVar(value=choices[current])
        box = ttk.Combobox(parent, textvariable=var, values=list(choices.values()), state="readonly", width=26)
        box.grid(row=row, column=1, sticky="ew")
        return var, box

    class InputPad:
        """3x3 fighting-game notation grid: eight arrows around a neutral dot. Lit cells are
        filled, unlit ones are outlines, so state never depends on colour alone."""

        def __init__(self, parent, colour, cell=30):
            self.colour, self.lit = colour, None
            self.c = tk.Canvas(parent, width=cell * 3, height=cell * 3, bg=BG, highlightthickness=0)
            self.items = {}
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    cx, cy = (dx + 1.5) * cell, (dy + 1.5) * cell
                    if dx or dy:
                        item = self.c.create_polygon(arrow_points(cx, cy, dx, dy, cell * 0.8), fill=OFF, outline=BORDER)
                    else:
                        item = self.c.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill=OFF, outline=BORDER)
                    self.items[(dx, dy)] = item

        def show(self, cells):
            if cells != self.lit:  # redraw only on change
                self.lit = cells
                for k, item in self.items.items():
                    on = k in cells
                    self.c.itemconfigure(item, fill=self.colour if on else OFF, outline=self.colour if on else BORDER)

    # header: name + at-a-glance state (dot and word, not colour alone)
    head = ttk.Frame(frm)
    head.grid(row=0, column=0, columnspan=2, sticky="ew")
    head.columnconfigure(0, weight=1)
    ttk.Label(head, text="SOCD CLEANER", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    dot = tk.Canvas(head, width=12, height=12, bg=BG, highlightthickness=0)
    dot.grid(row=0, column=1, padx=(0, GAP // 2))
    dot_item = dot.create_oval(1, 1, 11, 11, fill=MUTED, outline="")
    state = ttk.Label(head, text="STARTING", font=FONT_BOLD)
    state.grid(row=0, column=2)

    # live input: what you hold -> what the game gets
    live = card(0, 1, "Live input")
    live.columnconfigure((0, 1, 2), weight=1)
    raw_pad, out_pad = InputPad(live, AMBER, 34), InputPad(live, ACCENT, 34)
    raw_pad.c.grid(row=0, column=0)
    arrow = tk.Canvas(live, width=40, height=24, bg=BG, highlightthickness=0)
    arrow.create_polygon(arrow_points(20, 12, 1, 0, 36), fill=MUTED, outline="")
    arrow.grid(row=0, column=1)
    out_pad.c.grid(row=0, column=2)
    ttk.Label(live, text="Pressed", style="Muted.TLabel").grid(row=1, column=0, pady=(GAP // 2, 0))
    ttk.Label(live, text="Sent to game", style="Muted.TLabel").grid(row=1, column=2, pady=(GAP // 2, 0))
    viewer = ttk.Label(live, text="", style="Muted.TLabel")  # text twin of the two grids
    viewer.grid(row=2, column=0, columnspan=3, pady=(GAP // 2, 0))

    clean_card = card(0, 2, "Cleaning")
    mode_var, _ = combo(clean_card, 0, "SOCD mode", MODES, cfg["mode"])
    on_var = tk.BooleanVar(value=cfg["enabled"])
    box_off, box_on = (tk.PhotoImage(data=_png(check_icon(v))) for v in (False, True))
    tk.Checkbutton(clean_card, text=" Cleaning enabled", variable=on_var, image=box_off, selectimage=box_on,
                   compound="left", indicatoron=False, bd=0, relief="flat", offrelief="flat", font=FONT,
                   bg=BG, fg=FG, selectcolor=BG, activebackground=BG, activeforeground=FG,
                   highlightthickness=1, highlightbackground=BG, highlightcolor=AMBER).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(GAP // 2, 0))

    dev = card(1, 1, "Device")
    ANY = "(first one found)"
    ttk.Label(dev, text="Controller").grid(row=0, column=0, sticky="w", pady=GAP // 2, padx=(0, GAP * 2))
    ctl_var = tk.StringVar(value=cfg["controller"] or ANY)
    ctl_box = ttk.Combobox(dev, textvariable=ctl_var, state="readonly", width=26)
    ctl_box.configure(postcommand=lambda: ctl_box.configure(values=[ANY] + sorted(set(eng.names))))
    ctl_box.grid(row=0, column=1, sticky="ew")
    out_var, out_box = combo(dev, 1, "Output", OUTPUTS, cfg["output"])
    tgt_var, tgt_box = combo(dev, 2, "Pad directions go to", TARGETS, cfg["pad_target"])

    binds = card(1, 2, "Directions")
    ttk.Label(binds, text="Controller button", style="Muted.TLabel").grid(row=0, column=1)
    ttk.Label(binds, text="Keyboard key", style="Muted.TLabel").grid(row=0, column=2)
    bind_btns, key_vars, key_entries = {}, {}, []

    icons, kind = {}, [eng.kind]

    def label_bind(d):
        b = bind[d]
        if (kind[0], b) not in icons:
            icons[kind[0], b] = tk.PhotoImage(data=_png(button_icon(kind[0], b)))
        bind_btns[d].configure(text=" " + LAYOUTS[kind[0]][1][b], image=icons[kind[0], b], compound="left")

    def stop_capture(_event=None):
        if capturing[0]:
            label_bind(capturing[0])
            capturing[0] = None

    def start_capture(d):
        stop_capture()
        capturing[0] = d
        eng.pressed = None
        bind_btns[d].configure(text="Press a button...", image="")

    root.bind("<Escape>", stop_capture)
    VEC = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

    for i, d in enumerate(DIRS):
        name = ttk.Frame(binds)
        name.grid(row=1 + i, column=0, sticky="w", pady=GAP // 2, padx=(0, GAP * 2))
        ico = tk.Canvas(name, width=22, height=22, bg=BG, highlightthickness=0)
        ico.create_polygon(arrow_points(11, 11, *VEC[d], 20), fill=FG, outline="")
        ico.grid(row=0, column=0, padx=(0, GAP))
        ttk.Label(name, text=d.capitalize()).grid(row=0, column=1)
        bind_btns[d] = ttk.Button(binds, width=18, command=lambda d=d: start_capture(d))
        bind_btns[d].grid(row=1 + i, column=1, sticky="ew", padx=(0, GAP), pady=GAP // 4)
        label_bind(d)
        key_vars[d] = tk.StringVar(value=cfg["keys"][d])
        key_entries.append(ttk.Entry(binds, textvariable=key_vars[d], width=8, justify="center"))
        key_entries[-1].grid(row=1 + i, column=2)
    ttk.Label(binds, text="Click a button, then press it on the controller. Esc cancels.",
              style="Muted.TLabel").grid(row=5, column=0, columnspan=3, sticky="w", pady=(GAP // 2, 0))

    def grey_out(*_):  # only the options that apply to the chosen output stay editable
        pad_mode = key_of(OUTPUTS, out_var.get()) == "pad"
        tgt_box.configure(state="readonly" if pad_mode else "disabled")
        for e in key_entries:
            e.configure(state="disabled" if pad_mode else "normal")

    out_var.trace_add("write", grey_out)
    grey_out()

    # footer: result of the last save sits right next to the button that caused it
    foot = ttk.Frame(frm)
    foot.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(GAP * 1.5, 0))
    foot.columnconfigure(0, weight=1)
    note = ttk.Label(foot, text="", wraplength=520)
    note.grid(row=0, column=0, sticky="w")
    status = ttk.Label(frm, text="", style="Muted.TLabel", wraplength=700)
    status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(GAP, 0))

    def say(text, ok=True):
        note.configure(text=text, foreground=GOOD if ok else BAD)

    def commit(new):
        """Validate, apply to the engine, write to disk. Returns False (with a note) if invalid."""
        try:
            check_cfg(new)
        except ValueError as e:
            say(f"Not saved: {e}.", ok=False)
            return False
        try:
            save_cfg(new)
            say("Saved and applied.")
        except OSError as e:
            say(f"Applied, but could not write {CFG_PATH.name}: {e}.", ok=False)
        old, eng.cfg = eng.cfg, new
        # Only these need the controller / virtual pad reopened; everything else applies live,
        # so toggling mid-game doesn't unplug the virtual pad.
        if any(old[k] != new[k] for k in ("controller", "output")):
            eng.reload = True
        mode_var.set(MODES[new["mode"]])
        on_var.set(new["enabled"])
        icon.update_menu()
        return True

    def save():
        stop_capture()
        commit({"controller": "" if ctl_var.get() == ANY else ctl_var.get(),
                "mode": key_of(MODES, mode_var.get()), "output": key_of(OUTPUTS, out_var.get()),
                "pad_target": key_of(TARGETS, tgt_var.get()), "enabled": on_var.get(), "bind": dict(bind),
                "keys": {d: key_vars[d].get().strip().lower() for d in DIRS}})

    ttk.Button(foot, text="Save and apply", style="Accent.TButton", command=save).grid(row=0, column=1, sticky="e")
    root.bind("<Return>", lambda e: save())

    try:  # dark title bar to match (Windows 10 2004+ / 11)
        root.update_idletasks()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(root.winfo_id()), 20,
                                                   ctypes.byref(ctypes.c_int(1)), 4)
    except (OSError, AttributeError):
        pass

    ui_q = queue.SimpleQueue()  # tray callbacks run on another thread; tkinter must not be touched from there
    img = Image.new("RGB", (64, 64), BG)
    draw = ImageDraw.Draw(img)
    for dx, dy in VEC.values():
        draw.polygon(arrow_points(32 + dx * 17, 32 + dy * 17, dx, dy, 26), fill=ACCENT)
    root.iconphoto(True, tk.PhotoImage(data=_png(img)))

    def mode_item(k):
        return pystray.MenuItem(MODES[k], lambda: ui_q.put(("mode", k)),
                                checked=lambda item: eng.cfg["mode"] == k, radio=True)

    icon = pystray.Icon("socd", img, "SOCD Cleaner", menu=pystray.Menu(
        pystray.MenuItem("Settings", lambda: ui_q.put(("show", None)), default=True),
        pystray.MenuItem("Cleaning enabled", lambda: ui_q.put(("toggle", None)),
                         checked=lambda item: eng.cfg["enabled"]),
        pystray.MenuItem("SOCD mode", pystray.Menu(*[mode_item(k) for k in MODES])),
        pystray.MenuItem("Start with Windows", lambda: ui_q.put(("autostart", None)),
                         checked=lambda item: autostart()),
        pystray.MenuItem("Quit", lambda: ui_q.put(("quit", None)))))
    icon.run_detached()
    shown_status = [None]

    def poll():
        while not ui_q.empty():
            what, arg = ui_q.get()
            if what == "quit":
                icon.stop()
                eng.running = False
                eng.join(1)
                root.destroy()
                return
            if what == "show":
                root.deiconify()
                root.lift()
                root.focus_force()
            elif what == "mode":
                commit({**eng.cfg, "mode": arg})
            elif what == "toggle":
                commit({**eng.cfg, "enabled": not eng.cfg["enabled"]})
            elif what == "autostart":
                try:
                    autostart(not autostart())
                except OSError as e:
                    say(f"Could not change startup entry: {e}.", ok=False)
                icon.update_menu()
        d = capturing[0]
        if d and eng.pressed is not None:
            # binding a button another direction already uses swaps the two, instead of failing on save
            other = next((o for o in DIRS if o != d and bind[o] == eng.pressed), None)
            if other:
                bind[other] = bind[d]
                label_bind(other)
            bind[d] = eng.pressed
            label_bind(d)
            capturing[0] = None
        if eng.kind != kind[0]:  # a different controller family connected: relabel with its icons
            kind[0] = eng.kind
            for o in DIRS:
                if o != capturing[0]:
                    label_bind(o)

        raw, x, y = eng.view
        raw_pad.show(frozenset(VEC[r] for r in raw) or frozenset({(0, 0)}))
        out_pad.show(frozenset({(x, y)}))
        out = (ARROW["up"] if y < 0 else ARROW["down"] if y > 0 else "") + \
              (ARROW["left"] if x < 0 else ARROW["right"] if x > 0 else "")
        viewer.configure(text=f"{' '.join(ARROW[r] for r in raw) or 'neutral'}  →  {out or 'neutral'}"
                              f"   ·   {MODES[eng.cfg['mode']] if eng.cfg['enabled'] else 'cleaning off'}")

        s = eng.status
        word, colour = (("CLEANING OFF", AMBER) if s.startswith("Running") and not eng.cfg["enabled"]
                        else ("RUNNING", GOOD) if s.startswith("Running")
                        else ("WAITING", AMBER) if s.startswith(("Waiting", "Starting", "Controller disconnected"))
                        else ("PROBLEM", BAD))
        state.configure(text=word, foreground=colour)
        dot.itemconfigure(dot_item, fill=colour)
        status.configure(text=s + (f"  \u00b7  {LAYOUTS[kind[0]][0]} button layout" if s.startswith("Running") else ""))
        if s != shown_status[0]:  # tooltip + toast on connect / disconnect / errors
            first, shown_status[0] = shown_status[0] is None, s
            try:
                icon.title = f"SOCD Cleaner - {s}"[:120]  # Windows caps tooltips at 127 chars
                if not first and not s.startswith(("Waiting", "Starting")):
                    icon.notify(s[:200], "SOCD Cleaner")
            except Exception:
                pass  # tray icon not ready yet; next status change will retry
        root.after(50, poll)

    if not first_run:
        root.withdraw()
    poll()
    root.mainloop()


def _png(img):
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


if __name__ == "__main__":
    main()

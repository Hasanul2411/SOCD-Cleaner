"""SOCD cleaner for any controller (XInput, DInput, HID - anything SDL recognises).

Reads a physical controller, resolves opposing directions, and outputs either a
virtual Xbox 360 pad (ViGEmBus) or keyboard keys. See README.md.
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
from ctypes import wintypes as w
from pathlib import Path

CFG_PATH = Path(sys.argv[0]).resolve().with_name("socd_config.json")
DB_PATH = CFG_PATH.with_name("gamecontrollerdb.txt")

DIRS = ("up", "down", "left", "right")
MODES = {"last": "Last input priority", "first": "First input priority",
         "neutral": "Neutral", "up": "Up priority"}
OUTPUTS = {"pad": "Virtual Xbox pad", "keyboard": "Keyboard keys"}
TARGETS = {"dpad": "D-pad", "stick": "Left stick", "both": "D-pad + left stick"}

# SDL GameController button indices, in order.
BUTTONS = ["A", "B", "X", "Y", "Back", "Guide", "Start", "L3", "R3", "LB", "RB",
           "D-pad up", "D-pad down", "D-pad left", "D-pad right",
           "Misc", "Paddle 1", "Paddle 2", "Paddle 3", "Paddle 4", "Touchpad"]
# XInput wButtons bit for each of the first 15 SDL buttons (the rest have no Xbox 360 equivalent).
XBITS = [0x1000, 0x2000, 0x4000, 0x8000, 0x0020, 0x0400, 0x0010, 0x0040, 0x0080,
         0x0100, 0x0200, 0x0001, 0x0002, 0x0004, 0x0008]

DEFAULTS = {"controller": "", "mode": "last", "output": "pad", "pad_target": "dpad",
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


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(CFG_PATH.read_text())
        for k in cfg:
            if k in saved:
                cfg[k] = {**cfg[k], **saved[k]} if isinstance(cfg[k], dict) else saved[k]
    except (OSError, ValueError, TypeError):
        pass  # missing or corrupt config: start from defaults
    return cfg


# --- keyboard output (scancodes, because games ignore virtual-key-only input) ---

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    # pad makes the struct as large as the real INPUT union (MOUSEINPUT is the biggest member)
    _fields_ = [("type", w.DWORD), ("ki", KEYBDINPUT), ("pad", ctypes.c_ubyte * 8)]


ARROWS = {"up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D}


def scancode(name):
    """Key name -> (scancode, extended flag). Accepts one character or an arrow name."""
    name = name.strip().lower()
    if name in ARROWS:
        return ARROWS[name], 1
    if len(name) == 1:
        u = ctypes.windll.user32
        sc = u.MapVirtualKeyW(u.VkKeyScanW(ord(name)) & 0xFF, 0)
        if sc:
            return sc, 0
    raise ValueError(f"Unknown key {name!r}: use one character or up/down/left/right")


def send_key(name, down):
    sc, ext = scancode(name)
    inp = INPUT(type=1, ki=KEYBDINPUT(0, sc, 0x8 | ext | (0 if down else 0x2), 0, 0))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


# --- engine: polls the controller at ~1 kHz on its own thread ---

class Engine(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.reload = False
        self.running = True
        self.status = "Starting"
        self.names = []      # controllers currently visible, for the settings window
        self.pressed = None  # last button pressed, for binding capture

    def run(self):
        os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"  # we never have focus
        if DB_PATH.exists():  # optional community mappings for obscure DInput devices
            os.environ["SDL_GAMECONTROLLERCONFIG_FILE"] = str(DB_PATH)
        import pygame
        from pygame._sdl2 import controller
        pygame.init()
        controller.init()

        ctl = pad = sent = None
        keys = dict.fromkeys(DIRS)  # dir -> key name currently held down
        held = dict.fromkeys(DIRS, False)
        last = {"x": 1, "y": 1}
        prev = [0] * len(BUTTONS)
        next_scan = 0

        def release():
            for d, name in keys.items():
                if name:
                    send_key(name, False)
                    keys[d] = None

        while self.running:
            try:
                pygame.event.pump()
                if time.monotonic() >= next_scan:
                    next_scan = time.monotonic() + 1
                    found = [controller.name_forindex(i) or f"Controller {i}"
                             for i in range(controller.get_count()) if controller.is_controller(i)]
                    self.names = found
                cfg = self.cfg
                if self.reload or (ctl and not ctl.attached()):
                    self.reload = False
                    release()
                    # Drop the virtual pad before rescanning so we can never open our own output.
                    # shortcut: controllers are matched by name, so two identical pads can't be told apart; match by GUID if that matters
                    ctl = pad = sent = None
                    next_scan = 0
                    continue
                if ctl is None:
                    want = cfg["controller"]
                    idx = next((i for i in range(controller.get_count()) if controller.is_controller(i)
                                and (not want or controller.name_forindex(i) == want)), None)
                    if idx is None:
                        self.status = f"Waiting for {want or 'a controller'}"
                        time.sleep(0.25)
                        continue
                    ctl = controller.Controller(idx)
                    self.status = f"Running on {ctl.name}"
                    if cfg["output"] == "pad":
                        try:
                            import vgamepad
                            pad = vgamepad.VX360Gamepad()
                        except Exception as e:  # driver missing: keep reading so binding still works
                            self.status = f"No virtual pad (is ViGEmBus installed?): {e}"
                    continue

                btn = [ctl.get_button(b) for b in range(len(BUTTONS))]
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
                x = clean(cfg["mode"], held["left"], held["right"], last["x"])
                y = clean(cfg["mode"], held["up"], held["down"], last["y"], vertical=True)

                if cfg["output"] == "keyboard":
                    active = {"up": y < 0, "down": y > 0, "left": x < 0, "right": x > 0}
                    for d in DIRS:
                        if active[d] != bool(keys[d]):
                            if active[d]:
                                keys[d] = cfg["keys"][d]
                                send_key(keys[d], True)
                            else:
                                send_key(keys[d], False)
                                keys[d] = None
                elif pad:
                    # Everything except the bound direction buttons passes straight through.
                    bound = set(bind.values())
                    mask = sum(XBITS[b] for b in range(len(XBITS)) if btn[b] and b not in bound)
                    lx, ly = ctl.get_axis(0), min(32767, -ctl.get_axis(1))  # SDL Y is down-positive
                    if cfg["pad_target"] in ("dpad", "both"):
                        mask |= (y < 0) * 1 | (y > 0) * 2 | (x < 0) * 4 | (x > 0) * 8
                    if cfg["pad_target"] in ("stick", "both") and (x or y):
                        lx, ly = x * 32767, -y * 32767
                    rep = (mask, ctl.get_axis(4) >> 7, ctl.get_axis(5) >> 7, lx, ly,
                           ctl.get_axis(2), min(32767, -ctl.get_axis(3)))
                    if rep != sent:
                        r = pad.report
                        (r.wButtons, r.bLeftTrigger, r.bRightTrigger,
                         r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY) = rep
                        pad.update()
                        sent = rep
                time.sleep(0.001)
            except Exception as e:  # e.g. controller yanked mid-read: report, then reconnect
                self.status = f"Error: {e}"
                self.reload = True
                time.sleep(1)
        release()


# --- tray icon + settings window ---

def main():
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
    root.protocol("WM_DELETE_WINDOW", root.withdraw)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    cfg = eng.cfg
    bind = dict(cfg["bind"])
    capturing = [None]

    def combo(row, label, choices, current):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=choices.get(current, next(iter(choices.values()))))
        ttk.Combobox(frm, textvariable=var, values=list(choices.values()), state="readonly",
                     width=28).grid(row=row, column=1, columnspan=2, sticky="ew")
        return var

    ttk.Label(frm, text="Controller").grid(row=0, column=0, sticky="w", pady=3)
    ctl_var = tk.StringVar(value=cfg["controller"])
    ctl_box = ttk.Combobox(frm, textvariable=ctl_var, state="readonly", width=28)
    ctl_box.configure(postcommand=lambda: ctl_box.configure(values=[""] + eng.names))
    ctl_box.grid(row=0, column=1, columnspan=2, sticky="ew")
    mode_var = combo(1, "SOCD mode", MODES, cfg["mode"])
    out_var = combo(2, "Output", OUTPUTS, cfg["output"])
    tgt_var = combo(3, "Pad directions go to", TARGETS, cfg["pad_target"])

    ttk.Label(frm, text="Controller button").grid(row=4, column=1, pady=(10, 0))
    ttk.Label(frm, text="Keyboard key").grid(row=4, column=2, pady=(10, 0))
    bind_btns, key_vars = {}, {}

    def start_capture(d):
        if capturing[0]:
            bind_btns[capturing[0]].configure(text=BUTTONS[bind[capturing[0]]])
        capturing[0] = d
        eng.pressed = None
        bind_btns[d].configure(text="Press a button...")

    for i, d in enumerate(DIRS):
        ttk.Label(frm, text=d.capitalize()).grid(row=5 + i, column=0, sticky="w", pady=2)
        bind_btns[d] = ttk.Button(frm, text=BUTTONS[bind[d]], width=18, command=lambda d=d: start_capture(d))
        bind_btns[d].grid(row=5 + i, column=1, padx=4)
        key_vars[d] = tk.StringVar(value=cfg["keys"][d])
        ttk.Entry(frm, textvariable=key_vars[d], width=8).grid(row=5 + i, column=2)

    status = ttk.Label(frm, text="", wraplength=340)
    status.grid(row=10, column=0, columnspan=3, sticky="w", pady=(10, 0))
    saved_note = [""]

    def save():
        def key_of(names, shown):
            return next(k for k, v in names.items() if v == shown)
        keys = {d: key_vars[d].get().strip().lower() for d in DIRS}
        try:
            for k in keys.values():
                scancode(k)
            if len(set(bind.values())) < 4 or len(set(keys.values())) < 4:
                raise ValueError("Each direction needs its own button and its own key")
        except ValueError as e:
            saved_note[0] = f"Not saved. {e}"
            return
        new = {"controller": ctl_var.get(), "mode": key_of(MODES, mode_var.get()),
               "output": key_of(OUTPUTS, out_var.get()), "pad_target": key_of(TARGETS, tgt_var.get()),
               "bind": dict(bind), "keys": keys}
        try:
            CFG_PATH.write_text(json.dumps(new, indent=2))
            saved_note[0] = "Saved."
        except OSError as e:
            saved_note[0] = f"Applied, but could not write {CFG_PATH.name}: {e}"
        eng.cfg = new
        eng.reload = True

    ttk.Button(frm, text="Save and apply", command=save).grid(row=9, column=0, columnspan=3, pady=(12, 0))

    ui_q = queue.SimpleQueue()  # tray callbacks run on another thread; tkinter must not be touched from there
    img = Image.new("RGB", (64, 64), "#1e1e2e")
    draw = ImageDraw.Draw(img)
    draw.rectangle((26, 8, 38, 56), fill="#f5c542")
    draw.rectangle((8, 26, 56, 38), fill="#f5c542")
    icon = pystray.Icon("socd", img, "SOCD Cleaner", menu=pystray.Menu(
        pystray.MenuItem("Settings", lambda: ui_q.put("show"), default=True),
        pystray.MenuItem("Quit", lambda: ui_q.put("quit"))))
    icon.run_detached()

    def poll():
        while not ui_q.empty():
            if ui_q.get() == "quit":
                icon.stop()
                eng.running = False
                eng.join(1)
                root.destroy()
                return
            root.deiconify()
            root.lift()
        d = capturing[0]
        if d and eng.pressed is not None:
            bind[d] = eng.pressed
            bind_btns[d].configure(text=BUTTONS[bind[d]])
            capturing[0] = None
        status.configure(text=f"{saved_note[0]} {eng.status}".strip())
        root.after(50, poll)

    if not first_run:
        root.withdraw()
    poll()
    root.mainloop()


if __name__ == "__main__":
    main()

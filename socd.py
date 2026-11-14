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
    root.protocol("WM_DELETE_WINDOW", root.withdraw)
    frm = ttk.Frame(root, padding=12)
    frm.grid()

    cfg = eng.cfg
    bind = dict(cfg["bind"])
    capturing = [None]

    def combo(row, label, choices, current):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=choices[current])
        box = ttk.Combobox(frm, textvariable=var, values=list(choices.values()), state="readonly", width=28)
        box.grid(row=row, column=1, columnspan=2, sticky="ew")
        return var, box

    def key_of(names, shown):
        return next(k for k, v in names.items() if v == shown)

    ANY = "(first one found)"
    ttk.Label(frm, text="Controller").grid(row=0, column=0, sticky="w", pady=3)
    ctl_var = tk.StringVar(value=cfg["controller"] or ANY)
    ctl_box = ttk.Combobox(frm, textvariable=ctl_var, state="readonly", width=28)
    ctl_box.configure(postcommand=lambda: ctl_box.configure(values=[ANY] + sorted(set(eng.names))))
    ctl_box.grid(row=0, column=1, columnspan=2, sticky="ew")
    mode_var, _ = combo(1, "SOCD mode", MODES, cfg["mode"])
    out_var, out_box = combo(2, "Output", OUTPUTS, cfg["output"])
    tgt_var, tgt_box = combo(3, "Pad directions go to", TARGETS, cfg["pad_target"])
    on_var = tk.BooleanVar(value=cfg["enabled"])
    ttk.Checkbutton(frm, text="Cleaning enabled", variable=on_var).grid(row=4, column=0, columnspan=3, sticky="w", pady=3)

    ttk.Label(frm, text="Controller button").grid(row=5, column=1, pady=(10, 0))
    ttk.Label(frm, text="Keyboard key").grid(row=5, column=2, pady=(10, 0))
    bind_btns, key_vars, key_entries = {}, {}, []

    def stop_capture(_event=None):
        if capturing[0]:
            bind_btns[capturing[0]].configure(text=BUTTONS[bind[capturing[0]]])
            capturing[0] = None

    def start_capture(d):
        stop_capture()
        capturing[0] = d
        eng.pressed = None
        bind_btns[d].configure(text="Press a button (Esc cancels)")

    root.bind("<Escape>", stop_capture)

    for i, d in enumerate(DIRS):
        ttk.Label(frm, text=f"{ARROW[d]} {d.capitalize()}").grid(row=6 + i, column=0, sticky="w", pady=2)
        bind_btns[d] = ttk.Button(frm, text=BUTTONS[bind[d]], width=26, command=lambda d=d: start_capture(d))
        bind_btns[d].grid(row=6 + i, column=1, padx=4)
        key_vars[d] = tk.StringVar(value=cfg["keys"][d])
        key_entries.append(ttk.Entry(frm, textvariable=key_vars[d], width=8))
        key_entries[-1].grid(row=6 + i, column=2)

    def grey_out(*_):  # only the options that apply to the chosen output stay editable
        pad_mode = key_of(OUTPUTS, out_var.get()) == "pad"
        tgt_box.configure(state="readonly" if pad_mode else "disabled")
        for e in key_entries:
            e.configure(state="disabled" if pad_mode else "normal")

    out_var.trace_add("write", grey_out)
    grey_out()

    viewer = ttk.Label(frm, text="")
    viewer.grid(row=11, column=0, columnspan=3, sticky="w", pady=(10, 0))
    status = ttk.Label(frm, text="", wraplength=380)
    status.grid(row=12, column=0, columnspan=3, sticky="w", pady=(4, 0))
    saved_note = [""]

    def commit(new):
        """Validate, apply to the engine, write to disk. Returns False (with a note) if invalid."""
        try:
            check_cfg(new)
        except ValueError as e:
            saved_note[0] = f"Not saved. {e}."
            return False
        try:
            save_cfg(new)
            saved_note[0] = "Saved."
        except OSError as e:
            saved_note[0] = f"Applied, but could not write {CFG_PATH.name}: {e}."
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

    ttk.Button(frm, text="Save and apply", command=save).grid(row=10, column=0, columnspan=3, pady=(12, 0))
    root.bind("<Return>", lambda e: save())

    ui_q = queue.SimpleQueue()  # tray callbacks run on another thread; tkinter must not be touched from there
    img = Image.new("RGB", (64, 64), "#1e1e2e")
    draw = ImageDraw.Draw(img)
    draw.rectangle((26, 8, 38, 56), fill="#f5c542")
    draw.rectangle((8, 26, 56, 38), fill="#f5c542")

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
                    saved_note[0] = f"Could not change startup entry: {e}."
                icon.update_menu()
        d = capturing[0]
        if d and eng.pressed is not None:
            # binding a button another direction already uses swaps the two, instead of failing on save
            other = next((o for o in DIRS if o != d and bind[o] == eng.pressed), None)
            if other:
                bind[other] = bind[d]
                bind_btns[other].configure(text=BUTTONS[bind[other]])
            bind[d] = eng.pressed
            bind_btns[d].configure(text=BUTTONS[bind[d]])
            capturing[0] = None
        raw, x, y = eng.view
        out = (ARROW["up"] if y < 0 else ARROW["down"] if y > 0 else "") + \
              (ARROW["left"] if x < 0 else ARROW["right"] if x > 0 else "")
        viewer.configure(text=f"Pressed: {' '.join(ARROW[r] for r in raw) or '-'}     "
                              f"Sent: {out or '-'}{'' if eng.cfg['enabled'] else '  (cleaning off)'}")
        status.configure(text=f"{saved_note[0]} {eng.status}".strip())
        if eng.status != shown_status[0]:  # tooltip + toast on connect / disconnect / errors
            first, shown_status[0] = shown_status[0] is None, eng.status
            try:
                icon.title = f"SOCD Cleaner - {eng.status}"[:120]  # Windows caps tooltips at 127 chars
                if not first and not eng.status.startswith(("Waiting", "Starting")):
                    icon.notify(eng.status[:200], "SOCD Cleaner")
            except Exception:
                pass  # tray icon not ready yet; next status change will retry
        root.after(50, poll)

    if not first_run:
        root.withdraw()
    poll()
    root.mainloop()


if __name__ == "__main__":
    main()

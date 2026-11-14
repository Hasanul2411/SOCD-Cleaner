"""SOCD cleaner for any controller (XInput, DInput, HID - anything SDL recognises).

Reads a physical controller, resolves opposing directions, and outputs either a
virtual Xbox 360 pad (ViGEmBus) or keyboard keys. See README.md.
"""
import ctypes
import json
import math
import os
import queue
import re
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
MODES = {"last": "Last input priority", "last_nr": "Last input, no re-press",
         "first": "First input priority", "neutral": "Neutral", "up": "Up priority"}
# Rules for a free pair of buttons (hitboxer's OPPOSITE / OPPOSITE-no-repress / NEUTRAL, plus two more).
PAIR_MODES = {"last": "Last pressed wins", "last_nr": "Last pressed wins, no re-press",
              "first": "First pressed wins", "neutral": "Both cancel out", "a": "Button 1 always wins"}
MAX_PAIRS = 8
ALL, MAX_PADS = "*", 4  # config value for "clean every controller"; XInput has four player slots
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

DEFAULTS = {"controller": "", "mode": "last", "output": "pad", "pad_target": "dpad", "enabled": True, "theme": "Midnight", "hide": True,
            "bind": {"up": 11, "down": 12, "left": 13, "right": 14},
            "keys": {"up": "w", "down": "s", "left": "a", "right": "d"},
            "pairs": []}  # each: {"a": button, "b": button, "mode": PAIR_MODES key, "key_a": "", "key_b": ""}


def clean(mode, neg, pos, last, vertical=False):
    """Resolve one axis. neg/pos: is up|left / down|right held; last: -1/+1, the side
    pressed most recently. Returns -1, 0 or +1."""
    if not (neg and pos):
        return pos - neg
    if mode == "last":
        return last
    if mode == "first":
        return -last
    if mode == "a" or (mode == "up" and vertical):
        return -1
    return 0  # neutral, and the horizontal axis of "up"


class Pair:
    """Two opposing inputs. Remembers what clean() cannot: which side was pressed last,
    and for "last_nr" which side lost and must be pressed again before it counts."""

    def __init__(self):
        self.a = self.b = False
        self.last = 1
        self.blocked = 0

    def update(self, mode, a, b, vertical=False):
        if a and not self.a:
            self.last = -1
        if b and not self.b:
            self.last = 1
        self.a, self.b = a, b
        if mode != "last_nr":
            self.blocked = 0
            return clean(mode, a, b, self.last, vertical)
        if a and b:
            self.blocked = -self.last  # the loser stays off until released and pressed again
        elif not (a if self.blocked < 0 else b):
            self.blocked = 0
        v = clean("last", a, b, self.last)
        return 0 if v == self.blocked else v


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
    def button(v):
        return type(v) is int and 0 <= v < len(BUTTONS)
    try:
        pairs = cfg["pairs"]
        ok = (cfg["mode"] in MODES and cfg["output"] in OUTPUTS and cfg["pad_target"] in TARGETS
              and isinstance(cfg["controller"], str) and isinstance(cfg["enabled"], bool)
              and isinstance(cfg["hide"], bool)
              and cfg["theme"] in THEMES and all(button(cfg["bind"][d]) for d in DIRS)
              and isinstance(pairs, list) and len(pairs) <= MAX_PAIRS
              and all(button(p["a"]) and button(p["b"]) and p["mode"] in PAIR_MODES
                      and isinstance(p["key_a"], str) and isinstance(p["key_b"], str) for p in pairs))
        keys = [cfg["keys"][d] for d in DIRS] + [k for p in pairs for k in (p["key_a"], p["key_b"]) if k]
        buttons = [cfg["bind"][d] for d in DIRS] + [v for p in pairs for v in (p["a"], p["b"])]
    except (KeyError, TypeError):
        ok = False
    if not ok or not all(isinstance(k, str) for k in keys):
        raise ValueError("Settings are invalid")
    codes = [scancode(k) for k in keys]  # scancodes, not names, so "W" and "w" count as the same key
    if len(set(buttons)) < len(buttons):
        raise ValueError("A controller button is used twice")
    if len(set(codes)) < len(codes):
        raise ValueError("A keyboard key is used twice")


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(CFG_PATH.read_text())
        for k in cfg:
            if k in saved:
                cfg[k] = {**cfg[k], **saved[k]} if isinstance(cfg[k], dict) else saved[k]
        if cfg["theme"] not in THEMES:  # theme removed in an update: keep the rest of the settings
            cfg["theme"] = DEFAULTS["theme"]
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


# --- hiding the real controller, so games only see the cleaned virtual pad ---
# Only a kernel driver can hide a device from other programs; HidHide is that driver. We talk to
# its documented control device directly (https://github.com/nefarius/HidHide/blob/master/DEVELOPER.md)
# so the user never has to open its configuration tool or replug anything.

def guid_vid_pid(guid):
    """USB vendor / product id out of an SDL joystick GUID, or (0, 0) when it carries none."""
    if len(guid) != 32 or guid.startswith("78696e707574"):  # "xinput": old-style GUID without ids
        return 0, 0
    return int(guid[10:12] + guid[8:10], 16), int(guid[18:20] + guid[16:18], 16)


def is_device(instance_id, vid, pid):
    """Does this Windows device instance id belong to vid:pid? Covers USB / HID ("VID_0F0D&PID_0184")
    and Bluetooth HID ("VID&0002054C_PID&09CC")."""
    return bool(re.search(rf"VID(_|&[0-9A-F]{{4}}){vid:04X}[&_]PID[_&]{pid:04X}", instance_id.upper()))


def device_instances(vid, pid):
    """Instance ids of every connected device node of that controller. An Xbox-style pad has a USB
    node and a HID node, and games reach it through either, so all of them must be hidden."""
    cm, n = ctypes.windll.cfgmgr32, w.ULONG()
    if cm.CM_Get_Device_ID_List_SizeW(ctypes.byref(n), None, 0x100):  # 0x100 = present devices only
        return []
    buf = ctypes.create_unicode_buffer(n.value)
    if cm.CM_Get_Device_ID_ListW(None, buf, n.value, 0x100):
        return []
    return [i for i in buf[:n.value].split("\0") if i and is_device(i, vid, pid) and not is_virtual(i)]


def is_virtual(instance_id):
    """True for devices made by a software bus such as ViGEm (our own virtual pads, which share
    the ids of a real Xbox 360 pad): one of their nearest ancestors is root-enumerated."""
    cm, node, buf = ctypes.windll.cfgmgr32, w.DWORD(), ctypes.create_unicode_buffer(256)
    if cm.CM_Locate_DevNodeW(ctypes.byref(node), instance_id, 0):
        return False
    for _ in range(3):
        if cm.CM_Get_Parent(ctypes.byref(node), node, 0) or cm.CM_Get_Device_IDW(node, buf, 256, 0):
            return False
        if buf.value.upper().startswith("ROOT\\"):
            return True
    return False


# Kernel drivers the virtual-pad output needs: Windows service name -> bundled official installer.
DRIVERS = {"ViGEmBus": "vgamepad/win/vigem/install/x64/ViGEmBusSetup_x64.msi",
           "HidHide": "drivers/HidHide_1.5.230_x64.exe"}


def missing_drivers():
    import winreg
    missing = []
    for service in DRIVERS:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{service}"))
        except OSError:
            missing.append(service)
    return missing


def driver_installer(service):
    """Path of the bundled installer, or None if this copy of the program does not carry it."""
    import importlib.util
    rel = DRIVERS[service]
    if rel.startswith("vgamepad/") and not getattr(sys, "frozen", False):
        spec = importlib.util.find_spec("vgamepad")  # located without importing: importing needs the driver
        base = Path(spec.origin).parent.parent if spec and spec.origin else Path()
    else:
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    path = base / rel
    return path if path.is_file() else None


def install_drivers(services):
    """Run each installer elevated, one after another (Windows shows its administrator prompt).
    Returns the services still missing afterwards."""
    import subprocess
    for service in services:
        path = driver_installer(service)
        if not path:
            continue
        quoted = str(path).replace("'", "''")
        target = (f"-FilePath msiexec -ArgumentList '/i \"{quoted}\"'" if path.suffix == ".msi"
                  else f"-FilePath '{quoted}'")
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Start-Process {target} -Verb RunAs -Wait"],
                       creationflags=0x08000000)  # CREATE_NO_WINDOW; failure or a declined prompt just leaves it missing
    return missing_drivers()


def nt_path(path):
    r"""C:\dir\app.exe -> \Device\HarddiskVolume3\dir\app.exe, the form HidHide's whitelist uses."""
    path = str(Path(path).resolve())
    buf = ctypes.create_unicode_buffer(512)
    if not ctypes.windll.kernel32.QueryDosDeviceW(path[:2], buf, 512):
        raise OSError(f"cannot resolve drive {path[:2]}")
    return buf.value + path[2:]


def multi_sz(strings):
    return ("".join(x + "\0" for x in strings) + "\0").encode("utf-16-le") if strings else b"\0\0\0\0"


def from_multi_sz(raw):
    return [x for x in raw.decode("utf-16-le").split("\0") if x]


def hidhide_code(function):
    return (32769 << 16) | (1 << 14) | (function << 2)  # CTL_CODE(32769, n, METHOD_BUFFERED, FILE_READ_DATA)


class Hider:
    """Hides device instances from every program except this one, through HidHide.
    Every method raises OSError when HidHide is not installed or refuses."""
    GET_APPS, SET_APPS, GET_DEVS, SET_DEVS, GET_ON, SET_ON, GET_INVERSE = 2048, 2049, 2050, 2051, 2052, 2053, 2054
    ADD_SESSION, CLR_SESSION = 2056, 2057

    def __init__(self):
        self.h, self.session, self.added = None, False, []
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateFileW.restype = ctypes.c_void_p
        self.k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
        self.k.DeviceIoControl.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD,
                                           ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        self.k.CloseHandle.argtypes = [ctypes.c_void_p]

    def _io(self, function, data=None, out=0):
        """out: 0 = nothing back, 1 = one BOOLEAN, -1 = a list of unknown size (ask for the size first)."""
        if self.h is None:
            h = self.k.CreateFileW(r"\\.\HidHide", 0x80000000, 7, None, 3, 0x80, None)  # GENERIC_READ, share all, OPEN_EXISTING
            if h in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            self.h = h
        got = w.DWORD()

        def call(buf, size):
            if not self.k.DeviceIoControl(self.h, hidhide_code(function), data, len(data or b""), buf, size, ctypes.byref(got), None):
                raise ctypes.WinError(ctypes.get_last_error())
        if out == 0:
            call(None, 0)
            return b""
        if out < 0:
            call(None, 0)
            out = got.value
        buf = ctypes.create_string_buffer(max(out, 1))
        call(buf, out)
        return buf.raw[:got.value]

    def allow_self(self):
        """Put this program on HidHide's application list so it can still read the hidden controller."""
        me = nt_path(sys.executable)
        apps = from_multi_sz(self._io(self.GET_APPS, out=-1))
        listed = [a for a in apps if a.lower() == me.lower()]
        if self._io(self.GET_INVERSE, out=1) == b"\1":  # inverse mode: the list is a block list
            if listed:
                self._io(self.SET_APPS, multi_sz([a for a in apps if a not in listed]))
        elif not listed:
            self._io(self.SET_APPS, multi_sz(apps + [me]))

    def hide(self, ids):
        self.allow_self()
        try:  # newer drivers: entries vanish by themselves when this process exits, even on a crash
            self._io(self.ADD_SESSION, multi_sz(ids))
            self.session = True
        except OSError:  # older drivers: use the saved list and take our entries out again in unhide()
            devs = from_multi_sz(self._io(self.GET_DEVS, out=-1))
            have = {d.lower() for d in devs}
            new = [i for i in ids if i.lower() not in have]
            if new:
                self._io(self.SET_DEVS, multi_sz(devs + new))
                self.added += new
        if self._io(self.GET_ON, out=1) != b"\1":
            self._io(self.SET_ON, b"\1")

    def unhide(self):
        """Give the real controller back (best effort; never raises)."""
        try:
            if self.session:
                self._io(self.CLR_SESSION)
            elif self.added:
                gone = {a.lower() for a in self.added}
                self._io(self.SET_DEVS, multi_sz([d for d in from_multi_sz(self._io(self.GET_DEVS, out=-1))
                                                  if d.lower() not in gone]))
        except OSError:
            pass
        self.session, self.added = False, []


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

        hider = Hider()
        if self.cfg["hide"]:
            try:  # before the first scan: if a controller is already hidden we must be allowed to see it
                hider.allow_self()
            except OSError:
                pass  # HidHide not installed; reported when a controller connects
        slots = {}     # SDL instance id -> Slot, one per physical controller being cleaned
        virtual = set()  # instance ids of our own virtual pads: never read those back
        pending = []   # expiry times of virtual pads created but not yet seen in a scan
        keys = {}      # output slot (direction name or pair index) -> key name currently held down
        next_scan = 0

        def release():
            for d, name in keys.items():
                if name:
                    send_key(name, False)
                    keys[d] = None

        def open_slot(i, name, guid, cfg):
            slot = Slot(controller.Controller(i), name)
            if cfg["output"] != "pad":
                return slot
            try:
                import vgamepad
                slot.pad = vgamepad.VX360Gamepad()
                pending.append(time.monotonic() + 3)
            except Exception as e:  # driver missing: keep reading so binding still works
                slot.error = f"No virtual pad (is ViGEmBus installed?): {e}"
                return slot

            def on_rumble(client, target, large, small, led, user_data):
                slot.rumble = (large, small)  # called on a ViGEm thread; applied in the main loop
            try:
                slot.on_rumble = on_rumble  # keep a reference alive for the driver callback
                slot.pad.register_notification(callback_function=on_rumble)
            except Exception:
                pass  # rumble passthrough is optional
            if cfg["hide"]:  # only once the virtual pad exists, or the game would have no pad at all
                try:
                    vid, pid = guid_vid_pid(guid)
                    real = device_instances(vid, pid) if vid else []
                    if not real:
                        raise LookupError
                    hider.hide(real)
                    slot.note = "real controller hidden from games"
                except LookupError:
                    slot.note = "could not identify the real controller, so it is NOT hidden"
                except OSError:
                    slot.note = "HidHide is not installed, so games still see the real controller"
            return slot

        def describe():
            error = next((x.error for x in slots.values() if x.error), None)
            notes = sorted({x.note for x in slots.values() if x.note})
            self.status = error or ("Running on " + " + ".join(x.name for x in slots.values())
                                    + (" - " + "; ".join(notes) if notes else ""))

        try:
            while self.running:
                try:
                    pygame.event.pump()
                    cfg = self.cfg
                    if self.reload:
                        self.reload = False
                        release()
                        hider.unhide()
                        for slot in slots.values():
                            slot.stop_rumble()
                        slots.clear()  # dropping a Slot unplugs its virtual pad
                        virtual.clear()
                        pending.clear()
                        self.view = ((), 0, 0)
                        self.status = "Starting"
                        time.sleep(0.3)  # let Windows remove the virtual pads before we look again
                        next_scan = 0
                        continue

                    if time.monotonic() >= next_scan:
                        next_scan = time.monotonic() + (1 if slots else 0.3)
                        present = {}
                        for i in range(pygame.joystick.get_count()):
                            if controller.is_controller(i):
                                joy = pygame.joystick.Joystick(i)
                                present[joy.get_instance_id()] = (i, controller.name_forindex(i) or f"Controller {i}", joy.get_guid())
                        changed = False
                        for jid in [j for j, x in slots.items() if j not in present or not x.ctl.attached()]:
                            slots.pop(jid).stop_rumble()
                            changed = True
                            if not slots:
                                self.status = f"Controller disconnected - waiting for {cfg['controller'].strip('*') or 'a controller'}"
                                self.view = ((), 0, 0)
                        virtual &= set(present)
                        now = time.monotonic()
                        pending[:] = [t for t in pending if t > now]
                        want = cfg["controller"]
                        for jid, (i, name, guid) in present.items():
                            if jid in slots or jid in virtual:
                                continue
                            if pending and guid_vid_pid(guid) in ((0x045E, 0x028E), (0, 0)):
                                # shortcut: a real Xbox 360 pad plugged in during the seconds our own virtual pad
                                # appears is taken for it and ignored; replug it, or track device paths if that bites
                                virtual.add(jid)
                                pending.pop()
                                continue
                            if len(slots) >= (MAX_PADS if want == ALL else 1) or (want not in ("", ALL) and name != want):
                                continue
                            slots[jid] = open_slot(i, name, guid, cfg)
                            changed = True
                        self.names = sorted(x[1] for j, x in present.items() if j not in virtual)
                        if slots and changed:
                            describe()
                            self.kind = controller_kind(next(iter(slots.values())).name)

                    if not slots:
                        if not self.status.startswith("Controller disconnected"):
                            self.status = f"Waiting for {cfg['controller'].strip('*') or 'a controller'}"
                        release()
                        time.sleep(0.05)
                        continue

                    want_keys = {}
                    for n, slot in enumerate(slots.values()):
                        x, y, duo = slot.read(cfg, self)
                        if n == 0:
                            self.view = (slot.raw, x, y)
                        if cfg["output"] == "keyboard":
                            # every controller drives the same keys: a key is down while any of them asks for it
                            want_keys.update({d: cfg["keys"][d] for d, act in zip(DIRS, (y < 0, y > 0, x < 0, x > 0)) if act})
                            if cfg["enabled"]:
                                for i, (c, v) in enumerate(zip(cfg["pairs"], duo)):
                                    k = c["key_a"] if v < 0 else c["key_b"] if v > 0 else ""
                                    if k:
                                        want_keys[i] = k
                        elif slot.pad:
                            slot.send(cfg, x, y, duo)
                    for k in set(keys) | set(want_keys):
                        if keys.get(k) and keys[k] != want_keys.get(k):
                            send_key(keys[k], False)  # released, lost its pair, or was rebound while held
                            keys[k] = None
                        if want_keys.get(k) and not keys.get(k):
                            keys[k] = want_keys[k]
                            send_key(keys[k], True)
                    time.sleep(0.001)
                except Exception as e:  # e.g. controller yanked mid-read: report, then reconnect
                    self.status = f"Error: {e}"
                    self.reload = True
                    time.sleep(1)
        finally:
            release()  # never leave a key stuck down
            hider.unhide()


class Slot:
    """One physical controller, its resolver state and (with pad output) its own virtual Xbox pad."""

    def __init__(self, ctl, name):
        self.ctl, self.name, self.pad, self.sent = ctl, name, None, None
        self.error = self.note = ""
        self.ax, self.ay, self.duos, self.seen = Pair(), Pair(), [], None
        self.prev = [1] * len(BUTTONS)  # buttons already held at connect are not new presses
        self.raw, self.btn, self.lt, self.rt = (), [], 0, 0
        self.rumble = self.rumbling = (0, 0)

    def read(self, cfg, engine):
        """Poll the controller; returns cleaned (x, y, [pair results])."""
        ctl = self.ctl
        btn = [ctl.get_button(b) for b in range(NSDL)]
        self.lt, self.rt = ctl.get_axis(4), ctl.get_axis(5)
        btn += [self.lt > 16000, self.rt > 16000]
        for b, now in enumerate(btn):
            if now and not self.prev[b]:
                engine.pressed = b
        self.prev = self.btn = btn
        if cfg is not self.seen:  # settings changed: fresh state for the button pairs
            self.seen, self.duos = cfg, [Pair() for _ in cfg["pairs"]]
        bind = cfg["bind"]
        x = self.ax.update(cfg["mode"], bool(btn[bind["left"]]), bool(btn[bind["right"]]))
        y = self.ay.update(cfg["mode"], bool(btn[bind["up"]]), bool(btn[bind["down"]]), vertical=True)
        duo = [p.update(c["mode"], bool(btn[c["a"]]), bool(btn[c["b"]])) for p, c in zip(self.duos, cfg["pairs"])]
        if not cfg["enabled"]:
            x = y = 0
        self.raw = tuple(d for d in DIRS if btn[bind[d]])
        return x, y, duo

    def send(self, cfg, x, y, duo):
        """Update the virtual pad: bound buttons are swallowed and re-sent cleaned, everything else
        passes straight through. When cleaning is off the virtual pad mirrors the real one."""
        # shortcut: paired paddles / touchpad / misc have no Xbox 360 button, so they only work with keyboard output
        ctl, btn, on = self.ctl, self.btn, cfg["enabled"]
        bound, emit = (set(cfg["bind"].values()) if on else set()), set()
        if on:
            for c, v in zip(cfg["pairs"], duo):
                bound |= {c["a"], c["b"]}
                if v:
                    emit.add(c["a"] if v < 0 else c["b"])
        mask = sum(XBITS[b] for b in range(len(XBITS)) if (btn[b] and b not in bound) or b in emit)
        lx, ly = ctl.get_axis(0), min(32767, -ctl.get_axis(1))  # SDL Y is down-positive
        if cfg["pad_target"] in ("dpad", "both"):
            mask |= (y < 0) * 1 | (y > 0) * 2 | (x < 0) * 4 | (x > 0) * 8
        if cfg["pad_target"] in ("stick", "both") and (x or y):
            lx, ly = x * 32767, -y * 32767
        rep = (mask, max(0, self.lt) >> 7 if LT not in bound or LT in emit else 0,
               max(0, self.rt) >> 7 if RT not in bound or RT in emit else 0,
               lx, ly, ctl.get_axis(2), min(32767, -ctl.get_axis(3)))
        if rep != self.sent:
            r = self.pad.report
            (r.wButtons, r.bLeftTrigger, r.bRightTrigger,
             r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY) = rep
            self.pad.update()
            self.sent = rep
        if self.rumble != self.rumbling:
            self.rumbling = self.rumble
            try:
                # shortcut: SDL caps one rumble call at ~65 s; re-send on a timer if a game holds rumble longer
                ctl.rumble(self.rumbling[0] / 255, self.rumbling[1] / 255, 0xFFFF)
            except Exception:
                pass  # controller has no rumble

    def stop_rumble(self):
        if self.rumbling != (0, 0):
            try:
                self.ctl.rumble(0, 0, 0)
            except Exception:
                pass


# --- tray icon + settings window ---

# Themes: (background, text, accent, second highlight). Original palettes; the character ones
# are only named after who inspired them. Every other colour is derived, and test_socd.py
# checks contrast for each theme.
THEMES = {
    "Midnight": ("#14141c", "#e8e8f0", "#ff3b4e", "#ffc233"),
    "Daylight": ("#f4f5f8", "#1a1b22", "#c81e3a", "#a85f00"),
    "Kazuya": ("#0b0a10", "#efeaf7", "#8a5cff", "#ff3030"),      # devil violet, red eye
    "Heihachi": ("#14100a", "#f7efe0", "#f2a100", "#ff5a2a"),    # tiger gold on black
    "Bryan": ("#111410", "#eef0e6", "#c4d600", "#ff5a3c"),       # acid green, gunmetal
    "Fahkumram": ("#1a0c08", "#fbeedd", "#f03a1e", "#f0b429"),   # red and gold ink
    "Reina": ("#140a1e", "#f8eefc", "#ff4fd8", "#9fd8ff"),       # magenta lightning
}
BADGE, BADGE_FG = "#0c0c12", "#e8e8f0"  # button glyphs sit on a dark badge in every theme


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(_rgb(a), _rgb(b)))


def luminance(h):
    c = [v / 255 for v in _rgb(h)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + 0.05) / (lo + 0.05)


def palette(name):
    bg, fg, accent, second = THEMES[name]
    dark = luminance(bg) < 0.4
    return dict(
        BG=bg, FG=fg, ACCENT=accent, AMBER=second, DARK=dark,
        FIELD=mix(bg, fg, 0.07), OFF=mix(bg, fg, 0.10), HOVER=mix(bg, fg, 0.14),
        BORDER=mix(bg, fg, 0.24), DIM=mix(bg, fg, 0.42), MUTED=mix(bg, fg, 0.68),
        ON_ACCENT=max(("#000000", "#ffffff"), key=lambda c: contrast(c, accent)),
        ACCENT_HOVER=mix(accent, fg, 0.2), ACCENT_DOWN=mix(accent, bg, 0.25),
        GOOD="#3ddc84" if dark else "#0b5e2c", BAD="#ff7b7b" if dark else "#a3161f")


def apply_theme(name):
    globals().update(palette(name))  # colours are module globals, read when widgets are (re)built


apply_theme("Midnight")
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
               [("A", BADGE_FG), ("B", BADGE_FG), ("X", BADGE_FG), ("Y", BADGE_FG)],
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
        d.ellipse((k, k, n - k, n - k), fill=BADGE, outline=col, width=int(1.5 * k))
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
        d.rounded_rectangle((k / 2, n * 0.2, n - k / 2, n * 0.8), radius=3 * k, fill=BADGE, outline=MUTED, width=k)
        text(label, BADGE_FG, n * (0.42 if len(label) < 3 else 0.34))
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
        d.line([(n * 0.26, n * 0.52), (n * 0.44, n * 0.70), (n * 0.76, n * 0.32)], fill=ON_ACCENT,
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

    import pystray

    eng = Engine(load_cfg())
    show = not CFG_PATH.exists() or "--show" in sys.argv  # first run, or asked for
    eng.start()
    apply_theme(eng.cfg["theme"])

    ui_q = queue.SimpleQueue()  # tray callbacks run on another thread; tkinter must not be touched from there

    def mode_item(k):
        return pystray.MenuItem(MODES[k], lambda: ui_q.put(("mode", k)),
                                checked=lambda item: eng.cfg["mode"] == k, radio=True)

    icon = pystray.Icon("socd", tray_image(), "SOCD Cleaner", menu=pystray.Menu(
        pystray.MenuItem("Settings", lambda: ui_q.put(("show", None)), default=True),
        pystray.MenuItem("Cleaning enabled", lambda: ui_q.put(("toggle", None)),
                         checked=lambda item: eng.cfg["enabled"]),
        pystray.MenuItem("SOCD mode", pystray.Menu(*[mode_item(k) for k in MODES])),
        pystray.MenuItem("Start with Windows", lambda: ui_q.put(("autostart", None)),
                         checked=lambda item: autostart()),
        pystray.MenuItem("Quit", lambda: ui_q.put(("quit", None)))))
    icon.run_detached()

    # The window is rebuilt from scratch on a theme change; the engine and virtual pad keep running.
    pos = None
    while True:
        pos = settings_window(eng, icon, ui_q, show, pos)
        if pos is None:
            break
        show = True
    icon.stop()
    eng.running = False
    eng.join(1)


def tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), BG)
    draw = ImageDraw.Draw(img)
    for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
        draw.polygon(arrow_points(32 + dx * 17, 32 + dy * 17, dx, dy, 26), fill=ACCENT)
    return img


def settings_window(eng, icon, ui_q, show, pos):
    """Build and run the settings window. Returns None to quit, or the window position
    when it closed itself to be rebuilt in a new theme."""
    import tkinter as tk
    from tkinter import ttk

    rebuild_at = [None]
    root = tk.Tk()
    if pos:
        root.geometry(pos)
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
    st.map("TButton", background=[("pressed", BORDER), ("active", HOVER)],
           bordercolor=[("focus", AMBER)])
    st.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, font=FONT_BOLD,
                 padding=(GAP * 2, GAP - 1), lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT)
    st.map("Accent.TButton", background=[("pressed", ACCENT_DOWN), ("active", ACCENT_HOVER)])
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=FG,
                 padding=GAP // 2)
    st.map("TCombobox", fieldbackground=[("disabled", BG), ("readonly", FIELD)],
           foreground=[("disabled", DIM), ("readonly", FG)],
           selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)],
           bordercolor=[("focus", AMBER)], arrowcolor=[("disabled", BORDER)])
    st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG, padding=GAP // 2)
    st.map("TEntry", fieldbackground=[("disabled", BG)], foreground=[("disabled", DIM)],
           bordercolor=[("focus", AMBER)])
    for opt, val in (("background", FIELD), ("foreground", FG), ("selectBackground", ACCENT),
                     ("selectForeground", ON_ACCENT)):
        root.option_add(f"*TCombobox*Listbox.{opt}", val)

    st.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, 0, 0, 0))
    st.configure("TNotebook.Tab", background=BG, foreground=MUTED, font=FONT_BOLD, padding=(GAP * 2, GAP - 2),
                 bordercolor=BORDER)
    st.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", FG)],
           expand=[("selected", (0, 0, 0, 0))])

    frm = ttk.Frame(root, padding=GAP * 2)
    frm.grid()
    nb = ttk.Notebook(frm)
    nb.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(GAP, 0))
    tab1 = ttk.Frame(nb, padding=(GAP * 2, GAP, GAP * 2, GAP * 2))
    tab2 = ttk.Frame(nb, padding=GAP * 2)
    nb.add(tab1, text="Controller")
    nb.add(tab2, text="Button pairs")

    cfg = eng.cfg
    bind = dict(cfg["bind"])
    capturing = [None]

    def key_of(names, shown):
        return next(k for k, v in names.items() if v == shown)

    def card(col, row, title, span=1):
        box = ttk.Labelframe(tab1, text=title.upper())
        box.grid(row=row, column=col, rowspan=span, sticky="new", pady=(GAP, 0), padx=(GAP * 2 if col else 0, 0))
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

    def check(parent, row, text, var):
        box = tk.Checkbutton(parent, text=" " + text, variable=var, image=box_off, selectimage=box_on,
                             compound="left", indicatoron=False, bd=0, relief="flat", offrelief="flat", font=FONT,
                             bg=BG, fg=FG, selectcolor=BG, activebackground=BG, activeforeground=FG,
                             disabledforeground=DIM, highlightthickness=1, highlightbackground=BG, highlightcolor=AMBER)
        box.grid(row=row, column=0, columnspan=2, sticky="w", pady=(GAP // 2, 0))
        return box

    check(clean_card, 1, "Cleaning enabled", on_var)

    look = card(0, 3, "Appearance")
    theme_var, theme_box = combo(look, 0, "Theme", {t: t for t in THEMES}, cfg["theme"])
    theme_box.configure(height=12)
    theme_box.bind("<<ComboboxSelected>>", lambda e: save())  # themes apply as soon as they are picked

    dev = card(1, 1, "Device")
    ANY, EVERY = "(first one found)", "(all controllers)"
    ttk.Label(dev, text="Controller").grid(row=0, column=0, sticky="w", pady=GAP // 2, padx=(0, GAP * 2))
    ctl_var = tk.StringVar(value={"": ANY, ALL: EVERY}.get(cfg["controller"], cfg["controller"]))
    ctl_box = ttk.Combobox(dev, textvariable=ctl_var, state="readonly", width=26)
    ctl_box.configure(postcommand=lambda: ctl_box.configure(values=[ANY, EVERY] + sorted(set(eng.names))))
    ctl_box.grid(row=0, column=1, sticky="ew")
    out_var, out_box = combo(dev, 1, "Output", OUTPUTS, cfg["output"])
    tgt_var, tgt_box = combo(dev, 2, "Pad directions go to", TARGETS, cfg["pad_target"])
    hide_var = tk.BooleanVar(value=cfg["hide"])
    hide_box = check(dev, 3, "Hide the real controller from games", hide_var)

    def install():
        from tkinter import messagebox
        need = [d for d in missing_drivers() if driver_installer(d)]
        if not need or not messagebox.askokcancel("Install drivers", "This installs " + " and ".join(need) + ", the drivers "
                                                  "the virtual pad needs.\n\nWindows will ask for administrator permission "
                                                  "for each one. HidHide needs a restart of Windows afterwards.", parent=root):
            return
        drv_btn.configure(state="disabled", text="Installing...")

        def work():  # installers block until finished, so keep them off the UI thread
            left = install_drivers(need)
            ui_q.put(("drivers", left))
        threading.Thread(target=work, daemon=True).start()

    drv_btn = ttk.Button(dev, command=install)

    def refresh_drivers(note=False):
        missing = missing_drivers()
        if missing and all(driver_installer(d) for d in missing):
            drv_btn.configure(state="normal", text="Install missing drivers: " + ", ".join(missing))
            drv_btn.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(GAP, 0))
        else:
            drv_btn.grid_remove()
        if note:
            say("Still missing: " + ", ".join(missing) + "." if missing else
                "Drivers installed. Restart Windows if hiding does not work yet.", ok=not missing)
            eng.reload = True

    binds = card(1, 2, "Directions", span=2)
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
        hide_box.configure(state="normal" if pad_mode else "disabled")
        for e in key_entries:
            e.configure(state="disabled" if pad_mode else "normal")

    out_var.trace_add("write", grey_out)
    grey_out()

    # button pairs: any two buttons, one rule (hitboxer-style mappings)
    duo_card = ttk.Frame(tab2)
    duo_card.grid(row=0, column=0, sticky="nw")
    ttk.Label(duo_card, text="Pick any two buttons and a rule for when both are held - face buttons, shoulders, "
              "triggers, anything. Keys are only used with keyboard output and may be left empty.",
              style="Muted.TLabel", wraplength=690, justify="left").grid(row=0, column=0, sticky="w")
    duo_grid = ttk.Frame(duo_card)
    duo_grid.grid(row=1, column=0, sticky="w", pady=(GAP // 2, 0))
    heads = [ttk.Label(duo_grid, text=t, style="Muted.TLabel") for t in ("Button 1", "", "Button 2", "Rule", "Key 1", "Key 2")]
    for col, h in enumerate(heads):
        h.grid(row=0, column=col, sticky="w")
    pair_rows, next_id = [], [0]

    def refresh_pairs():
        for h in heads:
            h.grid() if pair_rows else h.grid_remove()
        nb.tab(tab2, text=f"Button pairs ({len(pair_rows)})" if pair_rows else "Button pairs")
        add_btn.configure(state="normal" if len(pair_rows) < MAX_PAIRS else "disabled",
                          text="+ Add pair" if len(pair_rows) < MAX_PAIRS else f"Maximum of {MAX_PAIRS} pairs")
        grey_out()

    def add_pair(p=None):
        if len(pair_rows) >= MAX_PAIRS:
            return
        if p is None:  # new pair starts on the first two buttons nothing else uses
            free = [v for v in range(len(BUTTONS)) if v not in bind.values()]
            p = {"a": free[0], "b": free[1], "mode": "last", "key_a": "", "key_b": ""}
        pid = next_id[0]
        next_id[0] += 1
        rec = {"id": pid, "mode": tk.StringVar(value=PAIR_MODES[p["mode"]]), "widgets": [], "entries": [],
               "key_a": tk.StringVar(value=p["key_a"]), "key_b": tk.StringVar(value=p["key_b"])}

        def put(widget, col, **kw):
            widget.grid(row=pid + 1, column=col, padx=(0, GAP), pady=GAP // 4, **kw)
            rec["widgets"].append(widget)
            return widget

        for col, side in ((0, "a"), (2, "b")):
            slot = (pid, side)
            bind[slot] = p[side]
            bind_btns[slot] = put(ttk.Button(duo_grid, width=16, command=lambda s=slot: start_capture(s)), col)
            label_bind(slot)
        put(ttk.Label(duo_grid, text="vs", style="Muted.TLabel"), 1)
        put(ttk.Combobox(duo_grid, textvariable=rec["mode"], values=list(PAIR_MODES.values()),
                         state="readonly", width=28), 3)
        for col, side in ((4, "a"), (5, "b")):
            rec["entries"].append(put(ttk.Entry(duo_grid, textvariable=rec["key_" + side], width=7, justify="center"), col))
        key_entries.extend(rec["entries"])
        put(ttk.Button(duo_grid, text="Remove", command=lambda: remove_pair(rec)), 6)
        pair_rows.append(rec)
        refresh_pairs()

    def remove_pair(rec):
        stop_capture()
        for side in "ab":
            del bind[rec["id"], side], bind_btns[rec["id"], side]
        for e in rec["entries"]:
            key_entries.remove(e)
        for wdg in rec["widgets"]:
            wdg.destroy()
        pair_rows.remove(rec)
        refresh_pairs()

    add_btn = ttk.Button(duo_card, text="+ Add pair", command=add_pair)
    add_btn.grid(row=2, column=0, sticky="w", pady=(GAP // 2, 0))
    for p in cfg["pairs"]:
        add_pair(p)
    refresh_pairs()

    # footer: result of the last save sits right next to the button that caused it
    foot = ttk.Frame(frm)
    foot.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(GAP * 1.5, 0))
    foot.columnconfigure(0, weight=1)
    note = ttk.Label(foot, text="", wraplength=520)
    note.grid(row=0, column=0, sticky="w")
    status = ttk.Label(frm, text="", style="Muted.TLabel", wraplength=700)
    status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(GAP, 0))

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
        if any(old[k] != new[k] for k in ("controller", "output", "hide")):
            eng.reload = True
        mode_var.set(MODES[new["mode"]])
        on_var.set(new["enabled"])
        icon.update_menu()
        return True

    def save():
        stop_capture()
        was = eng.cfg["theme"]
        ok = commit({"theme": theme_var.get(), "controller": {ANY: "", EVERY: ALL}.get(ctl_var.get(), ctl_var.get()),
                "mode": key_of(MODES, mode_var.get()), "output": key_of(OUTPUTS, out_var.get()),
                "pad_target": key_of(TARGETS, tgt_var.get()), "enabled": on_var.get(), "hide": hide_var.get(),
                "bind": {d: bind[d] for d in DIRS}, "keys": {d: key_vars[d].get().strip().lower() for d in DIRS},
                "pairs": [{"a": bind[r["id"], "a"], "b": bind[r["id"], "b"], "mode": key_of(PAIR_MODES, r["mode"].get()),
                           "key_a": r["key_a"].get().strip().lower(), "key_b": r["key_b"].get().strip().lower()}
                          for r in pair_rows]})
        if not ok:
            theme_var.set(was)
        elif eng.cfg["theme"] != was:  # close this window; main() reopens it in the new colours, same place
            apply_theme(eng.cfg["theme"])
            icon.icon = tray_image()
            rebuild_at[0] = f"+{root.winfo_x()}+{root.winfo_y()}"
            root.after_cancel(poll_id[0])
            root.destroy()

    refresh_drivers()
    ttk.Button(foot, text="Save and apply", style="Accent.TButton", command=save).grid(row=0, column=1, sticky="e")
    root.bind("<Return>", lambda e: save())

    try:  # dark title bar to match (Windows 10 2004+ / 11)
        root.update_idletasks()
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.windll.user32.GetParent(root.winfo_id()), 20,
                                                   ctypes.byref(ctypes.c_int(int(DARK))), 4)
    except (OSError, AttributeError):
        pass

    root.iconphoto(True, tk.PhotoImage(data=_png(tray_image())))
    shown_status, poll_id = [None], [None]

    def poll():
        while not ui_q.empty():
            what, arg = ui_q.get()
            if what == "quit":
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
            elif what == "drivers":
                refresh_drivers(note=True)
            elif what == "autostart":
                try:
                    autostart(not autostart())
                except OSError as e:
                    say(f"Could not change startup entry: {e}.", ok=False)
                icon.update_menu()
        d = capturing[0]
        if d and eng.pressed is not None:
            # binding a button something else already uses swaps the two, instead of failing on save
            other = next((o for o in bind if o != d and bind[o] == eng.pressed), None)
            if other:
                bind[other] = bind[d]
                label_bind(other)
            bind[d] = eng.pressed
            label_bind(d)
            capturing[0] = None
        if eng.kind != kind[0]:  # a different controller family connected: relabel with its icons
            kind[0] = eng.kind
            for o in bind:
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
        state.configure(text=word)  # the dot carries the colour; the word stays full-contrast
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
        poll_id[0] = root.after(50, poll)

    if not show:
        root.withdraw()
    poll()
    root.mainloop()
    return rebuild_at[0]


def _png(img):
    import base64
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue())


if __name__ == "__main__":
    main()

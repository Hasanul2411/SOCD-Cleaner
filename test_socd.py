"""Run: python test_socd.py"""
from socd import is_virtual, missing_drivers, driver_installer, DRIVERS, Slot, ALL, Hider, guid_vid_pid, is_device, device_instances, nt_path, multi_sz, from_multi_sz, hidhide_code, Pair, THEMES, palette, contrast, controller_kind, button_icon, LAYOUTS, clean, scancode, INPUT, check_cfg, DEFAULTS, BUTTONS, autostart
import ctypes

# single direction or nothing: every mode agrees
for m in ("last", "first", "neutral", "up"):
    assert clean(m, True, False, 1) == -1
    assert clean(m, False, True, -1) == 1
    assert clean(m, False, False, 1) == 0

# both held; last=+1 means right/down was pressed most recently
assert clean("last", True, True, 1) == 1
assert clean("last", True, True, -1) == -1
assert clean("first", True, True, 1) == -1
assert clean("first", True, True, -1) == 1
assert clean("neutral", True, True, 1) == 0
assert clean("neutral", True, True, 1, vertical=True) == 0
assert clean("up", True, True, 1, vertical=True) == -1   # up wins over down
assert clean("up", True, True, 1) == 0                   # left+right is neutral

assert scancode("w") == (0x11, 0)
assert scancode("Left") == (0x4B, 1)
assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
try:
    scancode("nope")
    raise AssertionError("bad key accepted")
except ValueError:
    pass

assert scancode("space") == (0x39, 0)
assert BUTTONS[21:] == ["LT", "RT"]

# Pair: press order is tracked across updates
p = Pair()
assert p.update("last", True, False) == -1          # hold 1
assert p.update("last", True, True) == 1            # add 2 -> 2 wins
assert p.update("last", True, False) == -1          # release 2 -> 1 comes back
p = Pair()
p.update("last_nr", True, False)
assert p.update("last_nr", True, True) == 1         # 2 wins
assert p.update("last_nr", True, False) == 0        # release 2 -> 1 does NOT come back
assert p.update("last_nr", False, False) == 0
assert p.update("last_nr", True, False) == -1       # until it is pressed again
p = Pair()
p.update("first", True, False)
assert p.update("first", True, True) == -1          # first pressed keeps winning
assert Pair().update("neutral", True, True) == 0
assert Pair().update("a", True, True) == -1         # button 1 always wins
assert Pair().update("a", False, True) == 1

# button pairs in the config
pair = {"a": 0, "b": 1, "mode": "last", "key_a": "", "key_b": ""}
check_cfg({**DEFAULTS, "pairs": [pair, {**pair, "a": 2, "b": 3, "mode": "a", "key_a": "j", "key_b": "k"}]})
for wrong in ([{**pair, "b": 0}], [{**pair, "a": 11}], [{**pair, "mode": "up"}], [{**pair, "key_a": "w"}],
              [{**pair, "a": 99}], [pair, pair], [{"a": 0}], ["x"], "x",
              [{**pair, "a": i * 2, "b": i * 2 + 1} for i in range(9) if i * 2 not in (10, 12, 14)] + [{**pair, "a": 20, "b": 22}] * 3):
    try:
        check_cfg({**DEFAULTS, "pairs": wrong})
        raise AssertionError(f"accepted pairs {wrong}")
    except ValueError:
        pass

# config validation: defaults pass, anything that would crash the engine is rejected
check_cfg(DEFAULTS)
bad = [{"mode": "nope"}, {"hide": 1}, {"theme": "nope"}, {"output": 3}, {"enabled": "yes"}, {"controller": None},
       {"bind": {"up": 99, "down": 12, "left": 13, "right": 14}},     # out of range
       {"bind": {"up": "A", "down": 12, "left": 13, "right": 14}},    # wrong type
       {"bind": {"up": 12, "down": 12, "left": 13, "right": 14}},     # duplicate button
       {"bind": {"up": 11}},                                          # missing directions
       {"keys": {"up": "W", "down": "w", "left": "a", "right": "d"}},  # same key twice
       {"keys": {"up": "zzz", "down": "s", "left": "a", "right": "d"}}]
for change in bad:
    try:
        check_cfg({**DEFAULTS, **change})
        raise AssertionError(f"accepted {change}")
    except ValueError:
        pass
assert autostart() in (True, False)  # read-only; never touches the registry

# controller family detection and its labels / icons
assert controller_kind("PS5 Controller") == "playstation"
assert controller_kind("Sony DualShock 4") == "playstation"
assert controller_kind("Nintendo Switch Pro Controller") == "switch"
assert controller_kind("Xbox 360 Controller") == "xbox"
assert controller_kind("Generic USB Joystick") == "xbox" and controller_kind(None) == "xbox"
for kind, (_, names, face, short) in LAYOUTS.items():
    assert len(names) == len(BUTTONS) and len(face) == 4 and len(short) == len(BUTTONS) - 8
    for b in range(len(BUTTONS)):
        assert button_icon(kind, b).size == (24, 24)

# every theme must stay readable: text 4.5:1, graphics and controls 3:1 (WCAG AA)
for name in THEMES:
    p = palette(name)
    for token, need in (("FG", 4.5), ("MUTED", 4.5), ("GOOD", 4.5), ("BAD", 4.5), ("ACCENT", 3), ("AMBER", 3)):
        got = contrast(p[token], p["BG"])
        assert got >= need, f"{name}: {token} on background is {got:.2f}, need {need}"
    assert contrast(p["FG"], p["FIELD"]) >= 4.5, name
    assert contrast(p["ON_ACCENT"], p["ACCENT"]) >= 4.5, f"{name}: text on accent button"

# hiding the real controller through HidHide
assert guid_vid_pid("030000004c050000c405000000000000") == (0x054C, 0x05C4)
assert guid_vid_pid("0300a5390d0f00008401000000007200") == (0x0F0D, 0x0184)   # with CRC bytes
assert guid_vid_pid("78696e70757401000000000000000000") == (0, 0)             # xinput: no ids
assert is_device(r"USB\VID_0F0D&PID_0184\5&2A3B", 0x0F0D, 0x0184)
assert is_device(r"HID\vid_0f0d&pid_0184&IG_00\6&1&0&0000", 0x0F0D, 0x0184)
assert is_device(r"HID\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&09CC\8&1", 0x054C, 0x09CC)
assert not is_device(r"USB\VID_045E&PID_028E\01", 0x0F0D, 0x0184)
assert not is_device(r"USB\VID_0F0D&PID_0185\1", 0x0F0D, 0x0184)
assert from_multi_sz(multi_sz(["a", "b c"])) == ["a", "b c"] and multi_sz(["a"]) == "a\0\0".encode("utf-16-le")
assert from_multi_sz(multi_sz([])) == [] and len(multi_sz([])) == 4
assert hidhide_code(2048) == 0x80016000 and hidhide_code(2056) == 0x80016020
import sys as _sys
assert nt_path(_sys.executable).startswith("\\Device\\") and nt_path(_sys.executable).lower().endswith("python.exe")
import re as _re
# real enumeration: every USB device found by a broad scan must be found again by its own ids
import ctypes as _c
n = _c.c_ulong(); _c.windll.cfgmgr32.CM_Get_Device_ID_List_SizeW(_c.byref(n), None, 0x100)
buf = _c.create_unicode_buffer(n.value); _c.windll.cfgmgr32.CM_Get_Device_ID_ListW(None, buf, n.value, 0x100)
usb = [i for i in buf[:n.value].split("\0") if _re.match(r"USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}", i)]
if usb:
    v, p = int(usb[0][8:12], 16), int(usb[0][17:21], 16)
    assert usb[0] in device_instances(v, p), "present USB device not found by its own vid/pid"
assert device_instances(0xFFFE, 0xFFFD) == []
# our own virtual pads hang off a root-enumerated bus and must never be hidden
if usb:
    assert not is_virtual(usb[0]), "a real USB device was taken for a virtual one"
def _parent(i):
    node, b = _c.c_ulong(), _c.create_unicode_buffer(256)
    cm = _c.windll.cfgmgr32
    if cm.CM_Locate_DevNodeW(_c.byref(node), i, 0) or cm.CM_Get_Parent(_c.byref(node), node, 0) or cm.CM_Get_Device_IDW(node, b, 256, 0):
        return ""
    return b.value.upper()
rooted = [i for i in buf[:n.value].split("\0") if i and _parent(i).startswith("ROOT\\")]
assert rooted and is_virtual(rooted[0]), "child of a root-enumerated device not recognised"
assert not is_virtual("USB\\NOPE\\0")

# driver bundle: both installers are present in a source checkout, and detection never raises
assert set(missing_drivers()) <= set(DRIVERS)
assert all(driver_installer(d) for d in DRIVERS), "a bundled driver installer is missing"

# a Slot cleans one controller: fake pad state in, cleaned directions and pair results out
class _Ctl:
    def __init__(self): self.down, self.axes = set(), [0] * 6
    def get_button(self, b): return b in self.down
    def get_axis(self, a): return self.axes[a]
class _Eng: pressed = None
cfg2 = {**DEFAULTS, "pairs": [{"a": 0, "b": 1, "mode": "last", "key_a": "", "key_b": ""}]}
c1, c2 = _Ctl(), _Ctl()
s1, s2, e = Slot(c1, "one"), Slot(c2, "two"), _Eng()
s1.read(cfg2, e); s2.read(cfg2, e)
c1.down = {13}; assert s1.read(cfg2, e)[:2] == (-1, 0) and e.pressed == 13
c1.down = {13, 14}; assert s1.read(cfg2, e)[:2] == (1, 0) and s1.raw == ("left", "right")
assert s2.read(cfg2, e)[:2] == (0, 0), "controllers must not share state"
c2.down = {0}; s2.read(cfg2, e)
c2.down = {0, 1}; assert s2.read(cfg2, e)[2] == [1] and s1.read(cfg2, e)[2] == [0]
c1.axes[4] = 32767; s1.read(cfg2, e); assert e.pressed == 21   # LT counts as a button
assert s1.read({**cfg2, "enabled": False}, e)[:2] == (0, 0)
check_cfg({**DEFAULTS, "controller": ALL})

h = Hider()
try:
    h.allow_self()
    print("note: HidHide is installed on this machine")
except OSError:
    pass  # not installed here: must fail cleanly, and unhide must still be safe
h.unhide()
print("ok")

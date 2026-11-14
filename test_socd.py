"""Run: python test_socd.py"""
from socd import controller_kind, button_icon, LAYOUTS, clean, scancode, INPUT, check_cfg, DEFAULTS, BUTTONS, autostart
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

# config validation: defaults pass, anything that would crash the engine is rejected
check_cfg(DEFAULTS)
bad = [{"mode": "nope"}, {"output": 3}, {"enabled": "yes"}, {"controller": None},
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
print("ok")

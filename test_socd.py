"""Run: python test_socd.py"""
from socd import Pair, THEMES, palette, contrast, controller_kind, button_icon, LAYOUTS, clean, scancode, INPUT, check_cfg, DEFAULTS, BUTTONS, autostart
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
bad = [{"mode": "nope"}, {"theme": "nope"}, {"output": 3}, {"enabled": "yes"}, {"controller": None},
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
print("ok")

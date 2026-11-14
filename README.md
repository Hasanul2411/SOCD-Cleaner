# SOCD

SOCD cleaner for any controller, inspired by [hitboxer](https://github.com/valignatev/hitboxer)
(which does the same for keyboards).

Map any four buttons of any controller (XInput, DirectInput, PlayStation, Switch, generic HID —
anything SDL recognises) as Up / Down / Left / Right. Opposing presses are resolved and sent to
the game as either a virtual Xbox 360 pad or keyboard keys.

| Mode | Left + Right | Up + Down |
|---|---|---|
| Last input priority | newest wins | newest wins |
| First input priority | oldest wins | oldest wins |
| Neutral | nothing | nothing |
| Up priority | nothing | Up |

## Install

Download `socd.exe` from the latest run under **Actions → build → Artifacts**, or run from source:

```
set VGAMEPAD_SKIP_VIGEMBUS_INSTALL=true
pip install -r requirements.txt
python socd.py
```

**Keyboard output** needs nothing else.

**Virtual pad output** needs two drivers (admin rights to install):

1. [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) — provides the virtual Xbox pad.
2. [HidHide](https://github.com/nefarius/HidHide/releases) — hides your real controller from the
   game so it doesn't see every input twice. In *HidHide Configuration Client*: add `socd.exe`
   (or `python.exe`) under **Applications**, tick your controller under **Devices**, and enable
   **device hiding**.

## Use

It lives in the system tray; double-click the icon for settings (shown automatically on first run).

- **Controller** — "(first one found)" or a specific one. SOCD's own virtual pad is left out of the list.
- Click a direction's button, then press the controller button to bind it (Esc cancels). LT and RT
  can be bound too. Binding a button that another direction already uses swaps the two.
- **Pad directions go to** — d-pad, left stick (full tilt), or both.
- **Keyboard key** — one character, or `up` `down` `left` `right` `space` `enter` `tab` `shift` `ctrl` `alt`.
- **Pressed / Sent** at the bottom shows live what you hold and what the game receives — use it to
  check a mode without opening a game.
- In pad mode every other button, stick and trigger passes straight through, and rumble from the
  game is forwarded to the real controller.

Tray menu (right-click): turn cleaning on/off, switch SOCD mode, start with Windows, quit. Mode and
on/off changes apply instantly without unplugging the virtual pad. With cleaning off, pad mode
mirrors the real controller unchanged and keyboard mode sends nothing.

The tray tooltip shows the current status, and a notification appears when the controller connects,
disconnects (it reconnects by itself) or the virtual pad can't be created.

Settings are saved to `socd_config.json` next to the program; a damaged file is ignored and
defaults are used. If SDL doesn't recognise an obscure controller, drop a
[`gamecontrollerdb.txt`](https://github.com/mdqinc/SDL_GameControllerDB) next to it.

## Limits

- Stick directions can't be bound as directions, only buttons and triggers.
- Two identical controllers can't be told apart (matched by name).
- Keyboard output can't reach a game running as administrator unless SOCD is also run as administrator.
- Many games and tournaments restrict SOCD modes (e.g. last-input-priority); check the rules.

## Test

```
python test_socd.py
```

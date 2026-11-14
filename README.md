# SOCD Cleaner — manual

SOCD cleaner for any controller, inspired by [hitboxer](https://github.com/valignatev/hitboxer)
(which does the same for keyboards).

SOCD means *simultaneous opposing cardinal directions*: what the game should see when Left and
Right (or Up and Down) are held together. This program reads your controller, applies the rule you
pick, and passes the cleaned result to the game as a virtual Xbox 360 pad or as keyboard keys.

![Settings window](docs/screenshot.png)

## Contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [First-time setup](#3-first-time-setup)
4. [Settings reference](#4-settings-reference)
5. [Button pairs](#5-button-pairs)
6. [Tray menu](#6-tray-menu)
7. [Themes](#7-themes)
8. [Troubleshooting](#8-troubleshooting)
9. [Limits](#9-limits)
10. [Run or build from source](#10-run-or-build-from-source)

## 1. Requirements

| What | Needed for | Link |
|---|---|---|
| Windows 10 or 11, 64-bit | everything | — |
| A controller Windows can see (XInput, DirectInput, PlayStation, Switch, generic HID) | everything | — |
| `socd.exe` | everything | [release/socd.exe](release/socd.exe) |
| ViGEmBus driver | **Virtual Xbox pad** output | bundled in the exe — or [ViGEmBus releases](https://github.com/nefarius/ViGEmBus/releases) |
| HidHide driver | hiding the real controller from games | bundled in the exe — or [HidHide releases](https://github.com/nefarius/HidHide/releases) |
| Administrator rights, once | installing the two drivers | — |
| Nothing extra | **Keyboard keys** output | — |

Both drivers are kernel drivers by Nefarius; Windows only lets an administrator install those, so
that one prompt cannot be avoided. Everything after it is automatic.

## 2. Install

1. Download [`release/socd.exe`](release/socd.exe) and put it in a folder you can write to, for
   example `Documents\SOCD` (not `Program Files`). Settings are saved next to it.
2. Start `socd.exe`. If a driver is missing, the **Device** box shows an
   **Install missing drivers** button. Click it, confirm, and accept the Windows administrator
   prompt for each driver. (Keyboard-only users can skip this.)
3. Restart Windows once if HidHide was just installed.

That is all. You do **not** need to open HidHide's configuration tool, tick devices, or unplug the
controller: while it runs, SOCD Cleaner adds itself to HidHide's allowed programs, hides every
device entry of the real controller, and switches hiding on. When it quits it un-hides the
controller again, so the stick works normally without it.

**Start SOCD Cleaner before the game.** A game that was already running keeps the connection to
the real controller it opened earlier; restart the game if you see doubled inputs.

Windows SmartScreen or antivirus may warn about `socd.exe` because it is unsigned and sends input
to other programs. If you would rather not trust a prebuilt file, [run it from source](#10-run-or-build-from-source).

## 3. First-time setup

1. Plug in the controller and start `socd.exe`. The settings window opens on first run; afterwards
   it starts in the system tray (double-click the tray icon to open it).
2. Check the top-right status: **RUNNING** means a controller was found. **WAITING** means none is
   connected. **PROBLEM** shows the reason in the line at the bottom.
3. **Controller**: leave "(first one found)", pick yours, or choose "(all controllers)" for several players.
4. **Output**: *Virtual Xbox pad* (needs the drivers above) or *Keyboard keys*.
5. **SOCD mode**: pick the rule (table below).
6. **Directions**: click the button next to Up, press the controller button you want for Up, and
   repeat for Down, Left and Right. Esc cancels.
7. Click **Save and apply**.
8. Hold two opposite directions and watch **Live input**: *Pressed* shows what you hold, *Sent to
   game* shows the cleaned result.
9. Start the game. With virtual pad output, pick the **Xbox 360 controller** in the game if it asks.

## 4. Settings reference

### SOCD modes

| Mode | Left + Right | Up + Down |
|---|---|---|
| Last input priority | newest wins | newest wins |
| Last input, no re-press | newest wins; the other stays off until pressed again | same |
| First input priority | oldest wins | oldest wins |
| Neutral | nothing | nothing |
| Up priority | nothing | Up |

### Device

- **Controller** — "(first one found)", a specific one, or "(all controllers)": up to four controllers at once,
  each with its own cleaned virtual pad, all using the same bindings, mode and pairs. With keyboard
  output they all drive the same keys. SOCD's own virtual pads are left out of the list.
- **Hide the real controller from games** — on by default with pad output; see [Install](#2-install).
  The status line says whether hiding worked.
- **Output** — *Virtual Xbox pad*: the game sees a normal Xbox 360 controller; every button, stick
  and trigger you have not bound passes straight through, and rumble is forwarded back to the real
  controller. *Keyboard keys*: cleaned directions are sent as key presses; nothing else is sent.
- **Pad directions go to** — d-pad, left stick (full tilt), or both.

### Directions

- Any button, LT or RT can be a direction. Binding a button that is already used swaps the two.
- **Keyboard key** (keyboard output only) — one character, or `up` `down` `left` `right` `space`
  `enter` `tab` `shift` `ctrl` `alt`.
- The controller family (Xbox, PlayStation, Nintendo Switch) is detected from its name, and
  buttons are shown with that family's names and glyphs. Unknown controllers, and PlayStation pads
  running through Steam Input or DS4Windows, show as Xbox.

### Cleaning enabled

Unticked, pad output mirrors the real controller unchanged and keyboard output sends nothing.

## 5. Button pairs

The **Button pairs** tab applies a priority rule to any two buttons — face buttons, shoulders,
triggers — like hitboxer's mappings.

1. Open the **Button pairs** tab and click **+ Add pair**.
2. Click **Button 1**, press a controller button; do the same for **Button 2**.
3. Pick the **Rule**.
4. With keyboard output, type a key for each button (leave empty to send nothing).
5. Click **Save and apply**.

| Rule | Both held |
|---|---|
| Last pressed wins | newest wins; releasing it brings the other back |
| Last pressed wins, no re-press | newest wins; the other stays off until pressed again |
| First pressed wins | oldest wins |
| Both cancel out | neither is sent |
| Button 1 always wins | Button 1 |

Up to 8 pairs. A button can be in one pair or one direction only. With pad output the winner is
sent as that same button. Paddles, touchpad and misc buttons have no Xbox 360 equivalent, so in a
pair they only work with keyboard output.

## 6. Tray menu

Right-click the tray icon:

- **Settings** — open the window (also double-click).
- **Cleaning enabled** — on/off without unplugging the virtual pad.
- **SOCD mode** — switch mode instantly.
- **Start with Windows** — run at login (per user, no admin needed).
- **Quit**.

The tooltip shows the current status, and a notification appears when the controller connects,
disconnects (it reconnects by itself) or the virtual pad cannot be created.

`socd.exe --show` opens the settings window at start instead of staying in the tray.

## 7. Themes

**Appearance → Theme** switches the look instantly without interrupting the controller: Midnight
(default), Daylight, Kazuya, Heihachi, Bryan, Fahkumram and Reina. The character themes are
original colour palettes named after who inspired them; no game artwork is used.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| "No virtual pad (is ViGEmBus installed?)" | Click **Install missing drivers** in the Device box, or install [ViGEmBus](https://github.com/nefarius/ViGEmBus/releases) yourself, then **Save and apply**. Or switch **Output** to *Keyboard keys*. |
| Inputs arrive twice / cleaning has no effect in game | The game still sees the real controller. Check the status line: it must end with "real controller hidden from games". If it does, the game was started before SOCD Cleaner — restart the game. If it says HidHide is not installed, click **Install missing drivers** and restart Windows. |
| Status says "could not identify the real controller" | Windows did not report the controller's USB ids. Hide it once by hand in HidHide Configuration Client (tick every entry of the controller), or switch the controller to another mode (for example PS4 instead of PC). |
| The real controller stays dead after a crash | Start SOCD Cleaner and quit it from the tray; that un-hides it. Or untick it in HidHide Configuration Client. |
| Status stays **WAITING** | Check the controller in Windows *Set up USB game controllers* (`joy.cpl`). |
| Controller connected but not listed | Download `gamecontrollerdb.txt` from [SDL_GameControllerDB](https://github.com/mdqinc/SDL_GameControllerDB), put it next to `socd.exe`, restart. |
| Keyboard output ignored by the game | Games running as administrator ignore input from normal programs: run `socd.exe` as administrator too. |
| Button names look wrong (PlayStation pad shows A/B/X/Y) | The pad is reporting as Xbox (Steam Input, DS4Windows). Bindings still work; only the labels differ. |
| "SOCD Cleaner is already running" | It is in the system tray; double-click its icon. |
| Settings lost | `socd_config.json` next to the exe was damaged or the folder is read-only; defaults are used. Move the exe to a writable folder. |

## 9. Limits

- Stick directions cannot be bound, only buttons and triggers.
- Two identical controllers cannot be told apart by name; use "(all controllers)" to clean both.
- All controllers share one set of bindings, mode and pairs.
- A real Xbox 360 pad plugged in during the few seconds after SOCD Cleaner starts may be ignored; replug it.
- Many games and tournaments restrict SOCD modes (for example last-input-priority) or input
  remappers in general; check the rules before using it competitively.

## 10. Run or build from source

Requirements: [Python 3.12](https://www.python.org/downloads/) and [Git](https://git-scm.com/downloads).

```
git clone https://github.com/Hasanul2411/SOCD-Cleaner.git
cd SOCD-Cleaner
set VGAMEPAD_SKIP_VIGEMBUS_INSTALL=true
pip install -r requirements.txt
python socd.py
```

`VGAMEPAD_SKIP_VIGEMBUS_INSTALL` stops the `vgamepad` package from launching its own driver
installer; install ViGEmBus yourself from the link above.

Run the tests:

```
python test_socd.py
```

Build the exe (this is what [the build workflow](https://github.com/Hasanul2411/SOCD-Cleaner/actions) runs on every push):

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name socd --icon icon.ico --add-data "drivers;drivers" --collect-data vgamepad --collect-binaries vgamepad --hidden-import vgamepad --hidden-import vgamepad.win.virtual_gamepad --hidden-import vgamepad.win.vigem_client --hidden-import pygame._sdl2.controller socd.py
```

Libraries used: [pygame](https://www.pygame.org/) (SDL controller input),
[vgamepad](https://github.com/yannbouteiller/vgamepad) (ViGEm client),
[pystray](https://github.com/moses-palmer/pystray) and [Pillow](https://python-pillow.org/) (tray icon and glyphs).

## Design notes

Dark and light themes with one accent colour, text contrast of at least 4.5:1 (checked by the tests
for every theme), an 8 px spacing grid, visible keyboard focus, and state shown by shape or words
as well as colour. All icons are drawn by the program itself.

## Licence

[MIT](LICENSE). The bundled driver installers are unmodified official builds and keep their own
licences: [HidHide](drivers/HidHide-LICENSE.txt) (MIT, `drivers/HidHide_1.5.230_x64.exe`, SHA-256
`f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6`, from the
[v1.5.230.0 release](https://github.com/nefarius/HidHide/releases/tag/v1.5.230.0)) and
[ViGEmBus](drivers/ViGEmBus-LICENSE.txt) (BSD-3-Clause, the installer shipped inside the `vgamepad` package).

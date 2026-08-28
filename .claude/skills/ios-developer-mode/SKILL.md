---
name: ios-developer-mode
description: >-
  Enable (or diagnose) Developer Mode on a USB-connected iPhone or iPad from a
  Windows PC using pymobiledevice3. Use this whenever the user wants to turn on
  Developer Mode, "開發者模式", prepare an iOS/iPadOS device for app sideloading,
  Xcode debugging, or on-device developer tooling — and also when a connected
  Apple device "isn't showing up", won't pair, throws MuxException 183 /
  FatalPairingError, or usbmuxd seems dead. Covers the whole flow: detecting the
  device (including untrusted ones the normal list hides), reviving usbmuxd,
  clearing stale pairing records, and driving amfi reveal/enable/verify.
compatibility: Windows. Requires pymobiledevice3 and a running iTunes/Apple Mobile Device (usbmuxd).
---

# iOS Developer Mode (Windows)

Turn on Developer Mode on a USB-connected iPhone/iPad from Windows, and fix the
things that commonly block it. The last switch always requires a human tap on the
device when a passcode is set — that's an Apple security rule, not a limitation we
can code around. Everything up to that point can be automated.

## The fast path

Run the bundled helper — it does detect → usbmuxd health → pair → clear stale
pairing → reveal → enable → verify, and stops with clear guidance wherever a human
must act on the device:

```
python "<skill-dir>/scripts/enable_dev_mode.py"
```

Useful flags: `--status` (report only, change nothing), `--udid <UDID>` (pick one
of several connected devices), `--fix-pairing` (delete a stale pair record; may
need elevation). Exit codes: `0` on, `2` waiting on a human, `3` environment
problem to fix, `4` no device.

If the script reports an environment problem (usbmuxd down, tool missing, stale
pairing needing elevation), resolve it using the sections below, then rerun. When
the script says "finish on the device", relay those steps to the user, wait, then
confirm with `--status`.

## Why the manual steps below still matter

The helper is the happy path. But several fixes need either Administrator rights
(deleting a pair record) or physical actions by the user (replugging USB, tapping
Trust, flipping the final switch). Understand the mechanics so you can guide the
user through whatever the script hands back — don't just retry the script in a loop.

## Prerequisites

`pymobiledevice3` is the tool that talks to the device; on Windows the `usbmuxd`
transport it relies on is provided by **iTunes / Apple Mobile Device**, so iTunes
must be installed and running.

```
python -m pip install -U pymobiledevice3
```

## Step 1 — Detect the device (the non-obvious part)

The plain CLI list **silently hides devices that aren't trusted/paired yet**, so a
freshly plugged-in phone looks absent there. The raw usbmux API is the source of
truth for *presence*:

```
python -c "import asyncio; from pymobiledevice3 import usbmux; print(asyncio.run(usbmux.list_devices()))"
```

If that shows a `MuxDevice(...serial=...)` the device is physically connected even
when `python -m pymobiledevice3 usbmux list` returns `[]`. Use `usbmux list` only
for rich info (name, model, iOS version) once the device is paired.

If nothing shows up at all: the user needs to unlock the screen, use a
**data-capable** cable (not charge-only), and tap **Trust** when prompted.

## Step 2 — Make sure usbmuxd is alive

If tools report "Failed to connect to usbmuxd socket" or the raw list errors, the
Apple transport has died. Check it and revive it:

- Port `27015` on `127.0.0.1` open ⇒ usbmuxd up.
- Processes `AppleMobileDeviceProcess` / `AppleMobileDeviceHelper` / `iTunes`
  present ⇒ the service stack is running.

To revive: start the iTunes Store app, then **replug the USB cable** so usbmuxd
re-enumerates the device. Launching the Store-app iTunes:

```
explorer.exe "shell:appsFolder\AppleInc.iTunes_nzyj5cx40ttqa!iTunes"
```

(The package family name may differ on other machines — find it with
`Get-AppxPackage *iTunes*`.)

## Step 3 — Pair, and clear a stale pair record if needed

Windows stores one pair record per device in `C:\ProgramData\Apple\Lockdown\<UDID>.plist`.
If the device was **reset or re-imaged** since it last paired, that record is stale
and every service start fails with a confusing `MuxException ... Number: 183`
(Windows `ERROR_ALREADY_EXISTS`) and `lockdown info` throws `FatalPairingError`.
This is the single most common cause of "it detects but nothing works".

Fix: back up and delete the stale record, then re-pair.

```
# back up first
Copy-Item "C:\ProgramData\Apple\Lockdown\<UDID>.plist" "$env:TEMP\<UDID>.plist.bak"
# delete needs Administrator — this pops a UAC prompt for the user to accept
Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-Command','Remove-Item "C:\ProgramData\Apple\Lockdown\<UDID>.plist" -Force' -Wait
# re-pair (device unlocked, tap Trust)
python -m pymobiledevice3 lockdown pair --udid <UDID>
```

If pairing prints "Device is password protected. Please unlock and retry", the
screen is locked — have the user unlock and stay on the Home screen, then retry.

## Step 4 — Reveal, enable, verify

```
python -m pymobiledevice3 amfi developer-mode-status --udid <UDID>   # false = off
python -m pymobiledevice3 amfi reveal-developer-mode --udid <UDID>   # shows the menu on-device
python -m pymobiledevice3 amfi enable-developer-mode  --udid <UDID>  # tries to enable
```

`enable-developer-mode` will refuse with **"Cannot enable developer-mode when
passcode is set"** on any device that has a passcode — which is nearly all of them.
This is expected. The `reveal` step has already surfaced the toggle, so hand these
steps to the user:

1. Settings → Privacy & Security
2. Scroll to **Developer Mode**
3. Turn it on → tap **Restart**
4. After reboot & unlock, the confirmation pops up → tap **Turn On** → enter passcode

Then confirm it truly took — don't claim success off the menu appearing:

```
python -m pymobiledevice3 amfi developer-mode-status --udid <UDID>   # want: true
```

`true` means done. Developer Mode then persists across reboots until the user
turns it off or erases the device.

## Guiding the user well

- The final tap is unavoidable when a passcode is set — set that expectation up
  front so it doesn't read as a failure.
- Multiple Apple devices can be paired at once; their pair records are independent
  and don't interfere.
- Always finish on `developer-mode-status` returning `true` before reporting done.

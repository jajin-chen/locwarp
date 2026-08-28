#!/usr/bin/env python3
"""Enable (or diagnose) Developer Mode on a USB-connected iOS/iPadOS device on Windows.

Runs the full flow: detect -> usbmuxd health -> pair -> clear stale pairing ->
reveal -> enable -> verify. Stops with clear guidance wherever a human must act
on the physical device (trusting the computer, flipping the final switch).

Usage:
    python enable_dev_mode.py                 # act on the single connected device
    python enable_dev_mode.py --udid <UDID>   # target a specific device
    python enable_dev_mode.py --status        # only report current status, change nothing
    python enable_dev_mode.py --fix-pairing   # also delete a stale pair record (may need elevation)

Exit codes:
    0  developer mode is ON (or --status ran cleanly)
    2  waiting on a human action on the device (unlock / trust / flip switch)
    3  environment problem the caller should fix (usbmuxd down, tool missing)
    4  no device detected
"""
import argparse
import asyncio
import os
import socket
import subprocess
import sys

PMD = [sys.executable, "-m", "pymobiledevice3"]
USBMUXD_PORT = 27015
LOCKDOWN_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Apple", "Lockdown")


def say(msg=""):
    print(msg, flush=True)


def run_pmd(args, timeout=90):
    """Run a pymobiledevice3 subcommand. Returns (returncode, combined_output)."""
    try:
        p = subprocess.run(PMD + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def tool_present():
    rc, out = run_pmd(["version"], timeout=30)
    return rc == 0 or "pymobiledevice3" in out.lower()


def usbmuxd_alive():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", USBMUXD_PORT)) == 0


def list_raw():
    """Every device usbmuxd can see, INCLUDING ones not yet trusted/paired.

    The `usbmux list` CLI silently hides unpaired devices, so a freshly plugged-in
    phone looks 'missing' there. The raw API is the source of truth for presence.
    """
    from pymobiledevice3 import usbmux
    devs = asyncio.run(usbmux.list_devices())
    return [d.serial for d in devs]


def paired_info(udid=None):
    """Rich info for devices that ARE paired, via the CLI. Returns list of dicts."""
    import json
    rc, out = run_pmd(["usbmux", "list"], timeout=30)
    try:
        start = out.index("[")
        data = json.loads(out[start:])
    except (ValueError, json.JSONDecodeError):
        return []
    if udid:
        data = [d for d in data if d.get("UniqueDeviceID") == udid or d.get("Identifier") == udid]
    return data


def dev_mode_status(udid):
    rc, out = run_pmd(["amfi", "developer-mode-status", "--udid", udid], timeout=60)
    low = out.lower()
    if "true" in low.split():
        return True, out
    if "false" in low.split():
        return False, out
    # tolerate lines with surrounding whitespace/log noise
    if "true" in low and "false" not in low:
        return True, out
    if "false" in low and "true" not in low:
        return False, out
    return None, out  # unknown / errored (e.g. stale pairing -> mux 183)


def is_stale_pairing(out):
    o = out.lower()
    return "183" in o or "fatalpairing" in o or "not paired" in o


def stale_record_path(udid):
    return os.path.join(LOCKDOWN_DIR, f"{udid}.plist")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--udid")
    ap.add_argument("--status", action="store_true", help="report only, change nothing")
    ap.add_argument("--fix-pairing", action="store_true", help="delete a stale pair record if found")
    args = ap.parse_args()

    say("== iOS Developer Mode helper (Windows) ==")

    if not tool_present():
        say("[X] pymobiledevice3 not found. Install it first:")
        say("    python -m pip install -U pymobiledevice3")
        return 3

    if not usbmuxd_alive():
        say(f"[X] usbmuxd is down (127.0.0.1:{USBMUXD_PORT} closed).")
        say("    On Windows, usbmuxd is provided by iTunes / Apple Mobile Device.")
        say("    Start iTunes (the Store app), then REPLUG the USB cable, then rerun.")
        return 3
    say(f"[ok] usbmuxd is up (port {USBMUXD_PORT}).")

    present = list_raw()
    if not present:
        say("[X] No device seen by usbmuxd.")
        say("    Unlock the device, use a DATA-capable cable, and tap 'Trust' if prompted.")
        return 4

    udid = args.udid
    if not udid:
        if len(present) > 1:
            say(f"[!] Multiple devices connected: {present}")
            say("    Rerun with --udid <UDID> to pick one.")
            return 2
        udid = present[0]
    elif udid not in present:
        say(f"[X] Requested UDID {udid} is not connected. Present: {present}")
        return 4

    info = paired_info(udid)
    if info:
        d = info[0]
        say(f"[ok] Device: {d.get('DeviceName')}  ({d.get('ProductType')}, "
            f"{d.get('DeviceClass')} {d.get('ProductVersion')})")
    else:
        say(f"[!] Device {udid} is present but not paired yet.")

    # Probe developer-mode status; a mux/pairing error here means the pair record is stale.
    status, out = dev_mode_status(udid)

    if status is None and is_stale_pairing(out):
        rec = stale_record_path(udid)
        say("[!] Pairing is broken (likely a stale record from a previous reset of this device).")
        if os.path.exists(rec):
            say(f"    Stale pair record: {rec}")
            if args.fix_pairing:
                try:
                    os.remove(rec)
                    say("    [ok] Deleted stale pair record.")
                except PermissionError:
                    say("    [X] Delete needs Administrator rights. Run this elevated:")
                    say(f'        Remove-Item "{rec}" -Force')
                    return 2
            else:
                say("    Rerun with --fix-pairing to remove it (a backup is wise), or delete it")
                say(f'    manually (elevated): Remove-Item "{rec}" -Force')
                return 2
        # After clearing, we must re-pair.
        say("    Now pairing. Unlock the device and tap 'Trust' if prompted...")
        rc, pout = run_pmd(["lockdown", "pair", "--udid", udid], timeout=120)
        if "password protected" in pout.lower() or "unlock" in pout.lower():
            say("    [pause] Device is locked. Unlock it (stay on the Home screen), then rerun.")
            return 2
        say("    [ok] Paired. Re-checking status...")
        status, out = dev_mode_status(udid)

    if args.status:
        say(f"== Developer Mode: {status} ==")
        return 0 if status else 2

    if status is True:
        say("== Developer Mode is already ON. Nothing to do. ==")
        return 0

    if status is None:
        say("[X] Could not read developer-mode status:")
        say("    " + out.strip().splitlines()[-1] if out.strip() else "    (no output)")
        return 3

    # status is False -> reveal the menu and try to enable.
    say("[..] Revealing the Developer Mode menu on the device...")
    run_pmd(["amfi", "reveal-developer-mode", "--udid", udid], timeout=60)

    say("[..] Attempting to enable...")
    rc, eout = run_pmd(["amfi", "enable-developer-mode", "--udid", udid], timeout=120)
    low = eout.lower()

    if "passcode is set" in low:
        say("")
        say(">> Apple blocks the final switch from the computer when a passcode is set.")
        say(">> Finish on the device (the menu is now visible):")
        say("   1. Settings -> Privacy & Security")
        say("   2. Scroll to 'Developer Mode'")
        say("   3. Turn it ON -> tap Restart")
        say("   4. After reboot & unlock, tap 'Turn On' -> enter passcode")
        say("")
        say("   Then verify with:  python enable_dev_mode.py --status")
        return 2

    # No passcode path: enabling triggers a reboot. Verify after it settles.
    say("[..] Enable issued (device reboots). Waiting for it to come back...")
    status, _ = dev_mode_status(udid)
    if status is True:
        say("== Developer Mode is now ON. ==")
        return 0
    say("[!] Not confirmed yet. After the device reboots and you unlock it, run:")
    say("    python enable_dev_mode.py --status")
    return 2


if __name__ == "__main__":
    sys.exit(main())

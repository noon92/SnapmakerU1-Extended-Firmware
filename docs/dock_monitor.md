---
title: Toolhead Dock Monitor
---

# Toolhead Dock Monitor

Stock U1 firmware only checks whether toolheads are in their docks during a
tool change. If a crash knocks a parked toolhead off its dock in the middle
of a single-tool section, the print keeps running and the active toolhead
keeps hitting it.

The Dock Monitor watches every toolhead's dock sensors for the whole print.
If a parked toolhead leaves its dock (or the active toolhead leaves the
carriage) for 7 seconds outside a tool change, it pauses the print and shows
an error such as **"Toolhead 3 undocked while idle, pausing print!"**. Fix
the toolhead and resume.

## Configure

1. Enable Advanced Mode on the printer screen.
2. Open `http://<printer-ip>/firmware-config/`.
3. Go to **Settings -> Print Preferences -> Toolhead Dock Monitor**.
4. Choose **Enabled**. Klipper restarts.

Run `QUERY_DOCK_MONITOR` in the console to see the live sensor state of each
toolhead. `SET_DOCK_MONITOR ENABLE=0` disables it until the next restart.

Toolheads using the [Faulty Toolhead](faulty_toolhead.md) "Bypass MCU"
option are skipped automatically.

## Limitations

The monitor cannot prevent the initial crash; it stops the printer from
continuing afterwards. A toolhead that is hit but stays seated on its dock
sensor is not detected.

Technical details: [overlay README](../overlays/firmware-extended/42-feature-dock-monitor/README.md).

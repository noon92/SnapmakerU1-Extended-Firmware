---
title: Toolhead Dock Monitor
---

# Toolhead Dock Monitor

Stock U1 firmware only checks whether toolheads are in their docks during a
tool change. If a crash knocks a parked toolhead off its dock in the middle
of a single-tool section, the print keeps running and the active toolhead
keeps hitting it.

The Dock Monitor watches every toolhead's park sensors for the whole print
and, if a parked toolhead leaves its dock (or the active toolhead leaves the
carriage) for more than ~0.5 s outside a tool change, it either:

- **Pause Print** - shows a toolhead park error on the screen and pauses, or
- **Emergency Stop** - shuts Klipper down immediately (no further motion;
  the print cannot be resumed).

## Configure

1. Enable Advanced Mode on the printer screen.
2. Open `http://<printer-ip>/firmware-config/`.
3. Go to **Settings -> Print Preferences -> Toolhead Dock Monitor**.
4. Choose **Pause Print** or **Emergency Stop**. Klipper restarts.

Run `QUERY_DOCK_MONITOR` in the console to see the live sensor state of each
toolhead. `SET_DOCK_MONITOR ENABLE=0` disables it until the next restart.

Toolheads using the [Faulty Toolhead](faulty_toolhead.md) "Bypass MCU"
option are skipped automatically.

## Limitations

The monitor cannot prevent the initial crash; it stops the printer from
continuing afterwards. A toolhead that is hit but stays seated on its dock
sensor is not detected.

Technical details: [overlay README](../overlays/firmware-extended/42-feature-dock-monitor/README.md).

# Toolhead Dock Monitor

Adds a Klipper extra, `[dock_monitor]`, that watches every toolhead's park
detector **during a print** and pauses the print when a parked toolhead
leaves its dock or the active toolhead leaves the carriage.

## Why

Stock U1 firmware only reads the park detectors inside `ACTIVATE_EXTRUDER`
(pre-park, post-park, pre-pick and post-pick checks, error codes
`0002-0523-000N-0019/0020/0021/0042/0043/0044`). Between tool changes nothing
looks at them. If the carriage loses position and crashes into the dock wall,
a parked toolhead can be knocked off its dock - left hanging by its Bowden
tube and cable - while the print carries on and the active head keeps hitting
it. On a long single-tool section that can continue until the print ends.

Snapmaker shipped a background check for this in firmware 1.2.0
(`_park_detector_loop_check` in `klippy/kinematics/extruder.py`), but it was
hard-wired off (`park_check_enable = False`, all callers commented out), and
the loop was removed in 1.3.0. Only the unused `park_check_enable` config
option and a `# TODO: add extruder park check` comment remain. The only other
in-print signal is the fan-RPM based "pogopin disconnection" warning, which is
reported once, at the end of the print, and never pauses.

## Behaviour

Every `check_interval` (default 0.1 s), while `print_stats` is `printing`:

- each **parked** toolhead must read `PARKED`
- the **active** toolhead must read `ACTIVATE` (`check_active`, default on)

Checks are skipped while a tool change is in progress (`activating_move`, or
`toolhead.get_grab_complete()` false), for `switch_grace_time` (1.5 s) after
one, while paused or a pause is pending, and when not printing.

A mismatch that persists for `trigger_time` (default 7 s) pauses the print.
It raises a pause-level exception (`id 523`, index = the toolhead, code
reused from the stock park errors - `42` out of park position, `43`
detached, `44` conflicting, `20` other, `19` for the active toolhead) with
the message, e.g.:

    Toolhead 3 undocked while idle, pausing print!
    Toolhead 1 lost from carriage while printing, pausing print!

then runs `PAUSE`. The user can resume; monitoring re-arms on resume, so
resuming without fixing the dock pauses again 7 s later. Pin details
(park/active/grab) go to the console and klippy.log.

## Sensors

Per toolhead there are three inputs: `park_pin` (dock switch on the
mainboard: "a toolhead is sitting in this dock"), and two analog Hall
inputs on the toolhead board, `active_pin` (toolhead is on the carriage)
and `grab_valid_pin` (carriage grip is engaged), thresholded to booleans
by the stock `park_detector`. For a parked toolhead only `park_pin` says
anything positive - the carriage-side sensors read false whether the
toolhead is in its dock or hanging off it - so there is no useful 2-of-3
vote and a time threshold is used instead.

Toolheads under the Faulty Toolhead "Bypass MCU" option (`[mcu eN]` serial
under `/tmp/klipper_host_`) are skipped automatically, since the real
toolhead may be removed. Others can be skipped with `ignore_extruders`.

## Commands

- `QUERY_DOCK_MONITOR` - prints the live state of every watched toolhead
- `SET_DOCK_MONITOR ENABLE=0|1` - disable/enable at runtime (e.g. from a
  macro that deliberately moves a toolhead out of its dock)

## Configure

Firmware Config -> Settings -> Print Preferences -> Toolhead Dock Monitor:
`Enabled` / `Disabled` (default). Other options can be set in a
`[dock_monitor]` section in `extended/klipper/*.cfg`:

```
[dock_monitor]
trigger_time: 7.0
check_interval: 0.1
switch_grace_time: 1.5
check_active: True
ignore_extruders:        # e.g. extruder2, extruder3
ignore_simulated: True
```

## Measuring false positives

`tools/dock_logger.py` is a read-only Moonraker poller that logs every
dock-sensor mismatch with its context (UNEXPECTED / TOOLCHANGE /
NOT_PRINTING) and duration, to check how noisy the sensors are on a given
printer before relying on the monitor. Run it on the printer:

    nohup python3 dock_logger.py --verbose >/dev/null 2>&1 &

## Limitations

This does not prevent the first crash - it cannot know the carriage has lost
position. It stops the printer from continuing once a toolhead has been
dislodged. A toolhead that is bumped but stays seated on its dock sensor is
not detected.

## Tests

```
python3 overlays/firmware-extended/42-feature-dock-monitor/test/test_dock_monitor.py
```

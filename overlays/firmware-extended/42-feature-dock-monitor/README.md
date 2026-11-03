# Toolhead Dock Monitor

Adds a Klipper extra, `[dock_monitor]`, that watches every toolhead's park
detector **during a print** and pauses (or emergency-stops) the printer when
a parked toolhead leaves its dock or the active toolhead leaves the carriage.

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

`trigger_count` (5) consecutive bad samples trigger the action:

- `pause` (default): raises a pause-level exception (`id 523`, index = the
  toolhead, code reused from the stock park errors - `42` out of park
  position, `43` detached, `44` conflicting, `20` other, `19` for the active
  toolhead) so the screen shows it, then runs `PAUSE`. Monitoring re-arms on
  resume, so resuming without fixing the dock pauses again.
- `shutdown`: `invoke_shutdown()` - no further motion at all, print is lost.
  Use this if you do not trust the pause move to clear a dangling toolhead.

Toolheads under the Faulty Toolhead "Bypass MCU" option (`[mcu eN]` serial
under `/tmp/klipper_host_`) are skipped automatically, since the real
toolhead may be removed. Others can be skipped with `ignore_extruders`.

## Commands

- `QUERY_DOCK_MONITOR` - prints the live state of every watched toolhead
- `SET_DOCK_MONITOR ENABLE=0|1` - disable/enable at runtime (e.g. from a
  macro that deliberately moves a toolhead out of its dock)

## Configure

Firmware Config -> Settings -> Print Preferences -> Toolhead Dock Monitor:
`Disabled` (default), `Pause Print`, or `Emergency Stop`. Options beyond
`action` can be set in a `[dock_monitor]` section in
`extended/klipper/*.cfg`:

```
[dock_monitor]
action: pause            # pause | shutdown
check_interval: 0.1
trigger_count: 5
switch_grace_time: 1.5
check_active: True
ignore_extruders:        # e.g. extruder2, extruder3
ignore_simulated: True
```

## Limitations

This does not prevent the first crash - it cannot know the carriage has lost
position. It stops the printer from continuing once a toolhead has been
dislodged. A toolhead that is bumped but stays seated on its dock sensor is
not detected.

## Tests

```
python3 overlays/firmware-extended/42-feature-dock-monitor/test/test_dock_monitor.py
```

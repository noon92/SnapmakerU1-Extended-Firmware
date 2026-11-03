# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @paxx12
#
# Continuous toolhead dock monitoring during a print.
#
# Stock Snapmaker U1 firmware only reads the park detectors (dock/grab hall
# sensors) inside ACTIVATE_EXTRUDER, i.e. at tool-change time. Between tool
# changes nothing watches them: if the active toolhead crashes into the dock
# wall and knocks a parked toolhead off its dock (or half out of it), the
# print keeps going and the carriage keeps hitting it until the next tool
# change - which on a single-tool section may never come.
#
# Stock firmware 1.2.0 contained a background check for exactly this
# (_park_detector_loop_check in kinematics/extruder.py), but it was
# hard-wired off and removed in 1.3.0; only the unused `park_check_enable`
# config option remains.
#
# This module restores that check as a standalone extra:
#   - every parked (inactive) toolhead must read PARKED
#   - the active toolhead must read ACTIVATE (optional)
# checked only while print_stats is "printing", outside tool changes, and
# debounced. On a sustained mismatch it pauses the print (default) or
# shuts the printer down.
#
# Config:
#   [dock_monitor]
#   #action: pause            # pause | shutdown
#   #check_interval: 0.1      # seconds between samples
#   #trigger_count: 5         # consecutive bad samples before acting
#   #switch_grace_time: 1.5   # seconds to ignore after a tool change
#   #check_active: True       # also require the active toolhead to be grabbed
#   #ignore_extruders:        # comma separated, e.g. extruder2, extruder3
#   #ignore_simulated: True   # skip toolheads using the MCU-bypass simulator

import logging

SIMULATED_SERIAL_PREFIX = '/tmp/klipper_host_'

# Error codes reused from stock extruder.py so the screen shows a known
# toolhead park error (0002 = pause level, 0523 = extruder module).
CODE_PARK_GENERIC = 20       # "malfunction after parking"
CODE_PARK_NOT_RETURNED = 42  # pin state FTx: off the park sensor, still seen
CODE_PARK_DETACHED = 43      # pin state FFx: toolhead detached
CODE_PARK_CONFLICT = 44      # pin state TTx: parked and picked at once
CODE_ACTIVE_LOST = 19        # active toolhead no longer reads ACTIVATE

class DockMonitor:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.action = config.getchoice('action',
                                       {'pause': 'pause',
                                        'shutdown': 'shutdown'}, 'pause')
        self.check_interval = config.getfloat('check_interval', 0.1,
                                              minval=0.02, maxval=2.)
        self.trigger_count = config.getint('trigger_count', 5, minval=1)
        self.switch_grace_time = config.getfloat('switch_grace_time', 1.5,
                                                 minval=0.)
        self.check_active = config.getboolean('check_active', True)
        self.ignore_simulated = config.getboolean('ignore_simulated', True)
        self.ignore = set(
            n.strip() for n in config.getlist('ignore_extruders', ())
            if n.strip())
        self.enabled = True
        self.simulated = set()
        self._detect_simulated(config)
        self.extruders = []
        self.print_stats = None
        self.pause_resume = None
        self.toolhead = None
        self.bad_counts = {}
        self.last_switch_time = 0.
        self.tripped = None
        self.timer = self.reactor.register_timer(self._check)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:shutdown',
                                            self._handle_shutdown)
        self.printer.register_event_handler('extruder:activate_extruder',
                                            self._handle_tool_change)
        self.printer.register_event_handler('print_stats:new_task_start',
                                            self._reset)
        self.gcode.register_command('SET_DOCK_MONITOR',
                                    self.cmd_SET_DOCK_MONITOR,
                                    desc=self.cmd_SET_DOCK_MONITOR_help)
        self.gcode.register_command('QUERY_DOCK_MONITOR',
                                    self.cmd_QUERY_DOCK_MONITOR,
                                    desc=self.cmd_QUERY_DOCK_MONITOR_help)

    def _detect_simulated(self, config):
        # Toolheads under the faulty-toolhead "Bypass MCU" option are backed
        # by a simulator and may be physically absent from the dock.
        for i in range(8):
            section = 'mcu e%d' % (i,)
            if not config.has_section(section):
                continue
            serial = config.getsection(section).get('serial', '')
            if serial.startswith(SIMULATED_SERIAL_PREFIX):
                self.simulated.add(i)

    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.print_stats = self.printer.lookup_object('print_stats', None)
        self.pause_resume = self.printer.lookup_object('pause_resume', None)
        extruders = self.printer.lookup_object('extruder_list', [])
        self.extruders = [
            e for e in extruders
            if getattr(e, 'park_detector', None) is not None
            and e.name not in self.ignore
            and not (self.ignore_simulated
                     and getattr(e, 'extruder_num', None) in self.simulated)]
        logging.info("dock_monitor: watching %s (action=%s, %d x %.2fs)",
                     [e.name for e in self.extruders], self.action,
                     self.trigger_count, self.check_interval)
        if self.print_stats is None or len(self.extruders) < 1:
            logging.info("dock_monitor: nothing to watch, disabled")
            return
        self.reactor.update_timer(self.timer, self.reactor.NOW)

    def _handle_shutdown(self):
        self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _handle_tool_change(self):
        self.last_switch_time = self.reactor.monotonic()
        self._reset()

    def _reset(self):
        self.bad_counts = {}
        self.tripped = None

    def _should_check(self, eventtime):
        if not self.enabled:
            return False
        if self.print_stats.state != 'printing':
            return False
        if self.pause_resume is not None and (
                self.pause_resume.is_paused
                or getattr(self.pause_resume, 'pause_command_sent', False)):
            return False
        if not self.toolhead.get_grab_complete():
            return False
        for e in self.extruders:
            if getattr(e, 'activating_move', False):
                self.last_switch_time = eventtime
                return False
        if eventtime - self.last_switch_time < self.switch_grace_time:
            return False
        return True

    def _check(self, eventtime):
        try:
            if not self._should_check(eventtime):
                self.bad_counts = {}
                return eventtime + self.check_interval
            active = self.toolhead.get_extruder()
            for e in self.extruders:
                status = e.get_park_detector_status()
                if status is None:
                    continue
                if e is active:
                    if not self.check_active:
                        continue
                    ok = status['state'] == 'ACTIVATE'
                else:
                    ok = status['state'] == 'PARKED'
                if ok:
                    self.bad_counts.pop(e.name, None)
                    continue
                cnt = self.bad_counts.get(e.name, 0) + 1
                self.bad_counts[e.name] = cnt
                if cnt >= self.trigger_count:
                    self._trip(e, e is active, status)
                    return self.reactor.NEVER
        except Exception:
            logging.exception("dock_monitor: check failed")
        return eventtime + self.check_interval

    def _trip(self, extruder, is_active, status):
        pins = ''.join(['T' if status.get(k) else 'F' for k in
                        ('park_pin', 'active_pin', 'grab_valid_pin')])
        if is_active:
            code = CODE_ACTIVE_LOST
            msg = ("Dock monitor: active %s is no longer held by the carriage"
                   " during print, state: %s [%s]"
                   % (extruder.name, status['state'], pins))
        else:
            code = CODE_PARK_GENERIC
            what = "left its dock"
            if pins.startswith('FT'):
                code, what = CODE_PARK_NOT_RETURNED, "is out of its park position"
            elif pins.startswith('FF'):
                code, what = CODE_PARK_DETACHED, "detached from its dock"
            elif pins.startswith('TT'):
                code, what = CODE_PARK_CONFLICT, "reports parked and picked at once"
            msg = ("Dock monitor: parked %s %s during print (active: %s),"
                   " state: %s [%s]" % (extruder.name, what,
                                        self.toolhead.get_extruder().name,
                                        status['state'], pins))
        self.tripped = msg
        logging.error(msg)
        self.gcode.respond_raw("!! " + msg)
        if self.action == 'shutdown':
            self.printer.invoke_shutdown(msg)
            return
        index = getattr(extruder, 'extruder_num', 0)
        self.printer.send_event("print_stats:update_exception_info",
                                523, index, code, msg, 2)
        if self.pause_resume is not None:
            self.pause_resume.send_pause_command()
        exception_manager = self.printer.lookup_object('exception_manager',
                                                       None)
        if exception_manager is not None:
            exception_manager.raise_exception_async(
                id=523, index=index, code=code, message=msg,
                oneshot=1, level=2)
        self.reactor.register_callback(self._run_pause)

    def _run_pause(self, eventtime):
        try:
            self.gcode.run_script('\nPAUSE\nM400\n')
        except Exception:
            logging.exception("dock_monitor: PAUSE failed")
        # Resume watching; _should_check() stays idle until printing again.
        self.bad_counts = {}
        self.reactor.update_timer(self.timer,
                                  self.reactor.monotonic() + self.check_interval)

    cmd_SET_DOCK_MONITOR_help = "Enable or disable toolhead dock monitoring"
    def cmd_SET_DOCK_MONITOR(self, gcmd):
        self.enabled = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        self.bad_counts = {}
        gcmd.respond_info("dock_monitor %s"
                          % ('enabled' if self.enabled else 'disabled'))

    cmd_QUERY_DOCK_MONITOR_help = "Report toolhead dock monitor state"
    def cmd_QUERY_DOCK_MONITOR(self, gcmd):
        eventtime = self.reactor.monotonic()
        lines = ["dock_monitor: %s, action=%s, checking=%s"
                 % ('enabled' if self.enabled else 'disabled', self.action,
                    self.print_stats is not None
                    and self._should_check(eventtime))]
        active = self.toolhead.get_extruder() if self.toolhead else None
        for e in self.extruders:
            s = e.get_park_detector_status() or {}
            lines.append("  %s%s: %s park=%s active=%s grab=%s"
                         % (e.name, ' (active)' if e is active else '',
                            s.get('state'), s.get('park_pin'),
                            s.get('active_pin'), s.get('grab_valid_pin')))
        skipped = sorted(self.ignore) + ['e%d (simulated)' % i
                                         for i in sorted(self.simulated)]
        if skipped:
            lines.append("  ignored: %s" % (', '.join(skipped),))
        if self.tripped:
            lines.append("  last trip: %s" % (self.tripped,))
        gcmd.respond_info('\n'.join(lines))

    def get_status(self, eventtime):
        return {'enabled': self.enabled, 'action': self.action,
                'tripped': self.tripped}

def load_config(config):
    return DockMonitor(config)

# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @paxx12
#
# Offline tests for dock_monitor.py using fake Klipper objects.
# Run: python3 overlays/firmware-extended/42-feature-dock-monitor/test/test_dock_monitor.py

import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'root', 'home', 'lava', 'klipper',
                                'klippy', 'extras'))
import dock_monitor

NEVER = 9e99

class Reactor:
    NOW = 0.
    NEVER = NEVER
    def __init__(self):
        self.t = 100.
        self.timers = {}
        self.callbacks = []
    def monotonic(self):
        return self.t
    def register_timer(self, cb):
        self.timers[cb] = NEVER
        return cb
    def update_timer(self, cb, when):
        self.timers[cb] = when
    def register_callback(self, cb):
        self.callbacks.append(cb)

class GCode:
    def __init__(self):
        self.scripts, self.raw, self.commands = [], [], {}
    def register_command(self, name, fn, desc=None):
        self.commands[name] = fn
    def run_script(self, s):
        self.scripts.append(s)
    def respond_raw(self, m):
        self.raw.append(m)

class Section:
    def __init__(self, opts):
        self.opts = opts
    def get(self, k, default=None):
        return self.opts.get(k, default)

class Config:
    def __init__(self, printer, opts=None, sections=None):
        self.printer, self.opts = printer, opts or {}
        self.sections = sections or {}
    def get_printer(self):
        return self.printer
    def getchoice(self, k, choices, default):
        return choices[self.opts.get(k, default)]
    def getfloat(self, k, default, **kw):
        return float(self.opts.get(k, default))
    def getint(self, k, default, **kw):
        return int(self.opts.get(k, default))
    def getboolean(self, k, default):
        return bool(self.opts.get(k, default))
    def getlist(self, k, default):
        v = self.opts.get(k)
        return default if v is None else v.split(',')
    def has_section(self, s):
        return s in self.sections
    def getsection(self, s):
        return Section(self.sections[s])

class Extruder:
    def __init__(self, num):
        self.extruder_num = num
        self.name = 'extruder' if num == 0 else 'extruder%d' % num
        self.park_detector = object()
        self.activating_move = False
        self.state = 'PARKED'
    def get_park_detector_status(self):
        pins = {'PARKED': (True, False, False),
                'ACTIVATE': (False, True, True),
                'DETACHED': (False, False, False),
                'OUTOFPARK': (False, True, False)}[self.state]
        st = self.state if self.state in ('PARKED', 'ACTIVATE') else 'UNKNOWN'
        return {'state': st, 'park_pin': pins[0], 'active_pin': pins[1],
                'grab_valid_pin': pins[2]}

class Toolhead:
    def __init__(self, ext):
        self.ext, self.grab = ext, True
    def get_extruder(self):
        return self.ext
    def get_grab_complete(self):
        return self.grab

class PrintStats:
    state = 'printing'

class PauseResume:
    def __init__(self):
        self.is_paused, self.pause_command_sent, self.sent = False, False, 0
    def send_pause_command(self):
        self.pause_command_sent = True
        self.sent += 1

class ExcMgr:
    def __init__(self):
        self.raised = []
    def raise_exception_async(self, **kw):
        self.raised.append(kw)

class Printer:
    def __init__(self):
        self.reactor, self.gcode = Reactor(), GCode()
        self.extruders = [Extruder(i) for i in range(4)]
        self.extruders[0].state = 'ACTIVATE'
        self.objs = {
            'gcode': self.gcode,
            'toolhead': Toolhead(self.extruders[0]),
            'print_stats': PrintStats(),
            'pause_resume': PauseResume(),
            'exception_manager': ExcMgr(),
            'extruder_list': self.extruders,
        }
        self.handlers, self.events, self.shutdown = {}, [], None
    def get_reactor(self):
        return self.reactor
    def lookup_object(self, n, default=None):
        return self.objs.get(n, default)
    def register_event_handler(self, ev, cb):
        self.handlers[ev] = cb
    def send_event(self, *a):
        self.events.append(a)
    def invoke_shutdown(self, msg):
        self.shutdown = msg

def make(opts=None, sections=None):
    p = Printer()
    dm = dock_monitor.load_config(Config(p, opts, sections))
    p.handlers['klippy:ready']()
    return p, dm

def run(p, dm, n, dt=0.1):
    tripped_at = None
    for i in range(n):
        p.reactor.t += dt
        nxt = dm._check(p.reactor.t)
        if nxt == NEVER:
            tripped_at = i + 1
            break
    return tripped_at

class Tests(unittest.TestCase):
    def setUp(self):
        pass

    def test_quiet_when_all_ok(self):
        p, dm = make()
        p.reactor.t += 5
        self.assertIsNone(run(p, dm, 100))
        self.assertEqual(p.objs['pause_resume'].sent, 0)

    def test_parked_head_knocked_off_pauses(self):
        p, dm = make()
        p.reactor.t += 5
        run(p, dm, 10)
        p.extruders[2].state = 'DETACHED'
        self.assertEqual(run(p, dm, 20), 5)
        self.assertEqual(p.objs['pause_resume'].sent, 1)
        exc = p.objs['exception_manager'].raised[0]
        self.assertEqual((exc['id'], exc['index'], exc['code'], exc['level']),
                         (523, 2, 43, 2))
        for cb in p.reactor.callbacks:
            cb(p.reactor.t)
        self.assertIn('PAUSE', p.gcode.scripts[0])

    def test_glitch_shorter_than_threshold_ignored(self):
        p, dm = make()
        p.reactor.t += 5
        for _ in range(10):
            p.extruders[1].state = 'OUTOFPARK'
            self.assertIsNone(run(p, dm, 4))
            p.extruders[1].state = 'PARKED'
            self.assertIsNone(run(p, dm, 1))
        self.assertEqual(p.objs['pause_resume'].sent, 0)

    def test_active_head_lost(self):
        p, dm = make()
        p.reactor.t += 5
        p.extruders[0].state = 'DETACHED'
        self.assertEqual(run(p, dm, 20), 5)
        self.assertEqual(p.objs['exception_manager'].raised[0]['code'], 19)

    def test_active_check_can_be_disabled(self):
        p, dm = make({'check_active': False})
        p.reactor.t += 5
        p.extruders[0].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 20))

    def test_not_printing_ignored(self):
        p, dm = make()
        p.objs['print_stats'].state = 'standby'
        p.extruders[3].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 50))

    def test_paused_ignored(self):
        p, dm = make()
        p.objs['pause_resume'].is_paused = True
        p.extruders[3].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 50))

    def test_tool_change_and_grace(self):
        p, dm = make()
        p.reactor.t += 5
        # mid-swap: activating_move set, states are in flux
        p.extruders[1].activating_move = True
        p.extruders[0].state = 'OUTOFPARK'
        p.extruders[1].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 30))
        # swap done: T1 active, T0 parked; grace window still running
        p.extruders[1].activating_move = False
        p.extruders[0].state = 'OUTOFPARK'
        p.extruders[1].state = 'ACTIVATE'
        p.objs['toolhead'].ext = p.extruders[1]
        p.handlers['extruder:activate_extruder']()
        self.assertIsNone(run(p, dm, 14))   # 1.4 s < 1.5 s grace
        p.extruders[0].state = 'PARKED'
        self.assertIsNone(run(p, dm, 50))

    def test_grab_incomplete_ignored(self):
        p, dm = make()
        p.reactor.t += 5
        p.objs['toolhead'].grab = False
        p.extruders[0].state = 'PARKED'   # all parked (ACTION=PARK)
        self.assertIsNone(run(p, dm, 50))

    def test_ignore_list(self):
        p, dm = make({'ignore_extruders': 'extruder2, extruder3'})
        p.reactor.t += 5
        p.extruders[2].state = 'DETACHED'
        p.extruders[3].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 50))

    def test_simulated_mcu_ignored(self):
        p, dm = make(sections={'mcu e1': {'serial': '/tmp/klipper_host_e1'},
                               'mcu e2': {'serial': '/dev/serial/by-id/x'}})
        p.reactor.t += 5
        p.extruders[1].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 50))
        p.extruders[2].state = 'DETACHED'
        self.assertEqual(run(p, dm, 20), 5)

    def test_shutdown_action(self):
        p, dm = make({'action': 'shutdown'})
        p.reactor.t += 5
        p.extruders[3].state = 'DETACHED'
        self.assertEqual(run(p, dm, 20), 5)
        self.assertIn('extruder3', p.shutdown)
        self.assertEqual(p.objs['pause_resume'].sent, 0)

    def test_rearms_after_pause_and_resume(self):
        p, dm = make()
        p.reactor.t += 5
        p.extruders[2].state = 'DETACHED'
        self.assertEqual(run(p, dm, 20), 5)
        for cb in p.reactor.callbacks:
            cb(p.reactor.t)
        self.assertNotEqual(p.reactor.timers[dm._check], NEVER)
        pr = p.objs['pause_resume']
        pr.is_paused = True
        self.assertIsNone(run(p, dm, 20))
        # user resumes without fixing it -> pauses again
        pr.is_paused, pr.pause_command_sent = False, False
        self.assertEqual(run(p, dm, 20), 5)

    def test_disable_command(self):
        p, dm = make()
        p.reactor.t += 5
        class G:
            def get_int(self, *a, **k): return 0
            def respond_info(self, m): pass
        p.gcode.commands['SET_DOCK_MONITOR'](G())
        p.extruders[2].state = 'DETACHED'
        self.assertIsNone(run(p, dm, 50))

if __name__ == '__main__':
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @paxx12
#
# dock_logger.py - log toolhead dock (park detector) anomalies on a
# Snapmaker U1, to find out how often the dock sensors disagree with what
# the printer thinks is going on.
#
# Read-only: polls Moonraker's HTTP API on the printer, changes nothing.
# Stdlib only, runs on the printer's own python3.
#
# For every toolhead it compares the sensor state with what it should be:
#   - the active toolhead (toolhead.extruder) should read ACTIVATE
#   - every other toolhead should read PARKED
# and classifies each mismatch by context:
#   UNEXPECTED   printing, no tool change in progress or just finished
#                -> this is what a dock monitor would trip on
#   TOOLCHANGE   during a pick/park (activating_move) or the grace period
#                after it -> expected, the gantry is moving heads on purpose
#   NOT_PRINTING printer idle/paused/etc -> informational
#
# Each anomaly is logged when it starts and again when it ends, with its
# duration, so short sensor glitches can be told apart from real
# disconnections. Tool changes, print start/end and an hourly summary are
# logged too.
#
# Usage (on the printer, over SSH):
#   python3 dock_logger.py                       # foreground, Ctrl-C to stop
#   nohup python3 dock_logger.py >/dev/null 2>&1 &   # survives SSH logout
#   kill $(cat /tmp/dock_logger.pid)             # stop the background one
#
# Options:
#   --log PATH        default /oem/printer_data/logs/dock_logger.log
#                     (downloadable from Fluidd/Mainsail -> Logs)
#   --interval SEC    poll period, default 0.1
#   --grace SEC       seconds after a tool change still counted as
#                     TOOLCHANGE, default 2.0
#   --url URL         Moonraker base URL, default http://127.0.0.1:7125
#   --verbose         also log every raw sensor change, any context

import argparse, json, os, re, signal, sys, time
import urllib.request, urllib.error

DEFAULT_LOGS = ['/oem/printer_data/logs', '/home/lava/printer_data/logs']

def default_log_path():
    for d in DEFAULT_LOGS:
        if os.path.isdir(d):
            return os.path.join(d, 'dock_logger.log')
    return os.path.join(os.path.expanduser('~'), 'dock_logger.log')

class Moonraker:
    def __init__(self, url, timeout=2.0):
        self.url = url.rstrip('/')
        self.timeout = timeout
    def get(self, path):
        with urllib.request.urlopen(self.url + path,
                                    timeout=self.timeout) as r:
            return json.loads(r.read().decode())['result']

def short_name(name):
    # extruder -> TH1, extruder1 -> TH2 ... (1-based, like the U1 screen)
    m = re.match(r'^extruder(\d*)$', name)
    return 'TH%d' % ((int(m.group(1)) if m.group(1) else 0) + 1) if m else name

def pins(st):
    return ''.join('T' if st.get(k) else 'F'
                   for k in ('park_pin', 'active_pin', 'grab_valid_pin'))

class Logger:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.f = open(path, 'a', buffering=1)
    def log(self, kind, msg):
        t = time.time()
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))
        line = '%s.%03d %-12s %s' % (stamp, int((t % 1) * 1000), kind, msg)
        self.f.write(line + '\n')
        if sys.stdout.isatty():
            print(line, flush=True)

class DockLogger:
    def __init__(self, args):
        self.args = args
        self.mr = Moonraker(args.url)
        self.out = Logger(args.log)
        self.extruders = []
        self.query = None
        self.last_switch_end = 0.
        self.switching = False
        self.print_state = None
        self.klippy_state = None
        self.anomalies = {}      # name -> dict(start, context, state, pins, ...)
        self.last_raw = {}
        self.counts = {'UNEXPECTED': 0, 'TOOLCHANGE': 0, 'NOT_PRINTING': 0,
                       'switches': 0}
        self.max_unexpected = 0.
        self.last_summary = time.monotonic()
        self.errors = 0

    def discover(self):
        objs = self.mr.get('/printer/objects/list')['objects']
        self.extruders = sorted(o for o in objs
                                if re.match(r'^extruder\d*$', o))
        parts = ['%s=state,park_pin,active_pin,grab_valid_pin,activating_move'
                 % e for e in self.extruders]
        parts.append('toolhead=extruder,position,homed_axes')
        parts.append('print_stats=state,filename')
        parts.append('virtual_sdcard=progress')
        self.query = '/printer/objects/query?' + '&'.join(parts)
        self.out.log('START', 'watching %s, interval %.2fs, grace %.1fs, log %s'
                     % (' '.join('%s=%s' % (short_name(e), e)
                                 for e in self.extruders),
                        self.args.interval, self.args.grace, self.args.log))

    def context(self, now, status):
        if self.print_state != 'printing':
            return 'NOT_PRINTING'
        if self.switching or now - self.last_switch_end < self.args.grace:
            return 'TOOLCHANGE'
        return 'UNEXPECTED'

    def where(self, status):
        th = status.get('toolhead', {})
        pos = th.get('position') or [0, 0, 0, 0]
        prog = status.get('virtual_sdcard', {}).get('progress')
        s = 'active=%s pos=X%.1f Y%.1f Z%.2f' % (
            short_name(th.get('extruder', '?')), pos[0], pos[1], pos[2])
        if prog is not None and self.print_state in ('printing', 'paused'):
            s += ' progress=%.1f%%' % (prog * 100.,)
        return s

    def step(self):
        now = time.monotonic()
        status = self.mr.get(self.query)['status']
        ps = status.get('print_stats', {})
        state = ps.get('state')
        if state != self.print_state:
            self.out.log('PRINT', '%s -> %s %s' % (
                self.print_state, state, ps.get('filename') or ''))
            self.print_state = state

        switching = any(status.get(e, {}).get('activating_move')
                        for e in self.extruders)
        if switching and not self.switching:
            self.counts['switches'] += 1
            self.out.log('SWITCH', 'tool change start (%s)' % self.where(status))
        elif self.switching and not switching:
            self.last_switch_end = now
            self.out.log('SWITCH', 'tool change done (%s)' % self.where(status))
        self.switching = switching

        active = status.get('toolhead', {}).get('extruder')
        for e in self.extruders:
            st = status.get(e, {})
            if 'state' not in st:
                continue
            p = pins(st)
            raw = (st['state'], p)
            if self.args.verbose and self.last_raw.get(e) != raw:
                self.out.log('RAW', '%s %s [%s] (%s)' % (
                    short_name(e), st['state'], p, self.where(status)))
            self.last_raw[e] = raw

            role = 'active' if e == active else 'parked'
            expected = 'ACTIVATE' if e == active else 'PARKED'
            ok = st['state'] == expected
            cur = self.anomalies.get(e)
            if not ok:
                ctx = self.context(now, status)
                if cur is None:
                    self.anomalies[e] = {'start': now, 'ctx': ctx,
                                         'worst': ctx, 'state': st['state'],
                                         'pins': p, 'role': role}
                    self.counts[ctx] += 1
                    self.out.log(ctx, '%s (%s) reads %s [%s] expected %s (%s)'
                                 % (short_name(e), role, st['state'], p,
                                    expected, self.where(status)))
                else:
                    if (st['state'], p) != (cur['state'], cur['pins']):
                        cur['state'], cur['pins'] = st['state'], p
                        self.out.log('CHANGE', '%s now %s [%s] (%s)' % (
                            short_name(e), st['state'], p, self.where(status)))
                    # escalate if a TOOLCHANGE anomaly outlives the tool change
                    if ctx == 'UNEXPECTED' and cur['worst'] != 'UNEXPECTED':
                        cur['worst'] = 'UNEXPECTED'
                        cur['escalated'] = now
                        self.counts['UNEXPECTED'] += 1
                        self.out.log('UNEXPECTED',
                                     '%s still %s [%s] %.2fs after tool change'
                                     ' ended (%s)' % (
                                         short_name(e), st['state'], p,
                                         now - self.last_switch_end,
                                         self.where(status)))
            elif cur is not None:
                dur = now - cur['start']
                udur = now - cur.get('escalated', cur['start'])
                if cur['worst'] == 'UNEXPECTED':
                    self.max_unexpected = max(self.max_unexpected, udur)
                self.out.log('CLEARED', '%s back to %s after %.2fs (%s%s)' % (
                    short_name(e), expected, dur, cur['worst'],
                    ', unexpected part %.2fs' % udur
                    if cur['worst'] == 'UNEXPECTED' else ''))
                del self.anomalies[e]

    def summary(self, final=False):
        open_ = ', '.join('%s %s for %.1fs' % (short_name(e), a['worst'],
                                                time.monotonic() - a['start'])
                          for e, a in self.anomalies.items())
        self.out.log('SUMMARY', '%stool changes=%d unexpected=%d toolchange=%d'
                     ' not_printing=%d longest_unexpected=%.2fs%s' % (
                         'final ' if final else '', self.counts['switches'],
                         self.counts['UNEXPECTED'], self.counts['TOOLCHANGE'],
                         self.counts['NOT_PRINTING'], self.max_unexpected,
                         (' open: ' + open_) if open_ else ''))

    def run(self):
        stop = []
        signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            while not stop:
                t0 = time.monotonic()
                try:
                    if self.query is None:
                        self.discover()
                    self.step()
                    if self.errors:
                        self.out.log('INFO', 'Moonraker/Klipper reachable again')
                    self.errors = 0
                except (urllib.error.URLError, OSError, KeyError,
                        ValueError) as e:
                    # klippy restarting/shutdown, moonraker down, etc.
                    if self.errors == 0:
                        self.out.log('ERROR', 'query failed: %s' % (e,))
                    self.errors += 1
                    self.query = None if isinstance(e, KeyError) else self.query
                    time.sleep(2.)
                    continue
                if time.monotonic() - self.last_summary >= 3600:
                    self.summary()
                    self.last_summary = time.monotonic()
                time.sleep(max(0., self.args.interval
                               - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
        self.summary(final=True)
        self.out.log('STOP', 'dock_logger stopped')

def main():
    ap = argparse.ArgumentParser(description='Log Snapmaker U1 toolhead dock anomalies (read-only).')
    ap.add_argument('--log', default=default_log_path())
    ap.add_argument('--interval', type=float, default=0.1)
    ap.add_argument('--grace', type=float, default=2.0)
    ap.add_argument('--url', default='http://127.0.0.1:7125')
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--pidfile', default='/tmp/dock_logger.pid')
    args = ap.parse_args()
    try:
        with open(args.pidfile, 'w') as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    DockLogger(args).run()

if __name__ == '__main__':
    main()

# Temperature compensation for a thermistor-equipped switching PINDA probe.
# Uses Klipper's ProbeResult API, tested against 60fc7aa67a8.
# Manual calibration follows Klipper's ProbeCommandHelper workflow.
# This file may be distributed under the terms of the GNU GPLv3 license.
import bisect
import collections
import logging
import math
from . import manual_probe, probe


PRUSA_POINTS = ((35., 0.), (40., .020), (45., .060),
                (50., .120), (55., .200), (60., .300))


class NaturalCubicSpline:
    """Natural cubic interpolation; callers choose the out-of-range policy."""
    def __init__(self, points):
        self.points = tuple(tuple(p) for p in points)
        if len(self.points) < 2:
            raise ValueError("points needs at least two temperature, shift pairs")
        if any(len(p) != 2 or not all(math.isfinite(v) for v in p)
               for p in self.points):
            raise ValueError("points must contain finite temperature, shift pairs")
        self.x = [p[0] for p in self.points]
        self.y = [p[1] for p in self.points]
        h = [b - a for a, b in zip(self.x, self.x[1:])]
        if any(v <= 0. for v in h):
            raise ValueError("point temperatures must be strictly increasing")
        n = len(self.x)
        diagonal, rhs = [1.] * n, [0.] * n
        for i in range(1, n - 1):
            diagonal[i] = 2. * (h[i - 1] + h[i])
            rhs[i] = 6. * ((self.y[i + 1] - self.y[i]) / h[i]
                          - (self.y[i] - self.y[i - 1]) / h[i - 1])
            if i > 1:
                factor = h[i - 1] / diagonal[i - 1]
                diagonal[i] -= factor * h[i - 1]
                rhs[i] -= factor * rhs[i - 1]
        self.second = [0.] * n
        for i in range(n - 2, 0, -1):
            self.second[i] = (rhs[i] - h[i] * self.second[i + 1]) / diagonal[i]

    def evaluate(self, temperature, interpolation='cubic'):
        t = min(max(temperature, self.x[0]), self.x[-1])
        i = min(max(bisect.bisect_right(self.x, t) - 1, 0), len(self.x) - 2)
        h = self.x[i + 1] - self.x[i]
        a, b = (self.x[i + 1] - t) / h, (t - self.x[i]) / h
        if interpolation == 'linear':
            return a * self.y[i] + b * self.y[i + 1]
        return (a * self.y[i] + b * self.y[i + 1]
                + ((a**3 - a) * self.second[i]
                   + (b**3 - b) * self.second[i + 1]) * h * h / 6.)


class PindaTemperature:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sensor_name = config.get('sensor', 'temperature_sensor probe_temp')
        self.enabled = config.getboolean('enabled', False)
        self.reference = config.getfloat('reference_temperature', 35.)
        self.scale = config.getfloat('scale', 1.)
        self.interpolation = config.getchoice('interpolation',
                                               ['cubic', 'linear'], 'cubic')
        self.max_adjustment = config.getfloat('max_adjustment', .5, above=0.)
        self.range_margin = config.getfloat('range_margin', 0., minval=0.)
        self.stability_time = config.getfloat('stability_time', 60., above=0.)
        self.stability_tolerance = config.getfloat('stability_tolerance', .3,
                                                   above=0.)
        self.stability_min_time = config.getfloat('stability_min_time', 180.,
                                                  minval=0.)
        self.warmup_timeout = config.getfloat('warmup_timeout', 900.,
                                              above=self.stability_time)
        self.out_of_range = config.getchoice('out_of_range',
                                             ['error', 'clamp'], 'error')
        points = config.getlists('points', default=PRUSA_POINTS,
                                  seps=(',', '\n'), parser=float, count=2)
        try:
            self.curve = NaturalCubicSpline(points)
        except ValueError as e:
            raise config.error(str(e))
        if not all(math.isfinite(v) for v in
                   (self.scale, self.reference, self.max_adjustment,
                    self.range_margin, self.stability_time,
                    self.stability_tolerance, self.stability_min_time,
                    self.warmup_timeout)):
            raise config.error('PINDA temperature settings must be finite')
        if self.warmup_timeout <= self.stability_min_time:
            raise config.error('warmup_timeout must exceed stability_min_time')
        if not self.curve.x[0] <= self.reference <= self.curve.x[-1]:
            raise config.error('reference_temperature must be inside points range')
        self.sensor = self.pprobe = None
        self.last = None
        self.calibration_probe = False
        self.bootstrap_homing = False
        self.stability_status = None
        self.calibration_context = None
        self.calibration_points = []
        self.printer.register_event_handler('klippy:connect', self._connect)
        self.printer.register_event_handler('probe:update_results',
                                            self._update_results)
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('PINDA_TEMP_STATUS', self.cmd_STATUS,
                                    desc='Show PINDA temperature and correction')
        self.gcode.register_command('PINDA_TEMP_SET', self.cmd_SET,
                                    desc='Enable/disable compensation before homing')
        self.gcode.register_command('PINDA_TEMP_CALIBRATE', self.cmd_CALIBRATE,
                                    desc='Measure a PINDA offset with manual contact')
        self.gcode.register_command('PINDA_TEMP_POINTS', self.cmd_POINTS,
                                    desc='Show recorded temperature calibration points')
        self.gcode.register_command('PINDA_TEMP_BOOTSTRAP_HOME',
                                    self.cmd_BOOTSTRAP_HOME,
                                    desc='Preliminary home before probe warmup')
        self.gcode.register_command('PINDA_TEMP_STABILIZE', self.cmd_STABILIZE,
                                    desc='Wait for settled, calibrated probe temperature')

    def _connect(self):
        self.sensor = self.printer.lookup_object(self.sensor_name)
        self.pprobe = self.printer.lookup_object('probe')
        # Older Klipper uses a different event shape and a separate homing path.
        # Fail at startup instead of silently leaving homing uncompensated.
        if (not hasattr(manual_probe, 'ProbeResult')
                or not hasattr(probe, 'HomingViaProbeHelper')
                or not hasattr(self.pprobe, 'probe_offsets')
                or not hasattr(self.sensor, 'get_temp')):
            raise self.printer.config_error(
                'pinda_temperature requires the new Klipper ProbeResult and '
                'HomingViaProbeHelper interfaces (tested on 60fc7aa67a8)')
        logging.info('PINDA compensation: sensor=%s enabled=%s reference=%.2f',
                     self.sensor_name, self.enabled, self.reference)

    def _temperature(self, eventtime):
        temperature = self.sensor.get_temp(eventtime)[0]
        if not math.isfinite(temperature) or not 0. < temperature < 100.:
            raise self.printer.command_error(
                'PINDA temperature unavailable or implausible: %s' % temperature)
        return temperature

    def _shift(self, temperature, strict=True):
        in_range = (self.curve.x[0] - self.range_margin <= temperature
                    <= self.curve.x[-1] + self.range_margin)
        if strict and not in_range and self.out_of_range == 'error':
            raise self.printer.command_error(
                'PINDA %.2fC is outside the calibrated %.2f..%.2fC range. '
                'Warm/cool the probe into range or calibrate additional points.'
                % (temperature, self.curve.x[0] - self.range_margin,
                   self.curve.x[-1] + self.range_margin))
        shift = self.scale * (
            self.curve.evaluate(temperature, self.interpolation)
            - self.curve.evaluate(self.reference, self.interpolation))
        if not math.isfinite(shift) or abs(shift) > self.max_adjustment:
            raise self.printer.command_error(
                'PINDA correction %.6fmm exceeds max_adjustment %.6fmm'
                % (shift, self.max_adjustment))
        return shift

    def _update_results(self, results):
        if not results:
            return
        eventtime = self.printer.get_reactor().monotonic()
        if not self.enabled and not self.calibration_probe:
            # Passive mode must preserve the printer's original probe behavior.
            self.last = {'temperature': self.sensor.get_temp(eventtime)[0],
                         'adjustment': 0., 'raw_test_z': results[-1].test_z}
            return
        temperature = self._temperature(eventtime)
        shift = (0. if self.calibration_probe or self.bootstrap_homing
                 else self._shift(temperature))
        for i, result in enumerate(results):
            # Trigger distance grows by +shift. Subtract it from estimated
            # contact height; G28 subtracts that height from the toolhead frame.
            # Leave the measured test coordinates untouched for calibration.
            results[i] = result._replace(bed_z=result.bed_z - shift)
        self.last = {'temperature': temperature, 'adjustment': shift,
                     'raw_test_z': results[-1].test_z}

    def get_status(self, eventtime):
        temperature = self.sensor.get_temp(eventtime)[0] if self.sensor else 0.
        within = (math.isfinite(temperature)
                  and self.curve.x[0] <= temperature <= self.curve.x[-1])
        return {'enabled': self.enabled, 'temperature': temperature,
                'reference_temperature': self.reference,
                'interpolation': self.interpolation,
                'bootstrap_homing': self.bootstrap_homing,
                'stability': self.stability_status,
                'range_margin': self.range_margin,
                'within_calibrated_range': within,
                'calibrated_range': [self.curve.x[0], self.curve.x[-1]],
                'last_measurement': self.last,
                'calibration_points': list(self.calibration_points)}

    def cmd_STATUS(self, gcmd):
        now = self.printer.get_reactor().monotonic()
        try:
            temperature = self._temperature(now)
        except self.printer.command_error as e:
            gcmd.respond_info('PINDA compensation: %s; %s'
                              % ('enabled' if self.enabled else 'disabled', e))
            return
        shift = self._shift(temperature, strict=False)
        inside = self.curve.x[0] <= temperature <= self.curve.x[-1]
        gcmd.respond_info(
            'PINDA compensation: %s; probe %.2fC; reference %.2fC; '
            'predicted trigger-distance shift %+.6fmm%s. '
            'Applied only to probe measurements; no move or G-code offset.'
            % ('enabled' if self.enabled else 'disabled', temperature,
               self.reference, shift, '' if inside else ' (CLAMPED PREVIEW)'))
        if self.last is not None:
            gcmd.respond_info('Last probe measurement: %s' % self.last)

    def cmd_SET(self, gcmd):
        enable = bool(gcmd.get_int('ENABLE', int(self.enabled), minval=0, maxval=1))
        if enable != self.enabled:
            toolhead = self.printer.lookup_object('toolhead')
            now = self.printer.get_reactor().monotonic()
            ps = self.printer.lookup_object('print_stats', None)
            if ps is not None and ps.get_status(now)['state'] in ('printing', 'paused'):
                raise gcmd.error('Cannot change PINDA compensation during a print')
            if self.calibration_context is not None:
                raise gcmd.error('Finish or ABORT the current manual calibration')
            if 'z' in toolhead.get_status(now)['homed_axes']:
                raise gcmd.error('Run M84 first, then enable/disable and rehome Z')
            if enable:
                # Validate before mutating state, including on a failed enable.
                self._shift(self._temperature(now), strict=False)
            mesh = self.printer.lookup_object('bed_mesh', None)
            if mesh is not None:
                mesh.set_mesh(None)
            self.enabled = enable
            gcmd.respond_info('PINDA setting is runtime-only. Rehome and generate '
                              'a fresh mesh. Edit enabled in pinda_temperature.cfg '
                              'after validation to persist it.')
        self.cmd_STATUS(gcmd)

    def cmd_CALIBRATE(self, gcmd):
        now = self.printer.get_reactor().monotonic()
        ps = self.printer.lookup_object('print_stats', None)
        if ps is not None and ps.get_status(now)['state'] in ('printing', 'paused'):
            raise gcmd.error('Cannot manually calibrate PINDA during a print')
        manual_probe.verify_no_manual_probe(self.printer)
        params = self.pprobe.get_probe_params(gcmd)
        # This one measurement intentionally bypasses the candidate curve so
        # temperatures outside its range can be calibrated without extrapolation.
        self.calibration_probe = True
        try:
            ppos = probe.run_single_probe(self.pprobe, gcmd)
        finally:
            self.calibration_probe = False
        temperature = self.last['temperature']
        toolhead = self.printer.lookup_object('toolhead')
        curpos = toolhead.get_position()
        curpos[2] += 5.
        toolhead.manual_move(curpos, params['lift_speed'])
        curpos[0], curpos[1] = ppos.bed_x, ppos.bed_y
        toolhead.manual_move(curpos, params['probe_speed'])
        self.calibration_context = (temperature, ppos.test_z)
        try:
            manual_probe.ManualProbeHelper(self.printer, gcmd,
                                           self._calibration_finalize)
        except Exception:
            self.calibration_context = None
            raise

    def cmd_BOOTSTRAP_HOME(self, gcmd):
        manual_probe.verify_no_manual_probe(self.printer)
        # This preliminary home is only for reaching the warmup position.
        # Normal G28 after stabilization establishes the compensated Z frame.
        self.bootstrap_homing = True
        try:
            self.gcode.run_script_from_command('G28')
        finally:
            self.bootstrap_homing = False

    def cmd_STABILIZE(self, gcmd):
        manual_probe.verify_no_manual_probe(self.printer)
        toolhead = self.printer.lookup_object('toolhead')
        reactor = self.printer.get_reactor()
        if 'z' not in toolhead.get_status(reactor.monotonic())['homed_axes']:
            raise gcmd.error('Home and park the probe near the bed first')
        toolhead.wait_moves()
        started = last_report = reactor.monotonic()
        last_keepalive = started
        history = collections.deque()
        self.stability_status = {'state': 'waiting'}
        try:
            while True:
                now = reactor.monotonic()
                if self.printer.is_shutdown():
                    raise gcmd.error('Printer shut down during PINDA warmup')
                if now - started >= self.warmup_timeout:
                    raise gcmd.error('PINDA did not reach a stable temperature '
                                     'inside its calibrated range before timeout')
                temperature = self._temperature(now)
                history.append((now, temperature))
                while len(history) > 1 and history[1][0] <= now-self.stability_time:
                    history.popleft()
                span = max(t for _, t in history) - min(t for _, t in history)
                heaters_ready = True
                for name, tolerance in [('heater_bed', 1.), ('extruder', 2.)]:
                    heater = self.printer.lookup_object(name, None)
                    if heater is None:
                        continue
                    heater_status = heater.get_status(now)
                    current = heater_status['temperature']
                    target = heater_status['target']
                    if target <= 0.:
                        raise gcmd.error('Keep bed and nozzle heaters enabled '
                                         'during PINDA stabilization')
                    heaters_ready &= abs(current - target) <= tolerance
                in_range = (self.curve.x[0]-self.range_margin <= temperature
                            <= self.curve.x[-1]+self.range_margin)
                self.stability_status = {
                    'state': 'waiting', 'elapsed': now-started,
                    'temperature_span': span, 'inside_range': in_range}
                if (now-started >= self.stability_min_time
                        and now-history[0][0] >= self.stability_time
                        and span <= self.stability_tolerance
                        and heaters_ready and in_range):
                    self._shift(temperature)
                    self.stability_status['state'] = 'ready'
                    gcmd.respond_info('PINDA settled at %.2fC; %.3fC span over '
                                      '%.0fs. Normal G28 can now establish Z.'
                                      % (temperature, span, self.stability_time))
                    return
                if now - last_report >= 30.:
                    gcmd.respond_info('PINDA warmup: %.2fC; recent span %.3fC; '
                                      'elapsed %.0fs' %
                                      (temperature, span, now-started))
                    last_report = now
                if now - last_keepalive >= 20.:
                    self.gcode.run_script_from_command('G4 P1')
                    last_keepalive = now
                reactor.pause(now + 1.)
        except Exception:
            self.stability_status['state'] = 'failed'
            raise

    def _calibration_finalize(self, result):
        temperature, trigger_z = self.calibration_context
        self.calibration_context = None
        if result is None:
            return
        z_offset = trigger_z - result.bed_z
        self.calibration_points.append((temperature, z_offset))
        self.gcode.respond_info(
            'PINDA measured %.3fC: geometric z_offset %.6fmm. '
            'Recorded in RAM; printer.cfg z_offset unchanged. '
            'Use PINDA_TEMP_POINTS to show the candidate curve.'
            % (temperature, z_offset))

    def cmd_POINTS(self, gcmd):
        if not self.calibration_points:
            gcmd.respond_info('No points recorded. Use PINDA_TEMP_CALIBRATE, '
                              'TESTZ and ACCEPT at each probe temperature.')
            return
        points = sorted(self.calibration_points)
        gcmd.respond_info('Measured probe temperature, geometric z_offset (mm):\n'
                          + '\n'.join('  %.3f, %.6f' % p for p in points))
        if len(points) < 2:
            return
        # Anchor at an actual measured point, never an invented cold value.
        anchor = min(points, key=lambda p: abs(p[0] - self.reference))
        gcmd.respond_info(
            'Candidate config (review measurements before copying):\n'
            'reference_temperature: %.3f\ninterpolation: linear\n'
            'scale: 1.0\npoints:\n%s\n'
            'Corresponding [probe] z_offset at this reference: %.6f\n'
            'Nothing has been saved automatically.'
            % (anchor[0], '\n'.join('  %.3f, %.6f' % (t, z - anchor[1])
                                   for t, z in points), anchor[1]))


def load_config(config):
    return PindaTemperature(config)

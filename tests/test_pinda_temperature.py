import importlib.util
import math
import os
import pathlib
import sys
import types
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
KLIPPER_DIR = pathlib.Path(os.environ.get('KLIPPER_DIR', '~/klipper')).expanduser()
if not (KLIPPER_DIR / 'klippy' / 'extras' / 'probe.py').is_file():
    raise RuntimeError('Set KLIPPER_DIR to the root of a Klipper checkout')
sys.path.insert(0, str(KLIPPER_DIR / 'klippy'))
from configfile import ConfigFileReader, ConfigWrapper
from extras import manual_probe, probe, homing

spec = importlib.util.spec_from_file_location(
    'extras.pinda_temperature', ROOT / 'pinda_temperature.py')
pinda = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pinda)


class FakeGcmd:
    def __init__(self, params=None, command='PROBE'):
        self.params = params or {}
        self.command = command
        self.messages = []
        self.error = RuntimeError

    def get(self, key, default=None):
        return self.params.get(key, default)

    def get_int(self, key, default=None, **kwargs):
        value = int(self.params.get(key, default))
        if value < kwargs.get('minval', -math.inf) or value > kwargs.get('maxval', math.inf):
            raise RuntimeError('integer outside allowed range')
        return value

    def get_float(self, key, default=None, **kwargs):
        return float(self.params.get(key, default))

    def get_command(self):
        return self.command

    def respond_info(self, message):
        self.messages.append(message)


class FakeGcode(FakeGcmd):
    def __init__(self):
        super().__init__()
        self.commands = {}

    def register_command(self, name, callback, **kwargs):
        self.commands[name] = callback

    def create_gcode_command(self, command, text, params):
        return FakeGcmd(params, command)


class FakeToolhead:
    def __init__(self):
        self.position = [132., 139., 5., 0.]
        self.homed_axes = 'xy'
        self.moves = []

    def get_position(self):
        return list(self.position)

    def get_status(self, now):
        return {'homed_axes': self.homed_axes}

    def set_position(self, pos):
        self.position = list(pos)

    def manual_move(self, pos, speed):
        self.moves.append((list(pos), speed))
        self.position = [old if new is None else new
                         for old, new in zip(self.position, pos)]


class FakePrinter:
    command_error = config_error = RuntimeError

    def __init__(self):
        self.handlers = {}
        self.temp = 35.
        self.toolhead = FakeToolhead()
        self.objects = {
            'gcode': FakeGcode(), 'toolhead': self.toolhead,
            'temperature_sensor probe_temp': types.SimpleNamespace(
                get_temp=lambda now: (self.temp, 0.)),
            'probe': types.SimpleNamespace(probe_offsets=object()),
            'bed_mesh': mock.Mock(),
        }

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, name, cb):
        self.handlers.setdefault(name, []).append(cb)

    def send_event(self, name, *args):
        for callback in self.handlers.get(name, []):
            callback(*args)

    def get_reactor(self):
        return types.SimpleNamespace(monotonic=lambda: 100.)


class FakeConfig:
    error = RuntimeError

    def __init__(self, printer, **settings):
        self.printer = printer
        self.settings = settings

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.settings.get(name, default)

    def getfloat(self, name, default=None, **kwargs):
        return float(self.get(name, default))

    def getboolean(self, name, default=None):
        return bool(self.get(name, default))

    def getchoice(self, name, choices, default=None):
        return self.get(name, default)

    def getlists(self, name, default=None, **kwargs):
        return self.get(name, default)


class PindaTests(unittest.TestCase):
    def make_module(self, **settings):
        printer = FakePrinter()
        module = pinda.PindaTemperature(FakeConfig(printer, **settings))
        printer.send_event('klippy:connect')
        return printer, module

    def result(self, test_z=2.2, offset=2.2):
        return manual_probe.create_probe_result([132., 139., test_z], [-22., -22., offset])

    def test_factory_knots(self):
        spline = pinda.NaturalCubicSpline(pinda.PRUSA_POINTS)
        for temperature, value in pinda.PRUSA_POINTS:
            self.assertAlmostEqual(spline.evaluate(temperature), value)

    def test_spline_is_smooth_at_knots(self):
        s = pinda.NaturalCubicSpline(pinda.PRUSA_POINTS)
        step = .0001
        for t, _ in pinda.PRUSA_POINTS[1:-1]:
            left = (s.evaluate(t) - s.evaluate(t - step)) / step
            right = (s.evaluate(t + step) - s.evaluate(t)) / step
            self.assertAlmostEqual(left, right, places=5)
        self.assertEqual(s.second[0], 0.)
        self.assertEqual(s.second[-1], 0.)

    def test_two_points_are_linear(self):
        s = pinda.NaturalCubicSpline([(20., -.1), (40., .1)])
        self.assertAlmostEqual(s.evaluate(30.), 0.)

    def test_linear_interpolation_stays_between_sparse_measured_points(self):
        s = pinda.NaturalCubicSpline([(26., -.8), (28., -.4), (35., 0.), (45., .1)])
        for left, right in zip(s.points, s.points[1:]):
            for step in range(21):
                t = left[0] + (right[0] - left[0]) * step / 20.
                value = s.evaluate(t, 'linear')
                self.assertGreaterEqual(value, left[1] - 1.e-12)
                self.assertLessEqual(value, right[1] + 1.e-12)

    def test_measured_linear_curve_uses_configured_reference(self):
        printer, module = self.make_module(enabled=True, interpolation='linear',
            points=[(28., -.4), (35., 0.), (45., .1)])
        printer.temp = 30.
        shift = -.4 + .4 * 2. / 7.
        results = [self.result(2.2 + shift)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, 0.)

    def test_bad_tables_rejected(self):
        for points in [[], [(1., 0.)], [(35., 0.), (35., 1.)],
                       [(40., 0.), (35., 1.)], [(35., 0.), (40., math.nan)]]:
            with self.subTest(points=points), self.assertRaises(ValueError):
                pinda.NaturalCubicSpline(points)

    def test_reference_must_be_inside_table(self):
        with self.assertRaisesRegex(RuntimeError, 'reference_temperature'):
            self.make_module(reference_temperature=20.)

    def test_passive_is_unchanged_even_cold(self):
        printer, module = self.make_module()
        printer.temp = 27.
        original = self.result(2.1)
        results = [original]
        printer.send_event('probe:update_results', results)
        self.assertIs(results[0], original)
        self.assertEqual(module.last['adjustment'], 0.)
        self.assertEqual(printer.toolhead.moves, [])

    def test_hot_result_subtracts_trigger_distance_growth(self):
        printer, module = self.make_module(enabled=True)
        printer.temp = 60.
        results = [self.result(2.5)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, 0.)
        self.assertEqual(results[0].test_z, 2.5)
        self.assertEqual((results[0].bed_x, results[0].bed_y), (110., 117.))

    def test_custom_cold_point_corrects_opposite_direction(self):
        printer, module = self.make_module(enabled=True,
            points=[(20., -.1), (35., 0.), (60., .3)])
        printer.temp = 20.
        results = [self.result(2.1)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, 0.)

    def test_arbitrary_reference_is_subtracted(self):
        printer, module = self.make_module(enabled=True, reference_temperature=45.)
        printer.temp = 60.
        results = [self.result(2.44)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, 0.)

    def test_out_of_range_errors_without_mutating_result(self):
        printer, module = self.make_module(enabled=True)
        for t in (25., 70.):
            printer.temp = t
            original = self.result()
            results = [original]
            with self.assertRaisesRegex(RuntimeError, 'outside the calibrated'):
                printer.send_event('probe:update_results', results)
            self.assertIs(results[0], original)

    def test_explicit_clamp_does_not_extrapolate(self):
        printer, module = self.make_module(enabled=True, out_of_range='clamp')
        printer.temp = 90.
        results = [self.result(2.5)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, 0.)

    def test_excessive_and_bad_sensor_values_rejected(self):
        printer, module = self.make_module(enabled=True, max_adjustment=.1)
        printer.temp = 60.
        with self.assertRaisesRegex(RuntimeError, 'max_adjustment'):
            printer.send_event('probe:update_results', [self.result()])
        for t in (0., -20., 101., math.nan, math.inf):
            printer.temp = t
            with self.assertRaisesRegex(RuntimeError, 'implausible'):
                printer.send_event('probe:update_results', [self.result()])

    def test_real_klipper_averaging_handles_warming_probe(self):
        printer, module = self.make_module(enabled=True)
        printer.toolhead.homed_axes = 'xyz'
        class HardwareSession:
            def __init__(self):
                self.samples = iter([(45., 2.26), (50., 2.32)])
            def run_probe(self, gcmd):
                printer.temp, z = next(self.samples)
                self.result = self_outer.result(z)
            def pull_probed_results(self):
                return [self.result]
            def end_probe_session(self):
                pass
        self_outer = self
        hw = HardwareSession()
        params = {'samples': 2, 'samples_tolerance': .01,
                  'samples_tolerance_retries': 0, 'sample_retract_dist': 2.,
                  'lift_speed': 5., 'samples_result': 'average'}
        helper = probe.SampleAveragingHelper(
            FakeConfig(printer), types.SimpleNamespace(get_probe_params=lambda cmd: params),
            lambda cmd: hw)
        gcmd = FakeGcmd()
        helper.start_probe_session(gcmd)
        helper.run_probe(gcmd)
        result = helper.pull_probed_results()[0]
        self.assertAlmostEqual(result.bed_z, 0.)
        helper.end_probe_session()

    def test_real_klipper_g28_uses_corrected_result(self):
        printer, module = self.make_module(enabled=True)
        printer.temp = 60.
        printer.toolhead.position[2] = 2.5
        class Session:
            def run_probe(self, gcmd):
                self.results = [self_outer.result(2.5)]
                printer.send_event('probe:update_results', self.results)
            def pull_probed_results(self):
                return self.results
        self_outer = self
        state = homing.Homing(printer)
        state._probing_home(Session(), 1, 5.)
        self.assertAlmostEqual(printer.toolhead.position[2], 2.5)

    def test_mesh_retains_actual_bed_shape(self):
        printer, module = self.make_module(enabled=True)
        printer.temp = 45.
        results = [self.result(2.33)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, .07)

    def test_other_probe_result_adjustments_are_preserved(self):
        printer, module = self.make_module(enabled=True)
        printer.temp = 45.
        results = [self.result(2.26)._replace(bed_z=.09)]
        printer.send_event('probe:update_results', results)
        self.assertAlmostEqual(results[0].bed_z, .03)

    def test_enable_requires_unhomed_z_and_discards_mesh(self):
        printer, module = self.make_module()
        printer.toolhead.homed_axes = 'xyz'
        with self.assertRaisesRegex(RuntimeError, 'M84'):
            module.cmd_SET(FakeGcmd({'ENABLE': '1'}))
        self.assertFalse(module.enabled)
        printer.toolhead.homed_axes = ''
        module.cmd_SET(FakeGcmd({'ENABLE': '1'}))
        self.assertTrue(module.enabled)
        printer.objects['bed_mesh'].set_mesh.assert_called_once_with(None)
        self.assertEqual(printer.toolhead.moves, [])

    def test_enable_blocked_while_printing_or_paused(self):
        printer, module = self.make_module()
        for state in ('printing', 'paused'):
            printer.objects['print_stats'] = types.SimpleNamespace(
                get_status=lambda now, state=state: {'state': state})
            with self.assertRaisesRegex(RuntimeError, 'during a print'):
                module.cmd_SET(FakeGcmd({'ENABLE': '1'}))
            self.assertFalse(module.enabled)

    def test_calibration_bypasses_curve_and_restores_mode(self):
        printer, module = self.make_module(enabled=True)
        printer.temp = 25.
        printer.objects['probe'].get_probe_params = lambda cmd: {
            'lift_speed': 5., 'probe_speed': 5.}
        def sample(pprobe, gcmd):
            results = [self.result(2.1)]
            printer.send_event('probe:update_results', results)
            return results[0]
        with mock.patch.object(probe, 'run_single_probe', side_effect=sample), \
                mock.patch.object(manual_probe, 'verify_no_manual_probe'), \
                mock.patch.object(manual_probe, 'ManualProbeHelper'):
            module.cmd_CALIBRATE(FakeGcmd())
        self.assertFalse(module.calibration_probe)
        module._calibration_finalize(manual_probe.create_probe_result([110.,117.,.1]))
        self.assertEqual(module.calibration_points, [(25., 2.)])
        self.assertIsNone(module.calibration_context)

    def test_calibration_does_not_write_config_and_abort_records_nothing(self):
        printer, module = self.make_module()
        module.calibration_context = (35., 2.2)
        module._calibration_finalize(None)
        self.assertEqual(module.calibration_points, [])
        module.calibration_context = (35., 2.2)
        module._calibration_finalize(manual_probe.create_probe_result([110.,117.,0.]))
        module.calibration_context = (45., 2.26)
        module._calibration_finalize(manual_probe.create_probe_result([110.,117.,0.]))
        gcmd = FakeGcmd()
        module.cmd_POINTS(gcmd)
        text = '\n'.join(gcmd.messages)
        self.assertIn('35.000, 0.000000', text)
        self.assertIn('45.000, 0.060000', text)
        self.assertIn('Nothing has been saved', text)

    def test_legacy_probe_interface_rejected(self):
        printer = FakePrinter()
        printer.objects['probe'] = object()
        module = pinda.PindaTemperature(FakeConfig(printer))
        with self.assertRaisesRegex(RuntimeError, 'interfaces'):
            module._connect()

    def test_bootstrap_bypasses_only_its_preliminary_home(self):
        printer, module = self.make_module(enabled=True)
        printer.temp = 25.
        def run(script):
            self.assertEqual(script, 'G28')
            self.assertTrue(module.bootstrap_homing)
            results = [self.result(2.)]
            printer.send_event('probe:update_results', results)
            self.assertAlmostEqual(results[0].bed_z, -.2)
        printer.objects['gcode'].run_script_from_command = run
        with mock.patch.object(manual_probe, 'verify_no_manual_probe'):
            module.cmd_BOOTSTRAP_HOME(FakeGcmd())
        self.assertFalse(module.bootstrap_homing)
        with self.assertRaisesRegex(RuntimeError, 'outside the calibrated'):
            printer.send_event('probe:update_results', [self.result()])

    def test_bootstrap_restores_normal_correction_after_failure(self):
        printer, module = self.make_module(enabled=True)
        printer.objects['gcode'].run_script_from_command = mock.Mock(
            side_effect=RuntimeError('homing failed'))
        with mock.patch.object(manual_probe, 'verify_no_manual_probe'), \
                self.assertRaisesRegex(RuntimeError, 'homing failed'):
            module.cmd_BOOTSTRAP_HOME(FakeGcmd())
        self.assertFalse(module.bootstrap_homing)

    def test_small_range_margin_clamps_but_does_not_allow_unbounded_values(self):
        printer, module = self.make_module(enabled=True, range_margin=.5)
        self.assertAlmostEqual(module._shift(34.7), 0.)
        self.assertAlmostEqual(module._shift(60.3), .3)
        with self.assertRaisesRegex(RuntimeError, 'outside the calibrated'):
            module._shift(34.4)

    def stabilization_fixture(self, temperature):
        printer, module = self.make_module(enabled=True, stability_time=5.,
            stability_min_time=6., stability_tolerance=.1, warmup_timeout=12.)
        class Reactor:
            now = 0.
            def monotonic(self): return self.now
            def pause(self, until): self.now = until
        reactor = Reactor()
        printer.get_reactor = lambda: reactor
        printer.is_shutdown = lambda: False
        printer.toolhead.homed_axes = 'xyz'
        printer.toolhead.wait_moves = lambda: None
        module.sensor = types.SimpleNamespace(get_temp=lambda now: (temperature(now), 0.))
        printer.objects['heater_bed'] = types.SimpleNamespace(
            get_status=lambda now: {'temperature':60.,'target':60.})
        printer.objects['extruder'] = types.SimpleNamespace(
            get_status=lambda now: {'temperature':140.,'target':140.})
        printer.objects['gcode'].run_script_from_command = mock.Mock()
        return printer, module, reactor

    def test_stabilization_requires_full_window_and_minimum_soak(self):
        printer, module, reactor = self.stabilization_fixture(lambda now: 35.)
        with mock.patch.object(manual_probe, 'verify_no_manual_probe'):
            module.cmd_STABILIZE(FakeGcmd())
        self.assertGreaterEqual(reactor.now, 6.)
        self.assertEqual(module.stability_status['state'], 'ready')

    def test_stabilization_rejects_drift_and_out_of_range_temperatures(self):
        for fn in (lambda now: 35.+.1*now, lambda now: 25.):
            printer, module, reactor = self.stabilization_fixture(fn)
            with mock.patch.object(manual_probe, 'verify_no_manual_probe'), \
                    self.assertRaisesRegex(RuntimeError, 'before timeout'):
                module.cmd_STABILIZE(FakeGcmd())
            self.assertEqual(module.stability_status['state'], 'failed')

    def test_stabilization_stops_when_heaters_are_off(self):
        printer, module, reactor = self.stabilization_fixture(lambda now: 35.)
        printer.objects['heater_bed'].get_status = lambda now: {'temperature':60.,'target':0.}
        with mock.patch.object(manual_probe, 'verify_no_manual_probe'), \
                self.assertRaisesRegex(RuntimeError, 'heaters enabled'):
            module.cmd_STABILIZE(FakeGcmd())

    def test_stabilization_keeps_idle_timer_active_during_long_wait(self):
        printer, module, reactor = self.stabilization_fixture(lambda now: 35.)
        module.stability_min_time = 25.
        module.warmup_timeout = 40.
        with mock.patch.object(manual_probe, 'verify_no_manual_probe'):
            module.cmd_STABILIZE(FakeGcmd())
        printer.objects['gcode'].run_script_from_command.assert_any_call('G4 P1')
        self.assertEqual(printer.toolhead.moves, [])

    def test_measured_printer_profile_matches_manual_anchors(self):
        path = ROOT / 'examples' / 'pinda_temperature.cfg'
        fileconfig = ConfigFileReader().build_fileconfig(path.read_text(), str(path))
        printer = FakePrinter()
        config = ConfigWrapper(printer, fileconfig, {}, 'pinda_temperature')
        module = pinda.load_config(config)
        printer.send_event('klippy:connect')
        self.assertFalse(module.enabled)
        module.enabled = True
        reference_z = 2.300
        for temperature, shift in module.curve.points:
            printer.temp = temperature
            results = [self.result(reference_z+shift, reference_z)]
            printer.send_event('probe:update_results', results)
            self.assertAlmostEqual(results[0].bed_z, 0., places=8)
        with self.assertRaisesRegex(RuntimeError, 'outside the calibrated'):
            module._shift(28.7)

    def test_real_klipper_heater_bed_and_extruder_interfaces(self):
        # Run in the printer's klippy-env to exercise the actual wrappers.
        try:
            from kinematics.extruder import PrinterExtruder
        except ModuleNotFoundError as e:
            if e.name == 'cffi':
                self.skipTest('Requires cffi from Klipper’s Python dependencies')
            raise
        from extras.heaters import Heater
        from extras.heater_bed import PrinterHeaterBed
        printer, module, reactor = self.stabilization_fixture(lambda now: 35.)
        def real_heater(target):
            h = Heater.__new__(Heater)
            h.lock = threading.Lock()
            h.target_temp = h.smoothed_temp = target
            h.last_pwm_value = 0.
            h.can_extrude = True
            return h
        bed_heater = real_heater(60.)
        printer.load_object = lambda config, name: types.SimpleNamespace(
            setup_heater=lambda config, gcode_id: bed_heater)
        bed = PrinterHeaterBed(FakeConfig(printer))
        extruder = PrinterExtruder.__new__(PrinterExtruder)
        extruder.heater = real_heater(140.)
        extruder.extruder_stepper = None
        self.assertFalse(hasattr(bed, 'get_temp'))
        self.assertFalse(hasattr(extruder, 'get_temp'))
        printer.objects['heater_bed'] = bed
        printer.objects['extruder'] = extruder
        with mock.patch.object(manual_probe, 'verify_no_manual_probe'):
            module.cmd_STABILIZE(FakeGcmd())
        self.assertEqual(module.stability_status['state'], 'ready')


if __name__ == '__main__':
    unittest.main(verbosity=2)

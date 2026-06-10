"""
calibration.py -- MIDILAB V2
Auto-calibration for all ADC channels and joystick centre position.
Samples each channel at rest, computes noise floor, updates deadbands.
Results saved to calibration.json and loaded on every boot.
"""

import ujson
import time
from machine import Pin, ADC

CALIBRATION_FILE = 'calibration.json'
SAMPLE_COUNT     = 64    # samples per channel during calibration
SIGMA_MULTIPLIER = 4     # deadband = 4 * std_dev of noise floor


class Calibration:
    """
    Stores per-channel calibration data (noise floor, resting value).
    Loaded at boot. Applied by adc_scan_task and CCEngine.
    """

    def __init__(self):
        self._data = {}   # channel key to {mean, std_dev, deadband}
        self._joy_centre_x = 2048  # 12-bit ADC centre
        self._joy_centre_y = 2048
        self.load()

    def load(self):
        try:
            with open(CALIBRATION_FILE, 'r') as f:
                saved = ujson.load(f)
            self._data         = saved.get('channels', {})
            self._joy_centre_x = saved.get('joy_centre_x', 2048)
            self._joy_centre_y = saved.get('joy_centre_y', 2048)
            print('[Cal] Calibration loaded.')
        except Exception:
            print('[Cal] No calibration file -- using defaults.')

    def _save(self):
        try:
            with open(CALIBRATION_FILE, 'w') as f:
                ujson.dump({
                    'channels':     self._data,
                    'joy_centre_x': self._joy_centre_x,
                    'joy_centre_y': self._joy_centre_y,
                }, f)
        except Exception as e:
            print('[Cal] Save failed:', e)

    def get_deadband(self, source_key, default=32):
        """Return calibrated deadband for a source, or default if not calibrated."""
        ch_data = self._data.get(source_key)
        if ch_data:
            return ch_data.get('deadband', default)
        return default

    def get_joy_centre(self):
        return self._joy_centre_x, self._joy_centre_y

    def run(self, mux, display=None):
        """
        Run full calibration routine. Call with mux idle (no playing).
        Takes approximately 2-3 seconds.
        Reads all active channels SAMPLE_COUNT times and computes statistics.
        """
        from config import SOURCE_TO_CH, MUX_SETTLE_US

        if display:
            display.update('Calibrating...', 'Do not touch')

        print('[Cal] Starting calibration...')
        results = {}

        sources_to_cal = list(SOURCE_TO_CH.keys())

        for source_key in sources_to_cal:
            ch  = SOURCE_TO_CH[source_key]
            samples = []

            for _ in range(SAMPLE_COUNT):
                raw = mux.read_raw(ch)
                samples.append(raw)
                time.sleep_us(500)  # settle between samples

            mean    = sum(samples) / len(samples)
            sq_difs = [(s - mean) ** 2 for s in samples]  # list comprehension
            std_dev = (sum(sq_difs) / len(sq_difs)) ** 0.5

            deadband = max(8, int(std_dev * SIGMA_MULTIPLIER))

            results[source_key] = {
                'mean':     int(mean),
                'std_dev':  round(std_dev, 2),
                'deadband': deadband,
            }
            print(f'[Cal] {source_key}: mean={int(mean)} std={std_dev:.1f} db={deadband}')

        # Joystick centre calibration
        jx_ch = SOURCE_TO_CH.get('joy_x')
        jy_ch = SOURCE_TO_CH.get('joy_y')
        if jx_ch is not None and jy_ch is not None:
            jx_samples = []
            jy_samples = []
            for _ in range(SAMPLE_COUNT):
                jx_samples.append(mux.read_raw(jx_ch))
                jy_samples.append(mux.read_raw(jy_ch))
                time.sleep_us(500)
            self._joy_centre_x = int(sum(jx_samples) / len(jx_samples))
            self._joy_centre_y = int(sum(jy_samples) / len(jy_samples))
            print(f'[Cal] Joystick centre: X={self._joy_centre_x} Y={self._joy_centre_y}')

        self._data = results
        self._save()

        if display:
            display.update('Cal complete', 'OK')

        print('[Cal] Calibration complete.')
        return results

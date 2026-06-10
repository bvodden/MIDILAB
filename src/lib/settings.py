"""
settings.py -- MIDILAB V2
Non-volatile configuration. All settings with defaults in one place.
New in V2: arp, strum, multi-channel, presets, derived modes, calibration.
"""

import ujson

SETTINGS_FILE = 'settings.json'

DEFAULTS = {
    # Keypad
    'scale':           'major',
    'root_note':       60,
    'base_octave':     0,
    'keypad_mode':     'scale',

    # Velocity and expression
    'velocity_source':   'fixed',   # 'fixed' or 'breath' -- sonic removed as velocity source
    'default_velocity':  90,
    'expression_source': 'breath',

    # HC-SR04 -- sonic mode is now 'cc' or 'theremin' only (velocity removed)
    'sonic_min_cm':     3,
    'sonic_max_cm':     30,         # realistic indoor range for HC-SR04
    'sonic_mode':       'cc',       # 'cc' or 'theremin'
    'sonic_default_cc': 64,         # value sent when hand is out of range
    'theremin_scale':   'chromatic',
    'theremin_range':   24,
    'theremin_hold_ms': 80,
    'theremin_channel': 0,

    # Joystick
    'joy_deadzone_pct': 8,
    'pb_deadzone':      80,
    'pb_return_ms':     0,      # 0 = disabled; spring-centred joystick returns naturally

    # ADC
    'ema_alpha_pots':   0.20,
    'ema_alpha_sonic':  0.30,
    'fsr_threshold':    12,
    'adc_deadband':     32,

    # CC assignments
    'cc_assignments': {
        'pot_0':   74,  'pot_1':  71, 'pot_2': 73,
        'pot_3':   84,  'pot_4':   7,
        'pedal_0': 11,  'pedal_1': 64,
        'joy_x':    1,  'joy_y':  'pb',
        'fsr_0':   16,  'fsr_1':  17,
        'breath':  11,  'sonic':  11,
    },

    # MIDI
    'midi_channel':  0,
    'current_patch': 0,
    'current_bank':  0,
    'din_mirror':    True,

    # Multi-channel split (V2)
    'split_channel_enabled': False,
    'split_channel_bass':    0,
    'split_channel_upper':   1,

    # Arpeggiator (V2)
    'arp_enabled':   False,
    'arp_pattern':   'up',
    'arp_division':  '1/16',
    'arp_quality':   'standard',
    'arp_gate':      0.5,
    'arp_bpm':       120,
    'clock_source':  'internal',

    # Strum (V2)
    'strum_enabled':    False,
    'strum_delay_ms':   20,
    'strum_direction':  'low_high',

    # UI
    'led_brightness': 153,
    'wildcard_chord': 'aug',
}


class Settings:
    def __init__(self):
        self._data = {}

    def load(self):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                loaded = ujson.load(f)
            self._data = dict(DEFAULTS)
            self._data.update(loaded)
            if 'cc_assignments' in loaded:
                merged = dict(DEFAULTS['cc_assignments'])
                merged.update(loaded['cc_assignments'])
                self._data['cc_assignments'] = merged
        except Exception:
            print('[Settings] No valid settings -- writing defaults.')
            self._data = dict(DEFAULTS)
            self._data['cc_assignments'] = dict(DEFAULTS['cc_assignments'])
            self.save()

    def save(self):
        try:
            with open(SETTINGS_FILE, 'w') as f:
                ujson.dump(self._data, f)
        except Exception as e:
            print('[Settings] Save failed:', e)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def set_cc(self, source_key, cc_num):
        self._data['cc_assignments'][source_key] = cc_num
        self.save()

    def get_cc(self, source_key):
        return self._data['cc_assignments'].get(source_key)

    def reset(self):
        self._data = dict(DEFAULTS)
        self._data['cc_assignments'] = dict(DEFAULTS['cc_assignments'])
        self.save()

    def snapshot(self):
        """Deep copy via JSON round-trip -- used for preset save."""
        return ujson.loads(ujson.dumps(self._data))

    def restore(self, snapshot):
        """Restore from a preset snapshot dict."""
        self._data = dict(DEFAULTS)
        self._data.update(snapshot)
        if 'cc_assignments' in snapshot:
            merged = dict(DEFAULTS['cc_assignments'])
            merged.update(snapshot['cc_assignments'])
            self._data['cc_assignments'] = merged
        self.save()

    def __repr__(self):
        return '<Settings {}>'.format(self._data)

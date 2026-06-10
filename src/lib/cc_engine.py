"""
cc_engine.py -- MIDILAB
Handles all continuous controller sources:
  - 12 MUX analog channels (pots, pedals, joystick, FSRs, breath)
  - Joystick button (digital, GP19)
  - HC-SR04 ultrasonic (velocity, theremin, or CC)

Signal chain per channel:
  Raw 12-bit ADC, EMA smoothing, deadband filter, source-specific mapping, MIDI CC or PB
"""

import asyncio
import math
import time
from machine import Pin, ADC

from config import (
    MUX_S0, MUX_S1, MUX_S2, MUX_S3, MUX_EN, MUX_ADC_PIN, MUX_SETTLE_US,
    SOURCE_TO_CH, HCSR04_TRIG, HCSR04_ECHO, HCSR04_TIMEOUT_US, HCSR04_INTERVAL_MS,
    JOY_BTN, BREATH_RAW_MIN, BREATH_RAW_MAX,
)


# MUX ADC

class MuxADC:
    """CD74HC4067 16-channel MUX feeding the Pico built-in 12-bit ADC."""

    def __init__(self):
        # List comprehension: builds Pin list from a tuple of GPIO numbers in one line
        self._sel = [Pin(p, Pin.OUT) for p in (MUX_S0, MUX_S1, MUX_S2, MUX_S3)]
        self._en  = Pin(MUX_EN, Pin.OUT, value=1)
        self._adc = ADC(Pin(MUX_ADC_PIN))
        # Pre-allocated array of floats (C 32-bit float, not Python float objects)
        # indexed directly by channel number -- much faster than a dict lookup
        from array import array
        self._ema      = array('f', [0.0] * 16)   # 'f' = C float, 4 bytes each
        self._ema_init = bytearray(16)             # 0=not yet seeded, 1=seeded

    def _select(self, ch):
        # enumerate() gives (index, value) pairs -- avoids needing a separate counter variable
        for i, pin in enumerate(self._sel):
            pin.value((ch >> i) & 1)  # shift channel bits into select lines; & 1 isolates each bit

    def read_raw(self, ch):
        """
        Select channel, wait for signal to settle, sample ADC.
        Returns 12-bit value (0 to 4095).
        MUX is disabled between reads to reduce crosstalk between high-impedance sources.
        """
        self._en.value(0)                    # enable MUX (active LOW)
        self._select(ch)
        time.sleep_us(MUX_SETTLE_US)         # actual microsecond delay -- not a busy-wait loop
        val = self._adc.read_u16() >> 4      # read_u16 returns 0-65535; >> 4 scales to 12-bit
        self._en.value(1)                    # disable MUX
        return val

    def smooth(self, ch, raw, alpha):
        """
        Exponential moving average using pre-allocated C float array.
        First call for a channel seeds the filter with the raw value
        so there is no cold-start ramp from zero.
        array('f') avoids creating Python float objects on the heap.
        """
        if not self._ema_init[ch]:
            self._ema[ch]      = float(raw)  # seed on first read
            self._ema_init[ch] = 1
        s             = alpha * raw + (1.0 - alpha) * self._ema[ch]
        self._ema[ch] = s
        return int(s)

    @staticmethod
    def to_cc(raw_12bit):
        # @staticmethod: belongs to the class but doesn't need 'self' -- a pure utility function
        # Could be called as MuxADC.to_cc(val) or mux.to_cc(val)
        return raw_12bit >> 5  # bit shift right 5 = floor divide by 32; maps 0-4095 to 0-127


# Signal processing helpers (module-level functions, not methods -- no self needed)

def joy_x_to_mod(cc_val):
    """
    Mod wheel mapping from joystick X axis.
    Axis is inverted: raw high = stick left, raw low = stick right.
    After inversion: centre=64 gives 0, full right gives 127.
    Unipolar: left half (stick left of centre) also gives 0.
    """
    inverted = 127 - cc_val   # invert so right-of-stick = high value
    if inverted <= 64:
        return 0
    return min(127, (inverted - 64) * 2)


def fsr_to_cc(raw_12bit, threshold_cc):
    """
    FSR mapping with threshold gate and square root sensitivity curve.
    Input range is normalised from (threshold to 127) to (0.0 to 1.0)
    before the curve is applied, so output always starts at 0 when
    barely touching regardless of threshold setting.
    Returns None below threshold.
    """
    cc = raw_12bit >> 5
    if cc < threshold_cc:
        return None
    span = 127 - threshold_cc
    if span <= 0:
        return 127
    normalised = (cc - threshold_cc) / span   # 0.0 at threshold, 1.0 at max
    curved     = int(normalised ** 0.5 * 127) # sqrt expands upper range
    return max(0, min(127, curved))


def breath_to_cc(raw_12bit):
    """
    MPXV4006DP breath sensor floor correction.
    Removes the 0.2V resting output (BREATH_RAW_MIN) before scaling.
    Returns 7-bit CC value.
    """
    raw  = max(BREATH_RAW_MIN, min(BREATH_RAW_MAX, raw_12bit))  # clamp to known sensor range
    span = BREATH_RAW_MAX - BREATH_RAW_MIN
    return int((raw - BREATH_RAW_MIN) * 127 / span)


def distance_to_cc(dist_cm, min_cm, max_cm):
    """
    Logarithmic distance to CC mapping (close = high value).
    Log curve gives more sensitivity at close range -- mimics a theremin antenna.
    Returns None if out of range.
    """
    if dist_cm is None or dist_cm > max_cm:
        return None
    dist_cm  = max(min_cm, dist_cm)
    log_ratio = math.log(dist_cm / min_cm) / math.log(max_cm / min_cm)
    return int((1.0 - log_ratio) * 127)


# Joystick

class Joystick:
    """
    Manages X/Y axis readings with dead zone and button-press locking.
    Button (GP19) is active LOW -- pressed when pin reads LOW.
    Y axis (pitch bend) is never affected by the lock.
    """

    def __init__(self, dead_zone_pct=8):
        self._dead     = int(127 * dead_zone_pct / 100)
        self._locked   = False
        self._lock_x   = 64    # 7-bit centre value
        self._lock_y   = 2048  # 12-bit centre for pitch bend
        self._btn      = Pin(JOY_BTN, Pin.IN, Pin.PULL_UP)
        self._prev_btn = False

    @property
    def button_pressed(self):
        return not self._btn.value()  # active LOW: pin reads 0 when pressed, so invert

    def process_x(self, raw_cc):
        """Returns locked or live X axis value (0-127)."""
        if self._locked:
            return self._lock_x
        return raw_cc

    def process_y(self, raw_12bit):
        """Returns raw Y value unchanged -- pitch bend is never locked."""
        return raw_12bit

    def update_button(self):
        """Detect button press edge and engage lock. Call once per scan cycle."""
        btn = self.button_pressed
        if btn and not self._prev_btn:
            self._locked = True  # lock engages on press; unlock is handled by check_unlock
        self._prev_btn = btn

    def check_unlock(self, raw_x_cc, raw_y_12bit):
        """
        Auto-unlock when stick returns near centre.
        Call each scan cycle with the current raw values.
        """
        if self._locked:
            cx = abs(raw_x_cc - 64)
            cy = abs(raw_y_12bit - 2048) >> 5   # scale Y to same units as X for comparison
            if cx < self._dead and cy < self._dead:
                self._locked = False
                self._lock_x = 64
                self._lock_y = 2048
        else:
            self._lock_x = raw_x_cc   # always track last position so lock captures current value
            self._lock_y = raw_y_12bit


# CC Engine

class CCEngine:
    """
    Routes all analog and ultrasonic inputs to MIDI CC or pitch bend messages.
    Manages velocity source, expression source, and pitch bend auto-zero.
    """

    def __init__(self, ports, settings, note_engine, led=None):
        self._ports   = ports
        self._s       = settings
        self._ne      = note_engine
        self._led     = led
        self._last_cc = {}  # cc_num to last sent value; used to suppress redundant messages

        self._pb_value   = 8192  # current pitch bend (8192 = centre, no deviation)
        self._pb_last_ms = 0     # timestamp of last non-centre pitch bend reading

        self.joystick = Joystick(dead_zone_pct=settings.get('joy_deadzone_pct'))

    # MIDI send

    def _tx(self, *msg):
        for port in self._ports:
            port.write_data(*msg)

    def send_cc(self, cc_num, value):
        """Send CC only if value has changed since last send (suppresses redundant traffic)."""
        value = max(0, min(127, value))
        if self._last_cc.get(cc_num) == value:
            return
        self._last_cc[cc_num] = value
        self._tx(0xB0 | self._s.get('midi_channel'), cc_num, value)  # 0xB0 = Control Change status byte

    def send_pitch_bend(self, raw_12bit):
        """
        Send 14-bit pitch bend from a 12-bit ADC value.
        Y axis is inverted in hardware: stick up = raw high, stick down = raw low.
        Invert so pushing stick forward (up) = positive pitch bend.
        Dead zone applied around centre to prevent drift at rest.
        """
        raw       = 4095 - raw_12bit   # invert Y axis direction
        dead      = self._s.get('pb_deadzone', 80)
        deviation = raw - 2048
        if abs(deviation) < dead:
            deviation = 0

        pb = max(0, min(16383, (2048 + deviation) << 2))
        if pb == self._pb_value:
            return

        self._pb_value = pb
        if deviation != 0:
            self._pb_last_ms = time.ticks_ms()

        lsb = pb & 0x7F
        msb = (pb >> 7) & 0x7F
        self._tx(0xE0 | self._s.get('midi_channel', 0), lsb, msb)

    def pb_auto_zero_check(self):
        """
        Send centre pitch bend if Y axis has been physically at rest (near centre)
        for pb_return_ms. Only fires when pb_return_ms is set to a short value.
        Default pb_return_ms is 1000ms which gives a slow auto-return.
        Set pb_return_ms to 0 to disable auto-return entirely.
        """
        if self._pb_value == 8192 or self._pb_last_ms == 0:
            return
        pb_return = self._s.get('pb_return_ms', 1000)
        if pb_return == 0:
            return   # auto-return disabled
        elapsed = time.ticks_diff(time.ticks_ms(), self._pb_last_ms)
        if elapsed >= pb_return:
            self._pb_value   = 8192
            self._pb_last_ms = 0
            self._tx(0xE0 | self._s.get('midi_channel', 0), 0x00, 0x40)

    # Per-channel processing

    def on_analog_change(self, source_key, raw_12bit):
        """
        Dispatch a smoothed, deadband-filtered 12-bit reading to the correct MIDI output.
        Called by adc_scan_task for every channel that exceeds the deadband threshold.
        """
        s           = self._s
        assignments = s.get('cc_assignments')  # nested dict from settings

        if source_key == 'joy_x':
            cc_raw = raw_12bit >> 5
            locked = self.joystick.process_x(cc_raw)
            mod    = joy_x_to_mod(locked)
            cc_num = assignments.get('joy_x', 1)
            if cc_num != 'pb':
                self.send_cc(cc_num, mod)
            return

        if source_key == 'joy_y':
            locked_raw = self.joystick.process_y(raw_12bit)  # always returns raw -- Y is never locked
            self.send_pitch_bend(locked_raw)
            return

        if source_key in ('fsr_0', 'fsr_1'):  # 'in' tests membership in a tuple -- one check for both
            cc_val = fsr_to_cc(raw_12bit, s.get('fsr_threshold'))
            if cc_val is None:
                return
            cc_num = assignments.get(source_key)
            if cc_num and cc_num != 'pb':
                self.send_cc(cc_num, cc_val)
            return

        if source_key == 'breath':
            cc_val = breath_to_cc(raw_12bit)
            if self._led:
                self._led.update_breath(cc_val)   # modulate LED brightness with breath level
            if s.get('velocity_source') == 'breath':
                self._ne.velocity = cc_val
            expr_src = s.get('expression_source')
            if expr_src == 'breath':
                cc_num = assignments.get('breath', 11)
                if cc_num != 'pb':
                    self.send_cc(cc_num, cc_val)
            return

        # Standard sources (pots, pedals) -- no special processing, direct 12-to-7 bit scale
        cc_val = raw_12bit >> 5
        cc_num = assignments.get(source_key)
        if cc_num is not None and cc_num != 'pb':
            self.send_cc(cc_num, cc_val)

    def on_ultrasonic(self, dist_cm):
        """
        Called by hcsr04_task with each distance reading (or None on timeout).

        V4 changes:
          - Velocity mode removed. Sonic is CC or theremin only.
          - Timeouts ignored (last valid reading held) to prevent CC jumping
            to default on occasional missed echoes at range edges.
          - When hand moves out of range, sends sonic_default_cc (64)
            rather than abruptly cutting to 0 or jumping to default_velocity.
        """
        s      = self._s
        min_cm = s.get('sonic_min_cm', 3)
        max_cm = s.get('sonic_max_cm', 30)
        mode   = s.get('sonic_mode', 'cc')

        # Hold last valid reading -- ignore timeouts entirely
        # Prevents CC jumping on occasional missed echoes at range edges
        if dist_cm is not None:
            self._last_sonic_cm = dist_cm
        elif not hasattr(self, '_last_sonic_cm'):
            return   # no valid reading yet at all

        working_cm = self._last_sonic_cm if dist_cm is None else dist_cm

        if mode == 'theremin' and self._ne.theremin is not None:
            self._ne.theremin.update(
                dist_cm,                          # pass actual reading (None on timeout)
                time.ticks_ms(),
                channel=s.get('theremin_channel', 0),
            )
            return

        if mode == 'cc':
            cc_num     = s.get('cc_assignments', {}).get('sonic', 11)
            default_cc = s.get('sonic_default_cc', 64)

            if working_cm > max_cm or working_cm < min_cm:
                # Hand out of range -- send default value gracefully
                self.send_cc(cc_num, default_cc)
            else:
                cc_val = distance_to_cc(working_cm, min_cm, max_cm)
                if cc_val is not None and cc_num != 'pb':
                    self.send_cc(cc_num, cc_val)


# Asyncio tasks (module-level coroutines launched with asyncio.create_task in main.py)

async def adc_scan_task(mux, cc_engine, settings):
    """
    Scan active MUX channels at approximately 50Hz.
    Enable flags from config.py determine which channels are included.
    Yields between individual channel reads so other tasks remain responsive.
    """
    from config import (PEDAL_1_ENABLED, PEDAL_2_ENABLED,
                        FSR_1_ENABLED, FSR_2_ENABLED,
                        BREATH_ENABLED, JOY_ENABLED)

    # Joystick and FSR are handled by fast_scan_task at 200Hz.
    # This task covers pots, pedals, and breath at 50Hz.
    ACTIVE_SOURCES = ['pot_0', 'pot_1', 'pot_2', 'pot_3', 'pot_4']
    if PEDAL_1_ENABLED:
        ACTIVE_SOURCES.append('pedal_0')
    if PEDAL_2_ENABLED:
        ACTIVE_SOURCES.append('pedal_1')
    if BREATH_ENABLED:
        ACTIVE_SOURCES.append('breath')

    prev = {}  # tracks last dispatched value per source for deadband comparison

    # Local variables track joystick position each cycle without corrupting the EMA cache.
    # (Reading mux.smooth(ch, 0, 1.0) to "peek" the cache would corrupt it with alpha=1.)
    last_joy_x_cc  = 64    # 7-bit centre
    last_joy_y_raw = 2048  # 12-bit centre

    while True:
        alpha_pots   = settings.get('ema_alpha_pots')
        alpha_breath = 0.25
        deadband     = settings.get('adc_deadband')
        joy          = cc_engine.joystick

        for source_key in ACTIVE_SOURCES:
            ch       = SOURCE_TO_CH[source_key]  # dict lookup: source name to MUX channel number
            raw      = mux.read_raw(ch)
            alpha    = alpha_breath if source_key == 'breath' else (0.30 if 'joy' in source_key else alpha_pots)
            smoothed = mux.smooth(ch, raw, alpha)

            if source_key == 'joy_x':
                last_joy_x_cc  = smoothed >> 5
            elif source_key == 'joy_y':
                last_joy_y_raw = smoothed

            if abs(smoothed - prev.get(source_key, -999)) > deadband:
                prev[source_key] = smoothed
                cc_engine.on_analog_change(source_key, smoothed)

            await asyncio.sleep_ms(0)  # yield to event loop between channels -- keeps other tasks alive

        joy.update_button()
        cc_engine.pb_auto_zero_check()
        joy.check_unlock(last_joy_x_cc, last_joy_y_raw)

        await asyncio.sleep_ms(18)  # approx 50Hz total scan rate


async def fast_scan_task(mux, cc_engine, settings):
    """
    Scan time-sensitive sources at ~200Hz: joystick X/Y and FSR.
    Joystick benefits from faster updates for smooth pitch bend response.
    FSR benefits from faster attack detection for percussive playing.
    Runs independently from adc_scan_task which handles pots at 50Hz.
    """
    from config import JOY_ENABLED, FSR_1_ENABLED, FSR_2_ENABLED, SOURCE_TO_CH

    FAST_SOURCES = []
    if JOY_ENABLED:
        FAST_SOURCES += ['joy_x', 'joy_y']
    if FSR_1_ENABLED:
        FAST_SOURCES.append('fsr_0')
    if FSR_2_ENABLED:
        FAST_SOURCES.append('fsr_1')

    if not FAST_SOURCES:
        return

    prev          = {}
    last_joy_x_cc  = 64
    last_joy_y_raw = 2048

    while True:
        alpha    = 0.35   # slightly more responsive than pots
        deadband = max(1, settings.get('adc_deadband', 32) // 2)  # tighter deadband
        joy      = cc_engine.joystick

        for source_key in FAST_SOURCES:
            ch       = SOURCE_TO_CH[source_key]
            raw      = mux.read_raw(ch)
            smoothed = mux.smooth(ch, raw, alpha)

            if source_key == 'joy_x':
                last_joy_x_cc  = smoothed >> 5
            elif source_key == 'joy_y':
                last_joy_y_raw = smoothed

            if abs(smoothed - prev.get(source_key, -999)) > deadband:
                prev[source_key] = smoothed
                cc_engine.on_analog_change(source_key, smoothed)

            await asyncio.sleep_ms(0)

        joy.update_button()
        cc_engine.pb_auto_zero_check()
        joy.check_unlock(last_joy_x_cc, last_joy_y_raw)

        await asyncio.sleep_ms(4)   # ~200Hz


async def hcsr04_task(cc_engine):
    """
    Range the HC-SR04 every HCSR04_INTERVAL_MS and call cc_engine.on_ultrasonic().
    distance_cm() blocks for up to ~10ms -- acceptable because key detection is interrupt-driven.
    Gracefully disables itself if SONIC_ENABLED=False or if hcsr04 library is not installed.
    """
    from config import SONIC_ENABLED
    if not SONIC_ENABLED:
        print('[hcsr04_task] SONIC_ENABLED=False -- task disabled.')
        return

    try:
        from hcsr04 import HCSR04
        sensor = HCSR04(
            trigger_pin=HCSR04_TRIG,
            echo_pin=HCSR04_ECHO,
            echo_timeout_us=HCSR04_TIMEOUT_US,
        )
    except ImportError:
        print('[hcsr04_task] hcsr04.py not found -- task disabled.')
        return  # returning from an async function ends the coroutine permanently

    while True:
        try:
            dist = sensor.distance_cm()
            cc_engine.on_ultrasonic(dist)
        except OSError:              # OSError is raised on echo timeout (no object in range)
            cc_engine.on_ultrasonic(None)
        await asyncio.sleep_ms(HCSR04_INTERVAL_MS)

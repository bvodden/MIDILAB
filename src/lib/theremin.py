"""
theremin.py -- MIDILAB V4
Pitch-bin theremin controller for HC-SR04 ultrasonic sensor.

Divides the configured distance range into semitone bins.
Hand must remain in a bin for theremin_hold_ms before the note fires
-- prevents flickering as hand moves between positions.
When hand moves to a new bin, old note off then new note on.
Plays on theremin_channel (separate from keyboard channel).

Scale modes:
  'chromatic' -- all 12 semitones across the range
  'keypad'    -- only notes from the active scale/root
"""

import time


class ThereminController:
    """
    Monophonic pitch-bin theremin.
    Call update(dist_cm, now_ms) on every ultrasonic reading.
    Sends MIDI note-on/off via the provided tx function.
    """

    def __init__(self, tx_fn, settings):
        """
        tx_fn    : callable(status, data1, data2) for sending MIDI
        settings : Settings instance
        """
        self._tx       = tx_fn
        self._s        = settings
        self._current_note  = None   # currently sounding note or None
        self._candidate     = None   # note candidate being held in a bin
        self._candidate_since = 0    # ticks_ms when candidate was first seen
        self._last_valid_cm = None   # last non-timeout distance reading

    def _dist_to_note(self, dist_cm):
        """
        Map a distance to a MIDI note number.
        Returns None if out of configured range.
        """
        from music_theory import SCALES
        s        = self._s
        min_cm   = s.get('sonic_min_cm', 3)
        max_cm   = s.get('sonic_max_cm', 30)
        root     = s.get('root_note', 60)
        t_range  = s.get('theremin_range', 24)   # semitones across full range
        t_scale  = s.get('theremin_scale', 'chromatic')

        if dist_cm is None or dist_cm > max_cm or dist_cm < min_cm:
            return None

        # Normalise distance to 0.0 (close) to 1.0 (far)
        # Inverted: close = high pitch, far = low pitch
        normalised = 1.0 - (dist_cm - min_cm) / (max_cm - min_cm)
        semitone_offset = int(normalised * t_range)

        if t_scale == 'chromatic':
            note = root + semitone_offset
        else:
            # Map semitone offset to nearest scale degree
            scale = SCALES.get(s.get('scale', 'major'), SCALES['major'])
            n     = len(scale)
            # Find which scale degree the offset lands on
            octave = semitone_offset // 12
            rem    = semitone_offset % 12
            # Find nearest scale tone to rem
            nearest = min(scale, key=lambda x: abs(x - rem))
            note = root + octave * 12 + nearest

        return max(0, min(127, note))

    def update(self, dist_cm, now_ms, channel=0):
        """
        Call on every ultrasonic reading.
        dist_cm  : distance in cm, or None on timeout
        now_ms   : current time.ticks_ms()
        channel  : MIDI channel (0-indexed)
        """
        s         = self._s
        hold_ms   = s.get('theremin_hold_ms', 80)

        # Ignore timeouts -- hold last known position
        # This prevents velocity/note jumping on occasional missed echoes
        if dist_cm is not None:
            self._last_valid_cm = dist_cm
        else:
            dist_cm = self._last_valid_cm

        target_note = self._dist_to_note(dist_cm)

        if target_note is None:
            # Hand out of range -- silence current note
            if self._current_note is not None:
                self._tx(0x80 | channel, self._current_note, 0)
                self._current_note  = None
                self._candidate     = None
            return

        if target_note != self._candidate:
            # New bin entered -- start hold timer
            self._candidate       = target_note
            self._candidate_since = now_ms
            return

        # Same bin -- check if held long enough
        held = time.ticks_diff(now_ms, self._candidate_since)
        if held < hold_ms:
            return   # not yet stable

        # Bin confirmed -- fire note if changed
        if target_note != self._current_note:
            if self._current_note is not None:
                self._tx(0x80 | channel, self._current_note, 0)   # note off old
            self._tx(0x90 | channel, target_note, 100)             # note on new
            self._current_note = target_note

    def silence(self, channel=0):
        """Send note-off for any currently sounding note."""
        if self._current_note is not None:
            self._tx(0x80 | channel, self._current_note, 0)
            self._current_note = None
            self._candidate    = None

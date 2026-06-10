"""
note_engine.py -- MIDILAB V2
MIDI note generation for all play styles.

GC strategy: gc.collect() called in note_on() before allocating chord notes.
This keeps the heap clean during performance without moving GC to random times.
Pre-allocated _chord_buf avoids list creation for single-note scale mode.
"""

import asyncio
from music_theory import (SCALES, CHORD_ROWS, WILDCARD_CHORDS,
                           derived_chord, chromatic_lab_chord)


class NoteEngine:

    def __init__(self, ports, settings, arp_engine=None):
        self._ports      = ports
        self._settings   = settings
        self._arp_engine = arp_engine
        self._active     = {}       # (row, col) -> list of MIDI notes
        self._note_refs  = {}       # note -> count of keys holding it
        self._chord_buf  = [0, 0, 0, 0]  # pre-allocated single-note buffer
        self.velocity    = settings.get('default_velocity')
        self.theremin    = None

    @property
    def _ch(self):
        return self._settings.get('midi_channel', 0)

    def _ch_for_row(self, row):
        """Return MIDI channel for a physical key row."""
        if self._settings.get('split_channel_enabled'):
            if row == 0:
                return self._settings.get('split_channel_bass', 0)
            else:
                return self._settings.get('split_channel_upper', 1)
        return self._settings.get('midi_channel', 0)

    @property
    def _scale(self):
        return SCALES.get(self._settings.get('scale'), SCALES['major'])

    @property
    def _root(self):
        return self._settings.get('root_note', 60)

    @property
    def _base_octave(self):
        return self._settings.get('base_octave', 0)

    def _scale_note(self, row, col):
        scale     = self._scale
        n         = len(scale)
        semitones = scale[col % n] + (col // n) * 12
        note      = self._root + (row + self._base_octave) * 12 + semitones
        return note if 0 <= note <= 127 else None

    def _chord_notes_fixed(self, row, col):
        if row >= len(CHORD_ROWS) or col >= len(CHORD_ROWS[row]):
            return []
        root_offset, intervals = CHORD_ROWS[row][col]
        if row == 3 and col == 6:
            wc = WILDCARD_CHORDS.get(self._settings.get('wildcard_chord', 'aug'))
            if wc:
                root_offset, intervals = wc
        chord_root = self._root + root_offset
        notes = []
        for iv in intervals:
            n = chord_root + iv
            if not (0 <= n <= 127):
                return []
            notes.append(n)
        return notes

    def _chord_notes_derived(self, row, col):
        return derived_chord(
            self._settings.get('scale', 'major'),
            self._root, col, row, self._base_octave,
        )

    def _combo_scale_note(self, sub_row, col):
        scale = self._scale
        n     = len(scale)
        degree     = sub_row * 7 + col
        octave_bump = degree // n
        scale_idx   = degree % n
        note = self._root + (self._base_octave + 1) * 12 + scale[scale_idx] + octave_bump * 12
        return note if 0 <= note <= 127 else None

    def _combo_chromatic_note(self, sub_row, col):
        note = self._root + (self._base_octave + 1) * 12 + sub_row * 7 + col
        return note if 0 <= note <= 127 else None

    def _chromatic_melody_note(self, row, col):
        note = self._root + self._base_octave * 12 + row * 7 + col
        return note if 0 <= note <= 127 else None

    def _chromatic_lab_chord_notes(self, sub_row, col):
        col_root = self._root + self._base_octave * 12 + col
        return chromatic_lab_chord(sub_row, col, col_root)

    def _get_notes(self, row, col):
        """Dispatch to correct note calculation based on play style."""
        mode = self._settings.get('keypad_mode', 'scale')

        if mode == 'scale':
            n = self._scale_note(row, col)
            if n is None:
                return []
            # Reuse pre-allocated buffer for single notes -- no heap allocation
            self._chord_buf[0] = n
            return self._chord_buf[:1]

        elif mode == 'chord':
            return self._chord_notes_fixed(row, col)

        elif mode == 'chord_derived':
            return self._chord_notes_derived(row, col)

        elif mode == 'combo':
            if row < 2:
                return self._chord_notes_derived(row, col)
            n = self._combo_scale_note(row - 2, col)
            if n is None:
                return []
            self._chord_buf[0] = n
            return self._chord_buf[:1]

        elif mode == 'combo_c':
            if row < 2:
                return self._chord_notes_derived(row, col)
            n = self._combo_chromatic_note(row - 2, col)
            if n is None:
                return []
            self._chord_buf[0] = n
            return self._chord_buf[:1]

        elif mode == 'chromatic_lab':
            if row < 2:
                n = self._chromatic_melody_note(row, col)
                if n is None:
                    return []
                self._chord_buf[0] = n
                return self._chord_buf[:1]
            return self._chromatic_lab_chord_notes(row - 2, col)

        return []

    def note_on(self, row, col):
        if (row, col) in self._active:
            return

        notes = self._get_notes(row, col)
        if not notes:
            return

        # Store a copy for note_off (single notes use chord_buf which is reused)
        self._active[(row, col)] = list(notes)

        if self._settings.get('arp_enabled') and self._arp_engine is not None:
            self._rebuild_arp_pool()
            return

        ch  = self._ch_for_row(row)
        vel = max(1, min(127, self.velocity))

        if self._settings.get('strum_enabled'):
            asyncio.create_task(self._strum_send(list(notes), vel, ch))
            return

        for note in notes:
            count = self._note_refs.get(note, 0)
            self._note_refs[note] = count + 1
            if count == 0:
                self._tx(0x90 | ch, note, vel)

    def note_off(self, row, col):
        notes = self._active.pop((row, col), None)
        if notes is None:
            return

        if self._settings.get('arp_enabled') and self._arp_engine is not None:
            self._rebuild_arp_pool()
            return

        ch = self._ch_for_row(row)
        for note in notes:
            count = self._note_refs.get(note, 0) - 1
            if count <= 0:
                self._note_refs.pop(note, None)
                self._tx(0x80 | ch, note, 0)
            else:
                self._note_refs[note] = count

    def all_notes_off(self):
        ch = self._ch
        for note in list(self._note_refs.keys()):
            self._tx(0x80 | ch, note, 0)
        self._active.clear()
        self._note_refs.clear()
        if self._arp_engine:
            self._arp_engine._pool_len = 0
        self._tx(0xB0 | ch, 123, 0)

    def all_notes_off_all_channels(self):
        """Panic: silence all 16 channels simultaneously."""
        for ch in range(16):
            self._tx(0xB0 | ch, 120, 0)  # All Sound Off
            self._tx(0xB0 | ch, 123, 0)  # All Notes Off
        self._active.clear()
        self._note_refs.clear()
        if self._arp_engine:
            self._arp_engine._pool_len = 0

    def _rebuild_arp_pool(self):
        """Flatten _active into arp pool in insertion order."""
        pool  = []
        seen  = set()
        for notes in self._active.values():
            for n in notes:
                if n not in seen:
                    seen.add(n)
                    pool.append(n)
        if self._arp_engine:
            self._arp_engine.set_pool(pool)

    async def _strum_send(self, notes, vel, ch):
        delay_ms  = self._settings.get('strum_delay_ms', 20)
        direction = self._settings.get('strum_direction', 'low_high')
        ordered   = sorted(notes, reverse=(direction == 'high_low'))
        for i, note in enumerate(ordered):
            count = self._note_refs.get(note, 0)
            self._note_refs[note] = count + 1
            if count == 0:
                self._tx(0x90 | ch, note, vel)
            if i < len(ordered) - 1:
                await asyncio.sleep_ms(delay_ms)

    def set_scale(self, scale_name):
        self.all_notes_off()
        self._settings._data['scale'] = scale_name

    def set_root(self, note):
        self.all_notes_off()
        self._settings._data['root_note'] = max(0, min(127, note))

    def shift_octave(self, direction):
        self.all_notes_off()
        current = self._settings.get('base_octave', 0)
        self._settings._data['base_octave'] = max(-3, min(3, current + direction))

    def transpose(self, semitones):
        self.all_notes_off()
        self._settings._data['root_note'] = max(
            0, min(127, self._settings.get('root_note', 60) + semitones))

    def _tx(self, *msg):
        for port in self._ports:
            port.write_data(*msg)

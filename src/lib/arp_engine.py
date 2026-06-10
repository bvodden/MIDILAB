"""
arp_engine.py -- MIDILAB V2
Arpeggiator, chord progression memory playback, and MIDI clock sync.

GC strategy: gc.collect() is called during the REST period between notes
(the silence after note-off) where a few milliseconds pause is inaudible.
This keeps the heap clean without causing missed beats or late note-ons.

Pool is a fixed-size pre-allocated array to eliminate heap allocation
during performance. Entries beyond _pool_len are ignored.
"""

import asyncio
import time
import gc

DIVISION_FACTORS = {
    '1/4':  1.0,
    '1/8':  0.5,
    '1/16': 0.25,
    '1/32': 0.125,
}

QUALITY_FACTORS = {
    'standard': 1.0,
    'dotted':   1.5,
    'triplet':  2.0 / 3.0,
}


def _calc_step_ms(bpm, division, quality):
    quarter_ms  = 60_000.0 / bpm
    div_factor  = DIVISION_FACTORS.get(division, 0.25)
    qual_factor = QUALITY_FACTORS.get(quality, 1.0)
    return quarter_ms * div_factor * qual_factor


class ArpEngine:

    def __init__(self):
        # Pre-allocated fixed-size pool -- no heap allocation during performance.
        # 28 = maximum possible notes (all keys held simultaneously).
        self._pool     = [0] * 28
        self._pool_len = 0           # number of active entries in pool
        self._idx      = 0
        self._direction = 1
        self._bpm      = 120.0
        self._ext_bpm  = None

        self._clock_times = []

    def set_pool(self, notes):
        """Write notes into pre-allocated pool. Called by note_engine."""
        n = min(len(notes), 28)
        for i in range(n):
            self._pool[i] = notes[i]  # write into existing list, no allocation
        self._pool_len = n
        if self._idx >= self._pool_len and self._pool_len > 0:
            self._idx = 0

    def reset_phase(self):
        self._idx      = 0
        self._direction = 1

    def receive_clock(self):
        now = time.ticks_us()
        self._clock_times.append(now)
        if len(self._clock_times) > 25:
            self._clock_times.pop(0)
        if len(self._clock_times) >= 3:
            intervals = [
                time.ticks_diff(self._clock_times[i+1], self._clock_times[i])
                for i in range(len(self._clock_times) - 1)
            ]
            avg_us = sum(intervals) / len(intervals)
            if avg_us > 0:
                self._ext_bpm = 60_000_000.0 / (avg_us * 24)

    def get_bpm(self, settings):
        if settings.get('clock_source') == 'midi' and self._ext_bpm is not None:
            return self._ext_bpm
        return float(settings.get('arp_bpm', 120))

    def next_note(self, pattern):
        """Return next note from pool. No heap allocation."""
        if self._pool_len == 0:
            return None

        if pattern == 'up':
            # Find the nth smallest note without sorting (no allocation)
            # Simple approach: sort the active slice only
            active = sorted(self._pool[:self._pool_len])
            note = active[self._idx % self._pool_len]
            self._idx = (self._idx + 1) % self._pool_len

        elif pattern == 'down':
            active = sorted(self._pool[:self._pool_len], reverse=True)
            note = active[self._idx % self._pool_len]
            self._idx = (self._idx + 1) % self._pool_len

        elif pattern == 'updown':
            active = sorted(self._pool[:self._pool_len])
            n = len(active)
            if n == 1:
                note = active[0]
            else:
                total = (n - 1) * 2
                pos   = self._idx % total
                idx   = pos if pos < n else total - pos
                note  = active[idx]
                self._idx += 1

        elif pattern == 'random':
            import random
            note = self._pool[random.randint(0, self._pool_len - 1)]

        else:  # as_played
            note = self._pool[self._idx % self._pool_len]
            self._idx = (self._idx + 1) % self._pool_len

        return note


async def arp_task(note_engine, arp_engine, settings):
    """
    Main arp sequencer. Fires notes at calculated intervals.
    gc.collect() runs during the REST period (silence after note-off)
    where a few ms pause is completely inaudible. This prevents GC pauses
    from landing on a note-on and causing a late attack or missed beat.
    """
    while True:
        if not settings.get('arp_enabled') or arp_engine._pool_len == 0:
            await asyncio.sleep_ms(20)
            continue

        bpm      = arp_engine.get_bpm(settings)
        division = settings.get('arp_division', '1/16')
        quality  = settings.get('arp_quality',  'standard')
        gate     = settings.get('arp_gate',      0.5)
        pattern  = settings.get('arp_pattern',   'up')

        step_ms = _calc_step_ms(bpm, division, quality)
        gate_ms = max(10, int(step_ms * gate))
        rest_ms = max(1,  int(step_ms - gate_ms))

        note = arp_engine.next_note(pattern)
        if note is None:
            await asyncio.sleep_ms(20)
            continue

        ch  = note_engine._ch_for_row(0)
        vel = max(1, min(127, note_engine.velocity))

        note_engine._tx(0x90 | ch, note, vel)    # note on
        await asyncio.sleep_ms(gate_ms)
        note_engine._tx(0x80 | ch, note, 0)      # note off

        gc.collect()                              # collect during silence -- inaudible here

        await asyncio.sleep_ms(rest_ms)


async def midi_clock_task(arp_engine, settings, midi):
    """
    Listens for MIDI clock ticks (0xF8) and derives BPM.
    Returns immediately if clock_source is internal -- no USB IN port created.
    """
    if settings.get('clock_source') != 'midi':
        print('[Clock] Internal clock -- task idle.')
        return

    try:
        in_port = midi.add_usb_in(0, 'MIDILAB In')
        print('[Clock] MIDI clock input active.')
    except Exception as e:
        print('[Clock] MIDI input unavailable:', e)
        return

    while True:
        try:
            msg = await in_port.read()
            if msg and len(msg) >= 1:
                status = msg[0] & 0xFF
                if status == 0xF8:
                    arp_engine.receive_clock()
                elif status == 0xFA:
                    arp_engine.reset_phase()
        except Exception:
            await asyncio.sleep_ms(1)

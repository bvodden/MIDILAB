"""
memory.py -- MIDILAB V2
Preset manager (PS-1 to PS-8) and chord progression memory (8 slots).
Both persist to flash as JSON files separate from settings.json.
"""

import ujson

PRESETS_FILE     = 'presets.json'
PROGRESSIONS_FILE = 'progressions.json'
NUM_PRESETS      = 8
NUM_PROG_SLOTS   = 8


class PresetManager:
    """
    Stores and recalls complete settings snapshots.
    Each preset is a full copy of settings._data at the time of saving.
    Presets are named PS-1 through PS-8.
    """

    def __init__(self, settings):
        self._settings = settings
        self._presets  = [None] * NUM_PRESETS  # None = empty slot
        self.load()

    def load(self):
        try:
            with open(PRESETS_FILE, 'r') as f:
                data = ujson.load(f)
            self._presets = data.get('presets', [None] * NUM_PRESETS)
            while len(self._presets) < NUM_PRESETS:
                self._presets.append(None)
        except Exception:
            self._presets = [None] * NUM_PRESETS

    def _save(self):
        try:
            with open(PRESETS_FILE, 'w') as f:
                ujson.dump({'presets': self._presets}, f)
        except Exception as e:
            print('[Presets] Save failed:', e)

    def save_preset(self, slot_idx):
        """Save current settings to slot (0-indexed)."""
        if not 0 <= slot_idx < NUM_PRESETS:
            return
        self._presets[slot_idx] = self._settings.snapshot()  # deep copy
        self._save()
        print(f'[Presets] Saved PS-{slot_idx + 1}')

    def load_preset(self, slot_idx):
        """Restore settings from slot. Returns True if slot had data."""
        if not 0 <= slot_idx < NUM_PRESETS:
            return False
        snap = self._presets[slot_idx]
        if snap is None:
            return False
        self._settings.restore(snap)
        print(f'[Presets] Loaded PS-{slot_idx + 1}')
        return True

    def is_empty(self, slot_idx):
        return self._presets[slot_idx] is None

    def slot_name(self, slot_idx):
        return f'PS-{slot_idx + 1}'

    def clear_all(self):
        self._presets = [None] * NUM_PRESETS
        self._save()


class ProgressionManager:
    """
    Stores and plays back up to 8 chord snapshots.
    Each slot holds a list of MIDI note numbers (the sounding notes at record time).
    Play All cycles through populated slots at the current arp tempo.
    """

    def __init__(self):
        self._slots   = [None] * NUM_PROG_SLOTS  # list of MIDI notes, or None
        self._play_idx = 0   # current playback position for Play All
        self.load()

    def load(self):
        try:
            with open(PROGRESSIONS_FILE, 'r') as f:
                data = ujson.load(f)
            self._slots = data.get('slots', [None] * NUM_PROG_SLOTS)
            while len(self._slots) < NUM_PROG_SLOTS:
                self._slots.append(None)
        except Exception:
            self._slots = [None] * NUM_PROG_SLOTS

    def _save(self):
        try:
            with open(PROGRESSIONS_FILE, 'w') as f:
                ujson.dump({'slots': self._slots}, f)
        except Exception as e:
            print('[Progression] Save failed:', e)

    def record(self, slot_idx, notes):
        """Record a list of MIDI notes to a slot."""
        if not 0 <= slot_idx < NUM_PROG_SLOTS:
            return
        self._slots[slot_idx] = list(notes)
        self._save()
        print(f'[Progression] Recorded slot {slot_idx + 1}: {notes}')

    def get_slot(self, slot_idx):
        """Return notes for a slot, or None if empty."""
        if not 0 <= slot_idx < NUM_PROG_SLOTS:
            return None
        return self._slots[slot_idx]

    def next_slot_for_playback(self):
        """
        Return the next non-empty slot for sequential playback.
        Advances internal index. Returns None if all slots are empty.
        """
        for _ in range(NUM_PROG_SLOTS):
            slot = self._slots[self._play_idx]
            self._play_idx = (self._play_idx + 1) % NUM_PROG_SLOTS  # % wraps index at end
            if slot is not None:
                return list(slot)
        return None

    def reset_playback(self):
        self._play_idx = 0

    def clear(self, slot_idx):
        if 0 <= slot_idx < NUM_PROG_SLOTS:
            self._slots[slot_idx] = None
            self._save()

    def clear_all(self):
        self._slots = [None] * NUM_PROG_SLOTS
        self._save()

    def is_empty(self, slot_idx):
        return self._slots[slot_idx] is None

    def populated_count(self):
        return sum(1 for s in self._slots if s is not None)

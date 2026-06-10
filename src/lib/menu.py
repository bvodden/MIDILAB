"""
menu.py -- MIDILAB V2
Menu state machine for encoder + LCD interface.

New in V2:
  Performance submenu: arp, strum, clock source, BPM.
  Presets submenu (root level): PS-1 to PS-8 save/load.
  Progression submenu: record and play chord snapshots.
  Updated Play Style: 6 modes including derived and combo variants.
  Multi-channel config in Config submenu.
  Calibration trigger in Config submenu.
"""

import asyncio
from music_theory import (SCALES, SCALE_NAMES, SCALE_KEYS, MODE_KEYS,
                           cc_display, WILDCARD_NAMES)


class MenuItem:
    def __init__(self, label):
        self.label  = label
        self.parent = None

    def on_enter(self, mgr):
        mgr.show(self.label, '')

    def on_scroll(self, mgr, delta):
        pass

    def on_short_press(self, mgr):
        pass

    def on_long_press(self, mgr):
        mgr.go_back()


class SubMenu(MenuItem):
    def __init__(self, label, children=None):
        super().__init__(label)
        self.children = []
        self._cursor  = 0
        if children:
            for child in children:
                self.add(child)

    def add(self, item):
        item.parent = self
        self.children.append(item)
        return self

    def on_enter(self, mgr):
        self._cursor = 0
        self._show(mgr)

    def _show(self, mgr):
        if self.children:
            mgr.show(self.label, '> ' + self.children[self._cursor].label)
        else:
            mgr.show(self.label, '(empty)')

    def on_scroll(self, mgr, delta):
        if not self.children:
            return
        self._cursor = (self._cursor + delta) % len(self.children)
        self._show(mgr)

    def on_short_press(self, mgr):
        if self.children:
            mgr.navigate_to(self.children[self._cursor])


class AdjustItem(MenuItem):
    """Root-level quick-adjust. Scroll changes value live. Short press confirms."""

    def __init__(self, label, get_fn, set_fn, min_val, max_val, step=1, formatter=None):
        super().__init__(label)
        self._get  = get_fn
        self._set  = set_fn
        self._min  = min_val
        self._max  = max_val
        self._step = step
        self._fmt  = formatter or str
        self._orig = None

    def on_enter(self, mgr):
        self._orig = self._get()
        mgr.show(self.label, self._fmt(self._orig))

    def on_scroll(self, mgr, delta):
        val = max(self._min, min(self._max, self._get() + delta * self._step))
        self._set(val)
        mgr.show(self.label, self._fmt(val))

    def on_short_press(self, mgr):
        mgr.settings.save()
        mgr.go_root()

    def on_long_press(self, mgr):
        if self._orig is not None:
            self._set(self._orig)
        mgr.go_root()


class ValueItem(MenuItem):
    """Numeric value with min/max/step. Short press saves."""

    def __init__(self, label, key, min_val, max_val, step=1, formatter=None, on_change=None):
        super().__init__(label)
        self._key       = key
        self._min       = min_val
        self._max       = max_val
        self._step      = step
        self._fmt       = formatter or str
        self._on_change = on_change

    def on_enter(self, mgr):
        val = mgr.settings.get(self._key)
        mgr.show(self.label, self._fmt(val))

    def on_scroll(self, mgr, delta):
        val = mgr.settings.get(self._key)
        val = max(self._min, min(self._max, val + delta * self._step))
        mgr.settings._data[self._key] = val
        if self._on_change:
            self._on_change(val)
        mgr.show(self.label, self._fmt(val))

    def on_short_press(self, mgr):
        mgr.settings.save()
        mgr.go_back()


class ChoiceItem(MenuItem):
    """Pick from a fixed list of (value, display_name) pairs."""

    def __init__(self, label, key, choices, on_change=None):
        super().__init__(label)
        self._key       = key
        self._choices   = choices
        self._on_change = on_change
        self._idx       = 0

    def _find_idx(self, mgr):
        val = mgr.settings.get(self._key)
        for i, (v, _) in enumerate(self._choices):
            if v == val:
                return i
        return 0

    def on_enter(self, mgr):
        self._idx = self._find_idx(mgr)
        mgr.show(self.label, self._choices[self._idx][1])

    def on_scroll(self, mgr, delta):
        self._idx = (self._idx + delta) % len(self._choices)
        val, name = self._choices[self._idx]
        mgr.settings._data[self._key] = val
        if self._on_change:
            self._on_change(val)
        mgr.show(self.label, name)

    def on_short_press(self, mgr):
        mgr.settings.save()
        mgr.go_back()


class CCAssignItem(MenuItem):
    """Scroll through CC 0-127 with names. Pitch bend option for joy_y."""

    def __init__(self, label, cc_key, allow_pb=False):
        super().__init__(label)
        self._cc_key   = cc_key
        self._allow_pb = allow_pb
        self._max_cc   = 128 if allow_pb else 127
        self._val      = 0

    def _to_display(self, val):
        return 'Pitch Bend' if val == 128 else cc_display(val)

    def _to_stored(self, val):
        return 'pb' if val == 128 else val

    def _from_stored(self, stored):
        return 128 if stored == 'pb' else (stored if stored is not None else 0)

    def on_enter(self, mgr):
        self._val = self._from_stored(mgr.settings.get_cc(self._cc_key))
        mgr.show(self.label, self._to_display(self._val))

    def on_scroll(self, mgr, delta):
        self._val = max(0, min(self._max_cc, self._val + delta))
        mgr.show(self.label, self._to_display(self._val))

    def on_short_press(self, mgr):
        mgr.settings.set_cc(self._cc_key, self._to_stored(self._val))
        mgr.go_back()


class ActionItem(MenuItem):
    """Execute a callback immediately on short press."""

    def __init__(self, label, action_fn, confirm_text='Done'):
        super().__init__(label)
        self._action  = action_fn
        self._confirm = confirm_text

    def on_short_press(self, mgr):
        self._action(mgr)
        mgr.show(self.label, self._confirm)


class PresetItem(MenuItem):
    """Load or save a single preset slot."""

    def __init__(self, label, slot_idx, mode):
        super().__init__(label)
        self._slot = slot_idx
        self._mode = mode   # 'load' or 'save'

    def on_enter(self, mgr):
        status = '(empty)' if mgr.presets.is_empty(self._slot) else 'Has data'
        mgr.show(self.label, status)

    def on_short_press(self, mgr):
        if self._mode == 'save':
            mgr.presets.save_preset(self._slot)
            mgr.show(self.label, 'Saved!')
        else:
            ok = mgr.presets.load_preset(self._slot)
            if ok:
                mgr.show(self.label, 'Loaded!')
                if mgr._led:
                    mgr._led.set_for_mode(mgr.settings.get('keypad_mode'))
            else:
                mgr.show(self.label, '(empty)')


class MenuManager:

    def __init__(self, display, settings, led,
                 note_engine=None, cc_engine=None, midi_out_ports=None,
                 presets=None, progressions=None, calibration=None, mux=None):
        self._display     = display
        self.settings     = settings
        self._led         = led
        self._note_engine = note_engine
        self._cc_engine   = cc_engine
        self._midi_ports  = midi_out_ports or []
        self.presets      = presets
        self.progressions = progressions
        self._calibration = calibration
        self._mux         = mux

        self._stack   = []
        self._current = None
        self._root    = None

        self._build_tree()

    def _build_tree(self):
        s  = self.settings
        ne = self._note_engine

        scale_choices    = [(k, SCALE_NAMES[k]) for k in SCALE_KEYS]
        mode_choices     = [(k, SCALE_NAMES[k]) for k in MODE_KEYS]
        wildcard_choices = [(k, WILDCARD_NAMES[k]) for k in WILDCARD_NAMES]

        def on_scale_change(val):
            if ne:
                ne.set_scale(val)

        def on_mode_change(val):
            if self._led:
                self._on_keypad_mode_change(val)
            if ne:
                ne.all_notes_off()

        # Patch/Bank closures (send MIDI immediately on scroll)
        def send_patch(val):
            ch = s.get('midi_channel', 0)
            for port in self._midi_ports:
                port.write_data(0xC0 | ch, val, 0)

        def send_bank(val):
            ch = s.get('midi_channel', 0)
            for port in self._midi_ports:
                port.write_data(0xB0 | ch, 0, val)

        # CC Assign submenu
        cc_menu = SubMenu('CC Assign', [
            CCAssignItem('Pot 1',   'pot_0'),
            CCAssignItem('Pot 2',   'pot_1'),
            CCAssignItem('Pot 3',   'pot_2'),
            CCAssignItem('Pot 4',   'pot_3'),
            CCAssignItem('Pot 5',   'pot_4'),
            CCAssignItem('Pedal 1', 'pedal_0'),
            CCAssignItem('Pedal 2', 'pedal_1'),
            CCAssignItem('Joy X',   'joy_x'),
            CCAssignItem('Joy Y',   'joy_y', allow_pb=True),
            CCAssignItem('FSR 1',   'fsr_0'),
            CCAssignItem('FSR 2',   'fsr_1'),
            CCAssignItem('Breath',  'breath'),
            CCAssignItem('Sonic',   'sonic'),
        ])

        # Keypad submenu
        keypad_menu = SubMenu('Keypad', [
            ChoiceItem('Scales', 'scale', scale_choices, on_change=on_scale_change),
            ChoiceItem('Modes',  'scale', mode_choices,  on_change=on_scale_change),
            ChoiceItem('Play Style', 'keypad_mode', [
                ('scale',         'Scale Play'),
                ('chord',         'Chords Fixed'),
                ('chord_derived', 'Chords Derived'),
                ('combo',         'Combo'),
                ('combo_c',       'Combo (C)'),
                ('chromatic_lab', 'Chromatic Lab'),
            ], on_change=on_mode_change),
        ])

        # Performance submenu (arp + strum + clock)
        def on_arp_toggle(val):
            if val and s.get('strum_enabled'):
                s._data['strum_enabled'] = False  # mutual exclusion

        def on_strum_toggle(val):
            if val and s.get('arp_enabled'):
                s._data['arp_enabled'] = False  # mutual exclusion

        perf_menu = SubMenu('Performance', [
            ChoiceItem('Arp', 'arp_enabled', [
                (False, 'Off'), (True, 'On'),
            ], on_change=on_arp_toggle),
            ChoiceItem('Arp Pattern', 'arp_pattern', [
                ('up',       'Up'),
                ('down',     'Down'),
                ('updown',   'Up-Down'),
                ('as_played','As Played'),
                ('random',   'Random'),
            ]),
            ChoiceItem('Arp Division', 'arp_division', [
                ('1/4',  '1/4 note'),
                ('1/8',  '1/8 note'),
                ('1/16', '1/16 note'),
                ('1/32', '1/32 note'),
            ]),
            ChoiceItem('Arp Quality', 'arp_quality', [
                ('standard', 'Standard'),
                ('dotted',   'Dotted'),
                ('triplet',  'Triplet'),
            ]),
            ValueItem('Arp Gate', 'arp_gate', 0.10, 1.00, step=0.05,
                      formatter=lambda v: f'Gate {int(v*100)}%'),
            ChoiceItem('Strum', 'strum_enabled', [
                (False, 'Off'), (True, 'On'),
            ], on_change=on_strum_toggle),
            ValueItem('Strum Delay', 'strum_delay_ms', 5, 100, step=5,
                      formatter=lambda v: f'Delay {v}ms'),
            ChoiceItem('Strum Dir', 'strum_direction', [
                ('low_high', 'Low to High'),
                ('high_low', 'High to Low'),
            ]),
            ChoiceItem('Clock Src', 'clock_source', [
                ('internal', 'Internal'),
                ('midi',     'MIDI Clock'),
            ]),
            ValueItem('BPM', 'arp_bpm', 40, 240, step=1,
                      formatter=lambda v: f'{v} BPM'),
        ])

        # Progression submenu
        def do_record_slot(slot_idx):
            def _action(mgr):
                if mgr._note_engine:
                    notes = []
                    for nlist in mgr._note_engine._active.values():
                        notes.extend(nlist)
                    if notes:
                        mgr.progressions.record(slot_idx, notes)
            return _action

        prog_slots_rec = SubMenu('Record', [
            ActionItem(f'Slot {i+1}', do_record_slot(i), f'Slot {i+1} saved')
            for i in range(8)
        ])

        def do_play_slot(slot_idx):
            def _action(mgr):
                notes = mgr.progressions.get_slot(slot_idx)
                if notes and mgr._note_engine:
                    ch  = mgr.settings.get('midi_channel', 0)
                    vel = mgr._note_engine.velocity
                    for note in notes:
                        mgr._note_engine._tx(0x90 | ch, note, vel)
            return _action

        prog_slots_play = SubMenu('Play Slot', [
            ActionItem(f'Slot {i+1}', do_play_slot(i), f'Playing {i+1}')
            for i in range(8)
        ])

        prog_menu = SubMenu('Progression', [
            prog_slots_rec,
            prog_slots_play,
            ActionItem('Clear All', lambda mgr: mgr.progressions.clear_all(), 'Cleared'),
        ])

        # Presets submenu (root level)
        def preset_load_sub():
            return SubMenu('Load Preset', [
                PresetItem(f'PS-{i+1}', i, 'load') for i in range(8)
            ])

        def preset_save_sub():
            return SubMenu('Save Preset', [
                PresetItem(f'PS-{i+1}', i, 'save') for i in range(8)
            ])

        presets_menu = SubMenu('Presets', [
            preset_load_sub(),
            preset_save_sub(),
        ])

        # Patch/Bank
        patch_menu = SubMenu('Patch/Bank', [
            ValueItem('Patch', 'current_patch', 0, 127,
                      formatter=lambda v: f'Patch {v+1:03d}',
                      on_change=send_patch),
            ValueItem('Bank', 'current_bank', 0, 127,
                      formatter=lambda v: f'Bank  {v:03d}',
                      on_change=send_bank),
        ])

        # Config submenu
        def do_calibrate(mgr):
            if mgr._calibration and mgr._mux:
                mgr._calibration.run(mgr._mux, mgr._display)

        config_menu = SubMenu('Config', [
            ValueItem('MIDI Chan',   'midi_channel', 0, 15,
                      formatter=lambda v: f'Channel {v+1:2d}'),
            ChoiceItem('Split Ch',   'split_channel_enabled', [
                (False, 'Off'), (True, 'On'),
            ]),
            ValueItem('Bass Ch',     'split_channel_bass',  0, 15,
                      formatter=lambda v: f'Ch {v+1}'),
            ValueItem('Upper Ch',    'split_channel_upper', 0, 15,
                      formatter=lambda v: f'Ch {v+1}'),
            ChoiceItem('Vel Source', 'velocity_source', [
                ('fixed',  'Fixed vel'),
                ('breath', 'Breath'),
            ]),
            ValueItem('Dflt Vel',    'default_velocity', 1, 127),
            ChoiceItem('Expr Src',   'expression_source', [
                ('breath', 'Breath'),
                ('sonic',  'Ultrasonic'),
                ('off',    'Off'),
            ]),
            ValueItem('Sonic Min',   'sonic_min_cm',  2,  30),
            ValueItem('Sonic Max',   'sonic_max_cm',  30, 200),
            ChoiceItem('Sonic Mode', 'sonic_mode', [
                ('cc',       'CC only'),
                ('theremin', 'Theremin'),
            ]),
            ValueItem('Sonic Dflt', 'sonic_default_cc', 0, 127,
                      formatter=lambda v: f'Default {v}'),
            ValueItem('Theremin Ch', 'theremin_channel', 0, 15,
                      formatter=lambda v: f'Channel {v+1}'),
            ValueItem('FSR Thresh',  'fsr_threshold', 0, 60),
            ValueItem('EMA Pots',    'ema_alpha_pots', 0.05, 0.50, step=0.05,
                      formatter=lambda v: f'Alpha {v:.2f}'),
            ValueItem('LED Bright',  'led_brightness', 10, 255, step=5,
                      formatter=lambda v: f'{v:3d}/255'),
            ChoiceItem('Wildcard',   'wildcard_chord', wildcard_choices),
            patch_menu,
            ActionItem('Calibrate',  do_calibrate, 'Calibrating...'),
            ActionItem('Reset All',  self._do_reset, 'Resetting...'),
        ])

        # Root menu
        self._root = SubMenu('MIDILAB', [
            AdjustItem('Octave',
                get_fn=lambda: s.get('base_octave'),
                set_fn=self._set_octave,
                min_val=-3, max_val=3,
                formatter=lambda v: f'{v:+d} octave'),
            AdjustItem('Transpose',
                get_fn=lambda: s.get('root_note'),
                set_fn=self._set_root,
                min_val=0, max_val=127,
                formatter=self._fmt_root),
            keypad_menu,
            perf_menu,
            prog_menu,
            presets_menu,
            cc_menu,
            config_menu,
        ])

        self._current = self._root
        self._root.on_enter(self)

    # Action helpers

    def _set_octave(self, val):
        if self._note_engine:
            self._note_engine.all_notes_off()
        self.settings._data['base_octave'] = val

    def _set_root(self, val):
        if self._note_engine:
            self._note_engine.all_notes_off()
        self.settings._data['root_note'] = val

    def _fmt_root(self, val):
        from music_theory import note_name
        return note_name(val)

    def _on_keypad_mode_change(self, val):
        if not self._led:
            return
        if val == 'chromatic_lab':
            self._led.start_rainbow(period_ms=6000)
        elif val in ('chord_derived', 'combo', 'combo_c'):
            self._led.start_breathe(period_ms=2000)
        else:
            self._led.set_for_mode(val)

    def _do_reset(self, mgr):
        mgr.settings.reset()
        if self._note_engine:
            self._note_engine.all_notes_off()

    # Navigation

    def navigate_to(self, item):
        self._stack.append(self._current)
        self._current = item
        item.on_enter(self)

    def go_back(self):
        if self._stack:
            self._current = self._stack.pop()
            self._current.on_enter(self)
        else:
            self.go_root()

    def go_root(self):
        self._stack.clear()
        self._current = self._root
        self._root.on_enter(self)

    # Encoder callbacks

    def on_turn(self, delta):
        if self._current:
            self._current.on_scroll(self, delta)

    def on_short_press(self):
        if self._current:
            self._current.on_short_press(self)

    def on_long_press(self):
        if self._current:
            self._current.on_long_press(self)

    # Display helper

    def show(self, line1, line2):
        if self._display:
            self._display.update(line1[:16], str(line2)[:16])

    async def run(self):
        while True:
            await asyncio.sleep_ms(500)

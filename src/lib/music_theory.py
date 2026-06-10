"""
music_theory.py -- MIDILAB V2
Scale definitions, chord voicings, and derived chord engine.
New in V2: derived_chord(), chromatic_lab_chord(), PARALLEL_SCALES.
"""

# Scale interval sets (semitones from root)
SCALES = {
    'major':            (0, 2, 4, 5, 7, 9, 11),
    'natural_minor':    (0, 2, 3, 5, 7, 8, 10),
    'harmonic_minor':   (0, 2, 3, 5, 7, 8, 11),
    'melodic_minor':    (0, 2, 3, 5, 7, 9, 11),
    'major_pentatonic': (0, 2, 4, 7, 9),
    'minor_pentatonic': (0, 3, 5, 7, 10),
    'blues':            (0, 3, 5, 6, 7, 10),
    'dorian':           (0, 2, 3, 5, 7, 9, 10),
    'phrygian':         (0, 1, 3, 5, 7, 8, 10),
    'lydian':           (0, 2, 4, 6, 7, 9, 11),
    'mixolydian':       (0, 2, 4, 5, 7, 9, 10),
    'aeolian':          (0, 2, 3, 5, 7, 8, 10),
    'locrian':          (0, 1, 3, 5, 6, 8, 10),
    'phrygian_dom':     (0, 1, 4, 5, 7, 8, 10),
    'whole_tone':       (0, 2, 4, 6, 8, 10),
    'diminished':       (0, 2, 3, 5, 6, 8, 9, 11),
    'hirajoshi':        (0, 2, 3, 7, 8),
    'hungarian_minor':  (0, 2, 3, 6, 7, 8, 11),
    'chromatic':        (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11),
}

SCALE_NAMES = {
    'major':            'Major',
    'natural_minor':    'Nat Minor',
    'harmonic_minor':   'Harm Minor',
    'melodic_minor':    'Mel Minor',
    'major_pentatonic': 'Maj Pent',
    'minor_pentatonic': 'Min Pent',
    'blues':            'Blues',
    'dorian':           'Dorian',
    'phrygian':         'Phrygian',
    'lydian':           'Lydian',
    'mixolydian':       'Mixolydian',
    'aeolian':          'Aeolian',
    'locrian':          'Locrian',
    'phrygian_dom':     'Phryg Dom',
    'whole_tone':       'Whole Tone',
    'diminished':       'Diminished',
    'hirajoshi':        'Hirajoshi',
    'hungarian_minor':  'Hungarian',
    'chromatic':        'Chromatic',
}

SCALE_KEYS = ['major', 'natural_minor', 'harmonic_minor', 'melodic_minor',
              'major_pentatonic', 'minor_pentatonic', 'blues',
              'whole_tone', 'diminished', 'hirajoshi', 'hungarian_minor',
              'phrygian_dom', 'chromatic']

MODE_KEYS  = ['dorian', 'phrygian', 'lydian', 'mixolydian', 'aeolian', 'locrian']

# Parallel scale for each scale (used in derived chord row 3 -- modal interchange)
PARALLEL_SCALES = {
    'major':            'natural_minor',
    'natural_minor':    'major',
    'harmonic_minor':   'major',
    'melodic_minor':    'major',
    'dorian':           'phrygian',
    'phrygian':         'dorian',
    'lydian':           'mixolydian',
    'mixolydian':       'lydian',
    'aeolian':          'major',
    'locrian':          'major',
    'major_pentatonic': 'minor_pentatonic',
    'minor_pentatonic': 'major_pentatonic',
    'blues':            'major_pentatonic',
    'whole_tone':       'whole_tone',    # self-parallel (symmetric)
    'diminished':       'diminished',   # self-parallel (symmetric)
    'hirajoshi':        'major_pentatonic',
    'hungarian_minor':  'major',
    'phrygian_dom':     'major',
    'chromatic':        'chromatic',
}


def _scale_tone(scale, root_note, degree, deg_offset, base_octave):
    """
    Return MIDI note for scale degree + offset, with octave wrapping.
    Used by derived_chord to stack chord tones within a scale.
    """
    n    = len(scale)
    d    = (degree + deg_offset) % n        # wrap within scale length
    bump = (degree + deg_offset) // n       # integer division gives octave bumps
    return root_note + base_octave * 12 + scale[d] + bump * 12


def derived_chord(scale_name, root_note, degree, row, base_octave):
    """
    Build a 4-voice chord from the active scale at a given degree.

    Unified algorithm: stack every-other scale degree.
    7-note scales  -> stacked thirds  -> major/minor triads  (standard harmony)
    5-note scales  -> stacked fourths -> quartal harmony     (pentatonic chords)
    6-note scales  -> augmented/mixed intervals              (whole tone, blues)

    Rows:
      0 = root position triad + bass doubled at -12
      1 = triad + contextual extension (7th, 9th, or 6th -- best available)
      2 = drop-2 voicing (3rd in bass, adds colour and voice-leading smoothness)
      3 = parallel mode interchange (same degree in the parallel scale)

    Returns list of 4 MIDI note numbers, or empty list if out of range.
    """
    scale = SCALES.get(scale_name, SCALES['major'])

    def tone(offset):
        return _scale_tone(scale, root_note, degree, offset, base_octave)

    chord_root = tone(0)
    third      = tone(2)   # every-other scale degree
    fifth      = tone(4)
    seventh    = tone(6)
    ninth      = tone(8)
    bass       = chord_root - 12

    if row == 0:
        # Root position triad with bass doubled
        notes = [bass, chord_root, third, fifth]

    elif row == 1:
        # Triad + contextual extension (always a scale tone)
        seventh_interval = (seventh - chord_root) % 12
        ninth_interval   = (ninth   - chord_root) % 12

        if ninth_interval == 2:      # major 9th available -- very consonant
            ext = ninth
        elif seventh_interval == 11: # major 7th available
            ext = seventh
        elif seventh_interval == 9:  # add 6th instead of dim7 (sounds better)
            ext = chord_root + 9
        else:
            ext = seventh            # use whatever 7th the scale provides

        notes = [bass, chord_root, third, ext]

    elif row == 2:
        # First inversion (3rd in bass) for voice-leading across adjacent chords.
        # The 3rd in the bass creates a smoother bass line than root position.
        notes = [third - 12, chord_root, fifth, seventh]

    elif row == 3:
        # Parallel mode interchange -- borrow from the parallel scale
        par_name  = PARALLEL_SCALES.get(scale_name, 'natural_minor')
        par_scale = SCALES.get(par_name, SCALES['natural_minor'])

        def par_tone(offset):
            return _scale_tone(par_scale, root_note, degree, offset, base_octave)

        par_root  = par_tone(0)
        par_third = par_tone(2)
        par_fifth = par_tone(4)
        par_bass  = par_root - 12

        notes = [par_bass, par_root, par_third, par_fifth]

    else:
        return []

    # Discard entire chord if any note is out of MIDI range
    if any(n < 0 or n > 127 for n in notes):
        return []

    return notes


# Chromatic Lab chord palette for rows 2-3
# Each tuple: (bass_offset, root, interval2, interval3) relative to column's chromatic root
# Row 2: classic jazz chord qualities
CHROMATIC_LAB_ROW2 = [
    (-12, 0, 4, 11),   # col 0: Maj7
    (-12, 0, 4, 10),   # col 1: Dom7
    (-12, 0, 3, 10),   # col 2: min7
    (-12, 0, 3,  9),   # col 3: m7b5 (half-diminished)
    (-12, 0, 3,  6),   # col 4: dim7 (bass + root + b3 + b5)
    (-12, 0, 4,  8),   # col 5: Augmented
    (-12, 0, 5, 10),   # col 6: Dom7sus4
]

# Row 3: extensions and alternatives
CHROMATIC_LAB_ROW3 = [
    (-12, 0, 4, 14),   # col 0: Add9    (major triad + 9th, no 7th)
    (-12, 0, 3, 14),   # col 1: min9    (minor triad + 9th)
    (-12, 0, 4, 11),   # col 2: Maj7    (same as row2 col0 -- different chromatic root)
    (-12, 0, 2,  7),   # col 3: Sus2
    (-12, 0, 4, 21),   # col 4: 13th    (bass + root + 3rd + 13th, very open)
    (-12, 0, 4, 11+4), # col 5: AugMaj7 (0, 4, 8, 11 -- augmented with major 7th)
    (-12, 0, 5,  7),   # col 6: Sus4
]


def chromatic_lab_chord(sub_row, col, col_root_note):
    """
    Return 4-voice chord for chromatic lab rows 2-3.
    sub_row: 0 = physical row 2, 1 = physical row 3
    col: 0-6
    col_root_note: MIDI note of this column's chromatic root
    """
    palette = CHROMATIC_LAB_ROW2 if sub_row == 0 else CHROMATIC_LAB_ROW3
    if col >= len(palette):
        return []

    intervals = palette[col]
    notes = [col_root_note + iv for iv in intervals]

    if any(n < 0 or n > 127 for n in notes):
        return []

    return notes


# Fixed chord rows (unchanged from V1, used by 'chord' mode)
CHORD_ROWS = [
    # Row 0: diatonic triads, root position, bass doubled
    [
        ( 0, (-12, 0,  4, 7)),   ( 2, (-12, 0,  3, 7)),
        ( 4, (-12, 0,  3, 7)),   ( 5, (-12, 0,  4, 7)),
        ( 7, (-12, 0,  4, 7)),   ( 9, (-12, 0,  3, 7)),
        (11, (-12, 0,  3, 6)),
    ],
    # Row 1: seventh chords, drop-2 voicing (5th below, then root+3rd+7th)
    [
        ( 0, (-5,  0,  4, 11)),  ( 2, (-5,  0,  3, 10)),
        ( 4, (-5,  0,  3, 10)),  ( 5, (-5,  0,  4, 11)),
        ( 7, (-5,  0,  4, 10)),  ( 9, (-5,  0,  3, 10)),
        (11, (-6,  0,  3,  9)),
    ],
    # Row 2: parallel quality swap, same register as row 0
    [
        ( 0, (-12, 0,  3, 7)),   ( 2, (-12, 0,  4, 7)),
        ( 4, (-12, 0,  4, 7)),   ( 5, (-12, 0,  3, 7)),
        ( 7, (-12, 0,  3, 7)),   ( 9, (-12, 0,  4, 7)),
        (11, (-12, 0,  4, 8)),
    ],
    # Row 3: non-diatonic borrowed chords, 4 voices throughout
    [
        (10, (-12, 0,  4,  7)),  ( 8, (-12, 0,  4,  7)),
        ( 3, (-12, 0,  4,  7)),  ( 1, (-12, 0,  4,  7)),
        ( 5, (-12, 0,  4, 10)),  ( 7, (-12, 0,  5, 10)),
        ( 0, (-12, 0,  4,  8)),
    ],
]

CHORD_ROW_NAMES = [
    ['I', 'ii', 'iii', 'IV', 'V', 'vi', 'vii'],
    ['IΔ7', 'ii7', 'iii7', 'IVΔ7', 'V7', 'vi7', 'viiø7'],
    ['i', 'II', 'III', 'iv', 'v', 'VI', 'VII+'],
    ['bVII', 'bVI', 'bIII', 'bII', 'IV7', 'V7s4', '\u2605'],
]

WILDCARD_CHORDS = {
    'aug':  (0, (-12, 0,  4,  8)),
    'sus2': (0, (-12, 0,  2,  7)),
    'sus4': (0, (-12, 0,  5,  7)),
    'dom7': (0, (-12, 0,  4, 10)),
    'dim7': (0, (-12, 0,  3,  9)),
    'add9': (0, ( -5, 0,  4, 14)),
}

WILDCARD_NAMES = {
    'aug': 'I Aug', 'sus2': 'I Sus2', 'sus4': 'I Sus4',
    'dom7': 'I Dom7', 'dim7': 'I Dim7', 'add9': 'I Add9',
}

NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']

def note_name(midi_note):
    return NOTE_NAMES[midi_note % 12] + str(midi_note // 12 - 1)

def cc_display(cc_num):
    CC_NAMES = {
        1:'Mod Wheel', 2:'Breath', 7:'Ch Volume', 10:'Pan',
        11:'Expression', 16:'General 1', 17:'General 2',
        64:'Sustain', 71:'Resonance', 73:'Attack',
        74:'Filt Cut', 84:'Portamento', 123:'All Notes Off',
    }
    name = CC_NAMES.get(cc_num, '')
    return f'CC:{cc_num:03d} {name[:9]}' if name else f'CC:{cc_num:03d}'

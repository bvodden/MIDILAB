# MIDILAB

A handbuilt USB/DIN MIDI controller built on the Raspberry Pi Pico 2W (RP2350).
MIDILAB is a performance instrument combining a 28-key isomorphic grid keyboard,
analog expression controls, and a deep menu-driven configuration system.
Outputs simultaneous USB MIDI and DIN MIDI 5-pin.

---

## Features

### Keyboard
- 28-key isomorphic grid (4 rows × 7 columns)
- Six play styles:
  - **Scale Play** — any of 19 built-in scales and modes
  - **Chords Fixed** — pre-voiced diatonic chord rows
  - **Chords Derived** — chord voicings calculated live from the active scale
  - **Combo** — derived chords on rows 0-1, scale melody on rows 2-3
  - **Combo (C)** — derived chords on rows 0-1, chromatic melody on rows 2-3
  - **Chromatic Lab** — chromatic melody rows with jazz chord palette rows
- Transpose and octave shift
- Multi-channel split (row 0 on separate MIDI channel from rows 1-3)

### Performance
- **Arpeggiator** — Up, Down, Up-Down, As Played, Random patterns;
  1/4 through 1/32 note divisions; Standard, Dotted, Triplet quality;
  configurable gate and BPM; MIDI clock sync
- **Strum** — configurable inter-note delay and direction (low→high or high→low)
- **Chord Progression Memory** — 8 recordable chord slots
- **Presets** — 8 full settings snapshots (PS-1 through PS-8)
- **Panic** — encoder button + joystick button held silences all 16 MIDI channels

### Expression Controls
- **Ultrasonic sensor (HC-SR04)** — hand distance to CC expression or theremin pitch mode
- **Breath sensor (MPXV4006DP)** — pressure-to-CC with LED brightness feedback
- **2× FSR (force sensitive resistor)** — velocity-sensitive pressure pads
- **Joystick** — pitch bend (Y axis) and mod wheel (X axis) with dead zone
- **Expression pedal input** — TRS jack, configurable CC assignment
- All CC assignments configurable per source via menu

### MIDI Output
- USB MIDI (class compliant, Windows/Mac/Linux)
- DIN MIDI 5-pin simultaneous output
- All MIDI channels 1-16 accessible
- Program change and bank select

### Interface
- 16×2 LCD display with encoder navigation
- RGB LED mode indicator with animated modes (breathing, rainbow)
- Encoder with short press / long press / rotation
- Auto-calibration on first boot

---

## Hardware

| Component | Details |
|-----------|---------|
| Microcontroller | Raspberry Pi Pico 2W (RP2350) |
| Key matrix | 2× PCF8575 I²C expanders, 28 switches active LOW |
| Analog MUX | CD74HC4067 16-channel, MCP1700-3302E analog LDO |
| Display | 16×2 LCD with PCF8574 I²C backpack |
| Distance sensor | HC-SR04 ultrasonic (5V, ECHO voltage divider) |
| Breath sensor | MPXV4006DP differential pressure (4.7kΩ/10kΩ divider) |
| FSR | SEN0297 force sensitive resistors ×2 |
| Encoder | MD-15141 rotary encoder with RGB LED (common anode) |
| Joystick | 2-axis with button |
| USB | USB-C panel mount with integrated 5.1kΩ CC resistors |
| DIN MIDI | 5-pin DIN socket, 2× 220Ω resistors, GP4 UART TX |

---

## Installation

### Requirements
- Raspberry Pi Pico 2W
- MicroPython v1.27.0 for RP2350
  (download: https://micropython.org/download/RPI_PICO2_W/)
- Thonny IDE (https://thonny.org)

### MicroPython Setup
1. Hold BOOTSEL on Pico while connecting USB
2. Drag MicroPython UF2 onto the RPI-RP2 drive
3. Pico reboots into MicroPython

### File Installation
All files except `main.py` go into `/lib/` on the Pico.
`main.py` goes in the root `/`.

**Application files** (place in `/lib/`):
`arp_engine.py`, `calibration.py`, `cc_engine.py`, `config.py`,
`display.py`, `encoder.py`, `led.py`, `memory.py`, `menu.py`,
`midi_port.py`, `music_theory.py`, `note_engine.py`, `pcf8575.py`,
`settings.py`, `theremin.py`

**Multi-midi library** (place in `/lib/`):
`midi_manager.py`, `midi_usb.py`, `singleton.py`, `context.py`

**Third-party libraries** (place in `/lib/`):
`hcsr04.py`, `lcd_api.py`, `machine_i2c_lcd.py`

### First Boot
On first boot with no `calibration.json` present, MIDILAB runs
auto-calibration automatically. Leave all controls at rest for
approximately 3 seconds while "Calibrating..." is displayed.

A `settings.json` file is generated automatically with default values.
Delete `settings.json` to reset all settings to defaults.

---

## Menu Navigation

- **Rotate encoder** — scroll through options / change values
- **Short press** — select / confirm
- **Long press** — go back
- **Encoder + Joystick button held 500ms** — PANIC (all notes off, all channels)

Root menu items: Octave, Transpose, Keypad, Performance, Progression,
Presets, CC Assign, Config

---

## Known Issues

- **Android USB MIDI** — MIDILAB enumerates correctly on Android but MIDI
  data does not flow. Root cause is in the multi-midi library's USB
  descriptor (7-byte vs 9-byte endpoint descriptors). A partial fix is
  included in `midi_usb.py` but full Android compatibility is not yet
  achieved. Windows and macOS work correctly. DIN MIDI works on all devices.
- **Arpeggiator timing jitter** — asyncio scheduler drift causes occasional
  late notes, most noticeable at fast tempos (1/16 and 1/32). A PIO
  hardware timer implementation is planned for V5.
- **Ultrasonic sensor noise** — HC-SR04 readings bounce at distances beyond
  ~20cm. Keep `sonic_max_cm` at 20-25 for best stability. EMA smoothing
  planned for V5.
- **Breath sensor latency** — MPXV4006DP through silicone tubing has
  ~50-100ms effective latency. Fast articulative breath playing is not
  currently practical.

---

## Roadmap

### V5 (in development)
- PIO hardware timer for arp clock (eliminates timing jitter)
- Drum machine play mode with accent/ghost velocity rows
- Software LFO (sine/triangle/saw/square/S&H, tempo sync or free running)
- CC step sequencer (8 steps, advances on arp clock)
- `@micropython.native` and `@micropython.viper` performance optimizations
- Improved GC strategy
- Ultrasonic EMA smoothing
- Breath sensor fast scan (200Hz)

### MK2 Hardware
- Bare RP2350 PCB (no Pico module)
- Motherboard + front panel daughterboard with IDC ribbon
- OLED display, two encoders, dedicated transport buttons
- Two independent range sensors (VL53L1X LIDAR) for 2D theremin
- Velocity-sensitive piezo drum pads ("Cajon pads")
- Expansion headers for accessories
- MIDI IN with optocoupler, MIDI Thru/Merge
- NeoPixel encoder rings
- Looper/clip launcher integration
- Conformal coating, Neutrik jacks, TVS protection diodes

### Accessories (planned)
- **Wand** — handheld baton with BNO055 IMU for 3D gesture control
- **Tricorder** — scanning device with LIDAR, color sensor, microphone,
  and light sensor for live performance interaction

---

## License

MIT License — see LICENSE file.

Hardware designs (schematics, PCB) will be released under
Creative Commons Attribution-ShareAlike 4.0 (CC-BY-SA 4.0) for MK2.
"""
midi_test_minimal.py -- MIDILAB MIDI connectivity test
Safe version: 5 second window at boot before MIDI takes USB.
Connect Thonny within 5 seconds of plugging in to regain REPL access.
LED colors: white=safe window, blue=waiting for USB, green=sending notes, red=error
"""

import time
from machine import Pin

LED_R = 16
LED_G = 18
LED_B = 17

def led(r, g, b):
    for pin, val in ((LED_R, r), (LED_G, g), (LED_B, b)):
        Pin(pin, Pin.OUT).value(0 if val else 1)

# WHITE for 5 seconds -- connect Thonny NOW if you need REPL access
led(1, 1, 1)
time.sleep(5)

import sys
sys.path.insert(0, '/lib')

led(0, 0, 1)  # blue = starting MIDI

try:
    from midi_manager import MidiManager
    midi = MidiManager()
    midi.set_usb_strings('MIDILAB', 'MIDILAB', None)
    midi.add_usb_out(0, 'MIDILAB')

    import asyncio

    async def main():
        await midi.run()
        led(0, 1, 0)  # green = connected

        note    = 60
        channel = 0
        count   = 0

        while True:
            midi.out_ports[0].write_data(0x90 | channel, note, 100)
            await asyncio.sleep_ms(100)
            midi.out_ports[0].write_data(0x80 | channel, note, 0)
            count += 1
            note = 60 + (count % 12)
            await asyncio.sleep_ms(1900)

    asyncio.run(main())

except Exception as e:
    led(1, 0, 0)  # red = error
    import sys as _sys
    _sys.print_exception(e)
    time.sleep(60)  # keep REPL available after error

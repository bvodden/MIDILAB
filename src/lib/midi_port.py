"""
midi_port.py -- MIDILAB V2
Wrapper around usb.device.midi.MIDIInterface that provides the
write_data(status, data1, data2) interface used throughout MIDILAB.

Replaces multi-midi's USB output entirely. multi-midi is no longer
imported -- usb-device-midi and multi-midi both configure the same USB
hardware and will conflict if both are initialised.

When DIN MIDI is reconnected, use MicroPython's built-in UART directly
rather than importing multi-midi.

Installation (run once in REPL or via mpremote):
    import mip; mip.install('usb-device-midi')
OR from PC terminal:
    mpremote mip install usb-device-midi

builtin_driver=False: MIDI only, no REPL (production mode)
builtin_driver=True:  MIDI + REPL as composite device (development mode)
Android compatibility requires False -- composite devices confuse Android's
stricter USB MIDI service.
"""

import usb.device
from usb.device.midi import MIDIInterface


class MIDIUSBPort:
    """
    Wraps MIDIInterface to provide write_data(status, data1, data2).
    Translates raw MIDI status bytes to USB MIDI 4-byte event packets.

    USB MIDI packet format (4 bytes per message):
      Byte 0: (cable_number << 4) | CIN
      Byte 1: MIDI status byte
      Byte 2: first data byte
      Byte 3: second data byte (0 for 2-byte messages)

    CIN (Code Index Number) matches the upper nibble of the status byte
    for all channel messages, which covers everything MIDILAB sends.
    """

    def __init__(self, iface):
        self._iface  = iface
        self._buf    = bytearray(4)  # pre-allocated -- no heap allocation per message

    def write_data(self, status, data1=0, data2=0):
        if not self._iface.is_open():
            return
        # CIN = upper nibble of status byte (cable 0 assumed throughout)
        self._buf[0] = (status >> 4) & 0x0F
        self._buf[1] = status
        self._buf[2] = data1
        self._buf[3] = data2
        try:
            self._iface.send_event(self._buf)
        except Exception:
            pass   # host disconnected mid-send -- ignore silently

    @property
    def is_open(self):
        return self._iface.is_open()


def init_usb_midi(builtin_driver=False, timeout_ms=5000):
    """
    Initialise usb-device-midi and wait for USB host to connect.
    Returns (MIDIInterface instance, MIDIUSBPort wrapper).

    builtin_driver=False removes the MicroPython REPL from the USB
    descriptor -- required for Android compatibility. Set True for
    development sessions where REPL access is needed.

    Raises RuntimeError if host does not connect within timeout_ms.
    """
    import time

    iface = MIDIInterface()
    usb.device.get().init(iface, builtin_driver=builtin_driver)

    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while not iface.is_open():
        if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
            raise RuntimeError('USB host did not connect within timeout')
        time.sleep_ms(50)

    return iface, MIDIUSBPort(iface)

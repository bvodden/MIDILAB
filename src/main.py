"""
main.py -- MIDILAB V4
Multi-midi restored as USB backend (usb-device-midi incompatible with RP2350 v1.27).
All other V3 improvements retained: pre-allocation, GC task, joystick fixes,
FSR curve, fast scan task, arp pool, Android 2s delay kept (harmless on Windows).
"""

import sys
import io
import time
import machine
import asyncio
from machine import I2C, Pin

try:
    cause = machine.reset_cause()
    with open('reset_cause.log', 'a') as f:
        f.write(str(cause) + '\n')
except Exception:
    pass

sys.path.insert(0, '/lib')

from config import (
    I2C0_ID, I2C0_SDA, I2C0_SCL, I2C0_FREQ,
    I2C1_ID, I2C1_SDA, I2C1_SCL, I2C1_FREQ,
    LCD_ADDR, ENC_A, ENC_B, ENC_BTN, JOY_BTN,
    LED_R, LED_G, LED_B,
)
from settings     import Settings
from music_theory import SCALE_NAMES
from led          import RGBLED, GREEN, DIM_BLUE
from display      import Display
from encoder      import RotaryEncoder
from pcf8575      import init_expanders, key_scan_task
from note_engine  import NoteEngine
from cc_engine    import MuxADC, CCEngine, adc_scan_task, fast_scan_task, hcsr04_task
from arp_engine   import ArpEngine, arp_task
from menu         import MenuManager
from memory       import PresetManager, ProgressionManager
from calibration  import Calibration
from theremin     import ThereminController


async def _gc_task():
    import gc
    while True:
        await asyncio.sleep_ms(10_000)
        gc.collect()


async def main():

    settings = Settings()
    settings.load()

    i2c0 = I2C(I2C0_ID, sda=Pin(I2C0_SDA), scl=Pin(I2C0_SCL), freq=I2C0_FREQ)
    i2c1 = I2C(I2C1_ID, sda=Pin(I2C1_SDA), scl=Pin(I2C1_SCL), freq=I2C1_FREQ)

    led = RGBLED(LED_R, LED_G, LED_B)
    led.set(DIM_BLUE)

    display = Display(i2c1, LCD_ADDR)
    display.update('MIDILAB V4', 'Booting...')

    # DIN MIDI initialized FIRST -- before USB so it's guaranteed in out_ports
    # regardless of USB MIDI outcome. UART needs no handshake, always works.
    from machine import UART
    _uart = UART(1, baudrate=31250, tx=Pin(4), rx=Pin(9))

    class _UARTMIDIPort:
        def __init__(self, u):
            self._uart = u
            self._buf  = bytearray(3)
        def write_data(self, status, data1=0, data2=0):
            self._buf[0] = status
            self._buf[1] = data1
            self._buf[2] = data2
            if (status & 0xF0) in (0xC0, 0xD0):
                self._uart.write(self._buf[:2])
            else:
                self._uart.write(self._buf)

    _din_port = _UARTMIDIPort(_uart)

    # USB MIDI via multi-midi -- appended after DIN so both ports receive all data
    out_ports = [_din_port]
    try:
        from midi_manager import MidiManager
        midi = MidiManager()
        midi.set_usb_strings('MIDILAB', 'MIDILAB', None)
        midi.add_usb_out(0, 'MIDILAB')
        await midi.run()
        out_ports += midi.out_ports   # add USB port(s) alongside DIN

        display.update('MIDILAB V4', 'Connecting...')
        deadline = time.ticks_add(time.ticks_ms(), 10_000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                out_ports[0].write_data(0xB0, 123, 0)
                break
            except Exception:
                await asyncio.sleep_ms(100)

        await asyncio.sleep_ms(1000)
        print('[main] MIDI ready,', len(out_ports), 'ports')

    except Exception as e:
        display.update('MIDI error', str(e)[:16])
        print('[main] MIDI error:', e)
        # DIN still works even if USB fails -- out_ports already has _din_port
    try:
        import network
        network.WLAN(network.STA_IF).active(False)
        network.WLAN(network.AP_IF).active(False)
    except Exception:
        pass

    await asyncio.sleep_ms(500)

    arp_engine   = ArpEngine()
    presets      = PresetManager(settings)
    progressions = ProgressionManager()
    calibration  = Calibration()
    exp0, exp1   = init_expanders(i2c0)
    mux          = MuxADC()

    note_engine = NoteEngine(out_ports, settings, arp_engine=arp_engine)
    cc_engine   = CCEngine(out_ports, settings, note_engine, led)

    # Wire theremin controller -- plays on theremin_channel, separate from keyboard
    note_engine.theremin = ThereminController(
        tx_fn    = note_engine._tx,
        settings = settings,
    )

    menu = MenuManager(
        display        = display,
        settings       = settings,
        led            = led,
        note_engine    = note_engine,
        cc_engine      = cc_engine,
        midi_out_ports = out_ports,
        presets        = presets,
        progressions   = progressions,
        calibration    = calibration,
        mux            = mux,
    )

    def on_panic():
        note_engine.all_notes_off_all_channels()
        if note_engine.theremin:
            note_engine.theremin.silence(
                channel=settings.get('theremin_channel', 0))
        asyncio.create_task(led.error_flash(count=3))
        display.update('PANIC', 'All notes off')

    def _safe_turn(delta):
        try:
            menu.on_turn(delta)
        except Exception as e:
            display.update('menu err', str(e)[:16])

    def _safe_short():
        try:
            menu.on_short_press()
        except Exception as e:
            display.update('short err', str(e)[:16])

    def _safe_long():
        try:
            menu.on_long_press()
        except Exception as e:
            display.update('long err', str(e)[:16])

    encoder = RotaryEncoder(
        pin_a          = ENC_A,
        pin_b          = ENC_B,
        pin_btn        = ENC_BTN,
        on_turn        = _safe_turn,
        on_short_press = _safe_short,
        on_long_press  = _safe_long,
        joy_pin        = JOY_BTN,
        on_panic       = on_panic,
    )

    scale_name = SCALE_NAMES.get(settings.get('scale'), settings.get('scale'))
    display.update('MIDILAB V4', scale_name)
    led.set(GREEN)
    await asyncio.sleep_ms(300)
    led.set_for_mode(settings.get('keypad_mode'))

    asyncio.create_task(key_scan_task(exp0, exp1, note_engine))
    asyncio.create_task(adc_scan_task(mux, cc_engine, settings))
    asyncio.create_task(fast_scan_task(mux, cc_engine, settings))
    asyncio.create_task(hcsr04_task(cc_engine))
    asyncio.create_task(encoder.run())
    asyncio.create_task(menu.run())
    asyncio.create_task(arp_task(note_engine, arp_engine, settings))
    asyncio.create_task(_gc_task())

    print('[main] All tasks running. MIDILAB V4 ready.')

    await asyncio.sleep_ms(1000)
    try:
        import os
        os.stat('calibration.json')
    except OSError:
        display.update('First boot', 'Calibrating...')
        calibration.run(mux, display)
        display.update('MIDILAB V4', scale_name)

    await asyncio.Event().wait()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('[main] Stopped.')
except Exception as e:
    buf = io.StringIO()
    sys.print_exception(e, buf)
    try:
        with open('crash.log', 'w') as f:
            f.write(buf.getvalue())
    except Exception:
        pass

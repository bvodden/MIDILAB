"""
display.py — MIDILAB
LCD 1602 (I²C PCF8574 backpack) wrapper.

LCD writes are offloaded to Core 1 via _thread so they never
block the Core 0 asyncio MIDI event loop.

Requires: dhylands/python_lcd library
  lcd_api.py + machine_i2c_lcd.py in the project directory.
  https://github.com/dhylands/python_lcd

Usage:
    display = Display(i2c1, LCD_ADDR)
    display.update('MIDILAB', 'Scale: Major')
"""

import _thread
import time


class Display:

    def __init__(self, i2c, addr, rows=2, cols=16):
        self._rows = rows
        self._cols = cols
        self._l1   = 'MIDILAB'
        self._l2   = 'Starting...'
        self._dirty = True
        self._running = True
        self._lcd = None

        try:
            from machine_i2c_lcd import I2cLcd
            self._lcd = I2cLcd(i2c, addr, rows, cols)
            self._lcd.backlight_on()
            # Launch display thread on Core 1
            _thread.start_new_thread(self._core1_loop, ())
        except Exception as e:
            print('[Display] LCD init failed:', e)
            print('[Display] Display updates will be printed to REPL instead.')

    def _pad(self, s):
        s = str(s)[:self._cols]
        return s + ' ' * (self._cols - len(s))  # manual pad — ljust not in all MicroPython builds

    def _core1_loop(self):
        """Runs on Core 1. Writes to LCD whenever content changes."""
        while self._running:
            if self._dirty and self._lcd:
                l1 = self._l1
                l2 = self._l2
                self._dirty = False
                try:
                    self._lcd.clear()
                    self._lcd.move_to(0, 0)
                    self._lcd.putstr(self._pad(l1))
                    self._lcd.move_to(0, 1)
                    self._lcd.putstr(self._pad(l2))
                except Exception:
                    pass   # silently ignore transient I²C errors
            time.sleep_ms(30)

    def update(self, line1, line2=''):
        """
        Set display content. Returns immediately — Core 1 does the write.
        Safe to call from Core 0 asyncio tasks.
        Strings are truncated to LCD column width.
        """
        self._l1    = str(line1)
        self._l2    = str(line2)
        self._dirty = True
        if self._lcd is None:
            # Fallback: print to REPL
            print(f'[LCD] {line1} | {line2}')

    def splash(self, line1='MIDILAB', line2='Ready'):
        self.update(line1, line2)

    def stop(self):
        """Clean shutdown — stop Core 1 loop."""
        self._running = False

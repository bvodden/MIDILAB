"""
encoder.py -- MIDILAB V2
Quadrature encoder with short/long press detection.

Changes from V1:
  Pin.PULL_UP removed from A and B pins -- external 2.2k pull-ups now fitted.
  Panic detection added: joystick button + encoder button held 500ms together
  triggers on_panic callback.
  Button polled at 100Hz (no IRQ) to avoid USB disruption.
  Detent accumulator added: 4 ISR pulses = 1 physical click.
"""

import asyncio
from machine import Pin
import time

LONG_PRESS_MS  = 600
PANIC_HOLD_MS  = 500


class RotaryEncoder:
    """
    IRQ-driven quadrature decoder with polled button and panic detection.

    Encoder A and B: no internal pull-ups (external 2.2k fitted on hardware).
    Button GP15: PULL_DOWN, active HIGH (encoder button).
    Panic: encoder button + joystick button (joy_pin, active LOW) held together.
    """

    # Full 16-entry state table. Index = (last_a << 3 | last_b << 2 | a << 1 | b).
    # Returns -1 (CCW), +1 (CW), or 0 (no change).
    _FULL_STATE = [0, -1,  1, 0,
                   1,  0,  0,-1,
                  -1,  0,  0, 1,
                   0,  1, -1, 0]

    def __init__(self, pin_a, pin_b, pin_btn,
                 on_turn=None, on_short_press=None, on_long_press=None,
                 joy_pin=None, on_panic=None):
        # No PULL_UP on A and B -- external 2.2k pull-ups are fitted
        self._a   = Pin(pin_a,   Pin.IN)
        self._b   = Pin(pin_b,   Pin.IN)
        self._btn = Pin(pin_btn, Pin.IN, Pin.PULL_DOWN)  # active HIGH

        self._on_turn        = on_turn
        self._on_short_press = on_short_press
        self._on_long_press  = on_long_press
        self._on_panic       = on_panic

        # Joystick button for panic detection (active LOW, pull-up)
        self._joy_btn = Pin(joy_pin, Pin.IN, Pin.PULL_UP) if joy_pin is not None else None

        # Encoder ISR state
        self._la           = self._a.value()
        self._lb           = self._b.value()
        self._accum        = 0    # raw ISR pulse accumulator
        self._detent_accum = 0    # partial detents between polls

        # Button poll state
        self._last_btn   = 0
        self._press_time = 0
        self._long_fired = False

        # Panic state
        self._panic_start  = 0
        self._panic_held   = False
        self._panic_fired  = False

        self._flag = asyncio.ThreadSafeFlag()

        # IRQs on A and B only -- button is polled, not IRQ-driven
        self._a.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=self._enc_irq)
        self._b.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=self._enc_irq)

    def _enc_irq(self, _pin):
        """
        Encoder ISR. Decodes quadrature via 16-entry table, accumulates delta.
        Runs at interrupt time -- must be short and allocation-free.
        """
        a   = self._a.value()
        b   = self._b.value()
        idx = (self._la << 3) | (self._lb << 2) | (a << 1) | b  # pack 4 bits into table index
        self._accum += self._FULL_STATE[idx]
        self._la = a
        self._lb = b
        self._flag.set()

    async def run(self):
        """
        Main async task. Drains encoder pulses into detents and polls button.
        Runs at 100Hz for button polling; encoder movement wakes it earlier via flag.
        """
        while True:
            # Convert ISR pulses to detents (4 pulses per physical click on EC11)
            self._detent_accum += self._accum
            self._accum = 0
            steps = self._detent_accum // 4   # whole detents ready
            self._detent_accum -= steps * 4   # keep remainder
            if steps and self._on_turn:
                self._on_turn(steps)

            btn = self._btn.value()   # 1 = pressed, 0 = not pressed

            # Panic check (both buttons held simultaneously)
            if self._joy_btn is not None:
                joy = not self._joy_btn.value()  # active LOW: invert to get pressed=True
                both = btn and joy
                if both:
                    if not self._panic_held:
                        self._panic_held  = True
                        self._panic_fired = False
                        self._panic_start = time.ticks_ms()
                    elif not self._panic_fired:
                        held = time.ticks_diff(time.ticks_ms(), self._panic_start)
                        if held >= PANIC_HOLD_MS:
                            self._panic_fired = True
                            if self._on_panic:
                                self._on_panic()
                else:
                    self._panic_held  = False
                    self._panic_fired = False

            # Normal button handling (only when panic is not active)
            if not self._panic_held:
                if btn and not self._last_btn:
                    self._press_time = time.ticks_ms()
                    self._long_fired = False

                elif not btn and self._last_btn:
                    if not self._long_fired and self._on_short_press:
                        self._on_short_press()

                elif btn and self._last_btn and not self._long_fired:
                    held_ms = time.ticks_diff(time.ticks_ms(), self._press_time)
                    if held_ms >= LONG_PRESS_MS:
                        self._long_fired = True
                        if self._on_long_press:
                            self._on_long_press()

            self._last_btn = btn
            await asyncio.sleep_ms(10)  # 100Hz poll rate

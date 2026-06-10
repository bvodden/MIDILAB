"""
pcf8575.py -- MIDILAB V2
PCF8575 I2C I/O expander driver and key scan task.

Performance change: scan list pre-allocated as [0, 0] at task start.
Values written in-place each cycle -- no list creation in the hot path.
"""

import asyncio
from machine import Pin, I2C

_VALID_MASK = 0x7FFE  # bits 1-14 only; P00 and P17 not wired to switches

def _build_bit_map():
    """Build bit-position to (row, col) lookup table at import time."""
    table = []
    for exp in range(2):
        for bit in range(16):
            if bit == 0 or bit == 15:
                table.append(None)
            elif 1 <= bit <= 7:
                table.append((exp * 2, bit - 1))
            else:
                table.append((exp * 2 + 1, 14 - bit))
    return table

BIT_MAP = _build_bit_map()  # module-level: runs once at import


class PCF8575:
    """Minimal driver for the PCF8575 16-bit I2C I/O expander."""

    def __init__(self, i2c, addr):
        self._i2c  = i2c
        self._addr = addr
        self._buf  = bytearray(2)  # pre-allocated read buffer -- no allocation per read
        self._i2c.writeto(self._addr, b'\xFF\xFF')  # all pins input with pull-ups

    def read_port(self):
        """Read all 16 pins as a single integer. No heap allocation."""
        self._i2c.readfrom_into(self._addr, self._buf)  # writes into existing buffer
        return self._buf[0] | (self._buf[1] << 8)


_key_flag = asyncio.ThreadSafeFlag()


def _key_irq(_pin):
    """ISR -- sets flag only, no allocation."""
    _key_flag.set()


def init_expanders(i2c0):
    from config import PCF8575_ADDR_0, PCF8575_ADDR_1, PCF8575_INT
    exp0    = PCF8575(i2c0, PCF8575_ADDR_0)
    exp1    = PCF8575(i2c0, PCF8575_ADDR_1)
    int_pin = Pin(PCF8575_INT, Pin.IN, Pin.PULL_UP)
    int_pin.irq(trigger=Pin.IRQ_FALLING, handler=_key_irq)
    return exp0, exp1


async def key_scan_task(exp0, exp1, note_engine):
    """
    Wakes on PCF8575 interrupt, reads both expanders, diffs state,
    dispatches note_on/note_off events.

    Performance: 'current' list is pre-allocated and reused each cycle.
    'last' list is also pre-allocated. No list creation in the hot path.
    """
    # Pre-allocate both state lists -- written in-place each cycle
    last    = [exp0.read_port(), exp1.read_port()]
    current = [0, 0]

    while True:
        await _key_flag.wait()

        # Write into pre-allocated list -- no allocation
        current[0] = exp0.read_port()
        current[1] = exp1.read_port()

        for exp_idx in range(2):
            changed = (current[exp_idx] ^ last[exp_idx]) & _VALID_MASK
            if not changed:
                continue

            bit = 1
            while bit <= 0x4000:
                if changed & bit:
                    bit_pos = 0
                    tmp     = bit
                    while tmp > 1:
                        tmp    >>= 1
                        bit_pos += 1
                    mapping = BIT_MAP[exp_idx * 16 + bit_pos]
                    if mapping is not None:
                        row, col = mapping
                        pressed  = not bool(current[exp_idx] & bit)
                        if pressed:
                            note_engine.note_on(row, col)
                        else:
                            note_engine.note_off(row, col)
                bit <<= 1

            last[exp_idx] = current[exp_idx]

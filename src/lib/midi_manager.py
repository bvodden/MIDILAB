''' Asynchronous MIDI I/O manager module for multi-port UART, PIO and USB MIDI on RP2040/RP2350 based boards

    Primary interface of the Multi-Midi library: https://github.com/HLammers/multi-midi

    Copyright (c) 2025 Harm Lammers

    See READYME.md and example.py for how to use.

    The base of the PIO code is taken from:
    - Simple MIDI Multi-RX-TX Router, copyright (c) 2023 diyelectromusic (Kevin),
      https://github.com/diyelectromusic/, https://diyelectromusic.com/
    which took it from:
    - https://github.com/micropython/micropython/blob/master/examples/rp2/pio_uart_rx.py
    - https://github.com/micropython/micropython/blob/master/examples/rp2/pio_uart_tx.py

    MIT licence:

    Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
'''

import rp2
from machine import Pin, UART
import micropython
import builtins
import asyncio
import gc

from singleton import singleton
try:
    from midi_usb import MidiUSB
except:
    pass # allow the midi_usb module to be missing (for applications which only use hardware MIDI)

if __debug__:
    try:
        from log import Log # pyright: ignore[reportMissingImports, reportAssignmentType]
    except:
        class Log:
            ''' Minimalist logger for if the log module is not avaialble '''

            def write(self, msg: str|bytes|bytearray) -> None:
                ''' Print message '''
                print(msg if type(msg) == str else msg.decode('ascii')) # pyright: ignore[reportAttributeAccessIssue]

_NONE               = const(-1)

_UART_BAUD          = const(31_250) # fixed baud rate for MIDI

_UART_READ_BUF_SIZE = const(16) # size of buffer used to read into from UART stream
_SYSEX_R_BUF_SIZE   = const(1032) # size of each IN port’s buffer for MIDI SysEx messages (size needs to be multiple of 3)
_RT_W_BUF_SIZE      = const(32) # size of each OUT port’s MIDI Real-Time messages queue
_SYSEX_W_BUF_SIZE   = const(1032) # size of each OUT port’s MIDI SysEx messages queue (size needs to be multiple of 3)
_DATA_W_BUF_SIZE    = const(72) # size of each OUT port’s other MIDI messages queue (size needs to be multiple of 3)

# Index numbers used in different versions of status_buf (bytearray to store data for faster access from viper code than class variables)
_STS_SYSEX_MODE     = const(0) # UART/USB IN, UART/USB OUT
_STS_RUN_STS        = const(1) # UART IN, UART OUT
_STS_RT_FLAG        = const(1) # USB IN
_STS_RT_SOURCE      = const(2) # UART/USB IN
_STS_RS_ENABLED     = const(2) # UART OUT
_STS_DATA_BYTES     = const(3) # UART IN
_STS_DATA_FLAG      = const(3) # USB IN
_STS_EXPECTED       = const(4) # UART IN
_STS_SYSEX_FLAG     = const(4) # USB IN
_STS_SYSEX_BYTES    = const(5) # UART/USB IN
_STS_PORT_ID        = const(6) # USB IN

# _process_midi_byte() return values
_TYPE_RT            = const(0)
_TYPE_SYSEX         = const(1)
_TYPE_DATA          = const(2)

# Global variables (faster than class variables)
_g_manager = None
_g_usb = None
_g_cb_rt = None
_g_cb_sysex = None
_g_cb_data = None
if __debug__: _g_log = None

@singleton
class MidiManager:
    ''' Singleton asynchronous MIDI I/O manager for multi-port UART, PIO and USB MIDI

    Attributes:
        out_ports (list[OutPortUART | OutPortPIO | OutPortUSB]): List with all single OUT port handler instances
        rt_dest_writers (list[Callable]): MIDI Real-Time writers (ports’ write_real_time functions) for those ports set up as destination
            (accessed by MIDI IN port handlers InPort*)
    '''
    def __init__(self) -> None:
        global _g_manager
        _g_manager = self
        self.manufacturer_str = None
        self.product_str = None
        self.serial_str = None
        self.in_ports = [] # single IN port handler instances
        self.out_ports = [] # single OUT port handler instances
        self.usb_ports = set() # used to count the number of USB MIDI ports (virtual cables)
        self.port_names = {} # USB MIDI port names (same for IN and OUT)
        self.external_jacks = {} # USB MIDI port External Jack settings (same for IN and OUT)
        self.uarts = {} # initiated UARTS
        self.rt_source = _NONE # port ID of the MIDI IN port to be used as source for MIDI Real-Time routing
        self.rt_destinations = set() # port IDs of the MIDI OUT ports to be used as destination for MIDI Real-Time routing
        self.rt_dest_writers = [] # MIDI Real-Time writers (ports’ write_real_time functions) for those ports set up as destination
        self.in_out_tasks = []
        if __debug__:
            global _g_log
            _g_log = Log()

    def set_usb_strings(self, manufacturer_str: str|bytes|bytearray|None = None, product_str: str|bytes|bytearray|None = None,
                 serial_str: str|bytes|bytearray|None = None) -> None:
        ''' Set USB device’s manufacturer name, product name and/or serial number

        Args:
            manufacturer_str (str | bytes | bytearray | None, optional): Manufacturer name to be assigned to the USB Device; defaults to `None`
                (copy manufacturer name from MicroPython’s built-in driver)
            product_str (str | bytes | bytearray | None, optional): Product name to be assigned to the USB Device; defaults to `None` (copy
                product name from MicroPython’s built-in driver)
            serial_str (str | bytes | bytearray | None, optional): Unique serial number to be assigned to the USB Device; defaults to `None`
                (copy serial number from MicroPython’s built-in driver)
        
        TIP: use `machine.unique_id()` to get a byte string with a unique identifier of a board/SoC to use as `serial_str`
        '''
        self.manufacturer_str = manufacturer_str
        self.product_str = product_str
        self.serial_str = serial_str

    async def usb_is_active(self) -> None:
        ''' `asyncio` awaitable which releases once the USB host activated the connection or returns immediately if USB is not initiated '''
        if (_usb := _g_usb) is not None: await _usb.usb_is_active()

    def assign_callbacks(self, cb_rt=None, cb_sys_ex=None, cb_data=None) -> None:
        ''' Assign callback functions to be called when receiving MIDI messages

        Args:
            cb_rt (Callable, optional): 
                Callback for MIDI Real-Time messages; signature: `f(port_id: int, byte: int) -> None`; defaults to `None`
            cb_sys_ex (Callable, optional): 
                Callback for MIDI SysEx messages; signature: `f(port_id: int, buf: bytearray, num_bytes: int) -> None`; defaults to `None`
            cb_data (Callable, optional): 
                Callback for regular MIDI messages (all except Real-Time and SysEx);
                signature: `f(port_id: int, byte_0: int, byte_1: int = 0, byte_2: int = 0) -> None`; defaults to `None`
        '''
        global _g_cb_rt, _g_cb_sysex, _g_cb_data
        _g_cb_rt = cb_rt
        _g_cb_sysex = cb_sys_ex
        _g_cb_data = cb_data

    def add_uart_in(self, uart_id: int, tx_pin: int|None = None, rx_pin: int|None = None) -> int:
        ''' Set up hardware MIDI IN port based on a standard set of RP2040/RP2350 UART pins, which gets the next available IN port ID assigned;
        only call this function once for a given `uart_id` number

        Args:
            uart_id (int): UART to be used (0: UART0, 1: UART1)
            tx_pin (int | None, optional): GPIO number to be used for transmitting UART; defaults to `None` (0 for UART0, 4 for UART1)
            rx_pin (int | None, optional): GPIO number to be used for receiving UART; defaults to `None` (1 for UART0, 5 for UART1)

        Returns:
            int: Assigned IN port ID (attribute `in_ports` index number)

        Important notes:
        - UART TX and RX come in pairs, so it is set up the first time an UART ID is used for either adding an IN port or an OUT port
        - If you’d like to adjust the TX and RX pins of a UART, you should do so the first time you assign an IN or OUT port to that UART
        - UART0 can only be mapped to GPIO 0/1, 12/13 and 16/17; UART1 can only be mapped to GPIO 4/5 and 8/9
        '''
        if uart_id in (uarts := self.uarts):
            _uart = uarts[uart_id]
        else:
            if tx_pin is None and rx_pin is None:
                _uart = UART(uart_id, _UART_BAUD)
            else:
                if tx_pin is None: tx_pin = 0 if uart_id == 0 else 4
                if rx_pin is None: rx_pin = 1 if uart_id == 0 else 5
                _uart = UART(uart_id, _UART_BAUD, tx=Pin(tx_pin), rx=Pin(rx_pin))
            uarts[uart_id] = _uart
        port_id = len((ports := self.in_ports))
        ports.append(InPortUART(port_id, _uart))
        return port_id

    def add_pio_in(self, pio_id: int, pin: int) -> int:
        ''' Set up hardware MIDI IN port based on a RP2040/RP2350 PIO state machine, which gets the next available IN port ID assigned; only
        call this function once for a given `pio_id` number

        Args:
            pio_id (int): PIO state machine ID to be used (RP2040: 0 to 7, RP2350: 0 to 11)
            pin (int): GPIO number to be used

        Returns:
            int: Assigned IN port ID (attribute `in_ports` index number)
        '''
        port_id = len((ports := self.in_ports))
        ports.append(InPortPIO(port_id, pio_id, pin))
        return port_id

    def add_usb_in(self, cable: int, port_name: str|bytes|bytearray|None = None, external_jack: bool|None = None) -> int:
        ''' Set up USB MIDI IN port based on a ‘virtual cable’, which gets the next available IN port ID assigned

        Args:
            cable (int): USB virtual IN cable number to be assigned
            port_name (str | bytes | bytearray | None, optional): Names to be shown by the host; the same name is used for IN and OUT
                ports with the same virtual cable number (overwrites previously defined name, if not `None`); defaults to `None`
            external_jack (bool | None, optional): Whether to set up an ‘external jack’ interface; this setting applies to both IN and OUT
                ports with the same virtual cable number (overwrites previous setting); defaults to `None` (becomes `True` if never set)

        Returns:
            int: Assigned IN port ID (attribute `in_ports` index number)

        Important notes:
        - Avoid skipping cable numbers because the missing ones will be set up as well and show up on the host
        - The Port name needs to be at least 2 characters long (otherwise a Windows host would fail to recognize the device) and is not
          shown on a Windows host
        '''
        port_id = len((ports := self.in_ports))
        ports.append(InPortUSB(port_id, cable))
        self.usb_ports.add(cable)
        if port_name is not None: self.port_names[cable] = port_name
        if external_jack is not None: self.external_jacks[cable] = external_jack
        return port_id

    def add_uart_out(self, uart_id: int, tx_pin: int|None = None, rx_pin: int|None = None, running_status: bool = True) -> int:
        ''' Set up hardware MIDI OUT port based on a standard set of RP2040/RP2350 UART pins, which gets the next available OUT port ID
        assigned; only call this function once for a given `uart_id` number

        Args:
            uart_id (int): UART to be used (0: UART0, 1: UART1)
            tx_pin (int | None, optional): GPIO number to be used for transmitting UART; defaults to `None` (0 for UART0, 4 for UART1)
            rx_pin (int | None, optional): GPIO number to be used for receiving UART; defaults to `None` (1 for UART0, 5 for UART1)
            running_status (bool, optional): Set whether to apply running status when sending out MIDI data; defaults to `True`

        Returns:
            int: Assigned OUT port ID (attribute `out_ports` index number)

        Important notes:
        - UART TX and RX come in pairs, so it is set up the first time an UART ID is used for either adding an IN port or an OUT port
        - If you’d like to adjust the TX and RX pins of a UART, you should do so the first time you assign an IN or OUT port to that UART
        - UART0 can only be mapped to GPIO 0/1, 12/13 and 16/17; UART1 can only be mapped to GPIO 4/5 and 8/9
        '''
        port_id = len((ports := self.out_ports))
        if uart_id in (uarts := self.uarts):
            _uart = uarts[uart_id]
        else:
            if tx_pin is None and rx_pin is None:
                _uart = UART(uart_id, _UART_BAUD)
            else:
                if tx_pin is None: tx_pin = 0 if uart_id == 0 else 4
                if rx_pin is None: rx_pin = 1 if uart_id == 0 else 5
                _uart = UART(uart_id, _UART_BAUD, tx=Pin(tx_pin), rx=Pin(rx_pin))
            uarts[uart_id] = _uart
        ports.append(OutPortUART(port_id, _uart, running_status))
        return port_id

    def add_pio_out(self, pio_id: int, pin: int, running_status: bool = True) -> int:
        ''' Set up hardware MIDI OUT port based on a RP2040/RP2350 PIO state machine, which gets the next available OUT port ID assigned;
        only call this function once for a given `pio_id` number

        Args:
            pio_id (int): PIO state machine ID to be used (RP2040: 0 to 7, RP2350: 0 to 11)
            pin (int): GPIO number to be used
            running_status (bool, optional): Set whether to apply running status when sending out MIDI data; defaults to `True`

        Returns:
            int: Assigned OUT port ID (attribute `out_ports` index number)
        '''
        port_id = len((ports := self.out_ports))
        ports.append(OutPortPIO(port_id, pio_id, pin, running_status))
        return port_id

    def add_usb_out(self, cable: int, port_name: str|bytes|bytearray|None = None, external_jack: bool|None = None) -> int:
        ''' Set up USB MIDI OUT port based on a ‘virtual cable’, which gets the next available OUT port ID assigned

        Args:
            cable (int): USB virtual OUT cable number to be assigned
            port_name (str | bytes | bytearray | None, optional): Names to be shown by the host; the same name is used for IN and OUT
                ports with the same virtual cable number (overwrites previously defined name, if not `None`); defaults to `None`
            external_jack (bool | None, optional): Whether to set up an ‘external jack’ interface; this setting applies to both IN and OUT
                ports with the same virtual cable number (overwrites previous setting); defaults to `None` (becomes `True` if never set)

        Returns:
            int: Assigned OUT port ID (attribute `out_ports` index number)

        Important notes:
        - Avoid skipping cable numbers because the missing ones will be set up as well and show up on the host
        - The Port name needs to be at least 2 characters long (otherwise a Windows host would fail to recognize the device) and is not
          shown on a Windows host
        '''
        port_id = len((ports := self.out_ports))
        ports.append(OutPortUSB(port_id, cable))
        self.usb_ports.add(cable)
        if port_name is not None: self.port_names[cable] = port_name
        if external_jack is not None: self.external_jacks[cable] = external_jack
        return port_id

    def set_midi_real_time_routing(self, source: int, destinations: list|tuple) -> None:
        ''' Set up fast-track routing of MIDI Real-Time messages to the destination MIDI OUT ports when received from the source MIDI
        IN port

        Args:
            source (int): Port ID of the MIDI IN port to be used as source for MIDI Real-Time routing
            destinations (list|tuple): Port IDs of the MIDI OUT ports to be used as destination for MIDI Real-Time routing
        '''
        self.rt_source = source
        self.rt_destinations = (dest := set(destinations))

    async def run(self) -> None:
        ''' `asyncio` awaitable to start the midi manager: start the USB driver (if USB MIDI ports were defined) and schedule tasks to run
        each set up MIDI ports
        '''
        # Initiate USBMidi if needed
        num_usb_ports = max(self.usb_ports) + 1 if self.usb_ports else 0
        if num_usb_ports > 0:
            port_names = []
            external_jacks = []
            names_dict = self.port_names
            jacks_dict = self.external_jacks
            for i in range(num_usb_ports):
                port_names.append(names_dict.get(i))
                external_jacks.append(jacks_dict.get(i, True))
            global _g_usb
            _g_usb = MidiUSB(num_usb_ports, port_names, external_jacks, manufacturer_str=self.manufacturer_str, product_str=self.product_str,
                            serial_str=self.serial_str)
        # Define MIDI Real-Time routing list
        out_ports = self.out_ports
        rt_dest_writers = self.rt_dest_writers
        rt_dest_writers.clear()
        for port_id in self.rt_destinations:
            rt_dest_writers.append(out_ports[port_id].write_real_time)
        rt_source = self.rt_source
        # Start USB driver (if needed)
        if num_usb_ports > 0: await _g_usb.run() # pyright: ignore[reportOptionalMemberAccess]
        # Start port tasks
        tasks = self.in_out_tasks
        _append = tasks.append
        _create_task = asyncio.create_task
        for i, _port in enumerate(self.in_ports):
            _port.is_rt_source = i == rt_source
            _append(_create_task(_port.run()))
        for _port in out_ports:
            _append(_create_task(_port.run()))
        gc.collect()
        await asyncio.sleep_ms(500) # avoid starting processes which send out MIDI data before all required processes are ready
        return

    def deinit(self) -> None:
        ''' Deinitialize all processes and tasks related to set up MIDI ports (including the `USBMidi` singleton if applicable) '''
        if (_usb := _g_usb) is not None: _usb.deinit()
        for task in self.in_out_tasks:
            task.cancel()
        for port in self.in_ports:
            port.deinit()
        for port in self.out_ports:
            port.deinit()
        for uart in self.uarts.values():
            uart.deinit()

class MidiFilter:
    ''' MIDI filter supporting message type, channel and CC number filtering
    
    `MidiFilter` instances could be used to filter outgoing MIDI messages before sending them to a MIDI OUT port. The classes `InPortUART`,
    `InPortPIO` and `InPortUSB` inherit this class to offer the option to filter incoming MIDI messages.

    '''

    def __init__(self):
        self.filter_buf = bytearray(176) # 0-31: type, 32-47: channel, 48-175: cc; blocked if set to 1, unblocked if set to 0

    def set_type_filter(self, msg_type: int, block: bool) -> None:
        ''' Set whether to block a MIDI message type

        Args:
            msg_type (int): MIDI message type (or status byte) to be (un)blocked; for Channel Voice or Channel Common type message the channel
            part of a status byte is ignored
            block (bool): `True` to block, `False` to unblock
        '''
        if msg_type >= 0xF0: # System Common message or System Real-Time message
            self.filter_buf[(msg_type & 0x0F) + 16] = block
        else: # Channel Voice message or Channel Common message
            self.filter_buf[msg_type >> 4] = block

    def set_all_type_filters(self, block: bool) -> None:
        ''' Set for all MIDI message types whether to be blocked

        Args:
            block (bool): `True` to block, `False` to unblock
        '''
        filter_buf = self.filter_buf
        for i in range(32):
            filter_buf[i] = block

    def set_channel_filter(self, channel: int, block: bool) -> None:
        ''' Set whether to block a MIDI channel

        Args:
            channel (int): MIDI channel (status byte) to be (un)blocked; if a status byte is provided, only its least significant nibble
                is taken
            block (bool): `True` to block, `False` to unblock
        '''
        self.filter_buf[(channel & 0x0F) + 32] = block

    def set_all_channel_filters(self, block: bool) -> None:
        ''' Set for all MIDI channel whether to be blocked

        Args:
            block (bool): `True` to block, `False` to unblock
        '''
        filter_buf = self.filter_buf
        for i in range(32, 48):
            filter_buf[i] = block

    def set_cc_filter(self, cc_number: int, block: bool) -> None:
        ''' Set whether to block a MIDI Control Change (CC) number

        Args:
            cc_number (int): MIDI Control Change number to be (un)blocked
            block (bool): `True` to block, `False` to unblock
        '''
        self.filter_buf[(cc_number & 0x7F) + 48] = block

    def set_all_cc_filters(self, block: bool) -> None:
        ''' Set for all MIDI Control Change (CC) numbers whether to be blocked

        Args:
            block (bool): `True` to block, `False` to unblock
        '''
        filter_buf = self.filter_buf
        for i in range(48, 176):
            filter_buf[i] = block

    @micropython.viper
    def filter(self, msg: ptr8) -> bool: # pyright: ignore[reportUndefinedVariable]
        ''' Determine whether to block a MIDI message

        Args:
            msg (bytearray|bytes|memoryview): At least the first 2-bytes of the MIDI message to be assessed

        Returns:
            bool: `True` if the MIDI message is to be blocked, otherwise `False`
        '''
        filter_buf = ptr8(self.filter_buf) # pyright: ignore[reportUndefinedVariable]
        if msg[0] >= 0xF0: # System Common message or System Real-Time message
            n = msg[0] & 0x0F
            n += 16
            return bool(filter_buf[n])
        n = msg[0] & 0xF0 # message type
        n >>= 4
        if filter_buf[n]: return False
        n = msg[0] & 0x0F # channel
        n += 32
        if filter_buf[n]: return False
        n = msg[1] + 48 # Control Change value
        if msg[0] == 0xB0 and filter_buf[n]: return False
        return True

class InPortUART(MidiFilter):
    ''' Single port handler for hardware UART based MIDI IN port; use `MidiPort.add_uart_in()` to set up (do not instance directly)

    Inherits midi filter from `MidiFilter` class

    Args:
        port_id (int): IN port ID (`MidiManager.in_ports` index number)
        uart (machine.UART): UART instance to be used

    Attributes:
        is_rt_source (bool): Whether port is MIDI Real-Time routing source (set by MidiManager.set_midi_real_time_routing)
    '''
    def __init__(self, port_id: int, uart: UART) -> None:
        self.port_id = port_id
        self._uart = uart
        self.is_rt_source = False # whether port is MIDI Real-Time routing source (set by MidiManager.set_midi_real_time_routing)
        super().__init__()

    async def run(self) -> None:
        ''' `asyncio` task reading data from UART buffer and sending it the right callback if new data is available '''
        _readinto = asyncio.StreamReader(self._uart).readinto # pyright: ignore[reportAttributeAccessIssue]
        read_buf = bytearray(_UART_READ_BUF_SIZE)
        filter_buf = self.filter_buf
        status_buf = bytearray(6)
        status_buf[_STS_RT_SOURCE] = self.is_rt_source
        sysex_buf = bytearray(_SYSEX_R_BUF_SIZE)
        data_buf = bytearray(3)
        port_id = self.port_id
        rt_dest_writers = _g_manager.rt_dest_writers # pyright: ignore[reportOptionalMemberAccess]
        _cb_rt = _g_cb_rt
        rt_flag = _cb_rt is not None
        _cb_sysex = _g_cb_sysex
        sysex_flag = _cb_sysex is not None
        _cb_data = _g_cb_data
        data_flag = _cb_data is not None
        _sleep = asyncio.sleep
        while True:
            n = await _readinto(read_buf)
            for i in range(n):
                msg_type = _process_midi_byte(read_buf[i], filter_buf, status_buf, sysex_buf, data_buf)
                if msg_type == _NONE: # skip if message is incomplete or invalid
                    await _sleep(0)
                    continue
                if msg_type == _TYPE_RT:
                    if status_buf[_STS_RT_SOURCE]:
                        for writer in rt_dest_writers: writer(read_buf[i]) # distribute MIDI Real-Time messages
                    if rt_flag: _cb_rt(port_id, read_buf[i])
                    await _sleep(0)
                    continue
                if msg_type == _TYPE_DATA:
                    if data_flag:
                        expected = status_buf[_STS_EXPECTED]
                        _cb_data(port_id, status_buf[_STS_RUN_STS], data_buf[0] if expected >= 1 else 0, data_buf[1] if expected == 2 else 0)
                if sysex_flag:
                    m = status_buf[_STS_SYSEX_BYTES]
                    if m == _SYSEX_R_BUF_SIZE - 1:
                        _cb_sysex(port_id, sysex_buf, _SYSEX_R_BUF_SIZE)
                    else:
                        _cb_sysex(port_id, sysex_buf, m + 1)
                await _sleep(0)

    def deinit(self) -> None:
        ''' Empty deinitialization function (no deinit needed) '''
        pass

class InPortPIO(MidiFilter):
    ''' Single port handler for PIO UART based MIDI IN port; use `MidiPort.add_pio_in()` to set up (do not instance directly)

    Inherits midi filter from `MidiFilter` class

    Args:
        port_id (int): IN port ID (`MidiManager.in_ports` index number)
        pio_id (int): PIO state machine ID to be used (RP2040: 0 to 7, RP2350: 0 to 11)
        pin (int): GPIO number to be used

    Attributes:
        is_rt_source (bool): Whether port is MIDI Real-Time routing source (set by MidiManager.set_midi_real_time_routing)
    '''
    def __init__(self, port_id: int, pio_id: int, pin: int) -> None:
        self.port_id = port_id
        self.is_rt_source = False # whether port is MIDI Real-Time routing source (set by MidiManager.set_midi_real_time_routing)
        self._sm = (_sm := rp2.StateMachine(pio_id, _uart_rx, freq=8 * _UART_BAUD, in_base=(_pin := Pin(pin, Pin.IN)), jmp_pin=_pin)) # pyright: ignore[reportCallIssue]
        _sm.irq(self._cb_pio)
        self._rx_flag = asyncio.ThreadSafeFlag() # flag indicate data is available to be processed
        super().__init__()

    async def run(self) -> None:
        ''' `asyncio` task reading data from PIO buffer and sending it to the right callback if new data is available '''
        _rx_flag = self._rx_flag.wait
        _sm_get = self._sm.get
        byte_buf = bytearray(1)
        filter_buf = self.filter_buf
        status_buf = bytearray(6)
        status_buf[_STS_RT_SOURCE] = self.is_rt_source
        sysex_buf = bytearray(_SYSEX_R_BUF_SIZE)
        data_buf = bytearray(3)
        port_id = self.port_id
        _cb_rt = _g_cb_rt
        rt_dest_writers = _g_manager.rt_dest_writers # pyright: ignore[reportOptionalMemberAccess]
        rt_flag = _cb_rt is not None
        _cb_sysex = _g_cb_sysex
        sysex_flag = _cb_sysex is not None
        _cb_data = _g_cb_data
        data_flag = _cb_data is not None
        _sleep = asyncio.sleep
        self._sm.active(1) # activate PIO state machine
        while True:
            await _rx_flag()
            _sm_get(byte_buf, 24)
            msg_type = _process_midi_byte(byte_buf[0], filter_buf, status_buf, sysex_buf, data_buf)
            if msg_type == _NONE: # skip if message is incomplete or invalid
                await _sleep(0)
                continue
            if msg_type == _TYPE_RT:
                if status_buf[_STS_RT_SOURCE]:
                    for writer in rt_dest_writers: writer(byte_buf[0]) # distribute MIDI Real-Time messages
                if rt_flag: _cb_rt(port_id, byte_buf[0])
                await _sleep(0)
                continue
            if msg_type == _TYPE_DATA:
                if data_flag:
                    expected = status_buf[_STS_EXPECTED]
                    _cb_data(port_id, status_buf[_STS_RUN_STS], data_buf[0] if expected >= 1 else 0, data_buf[1] if expected == 2 else 0)
                await _sleep(0)
                continue
            if sysex_flag:
                m = status_buf[_STS_SYSEX_BYTES]
                if m == _SYSEX_R_BUF_SIZE - 1:
                    _cb_sysex(port_id, sysex_buf, _SYSEX_R_BUF_SIZE)
                else:
                    _cb_sysex(port_id, sysex_buf, m + 1)
            await _sleep(0)

    def deinit(self) -> None:
        ''' Deactivate PIO state machine '''
        self._sm.active(0)

    def _cb_pio(self, _) -> None:
        ''' Callback to handle PIO interrupt: set RX flag '''
        _rx_flag = self._rx_flag
        _rx_flag.set() # set flag indicate data is available to be processed

class InPortUSB(MidiFilter):
    ''' Single port handler for USB MIDI virtual cable based MIDI IN port; use `MidiPort.add_usb_in()` to set up (do not instance directly)

    Inherits midi filter from `MidiFilter` class

    Args:
        port_id (int): IN port ID (`MidiManager.in_ports` index number)
        cable (int): Number of the USB virtual IN cable to be assigned)

    Attributes:
        is_rt_source (bool): Whether port is MIDI Real-Time routing source (set by MidiManager.set_midi_real_time_routing)
    '''
    def __init__(self, port_id: int, cable: int = 0) -> None:
        self.status_buf = (status_buf := bytearray(7)) # bytearray to store data for faster access from viper code than class variables
        status_buf[_STS_PORT_ID] = port_id
        self.cable = cable
        self.is_rt_source = False # whether port is MIDI Real-Time routing source (set by MidiManager.set_midi_real_time_routing)
        self.sysex_buf = bytearray(_SYSEX_R_BUF_SIZE)

    async def run(self) -> None:
        ''' `asyncio` task which only sets flags indicating whether callbacks are defined or not '''
        _g_usb.assign_callback(self.cable, self._process_midi_packet) # pyright: ignore[reportOptionalMemberAccess]
        status_buf = self.status_buf
        status_buf[_STS_RT_SOURCE] = self.is_rt_source
        status_buf[_STS_RT_FLAG] = _g_cb_rt is not None
        status_buf[_STS_SYSEX_FLAG] = _g_cb_sysex is not None
        status_buf[_STS_DATA_FLAG] = _g_cb_data is not None
        super().__init__()

    def deinit(self) -> None:
        ''' Empty deinitialization function (no deinit needed) '''
        pass

    @micropython.viper
    def _process_midi_packet(self, pckt: ptr8): # pyright: ignore[reportUndefinedVariable]
        ''' Process midi data packet from a USB MIDI IN stream and add to SysEx buffer `sysex_buf` if MIDI SysEx data has been received or
        to general data buffer `data_buf` if a MIDI message (everything except SysEx and System Real-Time messages) has been received

        Args:
            pckt (bytearray(4)): MIDI packet to be processed
        '''
        filter_buf = ptr8(self.filter_buf) # pyright: ignore[reportUndefinedVariable]
        status_buf = ptr8(self.status_buf) # pyright: ignore[reportUndefinedVariable]
        cin = pckt[0] & 0x0F
        if cin == 0x0F: # System Real-Time message
            if pckt[1] < 0xF8: return
            n = pckt[1] & 0x0F
            n += 16
            if not bool(filter_buf[n]):
                rt_dest_writers = _g_manager.rt_dest_writers # pyright: ignore[reportOptionalMemberAccess]
                if status_buf[_STS_RT_SOURCE]:
                    for writer in rt_dest_writers: writer(pckt[1]) # distribute MIDI Real-Time messages
                if status_buf[_STS_RT_FLAG]: _g_cb_rt(status_buf[_STS_PORT_ID], pckt[1]) # pyright: ignore[reportOptionalCall]
            return
        if 0x04 <= cin <= 0x07 and not (cin == 0x05 and pckt[1] != 0xF7): # SysEx Start/Continue or End of SysEx message
            if not status_buf[_STS_SYSEX_FLAG] or bool(filter_buf[16]): return
            buf = ptr8(self.sysex_buf) # pyright: ignore[reportUndefinedVariable]
            n = status_buf[_STS_SYSEX_BYTES]
            data_len = cin - 4 or 1
            for i in range(data_len):
                buf[n] = pckt[i + 1]
                n += 1
                if n == _SYSEX_R_BUF_SIZE:
                    _g_cb_sysex(status_buf[_STS_PORT_ID], buf, _SYSEX_R_BUF_SIZE) # pyright: ignore[reportOptionalCall]
                    n = 0
            status_buf[_STS_SYSEX_BYTES] = n
            if cin == 0x04:
                status_buf[_STS_SYSEX_MODE] = True
                return
            _g_cb_sysex(status_buf[_STS_PORT_ID], buf, n) # pyright: ignore[reportOptionalCall]
            status_buf[_STS_SYSEX_BYTES] = 0
            status_buf[_STS_SYSEX_MODE] = False
            return
        status_buf[_STS_SYSEX_MODE] = False # abort SysEx mode if needed
        if not status_buf[_STS_DATA_FLAG]: return
        if 0x08 <= cin <= 0x0B or cin == 0x0E:
            data_len = 3
        elif 0x02 <= cin <= 0x0D:
            data_len = 2
        elif cin == 0x05 or cin == 0x0F:
            data_len = 1
        else: # undefined MIDI message
            return
        # Check filter
        if pckt[1] >= 0xF0: # System Common message
            n = pckt[1] & 0x0F
            n += 16
            if filter_buf[n]: return
        else:
            n = pckt[1] & 0xF0 # message type
            n >>= 4
            if filter_buf[n]: return
            n = pckt[1] & 0x0F # channel
            n += 32
            if filter_buf[n]: return
            n = pckt[2] + 48 # Control Change value
            if pckt[1] == 0xB0 and filter_buf[n]: return
        _g_cb_data(status_buf[_STS_PORT_ID], pckt[1], pckt[2] if data_len >= 2 else 0, pckt[3] if data_len == 3 else 0) # pyright: ignore[reportOptionalCall]

class _UARTOrPIOOut:
    ''' Parent single port handling class for OutPortUART and OutPortPIO

    Args:
        port_id (int): IN port ID (`MidiManager.in_ports` index number)
        writer (machine.UART.write | rp2.StateMachine.put): function to write to UART or PIO state machine
        running_status (bool, optional): Set whether to apply running status when sending out MIDI data; defaults to `True`
    '''
    def __init__(self, port_id: int, writer, running_status: bool = True) -> None:
        self.port_id = port_id # only to be available for debugging purposes
        self._writer = writer
        self.status_buf = (status_buf := bytearray(3)) # bytearray to store data for faster access from viper code than class variables
        status_buf[_STS_RS_ENABLED] = running_status
        status_buf[_STS_RUN_STS] = 0
        self._rt_buf = micropython.RingIO(_RT_W_BUF_SIZE) # MIDI Real-Time messages queue for processing before sending
        self._sysex_buf = micropython.RingIO(_SYSEX_W_BUF_SIZE) # MIDI SysEx data queue for processing before sending
        self._data_buf = micropython.RingIO(_DATA_W_BUF_SIZE) # Other MIDI messages queue for processing before sending
        self.scratch_buf = bytearray(_SYSEX_W_BUF_SIZE) # Scratch buffer for collecting SysEx bytes before queueing
        self._data_flag = asyncio.ThreadSafeFlag() # flag indicating data has been queued and is now available for processing
        self.byte_buf = bytearray(1) # used in write_real_time

    @micropython.viper
    def write_real_time(self, byte: int):
        ''' Queue a MIDI Real-Time message to be sent to MIDI OUT port, which will be sent as quick as possible

        args:
            byte (int): Single-byte MIDI Real-Time message to be sent
        '''
        buf = self._rt_buf
        byte_buf = self.byte_buf
        buf_ptr = ptr8(byte_buf) # pyright: ignore[reportUndefinedVariable]
        buf_ptr[0] = byte
        n = int(buf.write(byte_buf))
        if n == 1: # write successful
            _data_flag = self._data_flag
            _data_flag.set()
        elif __debug__: # real-time buffer full
            _log = _g_log
            _log.write(f'{self.__class__.__name__}.write_real_time: real-time buffer full') # pyright: ignore[reportOptionalMemberAccess]

    @micropython.viper
    def write_sysex(self, sysex_bytes:ptr8, num_bytes:int): # pyright: ignore[reportUndefinedVariable]
        ''' Queue a block of MIDI SysEx data to be sent to MIDI OUT port

        args:
            sysex_bytes (bytearray | bytes | memoryview): SysEx data buffer from which to be sent
            num_bytes (int): Number of bytes to be sent
        '''
        status_buf = ptr8(self.status_buf) # pyright: ignore[reportUndefinedVariable]
        sysex_mode = bool(status_buf[_STS_SYSEX_MODE])
        end_pos = 0
        start_pos = 0
        scratch_buf = self.scratch_buf
        scratch_buf_ptr = ptr8(scratch_buf) # pyright: ignore[reportUndefinedVariable]
        for i in range(num_bytes):
            if sysex_mode:
                if sysex_bytes[i] == 0xF7: # End of SysEx
                    end_pos = i + 1
                    sysex_mode = False
                if 0x80 <= sysex_bytes[i] <= 0xEF: # Channel Voice message or Channel Common message: abort (invalid SysEx data)
                    sysex_mode = False
                    if end_pos == 0: # no complete SysEx block found yet
                        continue
                    else: # already captured a complete SysEx block
                        break
                scratch_buf_ptr[i] = sysex_bytes[i]
            else: # encountered End of SysEx or invalid data before
                if sysex_bytes[i] != 0xF0: # SysEx Start
                    continue
                if end_pos == 0: # first encounter of valid SysEx data
                    start_pos = i
                elif end_pos != i: # another SysEx block following immediately after the previous one
                    break
                sysex_mode = True
                scratch_buf_ptr[i] = sysex_bytes[i]
        if end_pos == 0:
            if sysex_mode:
                end_pos = i + 1
            else: # no valid SysEx found
                return
        num_bytes = end_pos - start_pos
        buf = self._sysex_buf
        mv = memoryview(scratch_buf)
        n = int(buf.write(mv[builtins.int(start_pos):builtins.int(end_pos)]))
        if __debug__ and n != end_pos - start_pos: # SysEx buffer full
            _log = _g_log
            _log.write('_OutPortUSB.write_sysex: SysEx buffer full') # pyright: ignore[reportOptionalMemberAccess]
        if n > 0:
            status_buf[_STS_SYSEX_MODE] = True # immediately switch to sending SysEx instead of regular MIDI data to make the self.run
            _data_flag = self._data_flag
            _data_flag.set()

    @micropython.viper
    def write_data(self, byte_0: int, byte_1: int = 0, byte_2: int = 0):
        ''' Queue a MIDI message to be sent to MIDI OUT port; do not use for System Real-Time and SysEx messages

        args:
            byte_0 (int): First byte of the MIDI message to be sent
            byte_1 (int, optional): Second byte of the MIDI message to be sent; defaults to 0
            byte_2 (int, optional): Third byte of the MIDI message to be sent; defaults to 0
        '''
        status_buf = ptr8(self.status_buf) # pyright: ignore[reportUndefinedVariable]
        if status_buf[_STS_SYSEX_MODE]: # ignore data received while sending SysEx
            return
        if byte_0 >= 0xF0: # System Common message or System Real-Time message
            if byte_0 >= 0xF8: return # ignore System Real-Time message
            status_buf[_STS_RUN_STS] = 0
            buf = self._data_buf
            if byte_0 == 0xF2: # Song Position Pointer
                n = int(buf.write(bytes((byte_0, byte_1, byte_2))))
                if n != 3: # data buffer full
                    if __debug__:
                        _log = _g_log
                        _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                    return
            elif byte_0 <= 0xF3: # Time Code Quarter Frame or Song Select
                n = int(buf.write(bytes((byte_0, byte_1))))
                if n != 2: # data buffer full
                    if __debug__:
                        _log = _g_log
                        _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                    return
            else:
                n = int(buf.write(bytes((byte_0,))))
                if n != 1: # data buffer full
                    if __debug__:
                        _log = _g_log
                        _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                    return
        else: # Channel Voice messages
            msg_type = byte_0 & 0xF0
            buf = self._data_buf
            if status_buf[_STS_RS_ENABLED] and status_buf[_STS_RUN_STS] == byte_0:
                if msg_type == 0xC0 or msg_type == 0xD0:
                    n = int(buf.write(bytes((byte_1,))))
                    if n != 1:
                        if __debug__: # data buffer full
                            _log = _g_log
                            _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                        return
                else:
                    n = int(buf.write(bytes((byte_1, byte_2))))
                    if n != 2:
                        if __debug__: # data buffer full
                            _log = _g_log
                            _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                        return
            elif msg_type == 0xC0 or msg_type == 0xD0:
                status_buf[_STS_RUN_STS] = byte_0
                n = int(buf.write(bytes((byte_0, byte_1))))
                if n != 2:
                    if __debug__: # data buffer full
                        _log = _g_log
                        _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                    return
            else:
                status_buf[_STS_RUN_STS] = byte_0
                n = int(buf.write(bytes((byte_0, byte_1, byte_2))))
                if n != 3:
                    if __debug__: # data buffer full
                        _log = _g_log
                        _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]
                    return
        _data_flag = self._data_flag
        _data_flag.set() # write successful

    async def run(self) -> None:
        ''' `asyncio` task merging data from real-time buffer, SysEx buffer and data buffer when available and writing it into the 
        read/write buffer '''
        _data_flag = self._data_flag.wait
        _rt_any = self._rt_buf.any
        _rt_readinto = self._rt_buf.readinto
        _sysex_readinto = self._sysex_buf.readinto
        _data_readinto = self._data_buf.readinto
        status_buf = self.status_buf
        rw_buf = bytearray(max(_RT_W_BUF_SIZE, _DATA_W_BUF_SIZE, _SYSEX_W_BUF_SIZE))
        byte_buf = bytearray(1)
        _write = self._writer
        _sleep = asyncio.sleep
        while True:
            await _data_flag()
            n = _rt_any()
            if n > 0:
                z = _rt_readinto(rw_buf)
                mv = memoryview(rw_buf)[:z]
                _write(mv)
                await _sleep(0)
            if status_buf[_STS_SYSEX_MODE]:
                n = _sysex_readinto(rw_buf)
                if rw_buf[n - 1] == 0xF7: # End of SysEx
                    status_buf[_STS_SYSEX_MODE] = False
            else:
                n = _data_readinto(rw_buf)
            mv = memoryview(rw_buf)
            for i in range(n):
                byte_buf[0] = rw_buf[i]
                _write(byte_buf)
                await _sleep(0)
                m = _rt_any()
                if m > 0:
                    z = _rt_readinto(rw_buf)
                    mv = memoryview(rw_buf)[:z]
                    _write(mv)
                    await _sleep(0)

class OutPortUART(_UARTOrPIOOut):
    ''' Single port handler for hardware UART based MIDI OUT port; use `MidiPort.add_uart_out()` to set up (do not instance directly)

    Args:
        port_id (int): OUT port ID (`MidiManager.out_ports` index number)
        uart (machine.UART): UART instance to be used
        running_status (bool, optional): Set whether to apply running status when sending out MIDI data; defaults to `True`
    '''
    def __init__(self, port_id: int, uart: UART, running_status: bool = True) -> None:
        super().__init__(port_id, uart.write, running_status)

    def deinit(self) -> None:
        ''' Empty deinitialization function (no deinit needed) '''
        pass

class OutPortPIO(_UARTOrPIOOut):
    ''' Single port handler for PIO UART based MIDI OUT port; use `MidiPort.add_pio_out()` to set up (do not instance directly)

    Args:
        port_id (int): IN port ID (`MidiManager.in_ports` index number)
        pio_id (int): PIO state machine ID to be used (RP2040: 0 to 7, RP2350: 0 to 11)
        pin (int): GPIO number to be used
        running_status (bool, optional): Set whether to apply running status when sending out MIDI data; defaults to `True`
    '''
    def __init__(self, port_id: int, pio_id: int, pin: int, running_status: bool = True) -> None:
        self._sm = (_sm := rp2.StateMachine(pio_id, _uart_tx, freq=8 * _UART_BAUD, sideset_base=(_pin := Pin(pin)), out_base=_pin)) # pyright: ignore[reportCallIssue]
        _sm.active(1) # activate PIO state machine
        super().__init__(port_id, _sm.put, running_status)

    def deinit(self) -> None:
        ''' Deactivate PIO state machine '''
        self._sm.active(0)

class OutPortUSB:
    ''' Single port handler for USB MIDI virtual cable based MIDI OUT port; use `MidiPort.add_usb_out()` to set up (do not instance directly)

    Args:
        port_id (int): IN port ID (`MidiManager.in_ports` index number)
        cable (int): Number of the USB virtual OUT cable to be assigned)
    '''
    def __init__(self, port_id: int, cable: int = 0) -> None:
        self.port_id = port_id # only to be available for debugging purposes
        self.cable = cable
        self.status_buf = bytearray(1) # bytearray to store data for faster access from viper code than class variables
        self._rt_buf = micropython.RingIO(_RT_W_BUF_SIZE) # MIDI Real-Time messages queue for processing before sending
        self._sysex_buf = micropython.RingIO(_SYSEX_W_BUF_SIZE) # MIDI SysEx data queue for processing before sending
        self._data_buf = micropython.RingIO(_DATA_W_BUF_SIZE) # Other MIDI messages queue for processing before sending
        self.scratch_buf = bytearray(_SYSEX_W_BUF_SIZE) # Scratch buffer for collecting SysEx bytes before queueing
        self._data_flag = asyncio.ThreadSafeFlag() # flag indicating data has been queued and is now available for processing
        self.byte_buf = bytearray(1) # used in write_real_time

    @micropython.viper
    def write_real_time(self, byte: int):
        ''' Queue a MIDI Real-Time message to be sent to MIDI OUT port, which will be sent as quick as possible

        args:
            byte (int): Single-byte MIDI Real-Time message to be sent
        '''
        buf = self._rt_buf
        byte_buf = self.byte_buf
        buf_ptr = ptr8(byte_buf) # pyright: ignore[reportUndefinedVariable]
        buf_ptr[0] = byte
        n = int(buf.write(byte_buf))
        if n == 1: # write successful
            _data_flag = self._data_flag
            _data_flag.set()
        elif __debug__: # real-time buffer full
            _log = _g_log
            _log.write('_OutPortUSB.write_real_time: real-time buffer full') # pyright: ignore[reportOptionalMemberAccess]

    @micropython.viper
    def write_sysex(self, sysex_bytes:ptr8, num_bytes:int): # pyright: ignore[reportUndefinedVariable]
        ''' Queue a block of MIDI SysEx data to be sent to MIDI OUT port

        args:
            sysex_bytes (bytearray | bytes | memoryview): SysEx data buffer from which to be sent
            num_bytes (int): Number of bytes to be sent
        '''
        status_buf = ptr8(self.status_buf) # pyright: ignore[reportUndefinedVariable]
        sysex_mode = bool(status_buf[_STS_SYSEX_MODE])
        end_pos = 0
        start_pos = 0
        scratch_buf = self.scratch_buf
        scratch_buf_ptr = ptr8(scratch_buf) # pyright: ignore[reportUndefinedVariable]
        for i in range(num_bytes):
            if sysex_mode:
                if sysex_bytes[i] == 0xF7: # End of SysEx
                    end_pos = i + 1
                    sysex_mode = False
                if 0x80 <= sysex_bytes[i] <= 0xEF: # Channel Voice message or Channel Common message: abort (invalid SysEx data)
                    sysex_mode = False
                    if end_pos == 0: # no complete SysEx block found yet
                        continue
                    else: # already captured a complete SysEx block
                        break
                scratch_buf_ptr[i] = sysex_bytes[i]
            else: # encountered End of SysEx or invalid data before
                if sysex_bytes[i] != 0xF0: # SysEx Start
                    continue
                if end_pos == 0: # first encounter of valid SysEx data
                    start_pos = i
                elif end_pos != i: # another SysEx block following immediately after the previous one
                    break
                sysex_mode = True
                scratch_buf_ptr[i] = sysex_bytes[i]
        if end_pos == 0:
            if sysex_mode:
                end_pos = i + 1
            else: # no valid SysEx found
                return
        num_bytes = end_pos - start_pos
        rem = num_bytes % 3
        if rem > 0:
            for i in range(3 - rem):
                scratch_buf[end_pos + i] = 0
            end_pos += (3 - rem)
        buf = self._sysex_buf
        mv = memoryview(scratch_buf)
        n = int(buf.write(mv[builtins.int(start_pos):builtins.int(end_pos)]))
        if __debug__ and n != end_pos - start_pos: # SysEx buffer full
            _log = _g_log
            _log.write('_OutPortUSB.write_sysex: SysEx buffer full') # pyright: ignore[reportOptionalMemberAccess]
        if n > 0:
            status_buf[_STS_SYSEX_MODE] = True # immediately switch to sending SysEx instead of regular MIDI data to make the self.run
            _data_flag = self._data_flag
            _data_flag.set()

    @micropython.viper
    def write_data(self, byte_0: int, byte_1: int = 0, byte_2: int = 0):
        ''' Queue a MIDI message to be sent to MIDI OUT port; do not use for System Real-Time and SysEx messages

        args:
            byte_0 (int): First byte of the MIDI message to be sent
            byte_1 (int, optional): Second byte of the MIDI message to be sent
            byte_2 (int, optional): Third byte of the MIDI message to be sent
        '''
        status_buf = ptr8(self.status_buf) # pyright: ignore[reportUndefinedVariable]
        if status_buf[_STS_SYSEX_MODE]: # ignore data received while sending SysEx
            return
        buf = self._data_buf
        n = int(buf.write(bytes((byte_0, byte_1, byte_2))))
        if n == 3:
            _data_flag = self._data_flag
            _data_flag.set()
        elif __debug__: # data buffer full
            _log = _g_log
            _log.write(f'{self.__class__.__name__}.write_data: data buffer full') # pyright: ignore[reportOptionalMemberAccess]

    async def run(self) -> None:
        ''' `asyncio` task merging data from real-time buffer, SysEx buffer and data buffer when available and writing it into the
        read/write buffer '''
        _is_active = _g_manager.usb_is_active # pyright: ignore[reportOptionalMemberAccess]
        _data_flag = self._data_flag.wait
        _rt_any = self._rt_buf.any
        _rt_readinto = self._rt_buf.readinto
        _sysex_readinto = self._sysex_buf.readinto
        _data_readinto = self._data_buf.readinto
        status_buf = self.status_buf
        rw_buf = bytearray(max(_RT_W_BUF_SIZE, _DATA_W_BUF_SIZE, _SYSEX_W_BUF_SIZE))
        cable = self.cable
        rt_pckt = bytearray(4)
        pckt = bytearray(4)
        _sysex_buf_2_pckt = _sysex_buf_to_pckt
        _data_buf_2_pckt = _data_buf_to_pckt
        _send_event = _g_usb.write_event # pyright: ignore[reportOptionalMemberAccess]
        _send_sysex = _g_usb.write_sysex # pyright: ignore[reportOptionalMemberAccess]
        _sleep = asyncio.sleep
        while True:
            await _is_active() # wait for USB to be active
            await _data_flag()
            n = _rt_any()
            if n > 0:
                n = _rt_readinto(rw_buf)
                for i in range(n):
                    rt_pckt[0] = cable
                    rt_pckt[1] = rw_buf[i]
                    _send_event(rt_pckt)
                    await _sleep(0)
            if status_buf[_STS_SYSEX_MODE]:
                n = _sysex_readinto(rw_buf)
                for i in range(0, n, 3):
                    pckt[0] = cable
                    num_bytes = _sysex_buf_2_pckt(rw_buf, i, pckt, status_buf)
                    _send_sysex(pckt, num_bytes)
                    await _sleep(0)
                    m = _rt_any()
                    if m > 0:
                        m = _rt_readinto(rw_buf)
                        for j in range(m):
                            rt_pckt[0] = cable
                            rt_pckt[1] = rw_buf[j]
                            _send_event(rt_pckt)
                            await _sleep(0)
            else:
                n = _data_readinto(rw_buf)
                for i in range(0, n, 3):
                    pckt[0] = cable
                    _data_buf_2_pckt(rw_buf, i, pckt)
                    _send_event(pckt)
                    await _sleep(0)
                    m = _rt_any()
                    if m > 0:
                        m = _rt_readinto(rw_buf)
                        for j in range(m):
                            rt_pckt[0] = cable
                            rt_pckt[1] = rw_buf[j]
                            _send_event(rt_pckt)
                            await _sleep(0)

    def deinit(self) -> None:
        ''' Empty deinitialization function (no deinit needed) '''
        pass

@micropython.viper
def _process_midi_byte(byte: int, filter_buf: ptr8, status_buf: ptr8, sysex_buf: ptr8, data_buf: ptr8) -> int: # pyright: ignore[reportUndefinedVariable]
    ''' Process single byte from a hardware (UART/PIO) MIDI IN stream, decode running status and add to SysEx buffer `sysex_buf` if MIDI
    SysEx data is being received or to general data buffer `data_buf` once a full MIDI message (everything except SysEx and System Real
    Time messages) has been received

    Args:
        byte (int): Single MIDI byte to be processed
        filter_buf (bytearray(176)): Buffer storing filter settings
        # status_buf (bytearray(5)): Buffer storing status variables which need to persist between calls of this function
        status_buf (bytearray(6)): Buffer storing status variables which need to persist between calls of this function
        sysex_buf (micropython.RingIO): Ring buffer to write incoming SysEx data into
        data_buf (micropython.RingIO): Ring buffer to write MIDI messages into

    Returns:
        int: Type of message ready for processing (_NONE: no complete message received yet, _TYPE_RT: System Real-Time Message,
            _TYPE_SYSEX: SysEx message, _TYPE_DATA: other type of MIDI message)
    '''
    if byte >= 0xF8: # System Real-Time message
        n = byte & 0x0F
        n += 16
        if bool(filter_buf[n]): return _NONE
        return _TYPE_RT
    if byte & 0x80: # status byte
        status_buf[_STS_DATA_BYTES] = 0
        if byte <= 0xEF: # Channel Voice message or Channel Common message
            status_buf[_STS_SYSEX_MODE] = False # abort SysEx mode if applicable
            status_buf[_STS_RUN_STS] = byte
            status_buf[_STS_EXPECTED] = 2 - int((byte & 0xE0) == 0xC0)
            return _NONE
        # System Common message
        status_buf[_STS_RUN_STS] = 0
        if byte == 0xF0: # SysEx Start message
            if bool(filter_buf[16]): return _NONE
            status_buf[_STS_SYSEX_MODE] = True
            sysex_buf[0] = 0xF0 # pyright: ignore[reportUndefinedVariable]
            status_buf[_STS_SYSEX_BYTES] = 1
            return _NONE
        if byte == 0xF7: # End of SysEx message
            if not status_buf[_STS_SYSEX_MODE]: return _NONE # Ignore end without start
            sysex_buf[status_buf[_STS_SYSEX_BYTES]] = 0xF7 # pyright: ignore[reportUndefinedVariable]
            status_buf[_STS_SYSEX_MODE] = False
            return _TYPE_SYSEX
        status_buf[_STS_SYSEX_MODE] = False # abort SysEx mode if applicable
        if byte == 0xF2: # Song Position Pointer
            status_buf[_STS_EXPECTED] = 2
            return _NONE
        if byte == 0xF1 or byte == 0xF3: # MIDI Time Code Quarter Frame or Song Select
            status_buf[_STS_EXPECTED] = 3
            return _NONE
        else: # undefined MIDI message
            status_buf[_STS_EXPECTED] = 0
            return _NONE
    # Data byte
    if status_buf[_STS_SYSEX_MODE]:
        n = status_buf[_STS_SYSEX_BYTES]
        sysex_buf[n] = byte # pyright: ignore[reportUndefinedVariable]
        status_buf[_STS_SYSEX_BYTES] = status_buf[_STS_SYSEX_BYTES] + 1
        return _TYPE_SYSEX if n + 1 == _SYSEX_R_BUF_SIZE - 1 else _NONE
    if status_buf[_STS_RUN_STS] == 0: return _NONE # ignore stray data byte
    n = status_buf[_STS_DATA_BYTES]
    data_buf[n] = byte # pyright: ignore[reportUndefinedVariable]
    if n + 1 == status_buf[_STS_EXPECTED]:
        status_buf[_STS_DATA_BYTES] = 0
        # Check filter
        if data_buf[0] >= 0xF0: # System Common message
            n = data_buf[0] & 0x0F
            n += 16
            return _NONE if filter_buf[n] else _TYPE_DATA
        n = data_buf[0] & 0xF0 # message type
        n >>= 4
        if filter_buf[n]: return _NONE
        n = data_buf[0] & 0x0F # channel
        n += 32
        if filter_buf[n]: return _NONE
        n = data_buf[1] + 48 # Control Change value
        if data_buf[0] == 0xB0 and filter_buf[n]: return _NONE
        return _TYPE_DATA
    status_buf[_STS_DATA_BYTES] = status_buf[_STS_DATA_BYTES] + 1
    return _NONE

@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_RIGHT)
def _uart_rx():
    ''' PIO UART RX routine used for MIDI input '''
    label("start")             # pyright: ignore[reportUndefinedVariable]
    wait(0, pin, 0)            # pyright: ignore[reportUndefinedVariable]
    set(x, 7)             [10] # pyright: ignore[reportIndexIssue, reportCallIssue, reportUndefinedVariable]
    label("rbitloop")          # pyright: ignore[reportUndefinedVariable]
    in_(pins, 1)               # pyright: ignore[reportUndefinedVariable]
    jmp(x_dec, "rbitloop") [6] # pyright: ignore[reportUndefinedVariable]
    jmp(pin, "good_stop")      # pyright: ignore[reportUndefinedVariable]
    wait(1, pin, 0)            # pyright: ignore[reportUndefinedVariable]
    jmp("start")               # pyright: ignore[reportUndefinedVariable]
    label("good_stop")         # pyright: ignore[reportUndefinedVariable]
    push(block)                # pyright: ignore[reportUndefinedVariable]
    irq(0)                     # pyright: ignore[reportUndefinedVariable]

@rp2.asm_pio(sideset_init=rp2.PIO.OUT_HIGH, out_init=rp2.PIO.OUT_HIGH, out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def _uart_tx():
    ''' PIO UART TX routine used for MIDI output '''
    pull()    .side(1)     [7] # pyright: ignore[reportUndefinedVariable]
    set(x, 7) .side(0)     [7] # pyright: ignore[reportAttributeAccessIssue, reportCallIssue, reportUndefinedVariable]
    label("tbitloop")          # pyright: ignore[reportUndefinedVariable]
    out(pins, 1)               # pyright: ignore[reportUndefinedVariable]
    jmp(x_dec, "tbitloop") [6] # pyright: ignore[reportUndefinedVariable]

@micropython.viper
def _data_buf_to_pckt(buf: ptr8, offset: int, pckt: ptr8): # pyright: ignore[reportUndefinedVariable]
    ''' Copy MIDI message from buffer at a given offset into MIDI packet

    Args:
        buf (bytearry | bytes | memoryview): Buffer to copy data from
        offset (int): First index from which to copy data from `buf`
        pckt (bytearray(4)): Packet to copy into
    '''
    # this is faster than a loop
    pckt[1] = buf[offset]
    pckt[2] = buf[offset + 1]
    pckt[3] = buf[offset + 2]

@micropython.viper
def _sysex_buf_to_pckt(buf: ptr8, start: int, pckt: ptr8, status_buf: ptr8) -> int: # pyright: ignore[reportUndefinedVariable]
    ''' Copy SysEx data from buffer into MIDI packet and detect End of SysEx message (if any)

    Args:
        buf (bytearry | bytes | memoryview): Buffer to copy data from
        start (int): First index from which to copy data from `buf`
        pckt (bytearray(4)): Packet to copy into
        status_buf (bytearray(1)): Buffer storing SysEx mode status, which needs to persist between calls of this function

    Returns:
        int: Number of bytes written (3 unless the first or second data byte is an End of SysEx message)
    '''
    # this is faster than a loop
    byte_0 = buf[start]
    byte_1 = buf[start + 1]
    byte_2 = buf[start + 2]
    pckt[1] = byte_0
    pckt[2] = byte_1
    pckt[3] = byte_2
    if byte_2 == 0xF7: # End of SysEx
        status_buf[_STS_SYSEX_MODE] = False
        return 3
    if byte_1 == 0xF7: # End of SysEx
        status_buf[_STS_SYSEX_MODE] = False
        return 2
    if byte_0 == 0xF7: # End of SysEx
        status_buf[_STS_SYSEX_MODE] = False
        return 1
    return 3
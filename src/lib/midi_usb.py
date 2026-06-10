''' Mid-level multi-port asynchronous USB MIDI 1.0 device implementation for MicroPython on RP2

    Part of the Multi-Midi library: https://github.com/HLammers/multi-midi

    Copyright (c) 2025 Harm Lammers
    
    Parts are taken from:
    - micropython-lib, https://github.com/micropython/micropython-lib, usb-device-midi library,
      copyright (c) 2023 Paul Hamshere, 2023-2024 Angus Gratton, published under MIT licence
    - micropython-lib, https://github.com/micropython/micropython-lib, usb-device-core library,
      copyright (c) 2022-2024 Angus Gratton, published under MIT licence
    - micropython-async, https://github.com/peterhinch/micropython-async, copyright (c) Peter Hinch, published under MIT licence

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

import machine
import micropython
import builtins
import asyncio
import struct

from singleton import singleton

if __debug__:
    try:
        from log import Log # pyright: ignore[reportMissingImports, reportAssignmentType]
    except:
        class Log:
            ''' Minimalist logger for if the log module is not avaialble '''

            def write(self, msg: str|bytes|bytearray) -> None:
                ''' Print message '''
                print(msg if type(msg) == str else msg.decode('ascii')) # pyright: ignore[reportAttributeAccessIssue]

_BUF_SIZE     = const(64) # size of receive buffer (from host)
_EP_PCKT_SIZE = const(64) # wMaxPacketSize for bulk endpoints

# Global variable (faster than class variable)
if __debug__: _g_log = None

@singleton
class MidiUSB:
    ''' Mid-level asynchronous singleton USB MIDI 1.0 device implementation supporting up to 16 MIDI ports in the form of virtual MIDI IN and
    OUT cables; initiated by `MidiManager.run()` (do not instance directly unless used with an alternative MIDI manager)

    Attributes:
        is_active (PersistentThreadSafeFlag): `asyncio` awaitable flag indicating USB is active

    Args:
        num_ports (int, optional): Number of MIDI IN/OUT ports (virtual cables) to be set up (up to 16); defaults to 1
        port_names (list[str | bytes | bytearray | None] | None, optional): List of names to be shown by the host for each MIDI port; defaults
            to `None`
        external_jacks (list[bool] | None, optional): List indicating for each port whether to set up an ‘external jack’ interface; defaults
            to `None` (external jacks are set up for all ports)
        manufacturer_str (str | bytes | bytearray | None, optional): Manufacturer name to be assigned to the USB Device; defaults to `None` (copy 
            manufacturer name from MicroPython’s built-in driver)
        product_str (str | bytes | bytearray | None, optional): Product name to be assigned to the USB Device; defaults to `None` (copy product
            name from MicroPython’s built-in driver)
        serial_str (str | bytes | bytearray | None, optional): Unique serial number to be assigned to the USB Device; defaults to `None` (copy
            serial number from MicroPython’s built-in driver)

    Important notes:
    - Port names need to be at least 2 characters long (otherwise a Windows host would fail to recognize the device) and are not shown on a
      Windows host
    - The MIDI driver turns off the USB serial port (USB CDC) used by the REPL, because a Windows host won’t recognize the MIDI ports
      if CDC and USB are both enabled

    '''
    def __init__(self, num_ports: int = 1, port_names: list[str|bytes|bytearray|None]|None = None, external_jacks: list[bool]|None = None,
                 cbs_in: list|None = None, manufacturer_str: str|bytes|bytearray|None = None, product_str: str|bytes|bytearray|None = None,
                 serial_str: str|bytes|bytearray|None = None) -> None:
        self.num_ports = num_ports
        if port_names is None: port_names = [None for _ in range(num_ports)]
        if (n := len(port_names)) > num_ports: del port_names[num_ports - n:]
        while len(port_names) < num_ports: port_names.append(None)
        self.port_names = port_names
        if external_jacks is None: external_jacks = [True for _ in range(num_ports)]
        while len(external_jacks) < num_ports: external_jacks.append(True)
        self.external_jacks = external_jacks
        if cbs_in is None: cbs_in = [None for _ in range(num_ports)]
        while len(cbs_in) < num_ports: cbs_in.append(None)
        self._cbs_in = cbs_in
        strs = [None, manufacturer_str, product_str, serial_str]
        self.ep_out = None
        self.ep_in = None
        _ThreadSafeFlag = asyncio.ThreadSafeFlag
        _RingIO = micropython.RingIO
        self.is_active = False # boolean flag indicating USB is active
        self.is_active_flag = asyncio.ThreadSafeFlag() # flag indicating to be ready for checking is_active
        self._xfer_lock = asyncio.Lock() # lock used to avoid multiple USB transfers (in/out) to be running at the same time
        self._xfer_done = (_xfer_done := _ThreadSafeFlag()) # flag indicating a USB transfer (in/out) completed
        _xfer_done.set() # start with set xfer_done flag, otherwise transfers will never kick off
        self._rx_buf = _RingIO(_BUF_SIZE) # receive buffer (from host)
        self._rx_flag = _ThreadSafeFlag() # flag indicating data is available to be processed
        self._tx_buf = _RingIO(_BUF_SIZE) # transmit buffer (to host)
        self._tx_flag = _ThreadSafeFlag() # flag indicating data is available to be transmitted
        self._rx_scratch_buf = bytearray(_EP_PCKT_SIZE) # temporary buffer used for receiving data from USB device
        self._tx_scratch_buf = bytearray(_EP_PCKT_SIZE) # temporary buffer used for writing data to USB device
        if __debug__:
            global _g_log
            _g_log = Log()
        # Configure USB
        self._usb = (_usb := machine.USBDevice())
        _usb.active(False)
        builtin_driver = _usb.BUILTIN_NONE
        fields = struct.unpack('<BBHBBBBHHHBBBB', builtin_driver.desc_dev)
        desc_dev = struct.pack('<BBHBBBBHHHBBBB',
                               fields[0],  # bLength
                               fields[1],  # bDescriptorType
                               fields[2],  # bcdUSB
                               0,          # bDeviceClass
                               0,          # bDeviceSubClass
                               0,          # bDeviceProtocol
                               fields[6],  # bMaxPacketSize
                               fields[7],  # idVendor
                               fields[8],  # idProduct
                               fields[9],  # bcdDevice
                               1,          # iManufacturer
                               2,          # iProduct
                               3,          # iSerialNumber
                               1)          # bNumConfigurations
        initial_cfg = (b'\x00' * 9)
        descriptor_length = len(initial_cfg) + 60 + 17  * num_ports + 15 * sum(self.external_jacks) # 60 = 56 + 4 extra bytes for 9-byte endpoint descriptors (USB MIDI 1.0 spec)
        desc = _Descriptor(bytearray(descriptor_length))
        desc.extend(initial_cfg)
        self._desc_cfg(desc, 0, 1, strs)
        # Update the Standard Configuration Descriptor header with values based on the complete descriptor
        desc.pack_into('<BBHBBBBB', 0,
                       9,                   # bLength
                       2,                   # bDescriptorType=CONFIGURATION
                       len(desc.buf),       # wTotalLength
                       2,                   # wNumInterfaces
                       1,                   # bConfigurationValue
                       0,                   # iConfiguration
                       (1 << 7) | (1 << 6), # bmAttributes: self-powered, no remote wake-up
                       125)                 # bMaxPower: 250mA
        _usb.config(desc_dev, desc.buf, strs, self._cb_open_itf, self._cb_reset, xfer_cb=self._cb_xfer)
        self.tasks = []

    @micropython.viper
    def write_event(self, cable_and_msg) -> bool:
        ''' Queue a MIDI Event Packet to be sent to the host; do not use for SysEx

        Args:
            cable_and_msg (bytearray(4)): Cable number (first byte) and MIDI message (remaining bytes) to be sent

        Returns:
            bool: `False` if failed due to the TX buffer being full, otherwise `True`
        '''
        cable_and_msg_ptr = ptr8(cable_and_msg) # pyright: ignore[reportUndefinedVariable]
        msg_byte_0 = cable_and_msg_ptr[1]
        if msg_byte_0 >= 0xF8: # System Real-Time
            cin = 0xF
        elif 0x80 <= msg_byte_0 <= 0xEF: # Channel Voice
            cin = msg_byte_0 >> 4
        elif msg_byte_0 == 0xF2: # Song Position Pointer
            cin = 0x3
        elif 0xF1 <= msg_byte_0 <= 0xF3: # MTC Quarter Frame / Song Select
            cin = 0x2
        elif msg_byte_0 == 0xF6: # Tune Request
            cin = 0x5
        else:
            return False # unsupported/invalid
        cable_and_msg_ptr[0] = (cable_and_msg_ptr[0] << 4) | cin
        tx_buf = self._tx_buf
        n = int(tx_buf.write(cable_and_msg))
        if n != 4: # transmit buffer full
            if __debug__:
                _log = _g_log
                _log.write(f'{self.__class__.__name__}.send_event: tx buffer full') # pyright: ignore[reportOptionalMemberAccess]
            return False
        _tx_flag = self._tx_flag
        _tx_flag.set()
        return True

    @micropython.viper
    def write_sysex(self, cable_and_msg, num_bytes: int) -> bool:
        ''' Queue a SyEx MIDI Event Packet to be sent to the host

        Args:
            cable_and_msg (bytearray(4)): Cable number (first byte) and MIDI message (remaining bytes) to be sent
            num_bytes (int): Number of bytes in cable_and_msg (including cable number)

        Returns:
            bool: `False` if failed due to the TX buffer being full, otherwise `True`
        '''
        cable_and_msg_ptr = ptr8(cable_and_msg) # pyright: ignore[reportUndefinedVariable]
        if num_bytes == 2: # assumes last byte is 0xF7 (End of SysEx)
            cin = 0x5 # SysEx Ends with 1 Byte
        elif num_bytes == 3: # assumes last byte is 0xF7 (End of SysEx)
            cin = 0x6 # SysEx Ends with 2 Bytes
        else:
            byte_2 = cable_and_msg_ptr[3]
            cin = 0x7 if byte_2 == 0xF7 else 0x4 # SysEx Ends with 3 Bytes / SysEx Starts/Continues
        cable_and_msg_ptr[0] = (cable_and_msg_ptr[0] << 4) | cin
        tx_buf = self._tx_buf
        n = int(tx_buf.write(cable_and_msg))
        if n != 4: # transmit buffer full
            if __debug__:
                _log = _g_log
                _log.write(f'{self.__class__.__name__}.send_sysex: tx buffer full') # pyright: ignore[reportOptionalMemberAccess]
            return False
        _tx_flag = self._tx_flag
        _tx_flag.set()
        return True

    def assign_callback(self, cable: int, cb_in) -> None:
        ''' Assign callback function to be called when receiving MIDI messages on a given cable number

        Args:
            cable (int):
                Cable number for which to assign callback
            cb_in (Callable):
                Callback to sent MIDI packets to; signature: `f(pckt: bytearray) -> None`
        '''
        if cable <= self.num_ports: self._cbs_in[cable] = cb_in # pyright: ignore[reportIndexIssue]

    async def run(self) -> None:
        ''' `asyncio` task which activates the USB driver and schedules tasks sending USB data to host and processing incoming data '''
        self._usb.active(True)
        tasks = self.tasks
        _create_task = asyncio.create_task
        tasks.append(_create_task(self._task_tx_xfer())) # send USB data to host whenever data is available
        tasks.append(_create_task(self._task_process())) # process incoming USB data whenever available
        await asyncio.sleep(0)

    async def usb_is_active(self) -> None:
        ''' `asyncio` awaitable which releases once the USB host activated the connection; do not use if `MidiUSB` is initiated by
        `MidiManager`, await `MidiManager.usb_is_active()` instead
        '''
        while not self.is_active:
            await self.is_active_flag.wait()
            if not self.is_active: self.is_active_flag.set() # re-set the is_active_flag to be ready for next time is_active is set to False

    def deinit(self) -> None:
        ''' Cancel all tasks and deactivate USB driver '''
        for task in self.tasks:
            task.cancel()
        self._usb.active(False)

    async def _task_tx_xfer(self) -> None:
        ''' `asyncio` task to keep an active IN transfer to send data to the host whenever there is data to send '''
        _is_active = self.usb_is_active
        _tx_flag = self._tx_flag.wait
        _xfer_lock = self._xfer_lock
        _submit_xfer = self._usb.submit_xfer
        ep_in = self.ep_in
        scratch_buf = self._tx_scratch_buf
        _buf_readinto = self._tx_buf.readinto
        _xfer_done_wait = self._xfer_done.wait
        while True:
            await _is_active() # wait for USB to be active
            await _tx_flag() # wait for data available to be transmitted
            async with _xfer_lock: # avoid multiple USB transfers (in/out) to be running at the same time
                n = _buf_readinto(scratch_buf)
                mv = memoryview(scratch_buf)[:n]
                _submit_xfer(ep_in, mv) # transmit data
                await _xfer_done_wait() # wait for transfer to complete

    async def _task_process(self) -> None:
        ''' `asyncio` task to process incoming MIDI events from USB '''
        _rx_flag = self._rx_flag.wait
        _readinto = self._rx_buf.readinto
        read_buf = bytearray(4)
        _cbs = self._cbs_in
        _sleep = asyncio.sleep
        while True:
            await _rx_flag() # wait for data available to be processed
            while _readinto(read_buf) == 4: # process one 4-bytes MIDI packet at a time (ignore incomplete packets)
                n = read_buf[0] >> 4
                if _cbs[n] is not None: _cbs[n](read_buf)
                await _sleep(0) # allow other tasks to run

    def _cb_open_itf(self, desc: memoryview) -> None:
        ''' Callback from TinyUSB lower layer when USB host does Set Configuration (called once per interface): Scan the full descriptor to
        build `_eps` and `_ep_addr` from the endpoint descriptors and find the highest numbered interface provided to the callback (which will
        be the first interface)

        Args:
            desc (memoryview): Memoryview of the interface descriptor that the host is accepting
        '''
        offset = 0
        max_itf = desc[2] # bInterfaceNumber
        while offset < len(desc):
            if desc[offset + 1] == 4: max_itf = max(max_itf, desc[offset + 2]) # bInterfaceNumber
            offset += desc[offset] # bLength
        if max_itf == 1: # only after second interface
            self.is_active = True # set boolean flag indicating USB is active
            self.is_active_flag.set() # notice waiter in usb_is_active that is_active is set to True
            self._usb.submit_xfer(self.ep_out, self._rx_scratch_buf) # prime for receiving first data

    def _cb_reset(self) -> None:
        ''' Callback from TinyUSB lower layer when the USB device is reset by the host: clear flag indicating USB is active '''
        self.is_active = False # clear flag indicating USB is active
        self.is_active_flag.set() # prepare to notice waiter in usb_is_active for the next time is_active is set to True

    @micropython.viper
    def _cb_xfer(self, ep: int, result: int, num_bytes: int):
        ''' Callback from TinyUSB lower layer when a transfer completes: read data when available and write it into RX buffer when available;
        set transfer complete flag

        Args:
            ep (int): The Endpoint number for the completed transfer
            result (int): 1 if transfer succeeded, otherwise 0
            num_bytes (int): Number of bytes successfully transferred
        '''
        if result != 0: return
        ep_out = int(self.ep_out) # pyright: ignore[reportArgumentType]
        if ep == ep_out:
            rx_buf = self._rx_buf
            scratch_buf = self._rx_scratch_buf
            mv = memoryview(scratch_buf)[builtins.int(0):builtins.int(num_bytes)]
            n = int(rx_buf.write(mv))
            if __debug__ and n != num_bytes: # receive buffer full
                _log = _g_log
                _log.write(f'{self.__class__.__name__}._xfer_cb: rx buffer full') # pyright: ignore[reportOptionalMemberAccess]
            _rx_flag = self._rx_flag
            _rx_flag.set() # set flag indicating data is available to be processed
            _submit_xfer = self._usb.submit_xfer
            _submit_xfer(ep, scratch_buf) # re-prime immediately for receiving data
        _xfer_done = self._xfer_done
        _xfer_done.set() # set flag indicating a USB transfer (in/out) completed

    def _desc_cfg(self, desc, itf_num: int, ep_num: int, strs: list[str|bytes]) -> None:
        ''' Build configuration descriptor contents

        Args:
            desc (_Descriptor): Descriptor helper to write the configuration descriptor bytes into
            itf_num (int): First bNumInterfaces value to assign
            ep_num (int): Address of the first available endpoint number to use for endpoint descriptor addresses
            strs (list[str | bytes]): List of string descriptors for this USB device
        '''
        _pack = desc.pack
        # Audio Control interface
        _pack('BBBBBBBBB',
              9,       # bLength
              4,       # bDescriptorType=INTERFACE
              itf_num, # bInterfaceNumber (unique ID)
              0,       # bAlternateSetting (unused) for USB MIDI 1.0)
              0,       # bNumEndpoints
              1,       # bInterfaceClass=AUDIO
              1,       # bInterfaceSubClass=AUDIO_CONTROL
              0,       # bInterfaceProtocol (unused)
              0)       # iInterface (index of string descriptor or 0 if none assigned)
        _pack('<BBBHHBB', 
              9,           # bLength (size of the descriptor in bytes)
              0x24,        # bDescriptorType=CS_INTERFACE
              1,           # bDescriptorSubType=MS_HEADER
              0x0100,      # bcdADC (USB MIDI 1.0 specs)
              9,           # wTotalLength (total size of class specific descriptors)
              1,           # bInCollection (number of streaming interfaces)
              itf_num + 1) # baInterfaceNr(1) (assign MIDIStreaming interface 1)
        # MIDI Streaming interface
        _pack('BBBBBBBBB',
              9,           # bLength
              4,           # bDescriptorType=INTERFACE
              itf_num + 1, # bInterfaceNumber (unique ID)
              0,           # bAlternateSetting (unused for USB MIDI 1.0)
              2,           # bNumEndpoints
              1,           # bInterfaceClass=AUDIO
              3,           # bInterfaceSubClass=MIDISTREAMING
              0,           # bInterfaceProtocol (unused)
              0)           # iInterface (index of string descriptor or 0 if none assigned)
        # Class-specific MIDI Streaming interface header
        wTotalLength = 7 + (num_ports := self.num_ports) * (6 + 9) + sum(external_jacks := self.external_jacks) * (6 + 9)
        _pack('<BBBHH',
              7,            # bLength (size of the descriptor in bytes)
              0x24,         # bDescriptorType=CS_INTERFACE
              1,            # bDescriptorSubType=MS_HEADER
              0x0100,       # bcdADC (USB MIDI 1.0 specs)
              wTotalLength) # wTotalLength (total size of class specific descriptors)
        # IN and OUT Jacks for each virtual IN and OUT Cable
        in_emb_jack_ids = []
        out_emb_jack_ids = []
        jack_id = 1
        for i, name in enumerate(self.port_names):
            # Embedded IN Jack for each virtual OUT Cable (required - create dummy if no OUT port is to be exposed)
            if name is None:
                iJack = 0
            else:
                iJack = len(strs)
                strs.append(name)
            _pack('<BBBBBB',
                  6,                           # bLength (size of the descriptor in bytes)
                  0x24,                        # bDescriptorType=CS_INTERFACE
                  2,                           # bDescriptorSubType=MIDI_IN_JACK
                  1,                           # bJackType=EMBEDDED
                  (in_emb_jack_id := jack_id), # bJackID (unique ID)
                  iJack)                       # iJack (index of string descriptor or 0 if none assigned)
            in_emb_jack_ids.append(jack_id)
            jack_id += 1
            # External IN Jack for each virtual OUT Cable (create dummy if no OUT port is to be exposed)
            if (ext_jacks := external_jacks[i]):
                _pack('<BBBBBB',
                    6,                           # bLength (size of the descriptor in bytes)
                    0x24,                        # bDescriptorType=CS_INTERFACE
                    2,                           # bDescriptorSubType=MIDI_IN_JACK
                    2,                           # bJackType=EXTERNAL
                    (in_ext_jack_id := jack_id), # bJackID (unique ID)
                    0)                           # iJack (index of string descriptor or 0 if none assigned)
                jack_id += 1
            # Embedded OUT Jack for each virtual OUT Cable (required - create dummy if no OUT port is to be exposed)
            in_jack_id = in_ext_jack_id if ext_jacks else in_emb_jack_id
            _pack('<BBBBBBBBB',
                  9,          # bLength (size of the descriptor in bytes)
                  0x24,       # bDescriptorType=CS_INTERFACE
                  3,          # bDescriptorSubType=MIDI_OUT_JACK
                  1,          # bJackType=EMBEDDED
                  jack_id,    # bJackID (unique ID)
                  1,          # bNrInputPins (number of input Pins on this MIDI OUT Jack)
                  in_jack_id, # baSourceID(1) (ID of the Entity to which the first Pin is connected)
                  1,          # baSourcePIN(1) (output Pin number for the Entity to which the first Pin is connected)
                  iJack)      # iJack (index of string descriptor or 0 if none assigned)
            out_emb_jack_ids.append(jack_id)
            jack_id += 1
            # External OUT Jack for each virtual OUT Cable (create dummy if no OUT port is to be exposed)
            if ext_jacks:
                _pack('<BBBBBBBBB',
                    9,              # bLength (size of the descriptor in bytes)
                    0x24,           # bDescriptorType=CS_INTERFACE
                    3,              # bDescriptorSubType=MIDI_OUT_JACK
                    2,              # bJackType=EXTERNAL
                    jack_id,        # bJackID (unique ID)
                    1,              # bNrInputPins (number of input Pins on this MIDI OUT Jack)
                    in_emb_jack_id, # baSourceID(1) (ID of the Entity to which the first Pin is connected)
                    1,              # baSourcePIN(1) (output Pin number for the Entity to which the first Pin is connected)
                    0)              # iJack (index of string descriptor or 0 if none assigned)
                jack_id += 1
        # Single shared OUT Endpoint
        # USB MIDI 1.0 spec requires 9-byte standard endpoint descriptor.
        # 7-byte version is accepted by Windows but rejected by Android's
        # ALSA snd-usb-midi driver which strictly validates descriptor length.
        self.ep_out = ep_num
        _pack('<BBBBHBBB',
              9,             # bLength (9 bytes per USB MIDI 1.0 spec, not 7)
              5,             # bDescriptorType=ENDPOINT
              ep_num,        # bEndpointAddress (0 to 15 with bit7=0 for OUT)
              2,             # bmAttributes (2 for Bulk)
              _EP_PCKT_SIZE, # wMaxPacketSize
              0,             # bInterval (ignored for Bulk)
              0,             # bRefresh (must be 0 for MIDI)
              0)             # bSynchAddress (must be 0 for MIDI)
        _pack('<BBBB' + num_ports * 'B',
              4 + num_ports,    # bLength (size of the descriptor in bytes)
              0x25,             # bDescriptorType=CS_ENDPOINT
              1,                # bDescriptorSubtype=MS_GENERAL
              num_ports,        # bNumEmbMIDIJack (number of Embedded MIDI IN Jacks)
              *in_emb_jack_ids) # baAssocJackID(1 to n) (IDs of the associated Embedded MIDI IN Jacks)
        # Single shared IN Endpoint (9 bytes per USB MIDI 1.0 spec)
        self.ep_in = (ep_in := ep_num | 0x80)
        _pack('<BBBBHBBB',
              9,             # bLength (9 bytes per USB MIDI 1.0 spec, not 7)
              5,             # bDescriptorType=ENDPOINT
              ep_in,         # bEndpointAddress (bit7=1 for IN: 128 to 143)
              2,             # bmAttributes (2 for Bulk)
              _EP_PCKT_SIZE, # wMaxPacketSize
              0,             # bInterval (ignored for Bulk)
              0,             # bRefresh (must be 0 for MIDI)
              0)             # bSynchAddress (must be 0 for MIDI)
        _pack('<BBBB' + num_ports * 'B',
              4 + num_ports,     # bLength (size of the descriptor in bytes)
              0x25,              # bDescriptorType=CS_ENDPOINT
              1,                 # bDescriptorSubtype=MS_GENERAL
              num_ports,         # bNumEmbMIDIJack (number of Embedded MIDI IN Jacks)
              *out_emb_jack_ids) # baAssocJackID(1 to n) (IDs of the associated Embedded MIDI OUT Jacks)

# Based on https://github.com/micropython/micropython-lib/blob/master/micropython/usb/usb-device/usb/device/core.py
# Copyright (c) 2022-2024 Angus Gratton, published under MIT licence
class _Descriptor:
    ''' Wrapper class for writing a descriptor in-place into a provided buffer

    Args:
        buf (bytearray): Buffer to write the descriptor into
    '''
    def __init__(self, buf: bytearray) -> None:
        self.buf = buf
        self.offset = 0 # offset of data written to the buffer

    def pack(self, fmt: str, *args) -> None:
        ''' Pack new data into the descriptor buffer, starting at the current offset

        Args:
            Arguments are the same as `struct.pack()`
        '''
        struct.pack_into(fmt, self.buf, self.offset, *args)
        self.offset += struct.calcsize(fmt)

    def pack_into(self, fmt: str, offset: int, *args) -> None:
        ''' Pack new data into the descriptor at a given offset

        Args:
            offset (int): First index from which to start adding data to descriptor
            Other arguments are the same as `struct.pack()`
        '''
        struct.pack_into(fmt, self.buf, offset, *args)
        self.offset = max(self.offset, offset + struct.calcsize(fmt))

    def extend(self, buf: bytearray|bytes|memoryview) -> None:
        ''' Extend the descriptor with some bytes-like data

        Args:
            buf (bytearray | bytes | memoryview): Bytes to add to descriptor
        '''
        self.buf[(offset := self.offset) : offset + len(buf)] = buf
        self.offset += len(buf)
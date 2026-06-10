"""
config.py -- MIDILAB V2
All hardware constants in one place. Edit this file only.

CHANGES FROM V1:
  Joystick X and Y swapped (physical wiring confirmed reversed).
  UART_MIDI_RX updated to GP9.
  All enable flags present for every MUX source.
"""

# I2C Bus 0 (PCF8575 key expanders, 400kHz)
I2C0_ID   = 0
I2C0_SDA  = 0
I2C0_SCL  = 1
I2C0_FREQ = 400_000

# PCF8575 expanders
PCF8575_INT    = 2
PCF8575_ADDR_0 = 0x20   # EXPANDER0, rows 0+1, all address pins to GND
PCF8575_ADDR_1 = 0x22   # EXPANDER1, rows 2+3, A1=VCC A0=A2=GND

# I2C Bus 1 (LCD, 100kHz)
I2C1_ID   = 1
I2C1_SDA  = 6
I2C1_SCL  = 7
I2C1_FREQ = 100_000
LCD_ADDR  = 0x27
LCD_ROWS  = 2
LCD_COLS  = 16

# MIDI DIN (UART1)
UART_ID      = 1
UART_MIDI_TX = 4
UART_MIDI_RX = 9    # GP9 -- only valid UART1 rx that avoids GP5 (MUX S1 conflict)
UART_BAUD    = 31250

# CD74HC4067 MUX
MUX_S0       = 3
MUX_S1       = 5
MUX_S2       = 8
MUX_S3       = 9
MUX_EN       = 10
MUX_ADC_PIN  = 26
MUX_SETTLE_US = 500   # microseconds; use time.sleep_us() not a busy-wait loop

# MUX channel assignments
# NOTE: Joystick X and Y are SWAPPED from V1 -- confirmed by hardware testing.
# Physical joystick X-axis (left/right = mod wheel) is on MUX CH8.
# Physical joystick Y-axis (forward/back = pitch bend) is on MUX CH7.
MUX_CH_POT_0   = 0
MUX_CH_POT_1   = 1
MUX_CH_POT_2   = 2
MUX_CH_POT_3   = 3
MUX_CH_POT_4   = 4
MUX_CH_PEDAL_0 = 5
MUX_CH_PEDAL_1 = 6
MUX_CH_JOY_X   = 8   # SWAPPED: was 7 in V1
MUX_CH_JOY_Y   = 7   # SWAPPED: was 8 in V1
MUX_CH_FSR_0   = 9
MUX_CH_FSR_1   = 10
MUX_CH_BREATH  = 11

# Source name to MUX channel lookup
SOURCE_TO_CH = {
    'pot_0': 0,   'pot_1': 1,   'pot_2': 2,   'pot_3': 3,  'pot_4': 4,
    'pedal_0': 5, 'pedal_1': 6,
    'joy_x': 8,   'joy_y': 7,   # swapped from V1
    'fsr_0': 9,   'fsr_1': 10,
    'breath': 11,
}

# HC-SR04 ultrasonic
# ECHO requires 1k/2.2k voltage divider (5V to ~3.4V) before GP12.
HCSR04_TRIG        = 11
HCSR04_ECHO        = 12
HCSR04_TIMEOUT_US  = 10_000
HCSR04_INTERVAL_MS = 50

# Rotary encoder
# External 2.2k pull-ups fitted on hardware -- no internal PULL_UP in firmware.
ENC_A   = 13
ENC_B   = 14
ENC_BTN = 15   # active HIGH, shares common anode; PULL_DOWN in firmware

# Joystick button
JOY_BTN = 19   # active LOW, internal PULL_UP

# RGB LED (common anode, sink logic)
# Confirmed by hardware test: encoder module pin2=Blue, pin4=Green.
# Cathodes: R via 1k to GP16, G via 1k to GP18, B via 220R to GP17.
LED_R    = 16
LED_G    = 18   # encoder module pin 4
LED_B    = 17   # encoder module pin 2
LED_FREQ = 1000

# Breath sensor MPXV4006DP
# Supply 5V (VBUS). Divider: 4.7k top, 10k bottom -> max 3.27V at ADC.
BREATH_RAW_MIN = 169
BREATH_RAW_MAX = 4053

# Key matrix
KEY_ROWS = 4
KEY_COLS = 7

# Hardware enable flags -- set False to disable a source without rewiring.
# All sources that are physically connected should be True.
PEDAL_1_ENABLED = True    # CH5
PEDAL_2_ENABLED = False   # CH6, jack not fitted
FSR_1_ENABLED   = True    # CH9
FSR_2_ENABLED   = False   # CH10, not fitted
BREATH_ENABLED  = True    # CH11 -- breath sensor reconnected
JOY_ENABLED     = True    # CH7/8
SONIC_ENABLED   = True    # HC-SR04 -- reconnected

"""
LAN Walkie -- Pico debug/bring-up firmware (MicroPython).

Not the final trigger firmware -- the actual upstream signal that should
drive PTT (a GPIO edge, a footswitch, a squelch line, whatever) is still
undefined. This is a standalone variant for testing the Pico <-> PC serial
link and getting visual feedback while that real trigger gets worked out.
GP0 below stays wired in as a placeholder for it -- either that or the
BOOTSEL button (see below) counts as "PTT held".

Two things this adds purely for hardware/software bring-up, not for talking
to the actual radio hardware:

  - The onboard BOOTSEL button, held down, also starts/stops talking.
    Reading it cleanly needs rp2.bootsel_button(), which only exists in
    MicroPython -- CircuitPython (what the "production" reference elsewhere
    uses) has no supported equivalent. That's the whole reason this is a
    separate MicroPython file instead of an addition to that one.
  - The onboard LED (GP25) reflects state. It's a single-color LED -- true
    red/green isn't physically possible on a stock Pico -- so this uses the
    closest honest equivalent:
        solid on  = PTT currently held (BOOTSEL or GP0)
        blinking  = audio is arriving from someone else (RX:1)
        off       = idle
    Holding PTT wins visually over blinking for RX, since seeing your own
    press reflected instantly is the most useful signal when bringing up
    hardware.

Same TX:/RX: wire protocol as the CircuitPython reference (see
SERIAL_PROTOCOL.md), just carried over the Pico's one USB-serial channel --
stock MicroPython has no separate data-only channel the way CircuitPython's
usb_cdc does. That's fine: the protocol already ignores any line it doesn't
recognize, including MicroPython's own banner text, specifically so it can
tolerate exactly this kind of thing.
"""

import select
import sys
import time

import rp2
from machine import Pin

led = Pin(25, Pin.OUT)
trigger = Pin(0, Pin.IN, Pin.PULL_DOWN)  # placeholder -- real trigger TBD

was_active = False
rx_active = False
blink_on = False
last_blink = time.ticks_ms()
BLINK_MS = 300

while True:
    active = bool(trigger.value()) or bool(rp2.bootsel_button())
    if active and not was_active:
        print("TX:1")
    elif not active and was_active:
        print("TX:0")
    was_active = active

    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline().strip()
        if line == "RX:1":
            rx_active = True
        elif line == "RX:0":
            rx_active = False

    if active:
        led.value(1)
    elif rx_active:
        now = time.ticks_ms()
        if time.ticks_diff(now, last_blink) >= BLINK_MS:
            blink_on = not blink_on
            last_blink = now
        led.value(blink_on)
    else:
        led.value(0)

    time.sleep_ms(20)

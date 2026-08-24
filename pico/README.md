# Pico debug/bring-up firmware

`main.py` in this folder is **not** the final trigger firmware — the actual
upstream signal that should drive PTT (a GPIO edge, a footswitch, a squelch
line, whatever it ends up being) is still undefined. This is a standalone
MicroPython variant for testing the Pico ↔ PC serial link and getting
visual feedback on the Pico itself, while that real trigger gets decided.

## What it does

Talks the same wire protocol the CLI already expects with `--serial`:
newline-terminated ASCII lines, both directions.

```
Pico -> PC (commands)          PC -> Pico (status)
  TX:1   start talking           RX:1   audio arriving, playing on line-out
  TX:0   stop talking            RX:0   nothing incoming right now
```

Two ways to trigger PTT, either one works:

- **GP0** — placeholder for whatever the real upstream trigger ends up
  being. Does nothing until something's actually wired to it.
- **The onboard BOOTSEL button, held down** — a debug/bring-up aid only,
  not connected to any real trigger hardware. Useful for confirming the
  serial link and the CLI/server side work before any real trigger exists.

The onboard LED (GP25) shows state:

- **Solid on** — PTT currently held (BOOTSEL or GP0)
- **Blinking** — someone else's audio is currently incoming (`RX:1`)
- **Off** — idle

A stock Pico's onboard LED is a single color, so there's no way to make it
show literal red vs. green — solid-vs-blinking is the honest equivalent on
this hardware. For real two-color feedback later, the simplest path is a
cheap external bicolor/RGB LED on two spare GPIOs; this file wouldn't need
much changed to add that once one's wired up.

## Why MicroPython, not CircuitPython

Reading BOOTSEL cleanly needs `rp2.bootsel_button()`, which only exists in
MicroPython. CircuitPython has no supported equivalent, which is why this
is a separate file/language rather than an addition to a CircuitPython
reference. The tradeoff: MicroPython doesn't have CircuitPython's clean,
separate `usb_cdc.data` channel, so this shares the Pico's one USB-serial
channel instead — harmless, since the protocol already ignores any line it
doesn't recognize (including MicroPython's own banner text) by design.

## Flashing it

1. Hold BOOTSEL, plug the Pico into USB — it mounts as a drive (`RPI-RP2`).
2. Drop MicroPython's `.uf2` for the **Raspberry Pi Pico** (not Pico W)
   onto it, from https://micropython.org/download/RPI_PICO/ — it reboots
   running MicroPython.
3. Copy this file onto the Pico as `main.py` (e.g. with
   [Thonny](https://thonny.org/), or `mpremote cp main.py :main.py`) so it
   runs automatically every time it powers up.

## Testing it against the CLI

Same pattern as the rest of this project's testing — verify the link works
before worrying about real audio hardware:

```bash
python3 lan_walkie_cli.py wss://<server-ip>:8443 --name "Test" \
    --serial /dev/ttyACM0 \
    --source "audiotestsrc is-live=true wave=silence" \
    --sink fakesink
```

Hold BOOTSEL: the CLI should print `[TALKING]` and the LED should go solid.
Release: it should go back to `[ready — space to talk]` (or show whoever
else is talking) and the LED should turn off — or start blinking, if
someone else happens to be talking on the channel at that moment.

**Not yet run against real hardware** — written and reasoned through, but
there's no Pico available to test this against in the environment this was
written in. The test above is the thing to actually run once you flash it.

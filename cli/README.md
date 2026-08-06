# LAN Walkie — headless CLI client

For machines with no browser: SSH into them, run this, talk. Uses real WebRTC
(GStreamer's `webrtcbin`) so it joins the exact same mesh as browser
participants — no bridge, no relay, no separate protocol.

## Why press-to-toggle instead of hold

A browser tab can detect "the mouse button is still down." An SSH session
can't — terminals only report keystrokes, not "key still held," even with
the terminal in raw mode. So PTT here is: press SPACE once to start
talking, press it again to stop. Functionally the same as the physical
button, just not literally analog.

## Install (Ubuntu Server, no GUI needed)

```bash
sudo apt update
sudo apt install -y python3-gi gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-nice gstreamer1.0-tools \
    python3-pip
pip install websockets --break-system-packages
pip install pyserial --break-system-packages   # only needed for --serial (hardware-triggered PTT)
```

Everything above is a stock Ubuntu package — no compiling, no pinned
versions, works the same on 22.04 and 24.04.

## Run it

```bash
python3 lan_walkie_cli.py wss://192.168.1.3:8443 --name "Farmnode"
```

(swap in optibox's real IP.) You'll see:

```
Connecting to wss://192.168.1.3:8443 as 'Farmnode'...
[ready — space to talk, q to quit]
```

Press **Space** to talk, **Space** again to stop, **q** to quit.

## Picking the right mic/speaker device

By default it uses `autoaudiosrc`/`autoaudiosink`, which auto-detects
whatever audio hardware is present. On a headless server that's often
nothing, or the wrong device (e.g. an HDMI dummy sink). List what's
actually there:

```bash
arecord -l   # capture devices
aplay -l     # playback devices
```

Then point the client at the right one:

```bash
python3 lan_walkie_cli.py wss://192.168.1.3:8443 --name "Farmnode" \
    --source "alsasrc device=hw:1,0" \
    --sink "alsasink device=hw:2,0"
```

## Testing without any audio hardware at all

Useful for checking the network/signaling side works before you've plugged
anything in:

```bash
python3 lan_walkie_cli.py wss://192.168.1.3:8443 --name "Test" \
    --source "audiotestsrc is-live=true wave=silence" \
    --sink fakesink
```

This still does the full real WebRTC handshake with other participants —
it just transmits silence and discards what it receives, instead of using
a mic/speaker.

## Running as a background service (systemd)

If this box should just always be listening on the channel:

```bash
sudo tee /etc/systemd/system/lan-walkie-cli.service << 'EOF'
[Unit]
Description=LAN Walkie CLI client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/lan-walkie-cli/lan_walkie_cli.py wss://192.168.1.3:8443 --name "Farmnode"
Restart=on-failure
User=your-username

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now lan-walkie-cli
```

Note: PTT via Space bar obviously doesn't work in a service with no
attached terminal. Without `--serial`, a service instance is listen-only —
useful on its own for a station that just needs to hear the channel, but
for anything that also needs to transmit unattended, see the next section.

## Hardware-triggered PTT (`--serial`) — for unattended, always-on nodes

The keyboard-based PTT above is fine for a person actively sitting at a
terminal, but it doesn't fit a box that runs 24/7 with nobody there. For
that case, pass `--serial /dev/ttyACM0` and something else — a Raspberry
Pi Pico, a footswitch controller, whatever's upstream — drives transmit
directly over USB serial, no keyboard involved:

```bash
python3 lan_walkie_cli.py wss://192.168.1.3:8443 --name "Farmnode" \
    --serial /dev/ttyACM0 \
    --source "alsasrc device=hw:1,0" \
    --sink "alsasink device=hw:2,0"
```

**Protocol**: newline-terminated ASCII lines over the serial connection,
in both directions.

```
Pico -> PC (commands)          PC -> Pico (status)
  TX:1   start talking           RX:1   audio arriving, playing on line-out
  TX:0   stop talking            RX:0   nothing incoming right now
```

`RX` fires automatically whenever the floor state changes — it's not
something you request, the client just tells the Pico whenever line-out
starts or stops carrying someone else's audio. Useful if the Pico needs
to react to incoming traffic too — light an LED, key an external
transmitter to relay the audio out, whatever the other side of this
bridge needs to know.

That's genuinely better than the keyboard toggle, not just a workaround
for the lack of a keyboard: a keyboard press/release can't be detected
over SSH, so the human-facing mode has to fake "hold" with a toggle. A
Pico *can* see a real rising and falling edge on whatever it's watching,
so `--serial` mode gets true hold-to-talk timing — it starts exactly when
the trigger goes active and stops exactly when it goes inactive, same as
the browser's PTT button.

Minimal MicroPython on the Pico side, USB CDC serial, both directions:

```python
import usb_cdc
import board
import digitalio

trigger = digitalio.DigitalInOut(board.GP0)  # whatever your upstream signal lands on
trigger.direction = digitalio.Direction.INPUT
trigger.pull = digitalio.Pull.DOWN

rx_led = digitalio.DigitalInOut(board.GP1)   # whatever should react to incoming audio
rx_led.direction = digitalio.Direction.OUTPUT

serial = usb_cdc.data
was_active = False

while True:
    # Outgoing: tell the PC when to transmit
    active = trigger.value
    if active and not was_active:
        serial.write(b"TX:1\n")
    elif not active and was_active:
        serial.write(b"TX:0\n")
    was_active = active

    # Incoming: react to whether audio is currently playing on line-out
    if serial.in_waiting:
        line = serial.readline().strip()
        if line == b"RX:1":
            rx_led.value = True
        elif line == b"RX:0":
            rx_led.value = False
```

Swap `trigger.value` for whatever the actual upstream IoT signal ends up
being — a GPIO from another board, a UART line, an I2C read. The Pico's
job is just "translate whatever that signal is doing into `TX:1`/`TX:0`
lines," the CLI doesn't need to know anything about the original trigger.

### Audio routing: line-in / line-out instead of a mic/speaker

`--source`/`--sink` accept any GStreamer ALSA element, so pointing them at
line-in/line-out instead of a mic/speaker is just a device string, not a
different code path:

```bash
arecord -l    # find the capture (line-in) device
aplay -l      # find the playback (line-out) device
```

Common gotcha: onboard codecs often default their *capture source* to the
built-in mic, not line-in, even when audio is physically wired into the
line-in jack. Check and fix with:

```bash
amixer scontrols                # list available controls
amixer sset 'Capture' cap       # or whatever the line-in-select control is called
alsamixer                       # interactive version — easier to find the right control
```

Worth confirming with the `--source ... --sink fakesink` / `audiotestsrc`
test flags first (see above) — verify the WebRTC side connects cleanly,
then swap in the real ALSA devices once that's confirmed.

### Combining with systemd

Since `--serial` mode doesn't need a keyboard, it's a good fit for the
systemd service above — just add the flags:

```
ExecStart=/usr/bin/python3 /opt/lan-walkie-cli/lan_walkie_cli.py wss://192.168.1.3:8443 \
    --name "Farmnode" --serial /dev/ttyACM0 \
    --source "alsasrc device=hw:1,0" --sink "alsasink device=hw:2,0"
```

### Priority — letting this node interrupt whoever's talking

By default this node is a normal participant: if a human's already
talking when the Pico fires, it gets `floor-denied` like anyone else and
just doesn't transmit that cycle. If the trigger driving this box should
always get through — an alert, a page, anything that shouldn't wait —
give it priority instead.

On the server, set a shared secret:

```bash
PRIORITY_TOKEN=some-long-random-string node server.js
```

(or as an env var in the Portainer stack — see the main README). Then
give this client the same token:

```bash
python3 lan_walkie_cli.py wss://192.168.1.3:8443 --name "Farmnode" \
    --token some-long-random-string --serial /dev/ttyACM0 ...
```

**The token is the actual identity check, not the `--name`.** Any
browser user can type any display name — names are cosmetic. The token
is what the server checks before granting priority, so keep it out of
version control (pass it via environment variable or a systemd
`EnvironmentFile`, not hardcoded in a committed script).

With a valid token, this node bumps whoever's currently talking instead
of getting denied — the bumped person's client gets an explicit
"interrupted by ⭐ Farmnode" notice and drops back to listening
immediately. Every participant — browser or CLI — sees a ⭐ next to this
node's name in the peer list, so it's visually obvious which one is the
priority node without having to guess from context.

Regular browser users and other CLI instances never need a token —
priority is opt-in per client, everything else behaves exactly as
before.

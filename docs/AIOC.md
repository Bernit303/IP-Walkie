# AIOC radio gateway

The CLI can bridge a handheld radio to the LAN channel using an
[AIOC](https://github.com/skuep/AIOC) (All-In-One-Cable), a small USB-C
adapter that presents a sound card, a virtual serial port and a CM108-style
HID device to the host. Flags and the systemd unit are in
[../cli/README.md](../cli/README.md); this page covers the hardware setup,
the design and its safety rules.

> **Status:** verified on real hardware (Ubuntu 24.04.5, GStreamer 1.24.2,
> an AIOC on a Baofeng handheld): the radio keys from LAN speech, radio audio
> reaches browsers and back, and it survives page reloads and reconnects. The
> logic is also unit-tested with GStreamer and the serial port stubbed. Not yet
> verified: a multi-day soak, other radios, other AIOC firmware versions.

## How it works

```
LAN -> radio:  floor{holder=other}  -> key PTT  (serial DTR=1, RTS=0)
               WebRTC audio -> mixer -> AIOC sound card -> radio mic
               floor{holder=null}   -> unkey after --ptt-tail-ms

radio -> LAN:  AIOC sound card -> level detect (VOX) -> unmute -> Opus -> peers
               VOX start -> ptt-start -> floor granted -> audio flows
               VOX stop  -> ptt-stop
```

The gateway is an ordinary participant; the server doesn't know it is a
gateway and the signaling protocol is unchanged.

### Why VOX

Receive detection works on the *audio level* because it works with any radio.
The AIOC also has a hardware carrier-detect (COS) input on hardware 1.2 with
firmware 1.4+, which needs a carrier-detect signal from the radio and is not
used. Consequences of VOX: radio noise with an open squelch occupies the
channel (set the squelch properly), and the first syllable can be clipped
(attack time plus the floor round trip).

### Safety rules

1. **Permission first.** The radio is keyed only after the server announces
   that someone else holds the floor.
2. **Always unkey.** The radio is unkeyed at startup, when the floor is free
   (after a short tail), when the gateway itself takes the floor, on signaling
   loss, on process exit, and after `--max-ptt-seconds` (default 90) whatever
   the server says.
3. **No self-trigger.** VOX is ignored while the radio is keyed and for
   0.5 s afterwards.
4. **No queue.** A denied request is dropped, like any other client.
5. **Firmware 1.2.0 or newer only.** Older AIOC firmware keys PTT on DTR
   alone, i.e. as soon as the serial port is opened.

### Priority and half-duplex

Radio users have no on-screen indication of who is talking, so the gateway is
normally run with the priority token. A handheld cannot listen while it
transmits: while the gateway is keyed (relaying a LAN speaker) it cannot hear
the radio side, so priority helps when the gateway is idle or just after a LAN
speaker releases, not mid-sentence. Feedback tones for radio users are not
implemented.

## Host setup (Ubuntu)

1. Flash AIOC firmware 1.2.0+ (see the AIOC repository) and plug it in:

   ```bash
   lsusb | grep -i 1209     # expect 1209:7388
   arecord -l               # note the AIOC card name
   ls /dev/ttyACM*
   ```

2. Stable device name and permissions:

   ```bash
   sudo tee /etc/udev/rules.d/99-aioc.rules << 'EOF'
   SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0660", GROUP="dialout", SYMLINK+="aioc"
   EOF
   sudo udevadm control --reload && sudo udevadm trigger
   sudo usermod -aG dialout,audio $USER    # log out and back in
   ```

3. Use a `plughw:CARD=<name>,DEV=0` ALSA device at 48 kHz.

## Levels

1. **Receive:** with `alsamixer -c <card>` (F4), set capture gain so normal
   received speech peaks around −12 dBFS without clipping; keep the radio's
   volume fixed.
2. **Transmit:** key the radio into a listening second handheld and raise the
   playback level until the audio is clear but not harsh.
3. **Squelch:** set it so idle noise never opens it, then tune
   `--vox-threshold-db`. `sudo alsactl store` to persist mixer settings. To rely
   on the radio's squelch alone, use the software squelch switch
   (`--squelch off` / `LAN_WALKIE_SQUELCH=off`, see `cli/README.md`).

## First tests

1. Run with `--source "audiotestsrc is-live=true wave=silence" --sink fakesink`:
   the radio must **not** be keyed at start.
2. Real devices, someone on the LAN speaks: radio keys, second handheld hears
   them, radio unkeys about 300 ms after they stop.
3. Speak on the second handheld: the browser shows the radio node talking.
4. Pull the network cable while the radio is keyed: it must unkey within about
   a second.

## What was verified, and what is still open

Verified on the reference setup:

- **PTT polarity:** DTR=1 / RTS=0 keys the radio; the idle state does not.
- **ALSA device:** the card shows up as `AllInOneCable`; use
  `plughw:CARD=AllInOneCable,DEV=0` (check `arecord -l` on your machine).
- **Levels:** the AIOC's capture and playback volumes default near maximum,
  which clips. About -15 dB capture and -20 dB playback (`amixer -c <card>
  cset numid=... <value>`, then `sudo alsactl store`) gave clean audio; the
  radio's squelch idle floor then sat near -64 dBFS and speech peaked around
  -17 dBFS, so the default VOX threshold of -40 dBFS works.
- **Receive detection** works from the audio level. `level` messages parse as
  `rms=(GValueArray)< a, b >`; the parser accepts that and the older forms.
- **Browser calls:** each peer gets its own RTP packetizer numbered from its own
  offer (Chrome offers Opus as payload 111, the gateway's own offers use 96).

Known limits and open items:

- A transmitter that is keyed but silent produces no audio, so the gateway
  cannot see it (no carrier detect through this cable). The floor is held only
  while the radio's speaker produces sound, plus the hang time.
- libnice's automatic UPnP is turned off (see `docs/MEMORY_LEAK_AUDIT.md`);
  without that the process grew by about 230 MB/min on Ubuntu 24.04.
- Memory drifts up by a small amount (about 0.15 MB) each time a peer joins and
  leaves. Not yet measured over days.
- Only one radio, one firmware version and one cable have been tried. Reports
  and fixes welcome.

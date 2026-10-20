#!/usr/bin/env python3
"""
LAN Walkie CLI — headless client for machines with no browser/GUI, and the
radio gateway for an AIOC (All-In-One-Cable, https://github.com/skuep/AIOC).

Speaks the exact same signaling protocol as the browser client, and joins
the mesh with real WebRTC (ICE, DTLS, SRTP) via GStreamer's webrtcbin, so
it interoperates directly with browser participants.

Two ways to run it:

  Interactive (a person at a terminal): press SPACE once to start talking,
  again to stop. Terminals can't see key release, so PTT is a toggle.

  Radio gateway (--aioc / --ptt-serial): the AIOC is the machine's sound
  card and PTT line. Someone on the LAN talks -> the radio is keyed and
  their audio plays into it. The radio receives something -> VOX on the
  AIOC's audio input requests the floor and the audio goes to the LAN.

Install (Ubuntu/Debian):
  sudo apt install python3-gi gir1.2-gst-plugins-bad-1.0 \\
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good \\
      gstreamer1.0-plugins-bad gstreamer1.0-nice gstreamer1.0-tools \\
      python3-websockets python3-serial

Run:
  python3 lan_walkie_cli.py wss://<server>:8443 --name "Radio" --aioc \\
      --alsa-device "plughw:CARD=AIOC,DEV=0"
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import ssl
import sys
import termios
import threading
import time
import tty

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC, GLib  # noqa: E402

Gst.init(None)

AIOC_USB_VID = 0x1209  # per the AIOC firmware-update docs (dfu-util -d 1209:7388)
AIOC_USB_PID = 0x7388
MAX_SIGNALING_BYTES = 256 * 1024  # SDP is a few KB; far above that is not signaling
LEVEL_INTERVAL_NS = 20_000_000  # 20 ms level reports for VOX


def safe_text(value, limit=64):
    """
    Peer-supplied strings (names, ids) go to a terminal and to logs. Make
    them printable: drop control/format characters and anything that cannot
    be encoded (lone surrogates), then cap the length.
    """
    text = str(value).encode("utf-8", "replace").decode("utf-8", "replace")
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:limit] or "?"


def sha256_pin(der_bytes):
    return hashlib.sha256(der_bytes).hexdigest()


def normalize_pin(pin):
    return re.sub(r"[^0-9a-f]", "", pin.lower())


class Vox:
    """
    Voice-activated detection on the radio's audio input. Pure logic, fed
    one level reading at a time, so it can be tested without GStreamer.

    update() returns "start" when audio has been above the threshold for
    attack_ms, "stop" when it has been below it for hang_ms, else None.
    Readings are ignored (and the detector re-armed) while `blocked` is
    true — i.e. while our own transmitter is keyed and its audio can leak
    back into the input.
    """

    def __init__(self, threshold_db=-40.0, attack_ms=60, hang_ms=800):
        self.threshold_db = threshold_db
        self.attack_ms = attack_ms
        self.hang_ms = hang_ms
        self.active = False
        self._above_ms = 0.0
        self._below_ms = 0.0

    def reset(self):
        self.active = False
        self._above_ms = 0.0
        self._below_ms = 0.0

    def update(self, level_db, dt_ms, blocked=False):
        if blocked:
            was_active = self.active
            self.reset()
            return "stop" if was_active else None

        if level_db > self.threshold_db:
            self._above_ms += dt_ms
            self._below_ms = 0.0
        else:
            self._below_ms += dt_ms
            self._above_ms = 0.0

        if not self.active and self._above_ms >= self.attack_ms:
            self.active = True
            return "start"
        if self.active and self._below_ms >= self.hang_ms:
            self.active = False
            return "stop"
        return None


class AiocPtt:
    """
    Keys the radio through the AIOC's virtual serial port.

    Per the AIOC docs (firmware >= 1.2.0), PTT is asserted when DTR=1 and
    RTS=0. Any other combination is receive. Firmware older than 1.2.0 keyed
    on DTR alone and would key the radio the moment the port is opened —
    update the firmware before using this.
    """

    def __init__(self, port):
        import serial

        self.port = port
        self.keyed = False
        self._ser = serial.Serial()
        self._ser.port = port
        # Idle state set before open() so it is applied as the port opens.
        self._ser.dtr = False
        self._ser.rts = True
        self._ser.open()
        self._apply(False)

    def _apply(self, on):
        self._ser.dtr = bool(on)
        self._ser.rts = not on
        self.keyed = bool(on)

    def key(self):
        if not self.keyed:
            self._apply(True)

    def unkey(self):
        # Always write the idle state, even if we believe we are not keyed.
        self._apply(False)

    def close(self):
        """Unkey and release the port. Safe to call more than once."""
        if not self._ser.is_open:
            return
        try:
            self.unkey()
        except Exception:
            pass
        try:
            self._ser.close()
        except Exception:
            pass
        self.keyed = False


def find_aioc_port():
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == AIOC_USB_VID and p.pid == AIOC_USB_PID:
            return p.device
    return None


class Peer:
    def __init__(self, peer_id, name, priority=False):
        self.id = peer_id
        self.name = name
        self.priority = priority
        self.webrtcbin = None
        self.tee_pad = None
        self.send_queue = None  # decouples this peer's sink from the shared capture thread
        self.mixer_pad = None
        self.recv_elements = []  # queue, depay, dec, conv, resample — for cleanup


class WalkieClient:
    def __init__(
        self,
        url,
        name,
        source,
        sink,
        stun,
        tls_mode,
        ca_file=None,
        pin=None,
        token=None,
        ptt=None,
        vox=None,
        ptt_tail_ms=300,
        max_ptt_seconds=90,
        vox_debug=False,
    ):
        self.url = url
        self.name = name
        self.source = source
        self.sink = sink
        self.stun = stun
        self.tls_mode = tls_mode  # "system" | "ca-file" | "pin" | "insecure"
        self.ca_file = ca_file
        self.pin = pin
        self.token = token
        self.verified = False  # set per connection once the server is authenticated

        self.ptt = ptt  # AiocPtt or None
        self.vox = vox  # Vox or None
        self.ptt_tail_ms = ptt_tail_ms
        self.max_ptt_seconds = max_ptt_seconds
        self._unkey_handle = None
        self._ptt_watchdog = None
        self._vox_blocked_until = 0.0
        self.vox_debug = vox_debug
        self._level_msgs = 0
        self._dbg_status_last = 0
        self._dbg_peak = float("-inf")
        self._dbg_last = time.monotonic()

        self.ws = None
        self.loop = None
        self.my_id = None
        self.peers = {}  # id -> Peer
        self.talking = False  # we asked for the floor and have not released it
        self.have_floor = False  # the server has granted it to us
        self.floor_holder = None

        self.pipeline = None
        self.tee = None
        self.mixer = None
        self.mic_mute = None

    # ---------- GStreamer setup ----------

    def build_pipeline(self):
        level = ""
        if self.vox:
            # Before the mute: the radio's audio is measured even while it is
            # not being sent to the LAN, which is exactly when VOX needs it.
            level = f"level name=lvl interval={LEVEL_INTERVAL_NS} post-messages=true ! "
        capture = (
            f"{self.source} ! audioconvert ! audioresample ! {level}"
            f"volume name=mic-mute mute=true ! "
            f"opusenc bitrate=32000 ! rtpopuspay pt=96 ! "
            f"capsfilter caps=application/x-rtp,media=audio,encoding-name=OPUS,"
            f"payload=96,clock-rate=48000 ! tee name=t"
        )
        playback = f"audiomixer name=mix ! audioconvert ! audioresample ! {self.sink}"
        # A permanent live silent input keeps the mixer producing output even
        # when no peer is sending. Without it the mixer waits for audio, the
        # sink never prerolls, and the WHOLE pipeline (including the capture
        # branch that VOX listens to) stays paused until someone talks.
        keepalive = "audiotestsrc is-live=true wave=silence ! mix."

        self.pipeline = Gst.parse_launch(f"{capture}  {playback}  {keepalive}")
        self.tee = self.pipeline.get_by_name("t")
        self.tee.set_property("allow-not-linked", True)  # fine to have zero peers connected
        self.mixer = self.pipeline.get_by_name("mix")
        self.mic_mute = self.pipeline.get_by_name("mic-mute")

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_bus_error)
        if self.vox:
            bus.connect("message::element", self.on_bus_element)
            GLib.timeout_add_seconds(5, self._check_levels_flowing)
            if self.vox_debug:
                GLib.timeout_add_seconds(2, self._debug_status)

        self.pipeline.set_state(Gst.State.PLAYING)

        # bus.add_signal_watch() dispatches through the GLib main context —
        # without a running main loop, error messages are silently never delivered.
        self.glib_loop = GLib.MainLoop()
        threading.Thread(target=self.glib_loop.run, daemon=True).start()

    def _status(self, text):
        """
        One-line state changes. On an interactive terminal they overwrite each
        other in place; everywhere else (systemd/journal, pipes, --vox-debug)
        each one is its own line, so none are lost or garbled by carriage returns.
        """
        if sys.stdout.isatty() and not self.vox_debug:
            print("\r" + text.ljust(40), end="", flush=True)
        else:
            print(text, flush=True)

    def _debug_status(self):
        n = self._level_msgs
        delta, self._dbg_status_last = n - self._dbg_status_last, n
        state = self.pipeline.get_state(0)[1].value_nick
        print(
            f"[vox] status: {delta} level readings in the last 2 s, pipeline {state}, "
            f"{len(self.peers)} peer(s), floor holder {self.floor_holder!r}, "
            f"radio keyed {bool(self.ptt and self.ptt.keyed)}",
            file=sys.stderr,
        )
        return True  # repeat

    def _check_levels_flowing(self):
        if self._level_msgs == 0:
            print(
                "[vox] WARNING: no audio-level readings from the input after 5 s — the "
                "capture device may be stalled, busy or the wrong --alsa-device/--source",
                file=sys.stderr,
            )
        return False  # one-shot

    def on_bus_error(self, bus, message):
        err, debug = message.parse_error()
        print(f"[gst error] {err} ({debug})", file=sys.stderr)

    # ---------- radio gateway: VOX (radio -> LAN) ----------

    def on_bus_element(self, bus, message):
        s = message.get_structure()
        if s is None or s.get_name() != "level":
            return
        level_db = self._level_from_structure(s)
        if level_db is None:
            return
        self._level_msgs += 1
        if self.vox_debug:
            self._dbg_peak = max(self._dbg_peak, level_db)
            now = time.monotonic()
            if now - self._dbg_last >= 1.0:
                print(
                    f"[vox] input peak {self._dbg_peak:6.1f} dBFS (threshold {self.vox.threshold_db:.0f})",
                    file=sys.stderr,
                )
                self._dbg_peak = float("-inf")
                self._dbg_last = now
        blocked = (self.ptt is not None and self.ptt.keyed) or time.monotonic() < self._vox_blocked_until
        # Don't ask for the floor while someone else holds it or we have no
        # session; the radio audio has nowhere to go.
        if self.ws is None or (self.floor_holder not in (None, self.my_id)):
            blocked = True
        event = self.vox.update(level_db, LEVEL_INTERVAL_NS / 1e6, blocked=blocked)
        if event == "start":
            self._status("[radio: carrier/voice detected]")
            asyncio.run_coroutine_threadsafe(self.start_talk(), self.loop)
        elif event == "stop":
            asyncio.run_coroutine_threadsafe(self.stop_talk(), self.loop)

    @staticmethod
    def _level_from_structure(s):
        try:
            rms = s.get_value("rms")
            if rms:
                return max(float(v) for v in rms)
        except Exception:
            pass
        # Some PyGObject builds don't convert GValueArray; parse the text form.
        # Seen on Ubuntu 24.04: "rms=(GValueArray)< -48.4, -48.4 >". Older
        # builds use (double) and/or { } instead, so accept any type tag.
        m = re.search(r"rms=\([A-Za-z]+\)[<{]\s*([^>}]*)[>}]", s.to_string())
        if m:
            try:
                return max(float(v) for v in m.group(1).split(","))
            except ValueError:
                return None
        return None

    # ---------- radio gateway: PTT (LAN -> radio) ----------

    def radio_key(self):
        if not self.ptt:
            return
        if self._unkey_handle:
            self._unkey_handle.cancel()
            self._unkey_handle = None
        if not self.ptt.keyed:
            self.ptt.key()
            self._status("[radio: TX keyed]")
        # Watchdog: the server's own transmit timeout should end a floor
        # first; this is the local backstop that guarantees the radio never
        # stays keyed if signaling or a peer misbehaves.
        if self._ptt_watchdog:
            self._ptt_watchdog.cancel()
        self._ptt_watchdog = self.loop.call_later(self.max_ptt_seconds, self._watchdog_unkey)

    def _watchdog_unkey(self):
        print("\n[radio: max PTT time reached — unkeying]", file=sys.stderr)
        self.radio_unkey(immediate=True)

    def radio_unkey(self, immediate=False):
        if not self.ptt:
            return
        if self._ptt_watchdog:
            self._ptt_watchdog.cancel()
            self._ptt_watchdog = None
        if self._unkey_handle:
            self._unkey_handle.cancel()
            self._unkey_handle = None
        if immediate or self.ptt_tail_ms <= 0:
            self._do_unkey()
        elif self.ptt.keyed:
            # Short tail so the last syllable isn't cut off at the radio.
            self._unkey_handle = self.loop.call_later(self.ptt_tail_ms / 1000.0, self._do_unkey)

    def _do_unkey(self):
        self._unkey_handle = None
        was_keyed = self.ptt.keyed
        self.ptt.unkey()  # always write the idle state, as a safety measure
        if not was_keyed:
            # Nothing was transmitting, so there is no TX leak to wait out.
            # Blocking VOX here would cancel our own radio->LAN transmission:
            # taking the floor calls this, and the block would make VOX report
            # "stop" right after "start" (the floor flapped on and off).
            return
        # Squelch tail / TX audio can leak into the input right after unkey.
        self._vox_blocked_until = time.monotonic() + 0.5
        self._status("[radio: TX released]")

    # ---------- WebRTC ----------

    def create_peer_connection(self, peer, initiator):
        peer_id = peer.id
        webrtcbin = Gst.ElementFactory.make("webrtcbin", f"peer-{peer_id}")
        webrtcbin.set_property("bundle-policy", "max-bundle")
        if self.stun:
            webrtcbin.set_property("stun-server", self.stun)

        self.pipeline.add(webrtcbin)
        webrtcbin.sync_state_with_parent()

        webrtcbin.connect("on-ice-candidate", self.on_ice_candidate, peer_id)
        webrtcbin.connect("pad-added", self.on_pad_added, peer)
        webrtcbin.connect(
            "notify::ice-connection-state",
            lambda wb, _pspec, pid=peer_id: print(
                f"[ice:{pid}] {wb.get_property('ice-connection-state').value_name}", file=sys.stderr
            ),
        )
        if initiator:
            webrtcbin.connect("on-negotiation-needed", self.on_negotiation_needed, peer_id)

        # Fan the shared outgoing audio into this peer's connection, through
        # a small leaky queue. Without it, a peer whose webrtcbin isn't
        # accepting data yet blocks the single capture streaming thread, which
        # also starves the level meter VOX depends on (radio -> LAN dies while
        # LAN -> radio keeps working).
        send_queue = Gst.ElementFactory.make("queue")
        send_queue.set_property("leaky", 2)  # downstream: drop oldest, never block
        send_queue.set_property("max-size-buffers", 0)
        send_queue.set_property("max-size-bytes", 0)
        send_queue.set_property("max-size-time", 200_000_000)  # 200 ms
        self.pipeline.add(send_queue)
        send_queue.sync_state_with_parent()

        tee_pad = self.tee.get_request_pad("src_%u")
        tee_pad.link(send_queue.get_static_pad("sink"))
        sink_pad = webrtcbin.get_request_pad("sink_%u")
        send_queue.get_static_pad("src").link(sink_pad)
        peer.tee_pad = tee_pad
        peer.send_queue = send_queue

        return webrtcbin

    def on_negotiation_needed(self, webrtcbin, peer_id):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, webrtcbin, peer_id)
        webrtcbin.emit("create-offer", None, promise)

    def on_offer_created(self, promise, webrtcbin, peer_id):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        set_promise = Gst.Promise.new()
        webrtcbin.emit("set-local-description", offer, set_promise)
        set_promise.interrupt()
        self._send_threadsafe(
            {"type": "offer", "target": peer_id, "sdp": {"type": "offer", "sdp": offer.sdp.as_text()}}
        )

    def handle_offer(self, peer_id, sdp_text):
        peer = self.peers[peer_id]
        if peer.webrtcbin is None:
            peer.webrtcbin = self.create_peer_connection(peer, initiator=False)

        ok, sdpmsg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(sdp_text.encode(), sdpmsg)
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)

        set_promise = Gst.Promise.new()
        peer.webrtcbin.emit("set-remote-description", offer, set_promise)
        set_promise.interrupt()

        answer_promise = Gst.Promise.new_with_change_func(self.on_answer_created, peer)
        peer.webrtcbin.emit("create-answer", None, answer_promise)

    def on_answer_created(self, promise, peer):
        promise.wait()
        reply = promise.get_reply()
        answer = reply.get_value("answer")
        set_promise = Gst.Promise.new()
        peer.webrtcbin.emit("set-local-description", answer, set_promise)
        set_promise.interrupt()
        self._send_threadsafe(
            {"type": "answer", "target": peer.id, "sdp": {"type": "answer", "sdp": answer.sdp.as_text()}}
        )

    def handle_answer(self, peer_id, sdp_text):
        peer = self.peers.get(peer_id)
        if not peer or not peer.webrtcbin:
            return
        ok, sdpmsg = GstSdp.SDPMessage.new()
        GstSdp.sdp_message_parse_buffer(sdp_text.encode(), sdpmsg)
        answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        promise = Gst.Promise.new()
        peer.webrtcbin.emit("set-remote-description", answer, promise)
        promise.interrupt()

    def on_ice_candidate(self, webrtcbin, mlineindex, candidate, peer_id):
        self._send_threadsafe(
            {"type": "ice", "target": peer_id, "candidate": {"candidate": candidate, "sdpMLineIndex": mlineindex}}
        )

    def handle_remote_ice(self, peer_id, candidate_dict):
        peer = self.peers.get(peer_id)
        if not peer or not peer.webrtcbin or not isinstance(candidate_dict, dict):
            return
        candidate = candidate_dict.get("candidate")
        mlineindex = candidate_dict.get("sdpMLineIndex", 0)
        if candidate:
            peer.webrtcbin.emit("add-ice-candidate", mlineindex, candidate)

    def on_pad_added(self, webrtcbin, pad, peer):
        if pad.direction != Gst.PadDirection.SRC:
            return
        # One audio receive chain per peer. A renegotiation that adds more
        # m-lines would otherwise overwrite the cleanup references and leak
        # the first chain's elements and mixer pad.
        if peer.mixer_pad is not None or peer.recv_elements:
            return
        queue = Gst.ElementFactory.make("queue")
        depay = Gst.ElementFactory.make("rtpopusdepay")
        dec = Gst.ElementFactory.make("opusdec")
        conv = Gst.ElementFactory.make("audioconvert")
        resample = Gst.ElementFactory.make("audioresample")

        for el in (queue, depay, dec, conv, resample):
            self.pipeline.add(el)
            el.sync_state_with_parent()
        peer.recv_elements = [queue, depay, dec, conv, resample]

        pad.link(queue.get_static_pad("sink"))
        queue.link(depay)
        depay.link(dec)
        dec.link(conv)
        conv.link(resample)

        mixer_pad = self.mixer.get_request_pad("sink_%u")
        resample.get_static_pad("src").link(mixer_pad)
        peer.mixer_pad = mixer_pad

    def remove_peer(self, peer_id):
        peer = self.peers.pop(peer_id, None)
        if not peer:
            return

        if peer.webrtcbin:
            peer.webrtcbin.set_state(Gst.State.NULL)
            self.pipeline.remove(peer.webrtcbin)

        if peer.tee_pad:
            self.tee.release_request_pad(peer.tee_pad)

        if peer.send_queue:
            peer.send_queue.set_state(Gst.State.NULL)
            self.pipeline.remove(peer.send_queue)

        for el in peer.recv_elements:
            el.set_state(Gst.State.NULL)
            self.pipeline.remove(el)

        if peer.mixer_pad:
            self.mixer.release_request_pad(peer.mixer_pad)

    def reset_for_reconnect(self):
        """
        Tear down everything tied to the old signaling connection so the next
        one starts clean. Peer ids and floor state come from the server and
        are meaningless once we've lost it — a stale webrtcbin left over from
        before the drop would just confuse renegotiation with whatever's
        handed out after we reconnect. The radio is unkeyed immediately: with
        no signaling nobody can be authorized to transmit through it.
        """
        for pid in list(self.peers.keys()):
            self.remove_peer(pid)
        self.my_id = None
        self.floor_holder = None
        self.talking = False
        self.have_floor = False
        self.mic_mute.set_property("mute", True)
        if self.vox:
            self.vox.reset()
        self.radio_unkey(immediate=True)

    # ---------- signaling ----------

    def _send_threadsafe(self, msg):
        # GStreamer callbacks fire on GLib/Gst threads, not the asyncio thread.
        asyncio.run_coroutine_threadsafe(self.send(msg), self.loop)

    async def send(self, msg):
        if self.ws:
            await self.ws.send(json.dumps(msg))

    async def handle_message(self, raw):
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            return
        mtype = msg.get("type")

        if mtype == "welcome":
            self.my_id = msg["id"]
            token = self.token if self.verified else None
            await self.send({"type": "join", "name": self.name, "token": token})

        elif mtype == "peers":
            incoming_ids = {p["id"] for p in msg["peers"]}
            # Drop departed peers first so cleanup can't be skipped by a
            # later failure while printing.
            for pid in list(self.peers.keys()):
                if pid not in incoming_ids:
                    name = safe_text(self.peers[pid].name)
                    self.remove_peer(pid)
                    print(f"-> {name} left")
            for p in msg["peers"]:
                if p["id"] not in self.peers:
                    peer = Peer(p["id"], p["name"], priority=bool(p.get("priority", False)))
                    self.peers[p["id"]] = peer
                    initiator = self.my_id < p["id"]
                    peer.webrtcbin = self.create_peer_connection(peer, initiator)
                    marker = "[priority] " if peer.priority else ""
                    print(f"-> {marker}{safe_text(p['name'])} joined")

        elif mtype == "offer":
            if msg["from"] not in self.peers:
                self.peers[msg["from"]] = Peer(msg["from"], msg.get("name", "?"))
            self.handle_offer(msg["from"], msg["sdp"]["sdp"])

        elif mtype == "answer":
            self.handle_answer(msg["from"], msg["sdp"]["sdp"])

        elif mtype == "ice":
            self.handle_remote_ice(msg["from"], msg.get("candidate"))

        elif mtype == "floor":
            self.floor_holder = msg.get("holder")
            name = safe_text(msg.get("name"))
            if self.floor_holder == self.my_id:
                self.have_floor = True
                self.mic_mute.set_property("mute", False)
                # We are the source; the radio must not be transmitting.
                self.radio_unkey(immediate=True)
                self._status("[TALKING]")
            elif self.floor_holder:
                self.mic_mute.set_property("mute", True)
                if self.have_floor:
                    self.have_floor = False
                    self.talking = False
                # Someone else is talking: their audio is arriving, send it to the radio.
                self.radio_key()
                marker = "[priority] " if msg.get("priority") else ""
                self._status(f"[{marker}{name} is talking]")
            else:
                self.mic_mute.set_property("mute", True)
                if self.have_floor:
                    self.have_floor = False
                    self.talking = False
                self.radio_unkey()
                self._status("[ready]")
            # Note: `talking` is deliberately NOT cleared by a broadcast for
            # someone else's floor when we never held it. A stale broadcast
            # racing our own pending ptt-start would otherwise drop our intent
            # and leave the mic open after release.

        elif mtype == "floor-denied":
            self.talking = False
            self.mic_mute.set_property("mute", True)
            self._status(f"[{safe_text(msg.get('name'))} is talking]")

        elif mtype == "floor-preempted":
            self.talking = False
            self.have_floor = False
            self.mic_mute.set_property("mute", True)
            print(f"\n[interrupted by priority node {safe_text(msg.get('by'))}]", flush=True)

    # ---------- PTT input (keyboard / VOX) ----------

    async def start_talk(self):
        if self.talking:
            return
        self.talking = True
        await self.send({"type": "ptt-start"})

    async def stop_talk(self):
        if not self.talking:
            return
        self.talking = False
        self.mic_mute.set_property("mute", True)
        await self.send({"type": "ptt-stop"})

    async def toggle_talk(self):
        if not self.talking:
            await self.start_talk()
        else:
            await self.stop_talk()

    def start_keyboard_thread(self, key_queue):
        def reader():
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while True:
                    ch = sys.stdin.read(1)
                    self.loop.call_soon_threadsafe(key_queue.put_nowait, ch)
                    if ch == "q":
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

        t = threading.Thread(target=reader, daemon=True)
        t.start()

    # ---------- TLS ----------

    def make_ssl_context(self):
        if not self.url.startswith("wss://"):
            return None
        if self.tls_mode == "insecure" or self.tls_mode == "pin":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.tls_mode == "ca-file":
            ctx = ssl.create_default_context(cafile=self.ca_file)
            # Python 3.13+ turns on strict X.509 checks by default, which
            # reject some self-signed leaf certificates. The certificate is
            # still fully verified against the file we pin it to.
            ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
            return ctx
        return ssl.create_default_context()

    def check_server_identity(self, ws):
        """Returns True if this connection's server is authenticated."""
        if self.url.startswith("ws://"):
            return False
        if self.tls_mode in ("system", "ca-file"):
            return True  # the TLS handshake already verified it
        ssl_obj = ws.transport.get_extra_info("ssl_object") if getattr(ws, "transport", None) else None
        if ssl_obj is None:
            raise ConnectionError("could not read the server certificate")
        fingerprint = sha256_pin(ssl_obj.getpeercert(binary_form=True))
        if self.tls_mode == "pin":
            if fingerprint != self.pin:
                raise ConnectionError(
                    f"server certificate does not match --pin-sha256 (got {fingerprint})"
                )
            return True
        print(f"[tls] UNVERIFIED server certificate, sha256 {fingerprint}", file=sys.stderr)
        return False

    # ---------- main loop ----------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.build_pipeline()

        ssl_ctx = self.make_ssl_context()

        import websockets

        quit_event = asyncio.Event()

        if self.ptt:
            print(f"[ready — radio gateway on {self.ptt.port}, Ctrl+C to quit]")
        elif sys.stdin.isatty():
            print("[ready — space to talk, q to quit]")
            key_queue = asyncio.Queue()
            self.start_keyboard_thread(key_queue)

            async def keys():
                while True:
                    ch = await key_queue.get()
                    if ch == " ":
                        await self.toggle_talk()
                    elif ch == "q":
                        print("\nDisconnected.")
                        quit_event.set()
                        return

            asyncio.create_task(keys())
        else:
            print("[ready — no TTY and no PTT interface given, this instance is listen-only]")

        if self.token and self.url.startswith("ws://"):
            print("[warning] plaintext ws:// — the priority token will not be sent", file=sys.stderr)

        # Outages are a "when, not if" on a 24/7 unattended box — the
        # signaling server rebooting, a power blip, or a flaky network link
        # all just look like the connection dropping. Reconnect with backoff
        # instead of exiting.
        async def connection_loop():
            backoff = 1
            while True:
                try:
                    print(f"Connecting to {self.url} as {safe_text(self.name)!r}...")
                    async with websockets.connect(self.url, ssl=ssl_ctx, max_size=MAX_SIGNALING_BYTES) as ws:
                        self.verified = self.check_server_identity(ws)
                        if self.token and not self.verified:
                            print(
                                "[tls] server not verified — joining WITHOUT the priority token. "
                                "Use --ca-file or --pin-sha256 to authenticate the server.",
                                file=sys.stderr,
                            )
                        self.ws = ws
                        backoff = 1
                        print("[connected]")
                        await self._recv_loop()
                except Exception as e:
                    print(f"[connection error] {e}", file=sys.stderr)

                self.ws = None
                self.verified = False
                self.reset_for_reconnect()
                print(f"[disconnected — retrying in {backoff}s]", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

        try:
            conn_task = asyncio.create_task(connection_loop())
            quit_task = asyncio.create_task(quit_event.wait())
            done, pending = await asyncio.wait({conn_task, quit_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # connection_loop() only ever finishes by raising (it's an
            # infinite retry loop) — surface that instead of swallowing it.
            if conn_task in done:
                conn_task.result()
        finally:
            # Whatever ended the process, never leave the transmitter keyed.
            if self.ptt:
                self.ptt.close()
            self.pipeline.set_state(Gst.State.NULL)

    async def _recv_loop(self):
        async for raw in self.ws:
            try:
                await self.handle_message(raw)
            except Exception as e:
                print(f"[error handling message] {safe_text(e, 200)}", file=sys.stderr)


def read_token(args):
    if args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    env = os.environ.get("LAN_WALKIE_TOKEN")
    if env:
        return env.strip() or None
    if args.token:
        print(
            "[warning] --token is visible to other users in `ps` and shell history; "
            "prefer --token-file or the LAN_WALKIE_TOKEN environment variable",
            file=sys.stderr,
        )
        return args.token
    return None


def main():
    parser = argparse.ArgumentParser(description="LAN Walkie headless CLI client / AIOC radio gateway")
    parser.add_argument("url", help="wss://<server>:8443")
    parser.add_argument("--name", default="Headless", help="Name shown to other participants")
    parser.add_argument(
        "--source",
        default="autoaudiosrc",
        help="GStreamer source element, e.g. autoaudiosrc, alsasrc device=hw:1,0, "
        "or 'audiotestsrc is-live=true wave=silence' for testing without a mic",
    )
    parser.add_argument(
        "--sink",
        default="autoaudiosink",
        help="GStreamer sink element, e.g. autoaudiosink, alsasink device=hw:1,0, "
        "or fakesink for testing without speakers",
    )
    parser.add_argument(
        "--alsa-device",
        default=None,
        help="Shortcut that sets both --source and --sink to this ALSA device, e.g. "
        "plughw:CARD=AIOC,DEV=0 (find the name with `arecord -l` / `aplay -l`).",
    )
    parser.add_argument(
        "--stun",
        default="stun://stun.l.google.com:19302",
        help="STUN server, or empty string to disable (pure-LAN/Tailscale setups usually don't need it; "
        "note the STUN server sees this machine's public IP)",
    )

    tls = parser.add_argument_group("TLS (wss:// is verified against the system CAs by default)")
    tls.add_argument("--ca-file", default=None, help="PEM file to trust, e.g. the server's own certs/cert.pem")
    tls.add_argument(
        "--pin-sha256",
        default=None,
        help="Expected SHA-256 fingerprint of the server certificate "
        "(openssl x509 -in cert.pem -noout -fingerprint -sha256)",
    )
    tls.add_argument(
        "--insecure",
        action="store_true",
        help="Skip certificate verification (the priority token is then never sent)",
    )

    radio = parser.add_argument_group("AIOC radio gateway")
    radio.add_argument(
        "--aioc",
        action="store_true",
        help=f"Auto-detect the AIOC's serial port (USB {AIOC_USB_VID:04x}:{AIOC_USB_PID:04x}) and run as a radio gateway",
    )
    radio.add_argument(
        "--ptt-serial",
        default=None,
        help="Serial port that keys the radio via DTR/RTS (e.g. /dev/ttyACM0). Implies gateway mode.",
    )
    radio.add_argument("--ptt-tail-ms", type=int, default=300, help="Keep the radio keyed this long after the floor frees")
    radio.add_argument("--max-ptt-seconds", type=int, default=90, help="Hard local limit on one keyed period")
    radio.add_argument(
        "--no-vox",
        action="store_true",
        help="Only send LAN audio to the radio; do not forward what the radio receives to the LAN",
    )
    radio.add_argument(
        "--vox-threshold-db",
        type=float,
        default=-40.0,
        help="Radio input level (dBFS RMS) that counts as received audio. Squelch must be closed below this.",
    )
    radio.add_argument(
        "--vox-debug",
        action="store_true",
        help="Print the radio input level once a second (use it to choose --vox-threshold-db)",
    )
    radio.add_argument("--vox-attack-ms", type=int, default=60)
    radio.add_argument("--vox-hang-ms", type=int, default=800)

    parser.add_argument(
        "--token-file",
        default=None,
        help="File containing the server's PRIORITY_TOKEN (also read from LAN_WALKIE_TOKEN). "
        "Priority lets this client interrupt whoever's talking.",
    )
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)  # deprecated: visible in `ps`
    args = parser.parse_args()

    if args.insecure and (args.ca_file or args.pin_sha256):
        parser.error("--insecure cannot be combined with --ca-file or --pin-sha256")
    pin = None
    if args.pin_sha256:
        pin = normalize_pin(args.pin_sha256)
        if len(pin) != 64:
            parser.error("--pin-sha256 must be a 64-hex-digit SHA-256 fingerprint")
    if args.insecure:
        tls_mode = "insecure"
    elif pin:
        tls_mode = "pin"
    elif args.ca_file:
        tls_mode = "ca-file"
    else:
        tls_mode = "system"

    source, sink = args.source, args.sink
    if args.alsa_device:
        source = f"alsasrc device={args.alsa_device}"
        sink = f"alsasink device={args.alsa_device}"

    ptt = None
    vox = None
    port = args.ptt_serial
    if args.aioc and not port:
        port = find_aioc_port()
        if not port:
            parser.error(
                f"no AIOC found (USB {AIOC_USB_VID:04x}:{AIOC_USB_PID:04x}); "
                "check `lsusb`, or pass --ptt-serial /dev/ttyACM0 explicitly"
            )
    if port:
        ptt = AiocPtt(port)  # opens the port and forces it to the idle (unkeyed) state
        if not args.no_vox:
            vox = Vox(args.vox_threshold_db, args.vox_attack_ms, args.vox_hang_ms)

    client = WalkieClient(
        url=args.url,
        name=args.name,
        source=source,
        sink=sink,
        stun=args.stun or None,
        tls_mode=tls_mode,
        ca_file=args.ca_file,
        pin=pin,
        token=read_token(args),
        ptt=ptt,
        vox=vox,
        ptt_tail_ms=args.ptt_tail_ms,
        max_ptt_seconds=args.max_ptt_seconds,
        vox_debug=args.vox_debug,
    )

    try:
        asyncio.run(client.run())
    except (KeyboardInterrupt, SystemExit):
        print("\nDisconnected.")
    finally:
        if ptt:
            ptt.close()  # idempotent; covers startup failures before run()


if __name__ == "__main__":
    main()

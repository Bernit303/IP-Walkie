#!/usr/bin/env python3
"""
LAN Walkie CLI — headless client for machines with no browser/GUI.

Speaks the exact same signaling protocol as the browser client, and joins
the mesh with real WebRTC (ICE, DTLS, SRTP) via GStreamer's webrtcbin, so
it interoperates directly with browser participants. No separate bridge,
no format conversion.

Terminals can't detect "button held down" the way a browser can, so PTT is
press-to-start / press-to-stop instead of hold: press SPACE once to start
talking, press it again to stop. Same idiom as the physical button, just
not literally analog.

Install (Ubuntu/Debian):
  sudo apt install python3-gi gir1.2-gst-plugins-bad-1.0 \\
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good \\
      gstreamer1.0-plugins-bad gstreamer1.0-nice gstreamer1.0-tools
  pip install websockets --break-system-packages

Run:
  python3 lan_walkie_cli.py wss://<server-ip>:8443 --name "HeadlessNode"

Controls once connected:
  SPACE   start/stop talking
  q       quit
"""

import argparse
import asyncio
import ssl
import sys
import termios
import threading
import tty
import json

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstSdp, GstWebRTC, GLib  # noqa: E402

Gst.init(None)


class Peer:
    def __init__(self, peer_id, name, priority=False):
        self.id = peer_id
        self.name = name
        self.priority = priority
        self.webrtcbin = None
        self.tee_pad = None
        self.mixer_pad = None
        self.recv_elements = []  # queue, depay, dec, conv, resample — for cleanup


class WalkieClient:
    def __init__(self, url, name, source, sink, stun, insecure, serial_port=None, token=None):
        self.url = url
        self.name = name
        self.source = source
        self.sink = sink
        self.stun = stun
        self.insecure = insecure
        self.serial_port = serial_port
        self.token = token
        self.serial_conn = None  # kept open for writes (RX signaling) as well as the read thread

        self.ws = None
        self.loop = None
        self.my_id = None
        self.peers = {}  # id -> Peer
        self.talking = False
        self.floor_holder = None

        self.pipeline = None
        self.tee = None
        self.mixer = None
        self.mic_mute = None

    # ---------- GStreamer setup ----------

    def build_pipeline(self):
        capture = (
            f"{self.source} ! audioconvert ! audioresample ! "
            f"volume name=mic-mute mute=true ! "
            f"opusenc bitrate=32000 ! rtpopuspay pt=96 ! "
            f"capsfilter caps=application/x-rtp,media=audio,encoding-name=OPUS,"
            f"payload=96,clock-rate=48000 ! tee name=t"
        )
        playback = f"audiomixer name=mix ! audioconvert ! audioresample ! {self.sink}"

        self.pipeline = Gst.parse_launch(f"{capture}  {playback}")
        self.tee = self.pipeline.get_by_name("t")
        self.tee.set_property("allow-not-linked", True)  # fine to have zero peers connected
        self.mixer = self.pipeline.get_by_name("mix")
        self.mic_mute = self.pipeline.get_by_name("mic-mute")

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self.on_bus_error)

        self.pipeline.set_state(Gst.State.PLAYING)

        # bus.add_signal_watch() dispatches through the GLib main context —
        # without a running main loop, error messages are silently never delivered.
        self.glib_loop = GLib.MainLoop()
        threading.Thread(target=self.glib_loop.run, daemon=True).start()

    def on_bus_error(self, bus, message):
        err, debug = message.parse_error()
        print(f"[gst error] {err} ({debug})", file=sys.stderr)

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

        # Fan the shared outgoing audio into this peer's connection.
        tee_pad = self.tee.get_request_pad("src_%u")
        sink_pad = webrtcbin.get_request_pad("sink_%u")
        tee_pad.link(sink_pad)
        peer.tee_pad = tee_pad

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
        if not peer or not peer.webrtcbin or not candidate_dict:
            return
        candidate = candidate_dict.get("candidate")
        mlineindex = candidate_dict.get("sdpMLineIndex", 0)
        if candidate:
            peer.webrtcbin.emit("add-ice-candidate", mlineindex, candidate)

    def on_pad_added(self, webrtcbin, pad, peer):
        if pad.direction != Gst.PadDirection.SRC:
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
        handed out after we reconnect.
        """
        for pid in list(self.peers.keys()):
            self.remove_peer(pid)
        self.my_id = None
        self.floor_holder = None
        self.talking = False
        self.mic_mute.set_property("mute", True)
        self.send_serial("RX:0")  # nothing can be arriving with no connection

    # ---------- signaling ----------

    def _send_threadsafe(self, msg):
        # GStreamer callbacks fire on GLib/Gst threads, not the asyncio thread.
        asyncio.run_coroutine_threadsafe(self.send(msg), self.loop)

    async def send(self, msg):
        if self.ws:
            await self.ws.send(json.dumps(msg))

    async def handle_message(self, raw):
        msg = json.loads(raw)
        mtype = msg.get("type")

        if mtype == "welcome":
            self.my_id = msg["id"]
            await self.send({"type": "join", "name": self.name, "token": self.token})

        elif mtype == "peers":
            incoming_ids = {p["id"] for p in msg["peers"]}
            for p in msg["peers"]:
                if p["id"] not in self.peers:
                    peer = Peer(p["id"], p["name"], priority=p.get("priority", False))
                    self.peers[p["id"]] = peer
                    initiator = self.my_id < p["id"]
                    peer.webrtcbin = self.create_peer_connection(peer, initiator)
                    label = f"⭐ {p['name']}" if peer.priority else p["name"]
                    print(f"-> {label} joined")
            for pid in list(self.peers.keys()):
                if pid not in incoming_ids:
                    print(f"-> {self.peers[pid].name} left")
                    self.remove_peer(pid)

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
            if self.floor_holder == self.my_id:
                self.mic_mute.set_property("mute", False)
                self.send_serial("RX:0")  # I'm transmitting, not receiving
                print("\r[TALKING]                        ", end="", flush=True)
            elif self.floor_holder:
                self.mic_mute.set_property("mute", True)
                self.talking = False
                self.send_serial("RX:1")  # someone else is talking — audio is arriving
                label = f"⭐ {msg.get('name')}" if msg.get("priority") else msg.get("name")
                print(f"\r[{label} is talking]      ", end="", flush=True)
            else:
                self.mic_mute.set_property("mute", True)
                self.talking = False
                self.send_serial("RX:0")
                print("\r[ready — space to talk]          ", end="", flush=True)

        elif mtype == "floor-denied":
            self.talking = False
            self.mic_mute.set_property("mute", True)
            print(f"\r[{msg.get('name')} is talking]      ", end="", flush=True)

        elif mtype == "floor-preempted":
            print(f"\n[interrupted by ⭐ {msg.get('by')}]", flush=True)

    # ---------- PTT input ----------

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

    def start_serial_thread(self):
        """
        Bidirectional serial link to a Pico (or similar) over USB CDC.

        Incoming (Pico -> this process), one line per event:
          TX:1   -> start talking
          TX:0   -> stop talking

        Outgoing (this process -> Pico), sent whenever the floor state
        changes, via send_serial():
          RX:1   -> audio is currently arriving and playing on line-out
          RX:0   -> nothing incoming right now (channel free, or it's me talking)
        """
        import serial

        def reader():
            self.serial_conn = serial.Serial(self.serial_port, baudrate=115200, timeout=1)
            print(f"[serial] listening on {self.serial_port}")
            try:
                while True:
                    line = self.serial_conn.readline().decode(errors="ignore").strip()
                    if line == "TX:1":
                        asyncio.run_coroutine_threadsafe(self.start_talk(), self.loop)
                    elif line == "TX:0":
                        asyncio.run_coroutine_threadsafe(self.stop_talk(), self.loop)
            finally:
                self.serial_conn.close()

        t = threading.Thread(target=reader, daemon=True)
        t.start()

    def send_serial(self, line):
        if not self.serial_conn:
            return
        try:
            self.serial_conn.write((line + "\n").encode())
        except Exception as e:
            print(f"[serial] write failed: {e}", file=sys.stderr)

    # ---------- main loop ----------

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.build_pipeline()

        ssl_ctx = None
        if self.url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if self.insecure:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE

        import websockets

        quit_event = asyncio.Event()

        # Input sources don't belong to any one connection attempt — start
        # them once, outside the reconnect loop below, so a Pico mid-session
        # never sees its serial link bounce and a keyboard operator never
        # loses keystrokes typed during a reconnect.
        if self.serial_port:
            self.start_serial_thread()
            print(f"[ready — driven by {self.serial_port}, Ctrl+C to quit]")
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
            print("[ready — no TTY and no --serial given, this instance is listen-only]")

        # Outages are a "when, not if" on a 24/7 unattended box — the
        # signaling server rebooting, a power blip, or a flaky network link
        # all just look like the connection dropping. Reconnect with backoff
        # instead of exiting; a clean process exit here previously looked
        # like success to systemd's `Restart=on-failure` and the service
        # would just stay dead until someone noticed and restarted it by
        # hand.
        async def connection_loop():
            backoff = 1
            while True:
                try:
                    print(f"Connecting to {self.url} as '{self.name}'...")
                    async with websockets.connect(self.url, ssl=ssl_ctx) as ws:
                        self.ws = ws
                        backoff = 1
                        print("[connected]")
                        await self._recv_loop()
                except Exception as e:
                    print(f"[connection error] {e}", file=sys.stderr)

                self.ws = None
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
            self.pipeline.set_state(Gst.State.NULL)

    async def _recv_loop(self):
        async for raw in self.ws:
            try:
                await self.handle_message(raw)
            except Exception as e:
                print(f"[error handling message] {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="LAN Walkie headless CLI client")
    parser.add_argument("url", help="wss://<server-ip>:8443")
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
        "--stun",
        default="stun://stun.l.google.com:19302",
        help="STUN server, or empty string to disable (pure-LAN setups usually don't need it)",
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify the server's TLS certificate against a real CA. "
        "Leave this off for the self-signed cert this project generates by default.",
    )
    parser.add_argument(
        "--serial",
        default=None,
        help="Serial device for hardware-triggered PTT (e.g. /dev/ttyACM0), for unattended "
        "setups where something else — a Pico, a footswitch, another sensor — decides when "
        "to transmit instead of a person at a keyboard. See cli/README.md for the protocol.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Shared secret matching the server's PRIORITY_TOKEN. If it matches, this client "
        "can interrupt whoever's currently talking instead of getting denied. Leave unset "
        "for a normal, non-priority participant.",
    )
    args = parser.parse_args()

    client = WalkieClient(
        url=args.url,
        name=args.name,
        source=args.source,
        sink=args.sink,
        stun=args.stun or None,
        insecure=not args.verify_tls,
        serial_port=args.serial,
        token=args.token,
    )

    try:
        asyncio.run(client.run())
    except (KeyboardInterrupt, SystemExit):
        print("\nDisconnected.")


if __name__ == "__main__":
    main()

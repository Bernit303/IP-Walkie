"""Linux/GStreamer regression: both audio directions, mixed RTP payloads, rejoin.

No server, browser, radio, or audio device. Run under an outer timeout:
  timeout -k 5s 90s python3 tools/audio_negotiation_probe.py
Use --repo to compare another checkout with the same test.
"""
import argparse
import gc
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / "cli"))
    import lan_walkie_cli as cli
    Gst, Sdp, WebRTC = cli.Gst, cli.GstSdp, cli.GstWebRTC

    c = cli.WalkieClient("wss://unused", "test", "audiotestsrc is-live=true wave=sine",
                         "fakesink sync=false", None, "system")
    c.build_pipeline()
    c.mic_mute.set_property("mute", False)  # simulate a granted floor
    remotes = {}
    errors = []
    outbound_sdps = []

    def description(kind, text):
        _, sdp = Sdp.SDPMessage.new()
        Sdp.sdp_message_parse_buffer(text.encode(), sdp)
        return WebRTC.WebRTCSessionDescription.new(kind, sdp)

    def set_description(wb, action, value):
        promise = Gst.Promise.new()
        wb.emit(action, value, promise)
        promise.interrupt()

    class Remote:
        def __init__(self, pid, pt, gateway_offerer=False):
            self.pid, self.pt = pid, pt
            self.received = 0
            self.nonzero_pcm = 0
            self.sent = 0
            self.pipeline = Gst.parse_launch(
                "webrtcbin name=wb bundle-policy=max-bundle "
                "audiotestsrc is-live=true wave=sine ! audioconvert ! audioresample ! opusenc ! "
                f"rtpopuspay pt={pt} name=pay ! application/x-rtp,media=audio,encoding-name=OPUS,"
                f"payload={pt},clock-rate=48000 ! wb.")
            self.wb = self.pipeline.get_by_name("wb")
            cli.configure_upnp(self.wb)
            self.wb.connect("on-ice-candidate", lambda wb, idx, cand:
                            c.handle_remote_ice(pid, {"sdpMLineIndex": idx, "candidate": cand}))
            self.wb.connect("pad-added", self.receive)
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect("message::error", lambda bus, msg: errors.append(str(msg.parse_error())))
            remotes[pid] = self
            peer = cli.Peer(pid, pid)
            c.peers[pid] = peer
            self.pipeline.set_state(Gst.State.PLAYING)
            if gateway_offerer:
                peer.webrtcbin = c.create_peer_connection(peer, True)
            else:
                time.sleep(1)
                self.wb.emit("create-offer", None, Gst.Promise.new_with_change_func(self.offer, None))

        def offer(self, promise, _):
            reply = promise.get_reply()
            offer = reply.get_value("offer")
            set_description(self.wb, "set-local-description", offer)
            c.handle_offer(self.pid, offer.sdp.as_text())

        def answer(self, promise, _):
            reply = promise.get_reply()
            answer = reply.get_value("answer")
            set_description(self.wb, "set-local-description", answer)
            c.handle_answer(self.pid, answer.sdp.as_text())

        def receive(self, wb, pad):
            if pad.direction != Gst.PadDirection.SRC:
                return
            branch = Gst.parse_bin_from_description(
                "queue ! rtpopusdepay ! opusdec ! audioconvert ! "
                "audio/x-raw,format=S16LE ! fakesink name=pcm sync=false signal-handoffs=true", True)
            branch.get_by_name("pcm").connect("handoff", self.pcm)
            self.pipeline.add(branch)
            pad.add_probe(Gst.PadProbeType.BUFFER, self.packet)
            pad.link(branch.get_static_pad("sink"))
            branch.sync_state_with_parent()

        def packet(self, pad, info):
            self.received += 1
            return Gst.PadProbeReturn.OK

        def pcm(self, sink, buf, pad):
            if any(buf.extract_dup(0, buf.get_size())):
                self.nonzero_pcm += 1

        def close(self):
            c.remove_peer(self.pid)
            self.pipeline.set_state(Gst.State.NULL)
            self.bus.remove_signal_watch()
            remotes.pop(self.pid, None)

    def send(msg):
        remote = remotes.get(msg["target"])
        if remote is None:
            return
        if msg["type"] == "ice":
            remote.wb.emit("add-ice-candidate", msg["candidate"]["sdpMLineIndex"], msg["candidate"]["candidate"])
        elif msg["type"] == "answer":
            outbound_sdps.append((msg["target"], "answer", msg["sdp"]["sdp"]))
            set_description(remote.wb, "set-remote-description", description(WebRTC.WebRTCSDPType.ANSWER, msg["sdp"]["sdp"]))
        elif msg["type"] == "offer":
            outbound_sdps.append((msg["target"], "offer", msg["sdp"]["sdp"]))
            set_description(remote.wb, "set-remote-description", description(WebRTC.WebRTCSDPType.OFFER, msg["sdp"]["sdp"]))
            remote.wb.emit("create-answer", None, Gst.Promise.new_with_change_func(remote.answer, None))

    c._send_threadsafe = send
    gateway_received = {}
    original_pad_added = c.on_pad_added

    def incoming(wb, pad, peer):
        if pad.direction == Gst.PadDirection.SRC:
            gateway_received[peer.id] = 0
            def count(pad, info):
                gateway_received[peer.id] += 1
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.BUFFER, count)
        original_pad_added(wb, pad, peer)
    c.on_pad_added = incoming

    def check(label):
        time.sleep(6)
        before = {pid: (r.received, r.nonzero_pcm, gateway_received.get(pid, 0)) for pid, r in remotes.items()}
        time.sleep(2)
        for pid, r in remotes.items():
            delta = (r.received - before[pid][0], r.nonzero_pcm - before[pid][1],
                     gateway_received.get(pid, 0) - before[pid][2])
            print(label, pid, 'gateway-to-remote RTP/PCM, remote-to-gateway RTP:', delta, flush=True)
            if min(delta) <= 0:
                errors.append('%s %s stalled direction: %r' % (label, pid, delta))
        for pid, kind, text in outbound_sdps:
            if 'm=audio' not in text or 'a=sendrecv' not in text:
                errors.append('%s %s missing sendrecv audio' % (pid, kind))

    try:
        Remote('chrome-111', 111)
        Remote('other-109', 109)
        Remote('gateway-offer-96', 96, True)
        check('mixed')
        remotes['chrome-111'].close()
        Remote('chrome-rejoined-111', 111)
        check('after-rejoin')
        for entry in outbound_sdps:
            print('SDP', entry[0], entry[1], [line for line in entry[2].splitlines()
                  if line.startswith(('m=audio', 'a=sendrecv', 'a=recvonly', 'a=rtpmap:'))], flush=True)
        if errors:
            print('FAIL:', errors, flush=True)
            return 1
        print('PASS: all peers received decoded non-silent audio; both RTP directions stayed active', flush=True)
        return 0
    finally:
        for remote in list(remotes.values()):
            remote.close()
        c.pipeline.set_state(Gst.State.NULL)
        c.glib_loop.quit()
        gc.collect()


if __name__ == '__main__':
    sys.exit(main())

"""
Memory-growth probe for the gateway's incoming-audio path. No browser, server
or audio device needed: a fake remote peer inside this process sends Opus audio
to the gateway's own WebRTC code, and the resident memory is sampled while
different amounts of the receive chain are in place.

    python3 tools/leak_probe.py

A healthy variant grows by under 2 MB/min.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cli"))
import lan_walkie_cli as cli
Gst, GstSdp, GstWebRTC = cli.Gst, cli.GstSdp, cli.GstWebRTC


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) / 1024.0


def parse_sdp(text, kind):
    ok, m = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(text.encode(), m)
    return GstWebRTC.WebRTCSessionDescription.new(kind, m)


def run_variant(name, tail, seconds=30):
    remote = Gst.parse_launch(
        "webrtcbin name=rw bundle-policy=max-bundle "
        "audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! opusenc ! "
        "rtpopuspay pt=111 ! application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000 ! rw.")
    rw = remote.get_by_name("rw")
    c = cli.WalkieClient("wss://x", "g", "audiotestsrc is-live=true wave=silence", "fakesink", None, "system")
    c.build_pipeline()
    peer = cli.Peer("remote", "Remote")
    c.peers["remote"] = peer
    keep = []

    if tail != "app":
        def custom_pad_added(webrtcbin, pad, p):
            if pad.direction != Gst.PadDirection.SRC or keep:
                return
            keep.append(pad)
            if tail == "unlinked":
                return
            names = {"queue": ["queue"], "depay": ["queue", "rtpopusdepay"],
                     "decode": ["queue", "rtpopusdepay", "opusdec"]}[tail]
            els = []
            for n in names:
                e = Gst.ElementFactory.make(n)
                c.pipeline.add(e)
                e.sync_state_with_parent()
                els.append(e)
            sink = Gst.ElementFactory.make("fakesink")
            sink.set_property("sync", False)
            c.pipeline.add(sink)
            sink.sync_state_with_parent()
            els.append(sink)
            pad.link(els[0].get_static_pad("sink"))
            for a, b in zip(els, els[1:]):
                a.link(b)
        c.on_pad_added = custom_pad_added

    pending = []

    def to_remote(msg):
        if msg["type"] == "answer":
            rw.emit("set-remote-description",
                    parse_sdp(msg["sdp"]["sdp"], GstWebRTC.WebRTCSDPType.ANSWER), Gst.Promise.new())
        elif msg["type"] == "ice":
            cand = msg["candidate"]
            rw.emit("add-ice-candidate", cand["sdpMLineIndex"], cand["candidate"])
    c._send_threadsafe = to_remote

    def on_remote_ice(wb, idx, cand):
        item = {"candidate": cand, "sdpMLineIndex": idx}
        if peer.webrtcbin is None:
            pending.append(item)
        else:
            c.handle_remote_ice("remote", item)
    rw.connect("on-ice-candidate", on_remote_ice)

    def on_offer_created(promise, _u):
        promise.wait()
        offer = promise.get_reply().get_value("offer")
        rw.emit("set-local-description", offer, Gst.Promise.new())
        c.handle_offer("remote", offer.sdp.as_text())
        for item in pending:
            c.handle_remote_ice("remote", item)
        pending.clear()

    rw.connect("on-negotiation-needed",
               lambda wb: wb.emit("create-offer", None, Gst.Promise.new_with_change_func(on_offer_created, None)))
    remote.set_state(Gst.State.PLAYING)
    time.sleep(8)
    a = rss_mb()
    time.sleep(seconds)
    b = rss_mb()
    print("%-30s %6.1f -> %6.1f MB  (%+.1f MB/min)" % (name, a, b, (b - a) * 60.0 / seconds), flush=True)
    remote.set_state(Gst.State.NULL)
    c.pipeline.set_state(Gst.State.NULL)
    c.glib_loop.quit()
    time.sleep(1)


for label, tail in (("1 incoming pad left unlinked", "unlinked"),
                    ("2 pad -> queue -> fakesink", "queue"),
                    ("3 + rtpopusdepay", "depay"),
                    ("4 + opusdec", "decode"),
                    ("5 the gateway's real chain", "app")):
    run_variant(label, tail)

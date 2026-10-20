"""
Logic tests for the CLI. GStreamer, termios and pyserial are stubbed, so these
run on any OS without audio hardware:

    python3 -m unittest discover -s cli -p "test_*.py"
"""

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class FakeSerial:
    instances = []

    def __init__(self):
        self.port = None
        self.dtr = None
        self.rts = None
        self.is_open = False
        FakeSerial.instances.append(self)

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False


def load_cli():
    for name in ("gi", "gi.repository", "termios", "tty"):
        sys.modules[name] = MagicMock()
    serial_mod = types.ModuleType("serial")
    serial_mod.Serial = FakeSerial
    sys.modules["serial"] = serial_mod
    path = Path(__file__).with_name("lan_walkie_cli.py")
    spec = importlib.util.spec_from_file_location("lan_walkie_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()


class VoxTests(unittest.TestCase):
    def test_start_after_attack_and_stop_after_hang(self):
        v = cli.Vox(threshold_db=-40, attack_ms=60, hang_ms=800)
        self.assertEqual([v.update(-20, 20) for _ in range(3)], [None, None, "start"])
        self.assertTrue(all(v.update(-60, 20) is None for _ in range(39)))
        self.assertEqual(v.update(-60, 20), "stop")

    def test_blocked_resets_and_stops(self):
        v = cli.Vox()
        for _ in range(5):
            v.update(-10, 20)
        self.assertEqual(v.update(-10, 20, blocked=True), "stop")
        self.assertFalse(v.active)

    def test_attack_needs_continuous_audio(self):
        v = cli.Vox()
        v.update(-10, 20)
        v.update(-90, 20)
        self.assertEqual(v._above_ms, 0)


class LevelParsingTests(unittest.TestCase):
    def test_uses_rms_list(self):
        s = MagicMock()
        s.get_value.return_value = [-31.5, -28.0]
        self.assertEqual(cli.WalkieClient._level_from_structure(s), -28.0)

    def test_falls_back_to_text_form(self):
        s = MagicMock()
        s.get_value.side_effect = TypeError("GValueArray")
        s.to_string.return_value = "level, endtime=(guint64)1, rms=(double){ -41.2, -39.9 }, peak=(double){ -30 };"
        self.assertEqual(cli.WalkieClient._level_from_structure(s), -39.9)

    def test_fallback_handles_angle_bracket_arrays(self):
        s = MagicMock()
        s.get_value.side_effect = TypeError("GValueArray")
        s.to_string.return_value = "level, endtime=(guint64)1, rms=(double)< -41.2, -35.5 >, peak=(double)< -30 >;"
        self.assertEqual(cli.WalkieClient._level_from_structure(s), -35.5)

    def test_real_ubuntu_24_04_message(self):
        s = MagicMock()
        s.get_value.side_effect = TypeError("GValueArray")
        s.to_string.return_value = (
            "level, endtime=(guint64)200000000, timestamp=(guint64)0, "
            "rms=(GValueArray)< -48.439406690821514, -48.439406690821514 >, "
            "peak=(GValueArray)< -46.28105621278533, -46.28105621278533 >;"
        )
        self.assertAlmostEqual(cli.WalkieClient._level_from_structure(s), -48.4394, places=3)

    def test_unparseable_returns_none(self):
        s = MagicMock()
        s.get_value.return_value = None
        s.to_string.return_value = "level, nothing=(int)1;"
        self.assertIsNone(cli.WalkieClient._level_from_structure(s))


class PipelineTests(unittest.TestCase):
    def test_mixer_has_permanent_silent_input(self):
        # Regression: an idle audiomixer stalls the whole pipeline, which
        # starved the capture-side level meter that VOX depends on.
        cli.Gst.parse_launch.reset_mock()
        c = cli.WalkieClient("wss://x", "R", "alsasrc", "alsasink", None, "system", vox=cli.Vox())
        c.build_pipeline()
        launch = cli.Gst.parse_launch.call_args[0][0]
        self.assertIn("audiotestsrc is-live=true wave=silence ! mix.", launch)
        self.assertIn("level name=lvl", launch)


class PeerWiringTests(unittest.TestCase):
    def test_each_peer_gets_a_leaky_queue_between_tee_and_webrtcbin(self):
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline = MagicMock()
        c.tee = MagicMock()
        made = []

        def make(factory, name=None):
            el = MagicMock(name=factory)
            el.factory = factory
            made.append(el)
            return el

        cli.Gst.ElementFactory.make.side_effect = make
        try:
            peer = cli.Peer("p1", "Dad")
            c.create_peer_connection(peer, initiator=False)
        finally:
            cli.Gst.ElementFactory.make.side_effect = None
        queues = [e for e in made if e.factory == "queue"]
        self.assertEqual(len(queues), 1)
        queues[0].set_property.assert_any_call("leaky", 2)
        self.assertIs(peer.send_queue, queues[0])
        # tee pad feeds the queue, not webrtcbin directly
        c.tee.get_request_pad.return_value.link.assert_called_once_with(queues[0].get_static_pad.return_value)

    def test_remove_peer_releases_the_queue(self):
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline, c.tee, c.mixer = MagicMock(), MagicMock(), MagicMock()
        peer = cli.Peer("p1", "Dad")
        peer.webrtcbin, peer.tee_pad, peer.send_queue = MagicMock(), MagicMock(), MagicMock()
        c.peers["p1"] = peer
        c.remove_peer("p1")
        peer.send_queue.set_state.assert_called_once()
        c.pipeline.remove.assert_any_call(peer.send_queue)


class DiagnosticsTests(unittest.TestCase):
    def test_sdp_lines_are_logged_only_in_debug_mode(self):
        import io
        from contextlib import redirect_stderr

        sdp = (
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
            "a=rtpmap:111 opus/48000/2\r\na=sendrecv\r\na=ice-ufrag:x\r\n"
        )
        quiet = cli.WalkieClient("wss://x", "R", "s", "k", None, "system")
        loud = cli.WalkieClient("wss://x", "R", "s", "k", None, "system", vox_debug=True)
        buf = io.StringIO()
        with redirect_stderr(buf):
            quiet._log_sdp("remote offer", sdp)
        self.assertEqual(buf.getvalue(), "")
        with redirect_stderr(buf):
            loud._log_sdp("remote offer", sdp)
        out = buf.getvalue()
        self.assertIn("a=sendrecv", out)
        self.assertIn("a=rtpmap:111 opus/48000/2", out)
        self.assertNotIn("ice-ufrag", out)

    def test_stats_counters_are_extracted(self):
        import io
        from contextlib import redirect_stderr

        c = cli.WalkieClient("wss://x", "R", "s", "k", None, "system")
        promise = MagicMock()
        promise.get_reply.return_value.to_string.return_value = (
            'x, a=(structure)"outbound-rtp, packets-sent=(guint64)812, bytes-sent=(guint64)9;", '
            'b=(structure)"inbound-rtp, packets-received=(guint64)40;";'
        )
        buf = io.StringIO()
        with redirect_stderr(buf):
            c._on_stats(promise, "Dad")
        self.assertIn("sent ['812']", buf.getvalue())
        self.assertIn("received ['40']", buf.getvalue())

    def test_mixer_has_latency_slack(self):
        cli.Gst.parse_launch.reset_mock()
        c = cli.WalkieClient("wss://x", "R", "alsasrc", "alsasink", None, "system", vox=cli.Vox())
        c.build_pipeline()
        self.assertIn("audiomixer name=mix latency=200000000", cli.Gst.parse_launch.call_args[0][0])


class StatusLineTests(unittest.TestCase):
    def test_non_tty_prints_plain_lines_without_carriage_returns(self):
        import io
        from contextlib import redirect_stdout

        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        buf = io.StringIO()  # StringIO.isatty() is False, like a journal or a pipe
        with redirect_stdout(buf):
            c._status("[radio: TX keyed]")
            c._status("[ready]")
        self.assertEqual(buf.getvalue(), "[radio: TX keyed]\n[ready]\n")


class SafeTextTests(unittest.TestCase):
    def test_strips_control_and_surrogates(self):
        s = cli.safe_text("Dad\x1b[2J\r\nX\ud800Y")
        for bad in "\x1b\r\n\ud800":
            self.assertNotIn(bad, s)

    def test_keeps_normal_text(self):
        self.assertEqual(cli.safe_text("Mamá ✓"), "Mamá ✓")

    def test_caps_length(self):
        self.assertEqual(len(cli.safe_text("a" * 500, limit=10)), 10)


class AiocPttTests(unittest.TestCase):
    def test_idle_key_unkey_close(self):
        ptt = cli.AiocPtt("/dev/ttyACM0")
        ser = FakeSerial.instances[-1]
        self.assertFalse(ser.dtr)
        self.assertTrue(ser.rts)
        self.assertFalse(ptt.keyed)
        ptt.key()
        self.assertTrue(ser.dtr and not ser.rts and ptt.keyed)  # DTR=1 RTS=0
        ptt.unkey()
        self.assertFalse(ser.dtr)
        self.assertTrue(ser.rts)
        ptt.key()
        ptt.close()
        ptt.close()  # idempotent
        self.assertFalse(ser.is_open)
        self.assertFalse(ser.dtr)


def floor(holder, name=None, priority=False):
    return json.dumps({"type": "floor", "holder": holder, "name": name, "priority": priority})


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, **kw):
        ptt = cli.AiocPtt("/dev/ttyACM9")
        c = cli.WalkieClient(
            "wss://x", "Radio", "src", "sink", None, "system",
            ptt=ptt, vox=cli.Vox(), ptt_tail_ms=50, max_ptt_seconds=1, **kw,
        )
        c.loop = asyncio.get_running_loop()
        c.mic_mute = MagicMock()
        c.my_id = "aaaa"
        c.sent = []

        async def fake_send(m):
            c.sent.append(m)

        c.send = fake_send
        return c, ptt

    async def test_keys_on_other_floor_and_unkeys_after_tail(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertTrue(ptt.keyed)
        await c.handle_message(floor(None))
        self.assertTrue(ptt.keyed)  # tail
        await asyncio.sleep(0.15)
        self.assertFalse(ptt.keyed)

    async def test_own_floor_unkeys_immediately(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor("aaaa", "Radio"))
        self.assertFalse(ptt.keyed)
        self.assertTrue(c.have_floor)

    async def test_taking_the_floor_does_not_lock_out_vox(self):
        # Regression: the gateway granted itself the floor for radio->LAN audio,
        # the "make sure the radio is unkeyed" step blocked VOX for 0.5 s, VOX
        # reported "stop", and the floor was released again immediately.
        c, ptt = self.make_client()
        c.vox.active = True
        await c.handle_message(floor("aaaa", "Radio"))
        self.assertEqual(c._vox_blocked_until, 0.0)
        self.assertEqual(c.vox.update(-10, 20, blocked=False), None)  # still active, not stopped
        self.assertTrue(c.vox.active)

    async def test_real_unkey_still_blocks_vox_briefly(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertTrue(ptt.keyed)
        await c.handle_message(floor(None))
        await asyncio.sleep(0.15)
        self.assertFalse(ptt.keyed)
        self.assertGreater(c._vox_blocked_until, 0.0)

    async def test_watchdog_unkeys(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await asyncio.sleep(1.2)
        self.assertFalse(ptt.keyed)

    async def test_reconnect_reset_unkeys(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        c.reset_for_reconnect()
        self.assertFalse(ptt.keyed)

    async def test_stale_floor_keeps_pending_request(self):
        c, _ = self.make_client()
        c.talking, c.have_floor = True, False
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertTrue(c.talking)
        await c.handle_message(json.dumps({"type": "floor-denied", "name": "Dad"}))
        self.assertFalse(c.talking)

    async def test_losing_a_held_floor_clears_intent(self):
        c, _ = self.make_client()
        c.talking, c.have_floor = True, True
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertFalse(c.talking)
        self.assertFalse(c.have_floor)

    async def test_token_only_on_verified_connection(self):
        c, _ = self.make_client()
        c.token = "secret-token-value"
        c.verified = False
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa"}))
        self.assertIsNone(c.sent[-1]["token"])
        c.verified = True
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa"}))
        self.assertEqual(c.sent[-1]["token"], "secret-token-value")

    async def test_non_object_messages_ignored(self):
        c, _ = self.make_client()
        for raw in ("null", "[]", '"x"', "42"):
            await c.handle_message(raw)

    async def test_server_identity_modes(self):
        c, _ = self.make_client()

        class Ssl:
            def getpeercert(self, binary_form=False):
                return b"cert-bytes"

        ws = types.SimpleNamespace(transport=types.SimpleNamespace(get_extra_info=lambda k: Ssl()))
        c.url, c.tls_mode, c.pin = "wss://x", "pin", cli.sha256_pin(b"cert-bytes")
        self.assertTrue(c.check_server_identity(ws))
        c.pin = "0" * 64
        with self.assertRaises(ConnectionError):
            c.check_server_identity(ws)
        c.tls_mode = "insecure"
        self.assertFalse(c.check_server_identity(ws))
        c.url, c.tls_mode = "ws://x", "system"
        self.assertFalse(c.check_server_identity(ws))


if __name__ == "__main__":
    unittest.main()

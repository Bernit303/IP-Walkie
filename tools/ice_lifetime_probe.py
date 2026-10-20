"""
Object-lifetime probe for the libnice UPnP switch.

Turning UPnP off means reaching the NiceAgent through webrtcbin's "ice-agent"
property. On GStreamer 1.24.2 the Python wrapper for that ICE object seems to
fight with webrtcbin over who owns it, which produces GLib CRITICAL
("G_IS_OBJECT (object)" failed) messages when peers are removed. This tool
creates and destroys 20 webrtcbin elements per variant, each in a fresh process,
and reports for each way of holding the wrapper:

  * how many GLib critical/assertion lines were printed (0 is what we want)
  * whether the process aborted
  * file-descriptor and memory growth (a "never free it" workaround would show
    up here as a leak)
  * whether the UPnP-off setting reads back as off

    python3 tools/ice_lifetime_probe.py

Linux only. No network, browser, radio or audio device is used.
"""

import argparse
import gc
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CYCLES = 20
VARIANTS = (
    ("none", "no UPnP change at all (control: must be clean)"),
    ("attr", "wrapper stored on the element (the code as pushed; known bad)"),
    ("ref-temp", "give the wrapper its own reference, keep nothing"),
    ("ref-attr", "give the wrapper its own reference AND store it on the element"),
    ("ref-after", "own reference, wrapper released only after the element is gone"),
)
KEEP = []


def add_ref(obj):
    """g_object_ref() on the C object behind a PyGObject wrapper (hash() is its address)."""
    import ctypes

    lib = ctypes.CDLL("libgobject-2.0.so.0")
    lib.g_object_ref.argtypes = [ctypes.c_void_p]
    lib.g_object_ref.restype = ctypes.c_void_p
    lib.g_object_ref(ctypes.c_void_p(hash(obj)))


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) / 1024.0


def fds():
    return len(os.listdir("/proc/self/fd"))


def child(variant):
    sys.path.insert(0, os.path.join(HERE, "..", "cli"))
    import lan_walkie_cli as cli

    Gst = cli.Gst
    notes = []
    readback = None
    pending = []
    gc.collect()
    base_fds, base_rss = fds(), rss_mb()
    for i in range(CYCLES):
        wb = Gst.ElementFactory.make("webrtcbin", None)
        wb.set_property("bundle-policy", "max-bundle")
        if variant != "none":
            ice = wb.get_property("ice-agent")
            if i == 0:
                notes.append("ice refcount after get=%s" % getattr(ice, "__grefcount__", "?"))
            agent = ice.get_property("agent")
            agent.set_property("upnp", False)
            if i == 0:
                readback = agent.get_property("upnp")
            if variant.startswith("ref"):
                before = getattr(ice, "__grefcount__", "?")
                add_ref(ice)
                if i == 0:
                    notes.append("refcount %s -> %s" % (before, getattr(ice, "__grefcount__", "?")))
            if variant in ("attr", "ref-attr"):
                wb._ice = ice
            elif variant == "ref-after":
                pending.append(ice)
            del agent
            del ice
        wb.set_state(Gst.State.READY)
        wb.set_state(Gst.State.NULL)
        del wb
        gc.collect()
        if variant == "ref-after":
            pending.clear()
            gc.collect()
    gc.collect()
    print("RESULT fds=%+d rss=%+.1fMB upnp_off_readback=%s %s"
          % (fds() - base_fds, rss_mb() - base_rss, (not readback) if readback is not None else "n/a",
             "; ".join(notes)), flush=True)


def parent():
    print("%d create/destroy cycles per variant, each in a fresh process\n" % CYCLES)
    print("%-10s %-9s %-9s %s" % ("variant", "criticals", "aborted", "details"))
    for name, desc in VARIANTS:
        proc = subprocess.run([sys.executable, __file__, "--child", name],
                              capture_output=True, text=True, timeout=120)
        lines = (proc.stderr or "").splitlines()
        criticals = sum(1 for ln in lines if "CRITICAL" in ln or "assertion" in ln)
        result = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), "no RESULT line")
        aborted = "yes (%d)" % proc.returncode if proc.returncode != 0 else "no"
        print("%-10s %-9d %-9s %s" % (name, criticals, aborted, result.replace("RESULT ", "")))
        print("           %s" % desc)
        if criticals and not proc.returncode:
            sample = next((ln for ln in lines if "assertion" in ln or "CRITICAL" in ln), "")
            print("           e.g. %s" % sample[:110])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--child", choices=[v for v, _ in VARIANTS])
    args = ap.parse_args()
    if args.child:
        child(args.child)
    else:
        parent()

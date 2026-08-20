"""Runtime gate for the one-radio reservation contract. NO RADIO TOUCHED.

Born 2026-08-20, out of two lock leaks found the same morning: aero_panel
returned from its retry loop still holding p80 (seen in the wild as a stale
lock at 11:30Z), and radio_panel.open_sdr released only if the Device()
constructor threw - not if setSampleRate..activateStream did.

py_compile proves syntax. labtuna_doctor's bare-open rule proves a lock is
MENTIONED. Neither proves the lock comes BACK, which is the only property
that matters to the next process that needs the radio. This drives the real
radio_lock (real files on disk) against a FAKE SoapySDR and asserts:

  1. a tool refuses to open while a foreign holder is live (and does not
     clobber that holder's lock),
  2. the lock is released after a normal run,
  3. the lock is released when device SETUP throws after the acquire.

Run it before pushing anything that touches an SDR open path:
    python tools/test_radio_lock_discipline.py

It refuses to run if a REAL holder has the radio, so it can never steal the
lock from a live capture. Exit 0 = all green, 1 = a leak, 2 = radio busy.

LAW THIS ENCODES: a test that passes on the broken code proves nothing.
--negative-control replays the same assertion against the pre-fix code from
git and REQUIRES it to fail; that is what makes a green run meaningful.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import radio_lock                                            # noqa: E402

RESULTS = []

# The commit whose radio_panel.py still LEAKS the lock on a setup failure -
# the negative control's known-bad specimen. A pinned sha, never a relative
# ref (see negative_control()). 51e7ac7 is the fix; this is its parent.
PREFIX_REV = "51e7ac7~1"


def rec(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def lock_free():
    return radio_lock.status() is None


def clear():
    radio_lock.LOCK.unlink(missing_ok=True)
    radio_lock.WANT.unlink(missing_ok=True)


# ---------------------------------------------------------------- fake SDR
class _FakeStream:
    pass


class FakeDevice:
    """Enough of SoapySDR.Device to run a capture loop.

    mode: 'ok' streams samples | 'deaf' never delivers (dud session)
          'setup' raises in setSampleRate | 'ctor' raises on construction
    """

    def __init__(self, mode="ok"):
        self.mode = mode
        if mode == "ctor":
            raise RuntimeError("fake: device open failed")

    def setSampleRate(self, *a):
        if self.mode == "setup":
            raise RuntimeError("fake: setSampleRate failed")

    def setFrequency(self, *a):
        pass

    def setAntenna(self, *a):
        pass

    def setGainMode(self, *a):
        pass

    def setGain(self, *a):
        pass

    def writeSetting(self, *a):
        pass

    def readSetting(self, *a):
        return "false"

    def getSettingInfo(self, *a):
        return []

    def setupStream(self, *a):
        return _FakeStream()

    def activateStream(self, *a):
        pass

    def deactivateStream(self, *a):
        pass

    def closeStream(self, *a):
        pass

    def readStream(self, st, buffs, n, timeoutUs=0):
        r = types.SimpleNamespace()
        if self.mode == "deaf":
            time.sleep(0.01)
            r.ret = -1
            return r
        buf = buffs[0]
        k = min(n, len(buf) // 2)
        buf[:2 * k] = 1000            # nonzero: dud-burners must see signal
        r.ret = k
        return r


def install_fake_soapy(mode="ok"):
    made = []

    class _Mod(types.ModuleType):
        SOAPY_SDR_RX = 0
        SOAPY_SDR_CS16 = "CS16"
        SOAPY_SDR_CF32 = "CF32"
        SOAPY_SDR_FATAL = 0

        @staticmethod
        def SoapySDR_setLogLevel(*a):
            pass

        @staticmethod
        def Device(*a, **k):
            d = FakeDevice(mode)
            made.append(d)
            return d

        @staticmethod
        def errToStr(*a):
            return "fake"

    sys.modules["SoapySDR"] = _Mod("SoapySDR")
    return made


def load(name, path):
    """Fresh import each time; module-level state must not carry over."""
    import importlib.util
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def ns(**kw):
    return types.SimpleNamespace(**kw)


# ------------------------------------------------------------------ checks
def refuses_while_held(name, path, entry, mk):
    """A foreign p100 holder is live; the tool must not open, and must not
    clobber the holder's lock."""
    clear()
    radio_lock.LOCK.write_text(json.dumps({
        "owner": "TESTER", "purpose": "synthetic pass", "priority": 100,
        "pid": os.getpid(),                    # alive, so never swept
        "since": radio_lock._now().isoformat(),
        "heartbeat": radio_lock._now().isoformat()}))
    made = install_fake_soapy("ok")
    mod = load(name, path)
    try:
        getattr(mod, entry)(mk())
    except SystemExit:
        pass                                   # exiting is a valid refusal
    except RuntimeError as e:
        if "busy" not in str(e).lower() and "lock" not in str(e).lower():
            raise
    st = radio_lock.status() or {}
    rec(f"{name}.{entry}: refuses to open while held",
        len(made) == 0 and st.get("owner") == "TESTER",
        f"devices_opened={len(made)} lock_owner={st.get('owner')}")
    clear()


def releases_after_success(name, path, entry, mk):
    clear()
    install_fake_soapy("ok")
    mod = load(name, path)
    late = None
    try:
        getattr(mod, entry)(mk())
    except SystemExit:
        pass
    except Exception as e:
        late = type(e).__name__                # late failure is ok; a leak is not
    rec(f"{name}.{entry}: released after success", lock_free(),
        f"leftover={radio_lock._read(radio_lock.LOCK)} late_err={late}")
    clear()


def releases_after_setup_failure(name, path, entry, args=(), kwargs=None,
                                 mk=None):
    """THE 8/20 BUG: setup threw after the acquire and the lock leaked."""
    clear()
    install_fake_soapy("setup")
    mod = load(name, path)
    raised = None
    try:
        if mk is not None:
            getattr(mod, entry)(mk())
        else:
            getattr(mod, entry)(*args, **(kwargs or {}))
    except SystemExit:
        pass
    except Exception as e:
        raised = type(e).__name__
    rec(f"{name}.{entry}: released after SETUP failure", lock_free(),
        f"leftover={radio_lock._read(radio_lock.LOCK)} raised={raised}")
    clear()


# The suite. Entry points must TERMINATE (no endless monitors) except where
# the refusal path returns early - wx_alerts is refusal-only for that reason.
CAPTURES = [
    ("ais", str(HERE / "ais.py"), "cmd_capture",
     lambda: ns(secs=0.05, antenna="Antenna C", quiet=True)),
    ("aprs", r"Z:\src\hamTuna\tools\aprs.py", "cmd_capture",
     lambda: ns(secs=0.05, antenna="Antenna C")),
]
REFUSAL_ONLY = [
    ("wx_alerts", r"Z:\src\wxTuna\tools\wx_alerts.py", "cmd_monitor",
     lambda: ns(khz=162550, antenna="Antenna C")),
]
OPENERS = [
    ("hd_radio", str(HERE / "hd_radio.py"), "open_sdr", (98.7,)),
    ("radio_panel", str(HERE / "radio_panel.py"), "open_sdr", (98.7,)),
]


def run_suite():
    for name, path, entry, mk in CAPTURES + REFUSAL_ONLY:
        try:
            refuses_while_held(name, path, entry, mk)
        except Exception as e:
            rec(f"{name}.{entry}: refuses while held", False, f"harness {e!r}")
            clear()
    for name, path, entry, mk in CAPTURES:
        try:
            releases_after_success(name, path, entry, mk)
        except Exception as e:
            rec(f"{name}.{entry}: released after success", False, f"harness {e!r}")
            clear()
        try:
            releases_after_setup_failure(name, path, entry, mk=mk)
        except Exception as e:
            rec(f"{name}.{entry}: released after SETUP failure", False,
                f"harness {e!r}")
            clear()
    for name, path, entry, args in OPENERS:
        try:
            releases_after_setup_failure(name, path, entry, args=args)
        except Exception as e:
            rec(f"{name}.{entry}: released after SETUP failure", False,
                f"harness {e!r}")
            clear()


def negative_control():
    """Replay the setup-failure assertion against the PRE-FIX code from git.
    It MUST leak, or this suite does not discriminate and a green run above
    means nothing."""
    print("\n--- negative control (pre-fix code must FAIL)")
    repo = HERE.parent
    tmp = Path(os.environ.get("TEMP", ".")) / "_prefix_radio_panel.py"
    try:
        # bytes, not text=True: a decode hiccup here used to silently skip
        # the control and print a green run anyway (a FALSE ALL-CLEAR).
        #
        # PIN THE ANCHOR, never a relative ref: this first said
        # labday-0820~1, then a later commit on that branch moved ~1 onto
        # the FIXED code and the control quietly compared the fix against
        # itself. Same stale-anchor bug as E45. The pre-fix content is
        # whatever last touched the file BEFORE the fix commit.
        src = subprocess.run(
            ["git", "-C", str(repo), "show",
             f"{PREFIX_REV}:tools/radio_panel.py"],
            capture_output=True, timeout=30)             # pipe-ok: git show
        if src.returncode != 0 or not src.stdout:
            print(f"  SKIP: pre-fix revision unavailable "
                  f"(rc={src.returncode}, "
                  f"{(src.stderr or b'').decode('utf-8', 'replace').strip()[:80]})")
            return None
        tmp.write_bytes(src.stdout)
    except Exception as e:
        print(f"  SKIP: {e}")
        return None
    clear()
    install_fake_soapy("setup")
    mod = load("_prefix_radio_panel", str(tmp))
    try:
        mod.open_sdr(98.7)
    except Exception:
        pass
    leaked = not lock_free()
    left = radio_lock._read(radio_lock.LOCK)
    clear()
    tmp.unlink(missing_ok=True)
    if leaked:
        print(f"  [OK] pre-fix code LEAKS (owner={left.get('owner') if left else None})"
              f" - the suite discriminates")
    else:
        print("  [BAD] pre-fix code also released - this suite proves nothing")
    return leaked


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--negative-control", action="store_true",
                    help="also prove the suite fails on the pre-fix code")
    a = ap.parse_args()

    print("radio_lock discipline gate (fake SDR, real lock files)")
    print(f"LOCK = {radio_lock.LOCK}")
    if not lock_free():
        print(f"\nREFUSING TO RUN: a real holder has the radio: "
              f"{radio_lock.status()}")
        print("This gate never steals the lock from a live capture.")
        return 2

    run_suite()
    nc = negative_control() if a.negative_control else None

    print("\n" + "=" * 62)
    bad = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    for n, _, d in bad:
        print(f"  FAILED: {n}  {d}")
    if nc is False:
        print("  NEGATIVE CONTROL FAILED - a green run here is not evidence")
        return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

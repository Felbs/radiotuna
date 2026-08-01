# kill-ok: the docstring below DESCRIBES the anti-hard-kill law (its
# "Stop-Process" mentions are prose, not kills) - nothing in this file
# kills a process; it IS the graceful alternative.
"""radio_lock.py — the one-radio reservation file.

Born 2026-07-19: the day lab's mystery wedges and 4%-delivery
starvation turned out to be TWO OF OUR OWN DAEMONS fighting over the
single-tenant RSPdx (the warden rotating test campaigns into every gap
the lab left). The SDRplay API gives no arbitration — so this file is
it. Every SDR-touching process in the fleet:

  1. calls acquire(owner, purpose, priority) before opening the radio,
  2. heartbeats while holding it (heartbeat() inside read loops, or a
     Holder context in a with-block),
  3. releases on exit, and
  4. polls should_yield() during long holds — a higher-priority waiter
     (a Meteor pass, a human clicking LISTEN) means wrap up and let go,
  5. polls stop_requested(owner) each work unit — the GRACEFUL STOP.

STOP-FILE CONVENTION (added 2026-08-01, after three SDRplay API wedges
in 24 h, every one caused by Stop-Process on a holder mid-stream —
each cost a service restart plus a sacrificial stream probe, because
the first stream after a wedge delivers 0 samples):

    <owner>.stop in the lock directory, next to radio.lock.json
    (i.e. stop_file(owner))

  * Operator side: request_stop(owner) — or just create the empty
    file — to ask a running holder to wind down. NEVER Stop-Process
    a streaming holder.
  * Holder side: check stop_requested(owner) between work units
    (same cadence as should_yield). On seeing it: clear_stop(owner)
    to acknowledge, close the stream, release the lock, exit 0.
  * A fresh run calls clear_stop(owner) right after acquiring, so a
    stale stop-file from a previous run can never kill a new one.
    request_stop therefore only targets the run in progress.

Priorities (higher outranks) — doc updated 8/01 to match fleet practice
(labTuna's audit caught this list two tiers behind reality):
  100  satellite pass / timed-catch recorders (unrepeatable events)
   90  window-hold reservers (sonde_window_hold: reserve ahead of a pass)
   80  human listening (hd_listen, the panels)
   60  user-driven lab runs (the user asked for it now)
   50  laboratory campaigns (hd_day_lab, hunts, baselines)
   20  background rotation (the warden, prop_atlas, storm_watch)

Stale locks (heartbeat older than TTL, or holder PID dead) are swept
automatically — a crashed process never wedges the fleet. Nothing here
preempts: the lock is cooperative, like the wx-pass yield guard that
proved the pattern on three live Meteor passes.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

LOCK = Path(r"Z:\SDR_Agent_v2\radio.lock.json")
WANT = Path(r"Z:\SDR_Agent_v2\radio.want.json")
TTL_S = 90.0


def _now():
    return datetime.now(timezone.utc)


def _read(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


_STILL_ACTIVE = 259          # Windows STILL_ACTIVE exit code


def _pid_alive(pid):
    """True only if the process is genuinely still running.

    FIXED 2026-07-31: OpenProcess alone is NOT a liveness test. Windows keeps
    the process object alive as a "zombie" while any handle to it remains, so
    OpenProcess SUCCEEDS on a process that has already exited -- verified
    against a PID that Get-Process reported as dead. That made this function
    return True forever for a crashed holder, which silently disabled the
    stale-lock sweep: a killed experiment kept the radio reserved and every
    other job skipped its cycle waiting for a corpse to finish.

    The reliable test is GetExitCodeProcess: a live process reports
    STILL_ACTIVE (259), an exited one reports its real exit code.
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True                  # cannot tell -> be polite
            return code.value == _STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:
        return True          # can't tell -> assume alive (be polite)


def status():
    """Current holder dict, or None if the radio is free (or stale)."""
    st = _read(LOCK)
    if not st:
        return None
    try:
        hb = datetime.fromisoformat(st["heartbeat"])
        if (_now() - hb).total_seconds() > TTL_S:
            return None                      # stale — sweep on acquire
        if not _pid_alive(st.get("pid", 0)):
            return None
    except Exception:
        return None
    return st


def acquire(owner, purpose, priority, wait_s=0.0):
    """Take the radio. Returns True on success. If held by someone,
    registers intent in the want-file (so the holder's should_yield()
    fires if we outrank them) and polls up to wait_s."""
    deadline = time.time() + wait_s
    while True:
        st = status()
        if st is None or st.get("owner") == owner:
            LOCK.write_text(json.dumps({
                "owner": owner, "purpose": purpose,
                "priority": int(priority), "pid": os.getpid(),
                "since": _now().isoformat(),
                "heartbeat": _now().isoformat()}))
            try:
                w = _read(WANT)
                if w and w.get("owner") == owner:
                    WANT.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        # register intent BEFORE giving up: even a wait_s=0 caller
        # leaves a want-file so the holder's should_yield() can fire.
        # Highest-priority want wins the file (a fresh low-prio ask
        # must not mask a pending pass recorder).
        w = _read(WANT)
        fresh = False
        try:
            fresh = w and (_now() - datetime.fromisoformat(
                w["asked"])).total_seconds() < TTL_S
        except Exception:
            pass
        if not (fresh and int(w.get("priority", 0)) > int(priority)):
            WANT.write_text(json.dumps({
                "owner": owner, "purpose": purpose,
                "priority": int(priority), "asked": _now().isoformat()}))
        if time.time() >= deadline:
            return False
        time.sleep(2.0)


def heartbeat():
    st = _read(LOCK)
    if st and st.get("pid") == os.getpid():
        st["heartbeat"] = _now().isoformat()
        LOCK.write_text(json.dumps(st))


def release(owner=None):
    st = _read(LOCK)
    if st and (owner is None or st.get("owner") == owner) \
            and st.get("pid") == os.getpid():
        LOCK.unlink(missing_ok=True)


def should_yield():
    """Reason string if a higher-priority waiter wants the radio."""
    st = _read(LOCK)
    if not st or st.get("pid") != os.getpid():
        return None
    w = _read(WANT)
    if not w:
        return None
    try:
        if (_now() - datetime.fromisoformat(w["asked"])).total_seconds() > TTL_S:
            return None
    except Exception:
        return None
    if int(w.get("priority", 0)) > int(st.get("priority", 0)):
        return f"{w['owner']} ({w.get('purpose', '?')}) outranks us"
    return None


def stop_file(owner):
    """Path of `owner`'s cooperative stop-file (see module docstring)."""
    return LOCK.with_name(owner + ".stop")


def request_stop(owner):
    """Operator side: ask a running holder to wind down gracefully."""
    stop_file(owner).touch()


def stop_requested(owner):
    """Holder side: poll this between work units, like should_yield()."""
    try:
        return stop_file(owner).exists()
    except Exception:
        return False


def clear_stop(owner):
    """Consume the stop-file: on acquire (stale-file guard) and as the
    acknowledgement when a stop is honored."""
    try:
        stop_file(owner).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


class Holder:
    """with radio_lock.Holder('lab', 'cube slot', 50): ..."""

    def __init__(self, owner, purpose, priority, wait_s=0.0):
        self.owner, self.purpose = owner, purpose
        self.priority, self.wait_s = priority, wait_s
        self.ok = False

    def __enter__(self):
        self.ok = acquire(self.owner, self.purpose, self.priority,
                          self.wait_s)
        return self

    def __exit__(self, *exc):
        if self.ok:
            release(self.owner)
        return False

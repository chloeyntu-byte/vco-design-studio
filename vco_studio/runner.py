"""One bounded subprocess runner for every solver launch (Spectre HB/PN, AC, ...).

Guarantees:
  * the child runs in its own session/process group;
  * timeout, cancellation and *any* interruption of the caller (Ctrl-C, SIGTERM
    turned into an exception, unexpected error) terminate the whole group
    (SIGTERM, then SIGKILL) -- no orphaned Spectre keeps a CPU or disk busy;
  * elapsed time uses a monotonic clock;
  * the stop reason is explicit: None | 'timeout' | 'cancelled' | 'interrupted'.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


class SolverInterrupted(RuntimeError):
    pass


def wrap_env(argv, env_script, label='vco-env'):
    """Source an environment script (if given) in a bash child, then exec argv."""
    if env_script and Path(env_script).is_file():
        return ['/bin/bash', '-c', 'source "$1" >/dev/null; shift; exec "$@"', label, str(env_script)] + [str(a) for a in argv]
    return [str(a) for a in argv]


def _kill_group(proc, grace=3.0):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def run_bounded(command, cwd, timeout_s, stdout_path, cancel_event=None, on_start=None, poll_s=0.2):
    """Run ``command`` in ``cwd``; returns a dict receipt fragment.

    ``on_start(pid)`` is called right after launch (e.g. to persist a receipt).
    Raises SolverInterrupted after cleaning up if the caller is interrupted.
    """
    if timeout_s is None or not (float(timeout_s) > 0):
        raise ValueError('timeout_s must be a positive number of seconds')
    timeout_s = float(timeout_s)
    t0 = time.monotonic()
    wall0 = time.time()
    stop = None
    with open(stdout_path, 'w') as out:
        proc = subprocess.Popen(command, cwd=str(cwd), stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            if on_start:
                on_start(proc.pid)
            while proc.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    stop = 'cancelled'
                    _kill_group(proc)
                    break
                if time.monotonic() - t0 > timeout_s:
                    stop = 'timeout'
                    _kill_group(proc)
                    break
                time.sleep(poll_s)
        except BaseException as exc:  # KeyboardInterrupt, SystemExit, errors in on_start
            _kill_group(proc)
            raise SolverInterrupted('interrupted: %s' % type(exc).__name__) from exc
    return {
        'pid': proc.pid,
        'exit_code': proc.returncode,
        'stop_reason': stop,
        'start_epoch': wall0,
        'elapsed_s': time.monotonic() - t0,
    }

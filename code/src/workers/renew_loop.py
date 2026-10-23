# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""Background renewal of something that expires unless refreshed (a merge
lease, a claimed task's idle time)."""
import logging
import threading

from workers.fault_state import FROZEN


logger = logging.getLogger(__name__)


class RenewLoop:
    """Daemon thread that calls renew() every interval_s until stopped.

    renew() returning False ends the loop (the thing is no longer ours). An
    exception (Redis failing over) is logged and retried after
    RETRY_AFTER_FAILURE_S, not a whole interval, so a renewal missed during
    an outage is made up as soon as the store answers. Nothing is renewed
    while a fault-injection freeze holds the process (workers.fault_state), and
    the thread dies with the process, so a crashed worker's lease or task
    expires.
    """

    RETRY_AFTER_FAILURE_S = 1.0

    name = 'renew'          # thread name
    what = 'Renewal'        # log text: "<what> failed: <error>"
    log = logger

    def __init__(self, interval_s: float):
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)

    def renew(self) -> bool:
        raise NotImplementedError

    def on_lost(self) -> None:
        """Called once when renew() returned False."""

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        wait = self._interval_s
        while not self._stop.wait(wait):
            wait = self._interval_s
            if FROZEN.is_set():
                continue    # a frozen process renews nothing
            try:
                if not self.renew():
                    self.on_lost()
                    return
            except Exception as e:  # noqa: BLE001 — keep trying until stopped
                self.log.warning(f"{self.what} failed: {e}")
                wait = min(self._interval_s, self.RETRY_AFTER_FAILURE_S)

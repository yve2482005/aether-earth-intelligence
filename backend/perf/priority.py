"""Priority queues (§5): P0 life-safety > P1 real-time hazards > P2 normal > P3 background.

In-process lane queues with starvation guards: a P3 job must not wait
forever while P0/P1 keep arriving. Guard: after N consecutive higher-lane
serves, one lower-lane job is served even if a higher-lane job is waiting
(weighted-fair-ish, deterministic and explainable).

WIRING POINT into the alert engine (final integration, not owned by this
workstream): the alert pipeline should emit alert tasks into `queues.enqueue`
with the lane derived from severity:
    critical/warning (life-safety) -> P0
    watch (hazard)                 -> P1
    advisory/info                  -> P2
    everything else                -> P3
The integration point is a single call site in the alert engine's
`_issue()`/dispatch path — owned by the alerting team; this module only
provides the queue and the documented contract. No alerting/ files are
edited here.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Deque, Dict, List, Optional

from backend.common.logging_setup import setup_logging

log = setup_logging("aether.perf.priority")


class Lane(IntEnum):
    P0 = 0  # life-safety
    P1 = 1  # real-time hazards
    P2 = 2  # normal
    P3 = 3  # background


LANE_NAMES = {Lane.P0: "P0 life-safety", Lane.P1: "P1 real-time hazards",
              Lane.P2: "P2 normal", Lane.P3: "P3 background"}


def lane_for_severity(severity: Optional[str]) -> Lane:
    """Documented mapping the alert-engine integration should use."""
    s = (severity or "").lower()
    if s in ("critical", "warning"):
        return Lane.P0
    if s in ("watch",):
        return Lane.P1
    if s in ("advisory", "info"):
        return Lane.P2
    return Lane.P3


@dataclass
class Task:
    lane: Lane
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.monotonic)
    # wiring contract: the alert engine fills these when bridging alerts in
    alert_id: Optional[str] = None
    severity: Optional[str] = None


class PriorityQueues:
    """Thread-safe lane queues with a starvation guard.

    Dequeue rule: always serve the highest non-empty lane, except every
    `starvation_every` serves we force-serve the lowest non-empty lane first
    so P3 cannot starve.
    """

    def __init__(self, starvation_every: int = 10) -> None:
        self._lanes: Dict[Lane, Deque[Task]] = {l: deque() for l in Lane}
        self._lock = threading.Lock()
        self._serves = 0
        self.starvation_every = starvation_every
        self.dropped = 0  # no drops allowed; kept for contract honesty

    def enqueue(self, task: Task) -> None:
        with self._lock:
            self._lanes[task.lane].append(task)

    def dequeue(self, timeout: Optional[float] = None) -> Optional[Task]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._serves += 1
                order = self._service_order()
                for lane in order:
                    if self._lanes[lane]:
                        return self._lanes[lane].popleft()
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def _service_order(self) -> List[Lane]:
        # highest lane first; every starvation_every-th serve, lowest non-empty
        # lane goes first once (guard), then normal order resumes.
        lanes = sorted(Lane)
        if self._serves % self.starvation_every == 0:
            for lane in reversed(lanes):
                if self._lanes[lane]:
                    return [lane] + [l for l in lanes if l != lane]
        return lanes

    def depth(self) -> Dict[str, int]:
        with self._lock:
            return {LANE_NAMES[l]: len(q) for l, q in self._lanes.items()}

    def __len__(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._lanes.values())


class WorkerPool:
    """Background threads draining the queues; slow jobs run here (see §6/7)."""

    def __init__(self, queues: PriorityQueues, workers: int = 2,
                 handler: Optional[Callable[[Task], None]] = None) -> None:
        self.queues = queues
        self.handler = handler or (lambda t: None)
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._loop, name=f"aether-pq-{i}",
                             daemon=True)
            for i in range(workers)]

    def start(self) -> None:
        for t in self._threads:
            t.start()

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        if join:
            for t in self._threads:
                t.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            task = self.queues.dequeue(timeout=0.5)
            if task is None:
                continue
            try:
                self.handler(task)
            except Exception:  # noqa: BLE001 - worker never dies on a task
                log.exception("priority worker failed on %s/%s", task.kind,
                              LANE_NAMES[task.lane])

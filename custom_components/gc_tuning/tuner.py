"""Pure-Python core of gc_tuning (no Home Assistant imports, unit-testable).

Why this exists (2026-11-18): Home Assistant Core 2026.11 runs on Python 3.14, whose cyclic
garbage collector is the generational one again. A full (generation 2) collection walks every tracked
object; on a large instance that is a pause of the whole process of 2-3 s, every few minutes, with no fixed
period. The event loop stops, no state is written, every API call waits.

Two separate jobs:
- OBSERVE (always): gc.callbacks records every collection. The callback does arithmetic and a few small
  list/deque appends only; every generation-2 collection is logged at INFO with its start time and
  duration, and any collection >= log_min_ms at WARNING. No I/O beyond the log line.
- TUNE (only apply(), by service or after the start-up delay): one full collection, gc.freeze() so later
  full collections skip the start-up heap, gc.set_threshold() so collections run less often. restore()
  undoes both.
- UPKEEP (1.1.0, driven by the runtime's schedules): maintain() = unfreeze + collect + freeze, freeze() = collect +
  freeze (the periodic re-freeze), collect() = one full collection. Every operation is recorded as an Operation
  and handed to `on_operation` (the runtime fires the bus event from there).

gc.get_freeze_count() walks the whole permanent generation (O(frozen objects), GIL held). It is read after
an operation that changes the frozen set and otherwise reused for up to FROZEN_COUNT_MAX_AGE_SECONDS.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import gc
import logging
import threading
import time
from typing import Any

from .const import (
    ACTION_APPLY_SERVICE,
    ACTION_COLLECT_SERVICE,
    ACTION_FREEZE_SERVICE,
    ACTION_MAINTAIN_SERVICE,
    ACTION_RESTORE,
    ACTION_RETUNE,
    ACTION_UNFREEZE_SERVICE,
    BOUNDS,
    CONF_THRESHOLD0,
    DEFAULT_OPTIONS,
    FROZEN_COUNT_MAX_AGE_SECONDS,
    MAINTENANCE_ACTIONS,
)

DEFAULT_THRESHOLDS = (
    int(DEFAULT_OPTIONS["threshold0"]),
    int(DEFAULT_OPTIONS["threshold1"]),
    int(DEFAULT_OPTIONS["threshold2"]),
)
MIN_THRESHOLD0, MAX_THRESHOLD0 = BOUNDS[CONF_THRESHOLD0]

HOUR_SECONDS = 3600.0
BUCKET_SECONDS = 10.0  # the longest-pause window is kept in 10 s buckets: one deque append per 10 s at most

# Read through a module attribute so tests can replace the clock the callback uses.
perf_counter = time.perf_counter


@dataclass(frozen=True, slots=True)
class Operation:
    """One thing this integration did to the collector: what the bus event and the "Last operation" sensor carry."""

    action: str
    collected: int
    frozen_before: int
    frozen_after: int
    duration_ms: float
    at: datetime

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form (the event data, the sensor attributes, diagnostics)."""
        return {
            "action": self.action,
            "collected": self.collected,
            "frozen_before": self.frozen_before,
            "frozen_after": self.frozen_after,
            "duration_ms": self.duration_ms,
            "at": self.at.isoformat(),
        }


def _clock() -> str:
    """Local wall-clock time with milliseconds, for the log line and the stats `at` field."""
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def _stamp(ts: float) -> str:
    """Local wall-clock time of an epoch second, with milliseconds."""
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int((ts % 1) * 1000):03d}"


class GcTuner:
    """Observe and (on command) tune CPython's cyclic garbage collector."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        freeze: bool = True,
        thresholds: tuple[int, int, int] = DEFAULT_THRESHOLDS,
        log_min_ms: float = 500.0,
        keep: int = 200,
    ) -> None:
        """Validate the settings; nothing is applied yet."""
        self._log = logger
        self.freeze_on_apply = bool(freeze)
        self.thresholds = self._validated(thresholds)
        self._log_min = [float(log_min_ms) / 1000.0]  # one-element list: the callback reads it, reconfigure() changes it
        # (epoch start, gen, seconds, collected); gen 1 and 2 and any slow gen 0 (what stats() shows as `recent`)
        self.recent: deque[tuple[float, int, float, int]] = deque(maxlen=keep)
        self.count = [0, 0, 0]
        self.seconds = [0.0, 0.0, 0.0]
        self.slowest = [0.0, 0.0, 0.0]
        # Last-hour book-keeping for the sensors. Full (generation 2) collections are rare, so they are kept exactly.
        self._fulls: deque[tuple[float, float]] = deque(maxlen=4096)  # (perf end, seconds)
        # Every collection folds into a 10 s bucket [bucket index, longest seconds]: a bounded ring, never grows.
        self._buckets: deque[list[float]] = deque(maxlen=int(HOUR_SECONDS / BUCKET_SECONDS) + 8)
        self.last_full_seconds: float | None = None
        self.last_full_started: float | None = None  # epoch seconds
        self.applied: dict[str, Any] | None = None
        self.applied_at: datetime | None = None
        self._froze = False  # True while a freeze made by this object is in force
        # 1.1.0 upkeep book-keeping (perf-counter seconds unless noted)
        self.on_operation: Callable[[Operation], None] | None = None
        self.last_operation: Operation | None = None
        self.last_maintenance_at: datetime | None = None
        self.peak_auto_full: float = 0.0  # longest AUTOMATIC full collection since the last freeze (ours do not count)
        self.last_full_end: float | None = None  # end of the newest full collection, automatic or ours
        self.last_freeze_at: float | None = None  # end of the last freeze of this object
        self.last_reset_at: float | None = None  # end of the last apply or maintenance (a freeze of a freshly collected heap)
        self._forcing_tid: int | None = None  # thread of our own collect in flight
        self._frozen_cache: tuple[float, int] | None = None  # (perf time, count)
        self._t0: float = 0.0
        self._wall0: float = 0.0
        self._cb: Callable[[str, dict[str, Any]], None] | None = None

    @property
    def frozen_by_us(self) -> bool:
        """True while a freeze made by this object is in force."""
        return self._froze

    @staticmethod
    def _validated(thresholds: tuple[int, int, int] | list[int]) -> tuple[int, int, int]:
        t0, t1, t2 = (int(x) for x in thresholds)
        if not (MIN_THRESHOLD0 <= t0 <= MAX_THRESHOLD0) or t1 < 1 or t2 < 1:
            raise ValueError(f"bad gc thresholds {tuple(thresholds)!r}")
        return t0, t1, t2

    # ---- configuration (no gc call) -----------------------------------------------------------
    def configure(self, *, freeze: bool, thresholds: tuple[int, int, int], log_min_ms: float) -> None:
        """Take new settings; the collector itself is not touched until apply() or retune()."""
        self.thresholds = self._validated(thresholds)
        self.freeze_on_apply = bool(freeze)
        self._log_min[0] = float(log_min_ms) / 1000.0

    # ---- observation -------------------------------------------------------------------------
    def install_callback(self) -> None:
        """Hook gc.callbacks (idempotent). Touches nothing else."""
        if self._cb is not None:
            return
        count, seconds, slowest, recent = self.count, self.seconds, self.slowest, self.recent
        fulls, buckets, log_min = self._fulls, self._buckets, self._log_min
        log = self._log
        perf, wall = perf_counter, time.time

        def _cb(phase: str, info: dict[str, Any]) -> None:
            if phase == "start":
                self._t0 = perf()
                self._wall0 = wall()
                return
            t0 = self._t0
            if not t0:
                return
            end = perf()
            dur = end - t0
            self._t0 = 0.0
            gen = info["generation"]
            if gen > 2:
                gen = 2
            count[gen] += 1
            seconds[gen] += dur
            if dur > slowest[gen]:
                slowest[gen] = dur
            # last-hour book-keeping: one list append per 10 s, one tuple append per full collection
            index = end // BUCKET_SECONDS
            if buckets and buckets[-1][0] == index:
                if dur > buckets[-1][1]:
                    buckets[-1][1] = dur
            else:
                buckets.append([index, dur])
            if gen == 2:
                fulls.append((end, dur))
                self.last_full_seconds = dur
                self.last_full_started = self._wall0
                self.last_full_end = end
                if self._forcing_tid != threading.get_ident() and dur > self.peak_auto_full:
                    self.peak_auto_full = dur
            log_min_s = log_min[0]
            if gen == 0 and dur < log_min_s:
                return
            collected = info["collected"]
            recent.append((self._wall0, gen, dur, collected))
            if gen == 2:
                log.info("gc generation 2 collection took %.3f s, started %s, collected %d", dur, _stamp(self._wall0), collected)
            if dur >= log_min_s:
                # the frozen count is the last one read: counting it here would walk the permanent generation
                # (tens to hundreds of ms) right after a pause that was already too long
                cache = self._frozen_cache
                log.warning(
                    "gc generation %d collection took %.3f s (started %s, collected %d, frozen %d, thresholds %s)",
                    gen,
                    dur,
                    _stamp(self._wall0),
                    collected,
                    cache[1] if cache is not None else 0,
                    gc.get_threshold(),
                )

        self._cb = _cb
        gc.callbacks.append(_cb)

    def remove_callback(self) -> None:
        """Unhook gc.callbacks."""
        if self._cb is not None and self._cb in gc.callbacks:
            gc.callbacks.remove(self._cb)
        self._cb = None

    def last_hour(self, now: float | None = None) -> dict[str, Any]:
        """Full collections and the longest pause of any generation in the last hour (counter reads only)."""
        now = perf_counter() if now is None else now
        cutoff = now - HOUR_SECONDS
        # list(deque) copies in one C call under the GIL: gc callbacks run in whichever thread triggers a
        # collection, so iterating the live deque from the event loop could see it change mid-walk
        fulls = list(self._fulls)
        buckets = list(self._buckets)
        full = sum(1 for end, _ in fulls if end >= cutoff)
        # a bucket counts while any part of it is inside the window: the longest pause is exact to 10 s
        first_index = cutoff // BUCKET_SECONDS
        longest = max((b[1] for b in buckets if b[0] >= first_index), default=0.0)
        return {"full_collections": full, "longest_pause_seconds": longest}

    # ---- the frozen-object count (O(frozen objects): read it sparingly) ---------------------------
    def frozen_read(self) -> int:
        """A fresh read of gc.get_freeze_count(); it also refreshes the reuse cache."""
        count = gc.get_freeze_count()
        self._frozen_cache = (perf_counter(), count)
        return count

    def frozen_count(self, max_age: float = FROZEN_COUNT_MAX_AGE_SECONDS) -> int:
        """The frozen-object count, reused while the last read is younger than `max_age` seconds."""
        cache = self._frozen_cache
        if cache is None or perf_counter() - cache[0] > max_age:
            return self.frozen_read()
        return cache[1]

    def _set_frozen_known(self, count: int) -> None:
        self._frozen_cache = (perf_counter(), count)

    # ---- operations ------------------------------------------------------------------------------
    def _forced_collect(self) -> tuple[int, float]:
        """gc.collect() made by us: its pause does not count towards `peak_auto_full`."""
        self._forcing_tid = threading.get_ident()
        started = perf_counter()
        try:
            collected = gc.collect()
        finally:
            self._forcing_tid = None
        return collected, perf_counter() - started

    def _mark_frozen(self, *, reset: bool) -> None:
        """Bookkeeping after one of our gc.freeze() calls."""
        self._froze = True
        self.peak_auto_full = 0.0
        now = perf_counter()
        self.last_freeze_at = now
        if reset:
            self.last_reset_at = now

    def _finish(self, action: str, collected: int, before: int, after: int, seconds: float, level: int | None = None) -> Operation:
        """Record the operation, log it (when `level` is given), and tell the listener (the runtime fires the event)."""
        op = Operation(action, collected, before, after, round(seconds * 1000.0, 1), datetime.now(UTC))
        self.last_operation = op
        if action in MAINTENANCE_ACTIONS:
            self.last_maintenance_at = op.at
        if level is not None:
            self._log.log(
                level,
                "gc operation %s: took %.3f s, collected %d, frozen %d -> %d",
                action,
                seconds,
                collected,
                before,
                after,
            )
        listener = self.on_operation
        if listener is not None:
            try:
                listener(op)
            except Exception:  # noqa: BLE001 - a failing listener must not fail the operation that already happened
                self._log.exception("gc_tuning operation listener failed")
        return op

    # ---- tuning (never runs on its own) --------------------------------------------------------
    def apply(self, action: str = ACTION_APPLY_SERVICE) -> dict[str, Any]:
        """One full collection, then (if configured) freeze the heap, then set the thresholds."""
        before = gc.get_threshold()
        frozen_before = self.frozen_count()
        started = perf_counter()
        collected, collect_s = self._forced_collect()
        frozen = 0
        if self.freeze_on_apply:
            gc.freeze()
            self._mark_frozen(reset=True)
            frozen = self.frozen_read()
        gc.set_threshold(*self.thresholds)
        self.applied = {
            "collected": collected,
            "collect_seconds": round(collect_s, 3),
            "frozen": frozen,
            "thresholds_before": list(before),
            "thresholds": list(gc.get_threshold()),
            "at": _clock(),
        }
        self.applied_at = datetime.now(UTC)
        self._log.warning(
            "gc tuning applied: full collection %.3f s collected %d; frozen %d objects; thresholds %s -> %s",
            collect_s,
            collected,
            frozen,
            before,
            gc.get_threshold(),
        )
        self._finish(action, collected, frozen_before, frozen, perf_counter() - started)
        return dict(self.applied)

    def retune(self) -> dict[str, Any]:
        """After apply(): follow new settings without a second heap-wide freeze.

        New thresholds are set at once. A freeze setting turned off unfreezes; turned on after apply()
        left the heap unfrozen it freezes now. The recorded apply time and apply-time facts stay as they were.
        """
        before = gc.get_threshold()
        started = perf_counter()
        frozen_before = self.frozen_count()
        frozen_after = frozen_before
        collected = 0
        action = "thresholds"
        if self.freeze_on_apply and not self._froze:
            collected, _ = self._forced_collect()
            gc.freeze()
            self._mark_frozen(reset=True)
            frozen_after = self.frozen_read()
            action = "thresholds and freeze"
        elif not self.freeze_on_apply and self._froze:
            gc.unfreeze()
            self._froze = False
            frozen_after = 0
            self._set_frozen_known(0)
            action = "thresholds and unfreeze"
        gc.set_threshold(*self.thresholds)
        if self.applied is not None:
            self.applied["thresholds"] = list(gc.get_threshold())
            self.applied["frozen"] = frozen_after
        self._log.warning("gc tuning retuned (%s): thresholds %s -> %s; frozen %d", action, before, gc.get_threshold(), frozen_after)
        self._finish(ACTION_RETUNE, collected, frozen_before, frozen_after, perf_counter() - started)
        return {"thresholds_before": list(before), "thresholds": list(gc.get_threshold()), "frozen": frozen_after}

    def restore(self, thresholds: tuple[int, int, int]) -> dict[str, Any]:
        """Undo apply(): unfreeze what this object froze and put the original thresholds back."""
        before = gc.get_threshold()
        started = perf_counter()
        unfrozen = 0
        if self._froze:
            unfrozen = self.frozen_count()
            gc.unfreeze()
            self._froze = False
            self._set_frozen_known(0)
        gc.set_threshold(*thresholds)
        self.applied = None
        self.applied_at = None
        res = {"unfrozen": unfrozen, "thresholds_before": list(before), "thresholds": list(gc.get_threshold())}
        self._log.warning("gc tuning restored: thresholds %s -> %s; %d objects back in generation 2", before, gc.get_threshold(), unfrozen)
        self._finish(ACTION_RESTORE, 0, unfrozen, 0, perf_counter() - started)
        return res

    def freeze(self, action: str = ACTION_FREEZE_SERVICE) -> dict[str, Any]:
        """Collect once, then move every tracked object into the permanent generation (also the periodic re-freeze)."""
        frozen_before = self.frozen_count()
        started = perf_counter()
        collected, collect_s = self._forced_collect()
        gc.freeze()
        self._mark_frozen(reset=False)
        frozen = self.frozen_read()
        res = {"collected": collected, "collect_seconds": round(collect_s, 3), "frozen": frozen}
        self._log.warning("gc freeze: full collection %.3f s collected %d; frozen now %d", collect_s, collected, frozen)
        self._finish(action, collected, frozen_before, frozen, perf_counter() - started)
        return res

    def unfreeze(self, action: str = ACTION_UNFREEZE_SERVICE) -> dict[str, Any]:
        """Move the permanent generation back into the oldest generation (undo freeze)."""
        started = perf_counter()
        before = self.frozen_read()
        gc.unfreeze()
        self._froze = False
        self._set_frozen_known(0)
        res = {"unfrozen": before, "frozen": 0}
        self._log.warning("gc unfreeze: %d objects back in generation 2", before)
        self._finish(action, 0, before, 0, perf_counter() - started)
        return res

    def collect(self, action: str = ACTION_COLLECT_SERVICE, *, log: bool = True) -> dict[str, Any]:
        """Time one full collection (the pause the event loop sees)."""
        collected, dur = self._forced_collect()
        frozen = self.frozen_count()
        res = {"collected": collected, "seconds": round(dur, 3), "frozen": frozen}
        if log:
            self._log.warning("gc collect: %.3f s, collected %d, frozen %d", dur, collected, frozen)
        self._finish(action, collected, frozen, frozen, dur, level=None if log else logging.INFO)
        return res

    def maintain(self, action: str = ACTION_MAINTAIN_SERVICE) -> dict[str, Any]:
        """Unfreeze, collect, freeze: a full reset. Reclaims what was frozen and has died since, then freezes the live set."""
        frozen_before = self.frozen_count()
        started = perf_counter()
        gc.unfreeze()
        self._froze = False
        try:
            collected, collect_s = self._forced_collect()
        finally:
            gc.freeze()
            self._mark_frozen(reset=True)
        frozen = self.frozen_read()
        res = {"collected": collected, "collect_seconds": round(collect_s, 3), "frozen_before": frozen_before, "frozen": frozen}
        self._finish(action, collected, frozen_before, frozen, perf_counter() - started, level=logging.WARNING)
        return res

    def stats(self, recent: int = 20) -> dict[str, Any]:
        """Counters since start plus the newest generation 1 and 2 collections (one frozen-set walk, no heap walk)."""
        return {
            "thresholds": list(gc.get_threshold()),
            "count_pending": list(gc.get_count()),
            "frozen": self.frozen_read(),
            "enabled": gc.isenabled(),
            "python_gc_stats": gc.get_stats(),
            "observed": [
                {"generation": g, "collections": self.count[g], "seconds": round(self.seconds[g], 3), "slowest_s": round(self.slowest[g], 3)}
                for g in range(3)
            ],
            "recent": [
                {"started": _stamp(ts), "generation": g, "seconds": round(d, 3), "collected": c}
                for ts, g, d, c in list(self.recent)[-recent:]
            ],
            "applied": self.applied,
        }

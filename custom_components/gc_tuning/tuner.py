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
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
import gc
import logging
import time
from typing import Any

from .const import BOUNDS, CONF_THRESHOLD0, DEFAULT_OPTIONS

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
            log_min_s = log_min[0]
            if gen == 0 and dur < log_min_s:
                return
            collected = info["collected"]
            recent.append((self._wall0, gen, dur, collected))
            if gen == 2:
                log.info("gc generation 2 collection took %.3f s, started %s, collected %d", dur, _stamp(self._wall0), collected)
            if dur >= log_min_s:
                log.warning(
                    "gc generation %d collection took %.3f s (started %s, collected %d, frozen %d, thresholds %s)",
                    gen,
                    dur,
                    _stamp(self._wall0),
                    collected,
                    gc.get_freeze_count(),
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

    # ---- tuning (never runs on its own) --------------------------------------------------------
    def apply(self) -> dict[str, Any]:
        """One full collection, then (if configured) freeze the heap, then set the thresholds."""
        before = gc.get_threshold()
        t = time.perf_counter()
        collected = gc.collect()
        collect_s = time.perf_counter() - t
        frozen = 0
        if self.freeze_on_apply:
            gc.freeze()
            self._froze = True
            frozen = gc.get_freeze_count()
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
        return dict(self.applied)

    def retune(self) -> dict[str, Any]:
        """After apply(): follow new settings without a second heap-wide freeze.

        New thresholds are set at once. A freeze setting turned off unfreezes; turned on after apply()
        left the heap unfrozen it freezes now. The recorded apply time and apply-time facts stay as they were.
        """
        before = gc.get_threshold()
        action = "thresholds"
        if self.freeze_on_apply and not self._froze:
            gc.collect()
            gc.freeze()
            self._froze = True
            action = "thresholds and freeze"
        elif not self.freeze_on_apply and self._froze:
            gc.unfreeze()
            self._froze = False
            action = "thresholds and unfreeze"
        gc.set_threshold(*self.thresholds)
        if self.applied is not None:
            self.applied["thresholds"] = list(gc.get_threshold())
            self.applied["frozen"] = gc.get_freeze_count()
        self._log.warning("gc tuning retuned (%s): thresholds %s -> %s; frozen %d", action, before, gc.get_threshold(), gc.get_freeze_count())
        return {"thresholds_before": list(before), "thresholds": list(gc.get_threshold()), "frozen": gc.get_freeze_count()}

    def restore(self, thresholds: tuple[int, int, int]) -> dict[str, Any]:
        """Undo apply(): unfreeze what this object froze and put the original thresholds back."""
        before = gc.get_threshold()
        unfrozen = 0
        if self._froze:
            unfrozen = gc.get_freeze_count()
            gc.unfreeze()
            self._froze = False
        gc.set_threshold(*thresholds)
        self.applied = None
        self.applied_at = None
        res = {"unfrozen": unfrozen, "thresholds_before": list(before), "thresholds": list(gc.get_threshold())}
        self._log.warning("gc tuning restored: thresholds %s -> %s; %d objects back in generation 2", before, gc.get_threshold(), unfrozen)
        return res

    def freeze(self) -> dict[str, Any]:
        """Collect once, then move every tracked object into the permanent generation."""
        t = time.perf_counter()
        collected = gc.collect()
        collect_s = time.perf_counter() - t
        gc.freeze()
        self._froze = True
        res = {"collected": collected, "collect_seconds": round(collect_s, 3), "frozen": gc.get_freeze_count()}
        self._log.warning("gc freeze: full collection %.3f s collected %d; frozen now %d", collect_s, collected, res["frozen"])
        return res

    def unfreeze(self) -> dict[str, Any]:
        """Move the permanent generation back into the oldest generation (undo freeze)."""
        before = gc.get_freeze_count()
        gc.unfreeze()
        self._froze = False
        res = {"unfrozen": before, "frozen": gc.get_freeze_count()}
        self._log.warning("gc unfreeze: %d objects back in generation 2", before)
        return res

    def collect(self) -> dict[str, Any]:
        """Time one full collection (the pause the event loop sees)."""
        t = time.perf_counter()
        collected = gc.collect()
        dur = time.perf_counter() - t
        res = {"collected": collected, "seconds": round(dur, 3), "frozen": gc.get_freeze_count()}
        self._log.warning("gc collect: %.3f s, collected %d, frozen %d", dur, collected, res["frozen"])
        return res

    def stats(self, recent: int = 20) -> dict[str, Any]:
        """Counters since start plus the newest generation 1 and 2 collections (cheap: no heap walk)."""
        return {
            "thresholds": list(gc.get_threshold()),
            "count_pending": list(gc.get_count()),
            "frozen": gc.get_freeze_count(),
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

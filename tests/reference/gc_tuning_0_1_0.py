"""Reference copy of the 0.1.0 pure core (the YAML-only module this integration replaces). Kept only so
tests/test_compat_0_1_0.py can compare the new tuner with it. Do not edit."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import gc
import logging
import time
from typing import Any

DEFAULT_THRESHOLDS = (50_000, 10, 10)
MIN_THRESHOLD0 = 700
MAX_THRESHOLD0 = 1_000_000


def _clock() -> str:
    """Local wall-clock time with milliseconds, for the log line."""
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


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
        t0, t1, t2 = (int(x) for x in thresholds)
        if not (MIN_THRESHOLD0 <= t0 <= MAX_THRESHOLD0) or t1 < 1 or t2 < 1:
            raise ValueError(f"bad gc thresholds {thresholds!r}")
        self._log = logger
        self.freeze_on_apply = bool(freeze)
        self.thresholds = (t0, t1, t2)
        self.log_min_s = float(log_min_ms) / 1000.0
        self.recent: deque[tuple[float, int, float, int]] = deque(maxlen=keep)  # (epoch start, gen, seconds, collected); gen 1 and 2 only
        self.count = [0, 0, 0]
        self.seconds = [0.0, 0.0, 0.0]
        self.slowest = [0.0, 0.0, 0.0]
        self.applied: dict[str, Any] | None = None
        self._t0: float = 0.0
        self._wall0: float = 0.0
        self._cb: Callable[[str, dict[str, Any]], None] | None = None

    # ---- observation -------------------------------------------------------------------------
    def install_callback(self) -> None:
        """Hook gc.callbacks (idempotent). Touches nothing else."""
        if self._cb is not None:
            return
        count, seconds, slowest, recent = self.count, self.seconds, self.slowest, self.recent
        log, log_min_s = self._log, self.log_min_s
        perf, wall = time.perf_counter, time.time

        def _cb(phase: str, info: dict[str, Any]) -> None:
            if phase == "start":
                self._t0 = perf()
                self._wall0 = wall()
                return
            t0 = self._t0
            if not t0:
                return
            dur = perf() - t0
            self._t0 = 0.0
            gen = info["generation"]
            if gen > 2:
                gen = 2
            count[gen] += 1
            seconds[gen] += dur
            if dur > slowest[gen]:
                slowest[gen] = dur
            if gen == 0 and dur < log_min_s:
                return
            collected = info["collected"]
            recent.append((self._wall0, gen, dur, collected))
            if gen == 2:
                started = time.strftime("%H:%M:%S", time.localtime(self._wall0)) + f".{int((self._wall0 % 1) * 1000):03d}"
                log.info("gc generation 2 collection took %.3f s, started %s, collected %d", dur, started, collected)
            if dur >= log_min_s:
                log.warning(
                    "gc generation %d collection took %.3f s (started %s, collected %d, frozen %d, thresholds %s)",
                    gen,
                    dur,
                    time.strftime("%H:%M:%S", time.localtime(self._wall0)) + f".{int((self._wall0 % 1) * 1000):03d}",
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
        self._log.warning(
            "gc tuning applied: full collection %.3f s collected %d; frozen %d objects; thresholds %s -> %s",
            collect_s,
            collected,
            frozen,
            before,
            gc.get_threshold(),
        )
        return dict(self.applied)

    def freeze(self) -> dict[str, Any]:
        """Collect once, then move every tracked object into the permanent generation."""
        t = time.perf_counter()
        collected = gc.collect()
        collect_s = time.perf_counter() - t
        gc.freeze()
        res = {"collected": collected, "collect_seconds": round(collect_s, 3), "frozen": gc.get_freeze_count()}
        self._log.warning("gc freeze: full collection %.3f s collected %d; frozen now %d", collect_s, collected, res["frozen"])
        return res

    def unfreeze(self) -> dict[str, Any]:
        """Move the permanent generation back into the oldest generation (undo freeze)."""
        before = gc.get_freeze_count()
        gc.unfreeze()
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
                {"started": time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int((ts % 1) * 1000):03d}", "generation": g, "seconds": round(d, 3), "collected": c}
                for ts, g, d, c in list(self.recent)[-recent:]
            ],
            "applied": self.applied,
        }

# GC tuning for Home Assistant

A Home Assistant integration that stops the long pauses of Python's garbage collector.

## The problem

Home Assistant Core runs on Python 3.14, whose cyclic garbage collector is the generational one again. A full
(generation 2) collection walks every tracked object. On a large instance (millions of objects) that pause
stops the whole process for 2-3 seconds, every few minutes: no state is written, every API call waits.

## What it does

All settings are Python's own (https://docs.python.org/3/library/gc.html) and live only inside the running process,
so they must be applied again after every start. Home Assistant has no setting for them.

| Step | Effect |
|---|---|
| After the start-up delay (default 300 s after Home Assistant has started): one `gc.collect()` | the start-up objects exist and the garbage is gone |
| `gc.freeze()` | the objects that live as long as Home Assistant are never walked again; a full collection checks a small fraction (0.3-0.6 s instead of 2-3 s on the instance this was built on) |
| `gc.set_threshold(50000, 10, 10)` | a young-generation collection runs after 50,000 new objects instead of 2,000: far fewer collections |

Measured on the instance this was built for (1.0.0, 24 hours): pauses of 1 s or more went from 7.7 to 0.42 an hour, the
longest 1.46 s, memory flat.

### Keeping the frozen heap small (1.1.0)

The freeze happens once, so what is created afterwards lives in the unfrozen generation that every full collection walks,
and full collections get rarer and larger as it grows. 1.1.0 adds three kinds of upkeep (all options, all on by default):

| Upkeep | Default | What it does |
|---|---|---|
| Daily maintenance | 04:30 | `unfreeze`, `collect`, `freeze`. Reclaims objects that were frozen and have died since, and starts the day with a small unfrozen heap. One pause of about 2 to 3 seconds. |
| Periodic re-freeze | every 6 h, after a full collection of 500 ms | `collect`, `freeze` on a wall-clock grid anchored at the maintenance time. Runs only once a full collection that Python started itself has taken at least the pause target since the last freeze (0 = every slot runs). Dead objects that were frozen stay until the daily maintenance; with the daily maintenance off the slots do the full maintenance. |
| Full collection at least every N minutes | 45 | small and frequent instead of rare and large: a full collection frees what built up since the last one, so a quiet night does not end in one long pause. The check runs once a minute; any full collection restarts the clock. |

The upkeep acts only while the tuning is enabled, applied and the heap was frozen by this integration.

### Defaults and why

- Thresholds stay 50000 / 10 / 10 (the 1.0.0 values, measured for 24 hours). 10000 / 10 / 10 was tried live for 6 minutes and
  a burst of allocations produced 13 full collections of 0.73 to 0.91 s in 110 s: once the heap is frozen a full collection starts after
  threshold 0 x threshold 1 x threshold 2 net allocations, so a lower threshold 0 means more full collections in a burst, each paying the walk of the
  unfrozen heap. It is one burst, not a controlled comparison; the threshold is a live option for a longer trial.
- 04:30, 6 hours, 500 ms and 45 minutes are chosen from the 1.0.0 measurements (the shortest full collection grew from 0.28 s to 0.56 s in
  19 hours; every pause of 1 s or more followed a gap of 1.3 to 3.4 hours without a full collection) and are not tuned
  further. Read the sensors and the event below to adjust them for your instance.

## Install

- HACS (or Marketplace in Home Assistant 2026.11+): search for **GC tuning**, download, and restart Home Assistant once.
  - Alternative (custom repository): HACS → Custom repositories → add `https://github.com/cchip201/homeassistant-python-tuning` as type Integration, download, and restart Home Assistant once.
- Then Settings, Devices & services, Add integration, "GC tuning". One instance only.
- Needs Home Assistant 2026.3.0 or later (the icon is shipped in `brand/`).

### Coming from the 0.1.0 YAML module

If `configuration.yaml` has a `gc_tuning:` block (`auto_apply: true`), the first start with this version imports it
into the integration entry (`auto_apply` becomes `enabled`, `delay` becomes `start_delay`). Nothing else has to be
done for the fix to keep working. A repair message then says the block can be removed; do that when convenient
(no restart is needed for it). Values outside the new bounds are pulled to the nearest bound and logged.
If an entry already exists, the YAML block is ignored: options changed in the UI are never overwritten.

## Options (Configure)

Changes apply at once. Home Assistant is not restarted.

| Option | Default | Range | Meaning |
|---|---|---|---|
| Enabled | on | | apply the tuning after start-up; off: observe only, put Python's own thresholds back, unfreeze what this integration froze and stop the upkeep |
| Start delay | 300 s | 60-1800 | seconds after Home Assistant has started until the tuning is applied (once per start) |
| Generation 0 threshold | 50000 | 700-200000 | new objects between young collections (Python: 2000) |
| Generation 1 threshold | 10 | 1-100 | |
| Generation 2 threshold | 10 | 1-100 | |
| Freeze the start-up objects | on | | `gc.freeze()` once; turning it off later unfreezes. The upkeep needs it. |
| Daily maintenance | on | | unfreeze, collect, freeze once a day |
| Daily maintenance time | 04:30 | local time | also the anchor of the re-freeze slots; choose a quiet hour |
| Re-freeze every | 6 h | 0-24 | 0 = off; rounded to 1, 2, 3, 4, 6, 8, 12 or 24 (a tie goes to the shorter) |
| Re-freeze only after a full collection of | 500 ms | 0-10000 | 0 = every slot runs; ignored while the daily maintenance is off |
| Full collection at least every | 45 min | 0, 5-1440 | 0 = off; 1 to 4 is raised to 5 |
| Log collections longer than | 500 ms | 10-60000 | a WARNING per slow collection (every full collection is logged at INFO anyway) |

"Reset to the proven defaults" is a menu entry of Configure and stores the values above. Freezing happens once per
start; a later change of the thresholds does not freeze a second time. An entry stored by 1.0.0 needs no migration: the
new options read as their defaults until the form is saved once.

## Sensors (diagnostic, one device)

| Sensor | Value |
|---|---|
| Full collections in the last hour | count of generation 2 collections |
| Longest pause in the last hour | seconds, any generation (exact to 10 s of the window edge) |
| Last full collection duration | seconds |
| Frozen objects | `gc.get_freeze_count()` |
| Thresholds | `50000 / 10 / 10`, attributes per generation |
| Applied at | timestamp of the last apply |
| Last operation | the action of the newest operation (see the event); attributes: `collected`, `frozen_before`, `frozen_after`, `duration_ms`, `at` |
| Last operation duration | seconds |
| Last maintenance at | timestamp of the last unfreeze + collect + freeze (daily, slot or service) |
| Longest full collection since freeze | seconds; the value the re-freeze pause target is compared with (full collections the integration ran itself are not counted) |
| Process memory (RSS) | MB, read from `/proc/self/statm` in the executor |

Cost: the garbage collector callback only does arithmetic and one small append; the sensors read counters once a minute.
`gc.get_freeze_count()` walks the whole permanent generation (about 20 ms per 2.6 million objects on a quiet machine, more on
a busy one), so the Frozen objects sensor reuses one read for 5 minutes and refreshes it after an operation that changes the
frozen set; the `stats` service reads fresh.

## Event

Every operation fires `gc_tuning_operation` once the sensors show it, so an automation reads the new values. Data: `action`,
`collected`, `frozen_before`, `frozen_after`, `duration_ms`, `at` (ISO 8601). `frozen_before` is the last count read (at most 5 minutes old).

| `action` | When |
|---|---|
| `startup_apply` | the apply after the start-up delay |
| `apply_service`, `freeze_service`, `unfreeze_service`, `collect_service`, `maintain_service` | the service of that name |
| `daily_maintenance` | the daily unfreeze + collect + freeze |
| `periodic_refreeze` | a re-freeze slot (collect + freeze) |
| `periodic_maintenance` | a re-freeze slot while the daily maintenance is off (unfreeze + collect + freeze) |
| `periodic_collection` | a full collection because none happened for the set time |
| `retune` | an options change while applied (new thresholds; a freeze change) |
| `restore` | disabled or unloaded: original thresholds back, unfrozen |

```yaml
triggers:
  - trigger: event
    event_type: gc_tuning_operation
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.duration_ms > 2000 }}"
```

## Services

`gc_tuning.apply`, `gc_tuning.freeze`, `gc_tuning.unfreeze`, `gc_tuning.collect`, `gc_tuning.maintain` (unfreeze, collect, freeze), `gc_tuning.stats`: all return a response.
`gc_tuning.stats` returns `thresholds`, `count_pending`, `frozen`, `enabled`, `python_gc_stats`, `observed`, `recent` and
`applied` (with `at`, `thresholds`, `thresholds_before`, `frozen`, `collected`, `collect_seconds`).

## Diagnostics

Settings, Devices & services, GC tuning, Download diagnostics: options, collector state, the `stats` response, the sensor values. No secrets are involved.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python tools/make_icon.py   # redraw custom_components/gc_tuning/brand/
```

`pytest-homeassistant-custom-component` pins the Home Assistant version the suite runs on.

## Credits

The daily maintenance (`unfreeze`, `collect`, `freeze`), the periodic re-freeze on a wall-clock grid anchored at the maintenance time,
the re-freeze pause target, the debounce of a re-freeze that follows another too closely, the rule that the slots become the full
maintenance while the daily one is off, and the operation event follow the design of
[GC Manager](https://github.com/bakerkj/ha-gc-manager) by Kenneth Baker (MIT licence, 2026). Its README states the caveat that frozen dead objects
are only reclaimed by the maintenance, and that is why the maintenance is on by default here. This integration's code is written for it, with its own
tests; GC Manager is a separate integration and the two should not run side by side (two tuners on one collector).

## Licence

MIT.

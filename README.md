# GC tuning for Home Assistant

A Home Assistant integration that stops the long pauses of Python's garbage collector.

## The problem

Home Assistant Core runs on Python 3.14, whose cyclic garbage collector is the generational one again. A full
(generation 2) collection walks every tracked object. On a large instance (millions of objects) that pause
stops the whole process for 2-3 seconds, every few minutes: no state is written, every API call waits.

## What it does

Both settings are Python's own (https://docs.python.org/3/library/gc.html) and live only inside the running process,
so they must be applied again after every start. Home Assistant has no setting for them.

| Step | Effect |
|---|---|
| After the start-up delay (default 300 s after Home Assistant has started): one `gc.collect()` | the start-up objects exist and the garbage is gone |
| `gc.freeze()` | the objects that live as long as Home Assistant are never walked again; a full collection checks a small fraction (0.3-0.6 s instead of 2-3 s on the instance this was built on) |
| `gc.set_threshold(50000, 10, 10)` | a young-generation collection runs after 50,000 new objects instead of 2,000: far fewer collections |

Measured on the instance this was built for: 8 full collections an hour at 2-3 s each before, none of 1 s or more in
the hour after, memory flat.

## Install

- HACS: Custom repositories, add `https://github.com/cchip201/homeassistant-python-tuning` as type Integration, download, restart Home Assistant once.
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
| Enabled | on | | apply the tuning after start-up; off: observe only, and put Python's own thresholds back / unfreeze what this integration froze |
| Start delay | 300 s | 60-1800 | seconds after Home Assistant has started until the tuning is applied (once per start) |
| Generation 0 threshold | 50000 | 700-200000 | new objects between young collections (Python: 2000) |
| Generation 1 threshold | 10 | 1-100 | |
| Generation 2 threshold | 10 | 1-100 | |
| Freeze the start-up objects | on | | `gc.freeze()` once; turning it off later unfreezes |
| Log collections longer than | 500 ms | 10-60000 | a WARNING per slow collection (every full collection is logged at INFO anyway) |

"Reset to the proven defaults" is a menu entry of Configure. Freezing happens once per start; a later change of the
thresholds does not freeze a second time.

## Sensors (diagnostic, one device)

| Sensor | Value |
|---|---|
| Full collections in the last hour | count of generation 2 collections |
| Longest pause in the last hour | seconds, any generation (exact to 10 s of the window edge) |
| Last full collection duration | seconds |
| Frozen objects | `gc.get_freeze_count()` |
| Thresholds | `50000 / 10 / 10`, attributes per generation |
| Applied at | timestamp of the last apply |
| Process memory (RSS) | MB, read from `/proc/self/statm` in the executor |

Cost: the garbage collector callback only does arithmetic and one small append; the sensors read counters once a minute
and never walk the heap.

## Services

`gc_tuning.apply`, `gc_tuning.freeze`, `gc_tuning.unfreeze`, `gc_tuning.collect`, `gc_tuning.stats`: all return a response.
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

## Licence

MIT.

# Changelog

## 1.0.0
- First release as an installable integration (config flow, options, sensors, diagnostics, icon). The 0.1.0 module lived in a Home Assistant config repository and was YAML only.
- Behaviour by default is the proven fix of 0.1.0: 300 s after Home Assistant has started, one full collection, `gc.freeze()` of the start-up objects, thresholds 50000 / 10 / 10.
- A `gc_tuning:` YAML block of 0.1.0 is imported once into a config entry (`auto_apply` becomes `enabled`, `delay` becomes `start_delay`); the YAML block can then be removed.
- Options (changes apply at once, no restart): enabled, start delay (60-1800 s), generation 0 threshold (700-200000), generations 1 and 2 (1-100), freeze, log limit, and a reset to the proven defaults.
- Seven diagnostic sensors: full collections in the last hour, longest pause in the last hour, last full collection duration, frozen objects, thresholds, applied at, process memory (RSS).
- Diagnostics download; English translations; icon shipped in `brand/` (Home Assistant 2026.3 and later).
- The five services (`apply`, `freeze`, `unfreeze`, `collect`, `stats`) and the `stats` response are unchanged.

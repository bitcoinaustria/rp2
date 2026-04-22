# AGENTS.md

## Project shape

- RP2 is a local-first, privacy-first crypto tax engine with a plugin architecture.
- This repo is the **Kassiber-maintained fork** at `bitcoinaustria/rp2`. The upstream is `eprbell/rp2`. Changes meant for upstream should be authored so they can be proposed as independent PRs (additive country plugin, additive accounting method, additive transaction field with default value, etc.).
- The primary consumer of this fork is Kassiber, which embeds RP2 as its tax engine via `kassiber/core/engines/rp2.py` in the Kassiber codebase. Kassiber normalizes raw transactions into RP2 inputs; RP2 owns the tax math.
- Developer documentation lives in [README.dev.md](README.dev.md). User documentation lives in [README.md](README.md). Read `README.dev.md` before touching core — it captures the design rules this codebase lives by.

## Source tree highlights

- [src/rp2/](src/rp2/) — engine core: transactions, gains, balances, accounting engine, ODS parser, decimal math, logger.
- [src/rp2/plugin/country/](src/rp2/plugin/country/) — country plugins / entry points. Each `<code>.py` defines a subclass of `AbstractCountry` and an `rp2_entry()` function wired into [setup.cfg](setup.cfg) `console_scripts`.
- [src/rp2/plugin/accounting_method/](src/rp2/plugin/accounting_method/) — accounting method plugins (currently `fifo`, `lifo`, `hifo`, `lofo`, each a subclass of `AbstractChronologicalAccountingMethod` or `AbstractFeatureBasedAccountingMethod`).
- [src/rp2/plugin/report/](src/rp2/plugin/report/) — report generators. Country-specific generators live in `src/rp2/plugin/report/<country>/`. ODS templates live in `src/rp2/plugin/report/data/<country>/`.
- [src/rp2/locales/](src/rp2/locales/) — Babel `.po`/`.mo` catalogs for report localization.
- [tests/](tests/) — `unittest`-based suites. ODS output-diff tests compare generated reports against golden files in [input/golden/](input/golden/).
- [config/](config/) and [input/](input/) — example configs and sample transaction ODS files, reused by tests.

## Fork goal: Austrian tax support

This fork adds Austrian crypto tax support (§ 27b EStG post eco-social tax reform). The constraints that drive the design:

- **Gleitender Durchschnittspreis (moving average)** is the mandatory cost basis method for Neuvermögen. Upstream RP2 has no moving-average engine — all existing methods are lot-tracking.
- **Altvermögen vs Neuvermögen** is a calendar-cutoff distinction (acquisition on/before 2021-02-28 → Altvermögen; after → Neuvermögen). Upstream RP2's `long_term_capital_gain_period` is a days-threshold, not a cutoff.
- **Crypto-to-crypto swaps are non-taxable** under § 27b Abs 3 Z 2 EStG with basis carryover. Upstream RP2 treats every disposal as taxable.

The implementation proceeds in phases, each a reviewable commit. Core changes are allowed only where the task strictly requires them — justify each change, keep it minimal, and preserve the existing plugin contracts for all unrelated paths.

1. **Phase 1 — Country skeleton.** `src/rp2/plugin/country/at.py`, `rp2_at` entry, FIFO-only. Pure additive plugin, zero core changes.
2. **Phase 2 — Moving-average engine.** `moving_average_at` accounting method. Expected minimal core touch: let the accounting method return an optional per-disposal cost-basis override alongside the selected lot (e.g. an extra field on `AcquiredLotAndAmount`), and thread it through `GainLoss` construction so the pool's running average overrides the per-lot cost basis without invalidating the lot-pairing audit trail.
3. **Phase 3 — Alt/Neu split.** The Austrian method reads an `at_regime=alt|neu` marker from `transaction.notes`. Altvermögen path: per-lot FIFO + 365-day Spekulationsfrist. Neuvermögen path: moving average per pool. Both paths emit standard `GainLoss` objects tied to real `InTransaction` lots.
4. **Phase 4 — Swap neutrality (outgoing side only on RP2).** Paired crypto-to-crypto swaps use `at_swap_link=<id>` in notes. The Austrian method emits a zero-gain `GainLoss` on the outgoing Neu leg by overriding cost basis with fee-aware per-unit taxable proceeds, and depletes the pool at the running average. The incoming leg's basis carry is Kassiber's responsibility (see the handoff contract above). Until Kassiber implements basis carry + paired emission, this phase is considered incomplete end-to-end.
5. **Phase 5 — E 1kv report generator.** ~~`src/rp2/plugin/report/at/tax_report_at.py`~~ — **rolled back in Phase 9.** Original phase emitted Kennzahlen 172/174/175/176/801 from an rp2 generator; ownership moved to Kassiber because E 1kv layout is a presentation concern and RP2 is the tax engine.
6. **Phase 6 — `de_AT` localization.** ~~`pybabel init -l de_AT` + translate AT-specific report strings.~~ — **rolled back in Phase 9.** The catalog existed primarily for `tax_report_at`; with that generator gone, de_AT presentation lives in Kassiber.

## Austrian-specific conventions

All markers are carried in the `notes` field of `InTransaction` / `OutTransaction`. No schema changes — this is a deliberate tradeoff to keep the Austrian plugin additive and upstream-proposable. Multiple markers can coexist on the same transaction separated by whitespace (or any of ` \t\n,`). Kassiber is responsible for emitting these markers; RP2 only interprets them.

| Marker | Shape | Who reads it | What happens |
| --- | --- | --- | --- |
| `at_regime=alt` / `at_regime=neu` | flag, no id | `moving_average_at`, Kassiber | Forces the regime for the lot or disposal, overriding the 2021-03-01 Europe/Vienna date cutoff. |
| `at_pool=<id>` | key=value, id is free-form | `moving_average_at` (Neu only) | Partitions the Neuvermögen moving-average pool. Lots and disposals with the same `<id>` share one running average. Absent marker → `"default"` pool. Ignored for Alt (universal FIFO). |
| `at_swap_link=<id>` | key=value, id required non-empty | `moving_average_at` (Neu only), Kassiber | Marks one leg of a matched crypto-to-crypto swap. On Neu disposals, forces cost basis = fee-aware taxable proceeds so the outgoing `GainLoss` stays at zero even when `crypto_balance_change` includes a fee. On Alt disposals, the marker is **ignored** (Austrian law treats Alt swaps as regime-breaking taxable disposals). |

**Disambiguation.** A disposal with no `at_regime` marker is routed by lot availability: only Alt available → consume Alt; only Neu available → consume Neu. If both regimes have lots for the disposal's pool, RP2 raises `RP2ValueError` — Kassiber must tag the disposal. There is no silent "Alt first" preference.

**Swap carryover contract (Kassiber's responsibility).** The outgoing leg is handled fully on RP2's side: detected by `at_swap_link=<id>` on the `OutTransaction`, emitted as a zero-gain `GainLoss` using fee-aware per-unit taxable proceeds as the override, and pool depleted by `crypto_out_with_fee * pool_average` at the running average. The incoming leg is Kassiber's job: for each matched pair, Kassiber must synthesize the destination asset's `InTransaction` with `fiat_in_with_fee = crypto_out_no_fee * neu_pool_avg_at_swap_time` so the non-fee portion of the basis carries across (the fee portion is absorbed as expense). RP2 does **not** perform cross-asset validation — it trusts the marker. Unmatched or uni-directional `at_swap_link` markers will silently produce zero-gain rows on the outgoing side without a corresponding basis carry on the incoming side; Kassiber is expected to pre-validate pairing before emission.

**Empty-id rejection.** `at_swap_link=` with no value after the `=` raises `RP2ValueError` — it almost always indicates a Kassiber bug (forgetting to interpolate the id).

These conventions keep RP2's public types unchanged, so the Austrian path is an additive overlay.

## Kassiber handoff surface

Since Phase 9 RP2 is the Austrian tax **engine** only; layout, Kennzahlen aggregation, and de_AT presentation live in Kassiber. The stable API Kassiber imports from `rp2.plugin.country.at`:

**Classification**
- `classify_disposal(gain_loss: GainLoss) -> AtDisposalCategory` — routes any `GainLoss` to its semantic Austrian category. Encapsulates the regime + Spekulationsfrist + swap-neutrality + earn-split rules so Kassiber does not re-implement them.
- `AtDisposalCategory` enum: `INCOME_GENERAL` (currently Kz 172), `INCOME_CAPITAL_YIELD` (currently Kz 175), `NEU_GAIN` (currently Kz 174), `NEU_LOSS` (currently Kz 176), `NEU_SWAP` (no Kennzahl), `ALT_SPEKULATION` (currently Kz 801), `ALT_TAXFREE` (no Kennzahl). Mapping category → BMF Kennzahl code lives in Kassiber because the codes can change across tax reforms while the semantic bucketing does not.

**Regime + marker primitives** (used by `classify_disposal` internally; also public for finer-grained consumers)
- `classify_lot_regime(lot: InTransaction) -> str` — returns `REGIME_ALT` or `REGIME_NEU` by marker or date-fallback.
- `pool_id_from_notes(notes: Optional[str]) -> str`
- `has_swap_link(event: Optional[AbstractTransaction]) -> bool`
- `swap_link_id(event: Optional[AbstractTransaction]) -> Optional[str]`
- `event_has_explicit_regime` / `explicit_event_regime` (for Alt/Neu disambiguation on disposal events).

**Constants**
- `REGIME_ALT`, `REGIME_NEU`, `AT_NEU_CUTOFF`, `AT_SPEKULATIONSFRIST_DAYS`, `AT_POOL_MARKER`, `AT_SWAP_MARKER`, `AT_DEFAULT_POOL`.

Kassiber iterates `ComputedData.gain_loss_set`, calls `classify_disposal` per row, groups by category, and renders the E 1kv layout — formatting, styling, German labels, and FinanzOnline transcription sheet are all on Kassiber's side. The `moving_average_at` method produces correct per-disposal `GainLoss` rows with pool-preserving Neu swaps and Alt FIFO basis regardless of how the output is rendered.

## Design rules (from [README.dev.md](README.dev.md))

- **Privacy.** User data never leaves the user's machine; no network calls allowed anywhere in RP2.
- **Immutability.** `@dataclass(frozen=True)`; private fields via double-underscore; read-only properties for exposed access; no write-properties.
- **Runtime checks.** Public functions type-check their parameters via `Configuration.type_check_*()` or `<class>.type_check()`.
- **High-precision math.** All decimal arithmetic goes through `RP2Decimal`. Never mix `RP2Decimal` with other numeric types in expressions.
- **One class per file.** File name matches the class name, lowercase-with-underscores. Abstract class names start with `Abstract`.
- **No `*` imports.** No raw strings unless they occur exactly once (use a named constant).
- **Logging via [src/rp2/logger.py](src/rp2/logger.py).**
- **Identity.** Any class added to a dict or set must redefine `__eq__`, `__ne__`, and `__hash__`.

## Working rules

- **Preserve RP2's approach.** Default to additive plugin work. Core edits (`GainLoss`, `compute_tax`, `AbstractAccountingMethod`, transaction dataclasses) are allowed only where the task strictly requires them — think twice, justify the need, keep the diff minimal, and leave every unrelated code path untouched.
- Each phase lands as its own commit so rollback points stay clean.
- Before committing, review `git diff --cached` plus any unstaged `git diff` as a separate pass from implementation. Fix correctness and consistency issues before push.
- Existing lot-based methods (`fifo`, `lifo`, `hifo`, `lofo`) must pass their golden ODS diffs unchanged after any new work lands.
- When adding a new country plugin, keep it minimal and additive — mirror [es.py](src/rp2/plugin/country/es.py) / [ie.py](src/rp2/plugin/country/ie.py) shape and add the corresponding entry in [setup.cfg](setup.cfg) `console_scripts`.
- Prefer the `notes` channel over schema changes for convention markers (`at_pool=…`, `at_regime=…`, `at_swap_link=…`).

## Verification

Dependencies install via:

```bash
virtualenv -p python3 .venv && . .venv/bin/activate && .venv/bin/pip3 install -e '.[dev]'
```

Baseline commands (run each before declaring work done):

```bash
pytest --tb=native --verbose        # unit tests
mypy src tests                      # type check
pylint -r y src tests/*.py          # lint
bandit -r src                       # security check
black src tests                     # reformat
isort .                             # sort imports
pre-commit run --all-files          # full pre-commit pass without committing
```

For debug logs prepend `LOG_LEVEL=DEBUG` to the relevant `rp2_<country>` command:

```bash
LOG_LEVEL=DEBUG rp2_at -o output -p at_example_ config/crypto_example.ini input/crypto_example.ods
```

## Known gaps and non-goals

- **Swap neutrality is only half-implemented end-to-end.** The rp2 side emits a zero-gain `GainLoss` on the outgoing Neu leg using fee-aware taxable proceeds and keeps the pool average invariant (Phase 4 complete on rp2). The incoming side — pairing legs across assets and synthesizing the destination `InTransaction` with the carried basis — lives in Kassiber and is not implemented yet. Until Kassiber lands that, Austrian swap handling is dark-launched.
- **Kassiber-side alignment is outstanding.** Kassiber's `tax_policy.py` still advertises `fifo` + generic reports for AT and rejects Austrian profiles in tests. The marker emission layer (`at_regime`, `at_pool`, `at_swap_link`) plus the E 1kv rendering (consuming `classify_disposal` + `AtDisposalCategory` from `rp2.plugin.country.at`) do not exist on the Kassiber side. Lifting the rejection guard without these changes would silently route AT through Kassiber's stale defaults instead of rp2's Austrian plugin.
- **E 1kv layout is not in rp2.** Since Phase 9 the BMF-aligned summary, FinanzOnline transcription sheet, and de_AT presentation are Kassiber's responsibility. CLI-only users of `rp2_at` get `open_positions` only; for an Austrian tax report they need Kassiber.
- This fork does **not** plan to host FinanzOnline filing, Regelbesteuerungsoption computation, Betriebsvermögen handling, NFT/asset-backed-token treatment, or multi-year crypto loss carryforward — per Kassiber scope.

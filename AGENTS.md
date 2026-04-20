# AGENTS.md

## Project shape

- RP2 is a local-first, privacy-first crypto tax engine with a plugin architecture.
- This repo is the **Kassiber-maintained fork** at `bitcoinaustria/rp2`. The upstream is `eprbell/rp2`. Changes meant for upstream should be authored so they can be proposed as independent PRs (additive country plugin, additive accounting method, additive transaction field with default value, etc.).
- The primary consumer of this fork is [Kassiber](https://github.com/bitcoinaustria/kassiber), which embeds RP2 as its tax engine via [kassiber/core/engines/rp2.py](../kassiber/kassiber/core/engines/rp2.py). Kassiber normalizes raw transactions into RP2 inputs; RP2 owns the tax math.
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
4. **Phase 4 — Swap neutrality.** Express paired crypto-to-crypto swaps through existing transaction types plus a convention marker in `notes` (e.g. `at_swap_link=<id>`). The Austrian method detects the linkage and carries cost basis across legs without realizing a gain.
5. **Phase 5 — E 1kv report generator.** `src/rp2/plugin/report/at/tax_report_at.py` + ODS template at `src/rp2/plugin/report/data/at/`. Emits Kennzahlen 171/172/173/174/175/176 plus Altvermögen-Spekulation table.
6. **Phase 6 — `de` localization.** `pybabel init -l de` + translate report strings.

## Austrian-specific conventions

- **Pool identity** for moving average is carried on individual transactions via an `at_pool=<id>` marker in `notes`. RP2 is agnostic about the pool's meaning — Kassiber decides whether a pool is one wallet, a group of wallets, or the user's entire holdings. If the marker is absent, RP2 falls back to a single `"default"` pool.
- **Regime classification** likewise comes in via `notes`: `at_regime=alt` or `at_regime=neu`. If absent, the AT method classifies by acquisition date at the 2021-03-01 cutoff (Europe/Vienna).
- **Swap linkage** is carried via `linked_unique_id` on both legs of a matched crypto-to-crypto swap.

These conventions keep RP2's public types almost unchanged, so the Austrian path is close to an additive overlay.

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

- Austrian moving-average output is under active development; until Phase 3 lands, `rp2_at` runs only FIFO and is not filing-ready for Neuvermögen.
- Crypto-to-crypto swap neutrality (§ 27b Abs 3 Z 2) ships in Phase 4; before that, swaps processed through the Austrian plugin will incorrectly realize gains/losses.
- E 1kv report output ships in Phase 5; until then, users export via `rp2_full_report` and reformat manually.
- This fork does **not** plan to host FinanzOnline filing, Regelbesteuerungsoption computation, Betriebsvermögen handling, NFT/asset-backed-token treatment, or multi-year crypto loss carryforward — per Kassiber scope.

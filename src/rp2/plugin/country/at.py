# Copyright 2026 bitcoinaustria
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import re
import sys
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from rp2.abstract_country import AbstractCountry
from rp2.abstract_transaction import AbstractTransaction
from rp2.entry_types import TransactionType
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.rp2_decimal import ZERO
from rp2.rp2_error import RP2ValueError
from rp2.rp2_main import rp2_main

# Austrian Altvermögen/Neuvermögen cutoff per § 27b EStG: anything acquired on or before
# 2021-02-28 (Europe/Vienna) is Altvermögen; everything later is Neuvermögen.
AT_NEU_CUTOFF: datetime = datetime(2021, 3, 1, 0, 0, 0, tzinfo=ZoneInfo("Europe/Vienna"))

REGIME_ALT: str = "alt"
REGIME_NEU: str = "neu"

_REGIME_MARKER_ALT: str = "at_regime=alt"
_REGIME_MARKER_NEU: str = "at_regime=neu"

# Crypto-to-crypto swap marker (§ 27b Abs 3 Z 2 EStG: tax-neutral for Neuvermögen). The
# accounting method consumes this; consumers that want to bucket swaps in their reporting
# layer (e.g. Kassiber) re-read it via `has_swap_link` / `swap_link_id`.
AT_SWAP_MARKER: str = "at_swap_link="

# Pool identity marker (§ 2 KryptowährungsVO: moving average is applied per pool). Kassiber
# decides what a pool is (single wallet, wallet group, whole-user-holdings). If the marker is
# absent, the AT method buckets lots/disposals into AT_DEFAULT_POOL, so single-pool users do
# not have to emit the marker.
AT_POOL_MARKER: str = "at_pool="
AT_DEFAULT_POOL: str = "default"

# Spekulationsfrist threshold for Altvermögen disposals (private-investor § 31 regime).
AT_SPEKULATIONSFRIST_DAYS: int = 365

# BMF income classification of crypto earn events:
# * Kz 175 (Einkünfte aus der Überlassung von Kryptowährungen): lending/staking — user
#   gives crypto to a third party for a return.
# * Kz 172 (Laufende Einkünfte): everything else rp2 models as an earn event (mining,
#   airdrops, hardforks, generic income, wages-in-crypto fallback).
# The category enum below exposes this split; the mapping from semantic category to a BMF
# Kennzahl code lives in the consumer (Kassiber), because Kennzahl codes can change with
# tax reforms while the semantic bucketing does not.
_CAPITAL_YIELD_TRANSACTION_TYPES: frozenset[TransactionType] = frozenset({TransactionType.STAKING, TransactionType.INTEREST})


class AtDisposalCategory(Enum):
    """Semantic Austrian bucketing of a GainLoss row.

    These are engine-level categories; the presentation layer (Kassiber) maps them onto the
    specific BMF Kennzahlen the taxpayer transcribes into FinanzOnline.
    """

    INCOME_GENERAL = "income_general"  # earn event, non-lending/staking (currently Kz 172)
    INCOME_CAPITAL_YIELD = "income_capital_yield"  # lending/staking (currently Kz 175)
    NEU_GAIN = "neu_gain"  # realized gain on Neuvermögen (currently Kz 174)
    NEU_LOSS = "neu_loss"  # realized loss on Neuvermögen (currently Kz 176, positive magnitude)
    NEU_SWAP = "neu_swap"  # tax-neutral crypto-to-crypto swap on Neuvermögen (excluded from all Kennzahlen)
    ALT_SPEKULATION = "alt_spekulation"  # Altvermögen disposal within Spekulationsfrist (currently Kz 801)
    ALT_TAXFREE = "alt_taxfree"  # Altvermögen disposal past Spekulationsfrist (no Kennzahl)


# `notes` is a free-form field, so marker parsing tokenizes on the documented separators and
# matches tokens exactly. Substring matching would let unrelated text like `not_at_regime=alt`
# or `at_swap_link_v2=…` flip the regime or trigger swap neutrality by accident, which on the
# Neu swap path becomes silent tax underreporting.
_MARKER_SEPARATOR_PATTERN: "re.Pattern[str]" = re.compile(r"[ \t\n,]+")


def _tokenize_notes(notes: Optional[str]) -> Tuple[str, ...]:
    if not notes:
        return ()
    return tuple(token for token in _MARKER_SEPARATOR_PATTERN.split(notes) if token)


def _marker_value(notes: Optional[str], marker: str) -> Optional[str]:
    # Returns the value after `marker` for the single token starting with it, or None if no
    # such token exists. Raises on duplicates — a repeated `at_pool=` or `at_swap_link=` is
    # ambiguous and always indicates a caller bug (Kassiber pre-validates pairing).
    matches: List[str] = [token for token in _tokenize_notes(notes) if token.startswith(marker)]
    if not matches:
        return None
    if len(matches) > 1:
        raise RP2ValueError(f"Duplicate `{marker}` markers in notes; only one is allowed per transaction. Found: {matches}")
    return matches[0][len(marker) :]


def _regime_from_notes(notes: Optional[str]) -> Optional[str]:
    regimes: List[str] = []
    for token in _tokenize_notes(notes):
        if token == _REGIME_MARKER_ALT:
            regimes.append(REGIME_ALT)
        elif token == _REGIME_MARKER_NEU:
            regimes.append(REGIME_NEU)
    if not regimes:
        return None
    if len(set(regimes)) > 1:
        raise RP2ValueError(f"Conflicting `at_regime` markers in notes: {regimes}. Only one of `at_regime=alt` or `at_regime=neu` is allowed per transaction.")
    if len(regimes) > 1:
        raise RP2ValueError(f"Duplicate `at_regime={regimes[0]}` markers in notes; only one is allowed per transaction.")
    return regimes[0]


def classify_lot_regime(lot: InTransaction) -> str:
    tagged = _regime_from_notes(lot.notes)
    if tagged is not None:
        return tagged
    return REGIME_ALT if lot.timestamp < AT_NEU_CUTOFF else REGIME_NEU


def event_has_explicit_regime(event: Optional[AbstractTransaction]) -> bool:
    if event is None:
        return False
    return _regime_from_notes(event.notes) is not None


def explicit_event_regime(event: Optional[AbstractTransaction]) -> str:
    # Precondition: event_has_explicit_regime(event) is True.
    if event is None:
        raise RuntimeError("explicit_event_regime called on None event")
    tagged = _regime_from_notes(event.notes)
    if tagged is None:
        raise RuntimeError("explicit_event_regime called without an explicit marker")
    return tagged


def pool_id_from_notes(notes: Optional[str]) -> str:
    value: Optional[str] = _marker_value(notes, AT_POOL_MARKER)
    if value is None or value == "":
        return AT_DEFAULT_POOL
    return value


def swap_link_id(event: Optional[AbstractTransaction]) -> Optional[str]:
    # Returns the swap-link id if the marker is present AND non-empty. Returns None both for
    # "marker absent" and "marker present but empty" — callers that need to distinguish the
    # two cases use `has_swap_link` first and then validate the id.
    if event is None:
        return None
    value: Optional[str] = _marker_value(event.notes, AT_SWAP_MARKER)
    if value is None or value == "":
        return None
    return value


def has_swap_link(event: Optional[AbstractTransaction]) -> bool:
    # True iff a token in notes starts with the literal `at_swap_link=` marker, regardless of
    # id value. Use this to decide "is this intended as a swap?" then validate the id with
    # swap_link_id. Substring matching would let unrelated text accidentally trigger the Neu
    # swap neutrality path — tokenized matching is required.
    if event is None:
        return False
    return any(token.startswith(AT_SWAP_MARKER) for token in _tokenize_notes(event.notes))


def validate_at_swap_link_pairing(input_data_list: Sequence[InputData]) -> None:
    """Verify every `at_swap_link=<id>` marker is paired across assets.

    Kassiber's annotation layer tags both legs of a swap when both legs are present in its
    input, but it cannot tag a row that was never imported. An orphan outgoing marker
    would silently zero a taxable gain on RP2's side with no corresponding basis carry
    anywhere in the dataset — a class of bug Kassiber structurally cannot catch. This
    validator walks the full unfiltered input across every asset, collects each
    `at_swap_link=<id>` appearance, and asserts each id has exactly one outgoing leg
    (OutTransaction) and one incoming leg (InTransaction) on two different assets.

    Runs against unfiltered transaction sets so the paired leg is still found even if it
    falls outside the user's from/to date window. No-op when no markers are present.

    Events explicitly tagged `at_regime=alt` are skipped: per § 27b EStG, Alt swaps are
    regime-breaking taxable disposals — the accounting method ignores `at_swap_link` on
    Alt markers and realizes a normal gain. Validating pairing there would hard-fail
    legitimate `at_regime=alt at_swap_link=...` inputs that the engine otherwise handles
    correctly. Events without an explicit regime marker are still validated — ambiguous
    cases should be disambiguated by the caller, not silently skipped.
    """
    # For each id, track appearances as (asset, transaction, direction) where direction is
    # "out" for OutTransaction and "in" for InTransaction. Only swap-neutrality-eligible
    # events participate in pairing; explicit Alt events are skipped.
    out_by_id: Dict[str, List[Tuple[str, OutTransaction]]] = {}
    in_by_id: Dict[str, List[Tuple[str, InTransaction]]] = {}
    for input_data in input_data_list:
        for entry in input_data.unfiltered_out_transaction_set:
            out_tx = entry
            if not isinstance(out_tx, OutTransaction):
                continue
            sid: Optional[str] = swap_link_id(out_tx)
            if sid is None:
                continue
            if event_has_explicit_regime(out_tx) and explicit_event_regime(out_tx) == REGIME_ALT:
                continue
            out_by_id.setdefault(sid, []).append((input_data.asset, out_tx))
        for entry in input_data.unfiltered_in_transaction_set:
            in_tx = entry
            if not isinstance(in_tx, InTransaction):
                continue
            # InTransaction is a subclass of AbstractTransaction, so swap_link_id works.
            sid = swap_link_id(in_tx)
            if sid is None:
                continue
            if event_has_explicit_regime(in_tx) and explicit_event_regime(in_tx) == REGIME_ALT:
                continue
            in_by_id.setdefault(sid, []).append((input_data.asset, in_tx))

    all_ids: Set[str] = set(out_by_id) | set(in_by_id)
    for sid in sorted(all_ids):
        outs: List[Tuple[str, OutTransaction]] = out_by_id.get(sid, [])
        ins: List[Tuple[str, InTransaction]] = in_by_id.get(sid, [])
        if len(outs) != 1 or len(ins) != 1:
            raise RP2ValueError(
                f"Unpaired `at_swap_link={sid}` marker: expected exactly one OutTransaction and one InTransaction, "
                f"found {len(outs)} outgoing and {len(ins)} incoming. "
                f"Kassiber must emit both legs of every crypto-to-crypto swap before RP2 can honor the marker. "
                f"Outgoing: {[(asset, tx.internal_id) for asset, tx in outs]}; Incoming: {[(asset, tx.internal_id) for asset, tx in ins]}"
            )
        out_asset: str = outs[0][0]
        in_asset: str = ins[0][0]
        if out_asset == in_asset:
            raise RP2ValueError(
                f"`at_swap_link={sid}` pair is same-asset (both legs on {out_asset}). A crypto-to-crypto swap "
                f"crosses two assets; a same-asset pair indicates a Kassiber emission bug or a misclassified "
                f"same-asset transfer. Outgoing: {outs[0][1].internal_id}; Incoming: {ins[0][1].internal_id}"
            )


def classify_disposal(gain_loss: GainLoss) -> AtDisposalCategory:
    """Route a GainLoss to its Austrian semantic category.

    Earn events (no `acquired_lot`) split on transaction type: STAKING/INTEREST go to
    INCOME_CAPITAL_YIELD (currently Kz 175), everything else to INCOME_GENERAL (currently
    Kz 172). Disposals route on regime: Neu disposals marked `at_swap_link=<id>` are
    tax-neutral (NEU_SWAP), other Neu disposals split on sign into NEU_GAIN/NEU_LOSS. Alt
    disposals split on the 365-day Spekulationsfrist: >= threshold → ALT_TAXFREE, else
    ALT_SPEKULATION.
    """
    if gain_loss.acquired_lot is None:
        if gain_loss.taxable_event.transaction_type in _CAPITAL_YIELD_TRANSACTION_TYPES:
            return AtDisposalCategory.INCOME_CAPITAL_YIELD
        return AtDisposalCategory.INCOME_GENERAL
    regime: str = classify_lot_regime(gain_loss.acquired_lot)
    if regime == REGIME_NEU:
        if has_swap_link(gain_loss.taxable_event):
            return AtDisposalCategory.NEU_SWAP
        return AtDisposalCategory.NEU_GAIN if gain_loss.fiat_gain >= ZERO else AtDisposalCategory.NEU_LOSS
    holding_days: int = (gain_loss.taxable_event.timestamp - gain_loss.acquired_lot.timestamp).days
    if holding_days >= AT_SPEKULATIONSFRIST_DAYS:
        return AtDisposalCategory.ALT_TAXFREE
    return AtDisposalCategory.ALT_SPEKULATION


# Austria-specific class
class AT(AbstractCountry):
    def __init__(self) -> None:
        super().__init__("at", "eur")

    # Measured in days. Austria has no generic long-term threshold: Neuvermögen disposals
    # are taxed at 27.5% regardless of holding period, and Altvermögen's 1-year
    # Spekulationsfrist applies only to pre-2021-03-01 lots. Regime-specific holding-period
    # handling belongs inside the Austrian accounting method (Phase 3+), not here.
    def get_long_term_capital_gain_period(self) -> int:
        return sys.maxsize

    # Default accounting method to use if the user doesn't specify one on the command line.
    # `moving_average_at` is the legally correct default: it routes Altvermögen disposals to a
    # FIFO path (Spekulationsfrist derivable in the report) and Neuvermögen disposals to the
    # gleitender Durchschnittspreis (§ 2 KryptowährungsVO).
    def get_default_accounting_method(self) -> str:
        return "moving_average_at"

    # Set of accounting methods accepted in the country. `moving_average` (plain) is kept for
    # comparison runs; `fifo` stays available for diagnostics and legacy imports.
    def get_accounting_methods(self) -> Set[str]:
        return {"fifo", "moving_average", "moving_average_at"}

    # Default set of generators when the user doesn't specify any on the command line. AT
    # ships only `open_positions` from rp2; the BMF E 1kv layout and Kennzahlen aggregation
    # live in Kassiber (the primary consumer per AGENTS.md), which imports `classify_disposal`
    # + `AtDisposalCategory` to bucket rows without re-implementing Austrian tax semantics.
    # `rp2_full_report` is intentionally excluded: its long/short split relies on
    # `get_long_term_capital_gain_period()` which we disable (sys.maxsize), so it would emit
    # a misleading "all short-term" report. Users can still request it explicitly via `-g`.
    def get_report_generators(self) -> Set[str]:
        return {"open_positions"}

    # Default language at report generation when the user doesn't specify `-g`. Kassiber
    # owns the German-language Austrian report; rp2's generators fall back to English.
    def get_default_generation_language(self) -> str:
        return "en"

    # Cross-asset invariant: every `at_swap_link=<id>` marker must appear on exactly one
    # OutTransaction and one InTransaction on two different assets. Kassiber's annotation
    # layer enforces this when both legs exist in its input, but cannot annotate rows the
    # user never imported — so an orphan outgoing marker can still reach RP2 and silently
    # zero a taxable gain. The cross-asset check here is the backstop.
    def validate_input_data(self, input_data_list: Sequence[InputData]) -> None:
        validate_at_swap_link_pairing(input_data_list)


# Austria-specific entry point
def rp2_entry() -> None:
    rp2_main(AT())

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


import sys
from datetime import datetime
from enum import Enum
from typing import Optional, Set
from zoneinfo import ZoneInfo

from rp2.abstract_country import AbstractCountry
from rp2.abstract_transaction import AbstractTransaction
from rp2.entry_types import TransactionType
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.rp2_decimal import ZERO
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


def _marker_value(notes: Optional[str], marker: str) -> Optional[str]:
    if not notes:
        return None
    idx: int = notes.find(marker)
    if idx < 0:
        return None
    rest: str = notes[idx + len(marker) :]
    for sep in (" ", "\t", "\n", ","):
        cut: int = rest.find(sep)
        if cut >= 0:
            rest = rest[:cut]
    return rest


def _regime_from_notes(notes: Optional[str]) -> Optional[str]:
    if not notes:
        return None
    if _REGIME_MARKER_ALT in notes:
        return REGIME_ALT
    if _REGIME_MARKER_NEU in notes:
        return REGIME_NEU
    return None


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
    # True iff the literal `at_swap_link=` substring appears in notes, regardless of id value.
    # Use this to decide "is this intended as a swap?" then validate the id with swap_link_id.
    if event is None or not event.notes:
        return False
    return AT_SWAP_MARKER in event.notes


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


# Austria-specific entry point
def rp2_entry() -> None:
    rp2_main(AT())

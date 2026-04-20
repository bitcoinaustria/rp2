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
from typing import Optional, Set
from zoneinfo import ZoneInfo

from rp2.abstract_country import AbstractCountry
from rp2.abstract_transaction import AbstractTransaction
from rp2.in_transaction import InTransaction
from rp2.rp2_main import rp2_main

# Austrian Altvermögen/Neuvermögen cutoff per § 27b EStG: anything acquired on or before
# 2021-02-28 (Europe/Vienna) is Altvermögen; everything later is Neuvermögen.
AT_NEU_CUTOFF: datetime = datetime(2021, 3, 1, 0, 0, 0, tzinfo=ZoneInfo("Europe/Vienna"))

REGIME_ALT: str = "alt"
REGIME_NEU: str = "neu"

_REGIME_MARKER_ALT: str = "at_regime=alt"
_REGIME_MARKER_NEU: str = "at_regime=neu"

# Crypto-to-crypto swap marker (§ 27b Abs 3 Z 2 EStG: tax-neutral for Neuvermögen). Both the
# accounting method and the report plugin depend on this marker; keep it in one place.
AT_SWAP_MARKER: str = "at_swap_link="

# Pool identity marker (§ 2 KryptowährungsVO: moving average is applied per pool). Kassiber
# decides what a pool is (single wallet, wallet group, whole-user-holdings). If the marker is
# absent, the AT method buckets lots/disposals into AT_DEFAULT_POOL, so single-pool users do
# not have to emit the marker.
AT_POOL_MARKER: str = "at_pool="
AT_DEFAULT_POOL: str = "default"

# Spekulationsfrist threshold for Altvermögen disposals (private-investor § 31 regime).
AT_SPEKULATIONSFRIST_DAYS: int = 365


def _marker_value(notes: Optional[str], marker: str) -> Optional[str]:
    if not notes:
        return None
    idx: int = notes.find(marker)
    if idx < 0:
        return None
    rest: str = notes[idx + len(marker):]
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

    # Default set of generators to use if the user doesn't specify them on the command line.
    # `rp2_full_report` is intentionally NOT in the default set: its long/short split relies on
    # `get_long_term_capital_gain_period()`, which we disable (sys.maxsize) because Austria has
    # no generic day-threshold. Including it here would emit a misleading "all short-term"
    # report for Altvermögen. Users can still request it explicitly via `-g rp2_full_report`.
    def get_report_generators(self) -> Set[str]:
        return {
            "open_positions",
            "at.tax_report_at",
        }

    # Default language to use at report generation if the user doesn't specify it on the
    # command line. Austrian taxpayers transcribe values into a German FinanzOnline form,
    # so the de_AT catalog is the natural default. Users can still request `-g en`.
    def get_default_generation_language(self) -> str:
        return "de_AT"


# Austria-specific entry point
def rp2_entry() -> None:
    rp2_main(AT())

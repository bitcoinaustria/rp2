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

from datetime import datetime
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from rp2.abstract_accounting_method import (
    AbstractAcquiredLotCandidates,
    AbstractChronologicalAccountingMethod,
    AcquiredLotAndAmount,
    AcquiredLotCandidatesOrder,
)
from rp2.abstract_transaction import AbstractTransaction
from rp2.in_transaction import InTransaction
from rp2.rp2_decimal import ZERO, RP2Decimal

# Austrian Altvermögen/Neuvermögen cutoff per § 27b EStG: anything acquired on or before
# 2021-02-28 (Europe/Vienna) is Altvermögen; everything later is Neuvermögen.
_AT_NEU_CUTOFF: datetime = datetime(2021, 3, 1, 0, 0, 0, tzinfo=ZoneInfo("Europe/Vienna"))

_REGIME_ALT: str = "alt"
_REGIME_NEU: str = "neu"

_REGIME_MARKER_ALT: str = "at_regime=alt"
_REGIME_MARKER_NEU: str = "at_regime=neu"


def _classify_regime_from_notes(notes: Optional[str]) -> Optional[str]:
    if not notes:
        return None
    if _REGIME_MARKER_ALT in notes:
        return _REGIME_ALT
    if _REGIME_MARKER_NEU in notes:
        return _REGIME_NEU
    return None


def _classify_lot_regime(lot: InTransaction) -> str:
    tagged = _classify_regime_from_notes(lot.notes)
    if tagged is not None:
        return tagged
    return _REGIME_ALT if lot.timestamp < _AT_NEU_CUTOFF else _REGIME_NEU


def _classify_event_regime(event: Optional[AbstractTransaction]) -> str:
    if event is None:
        # Defensive: if the engine couldn't supply the event (shouldn't happen for non-earn
        # disposals), default to Neuvermögen — it's the common post-reform regime and keeps
        # pool-based math self-consistent.
        return _REGIME_NEU
    tagged = _classify_regime_from_notes(event.notes)
    if tagged is not None:
        return tagged
    return _REGIME_NEU


# Austrian-specific moving-average method. Partitions lots into Altvermögen and Neuvermögen
# sub-pools by acquisition-date cutoff (2021-03-01 Europe/Vienna), with an explicit
# `at_regime=alt|neu` marker in `notes` overriding the date inference. Altvermögen disposals
# consume alt lots in FIFO order at their own cost basis (so the Spekulationsfrist can be
# derived in the report from `taxable_event.timestamp - acquired_lot.timestamp`). Neuvermögen
# disposals consume neu lots in FIFO order for the audit trail, but the cost basis surfaces
# as the Neuvermögen pool's running weighted average (gleitender Durchschnittspreis per
# § 2 KryptowährungsVO).
class AccountingMethod(AbstractChronologicalAccountingMethod):
    def __init__(self) -> None:
        super().__init__()
        # key = id(lot_candidates); value = (pool_qty, pool_cost_total, last_synced_to_index).
        # Only Neuvermögen lots contribute to this pool; Altvermögen is tracked per-lot via the
        # base framework's partial-amount mechanism.
        self.__neu_pool_state: Dict[int, Tuple[RP2Decimal, RP2Decimal, int]] = {}

    def lot_candidates_order(self) -> AcquiredLotCandidatesOrder:
        return AcquiredLotCandidatesOrder.OLDER_TO_NEWER

    def seek_non_exhausted_acquired_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
        taxable_event: Optional[AbstractTransaction] = None,
    ) -> Optional[AcquiredLotAndAmount]:
        self.__sync_neu_pool(lot_candidates)
        regime: str = _classify_event_regime(taxable_event)
        if regime == _REGIME_ALT:
            return self.__seek_alt_lot(lot_candidates)
        return self.__seek_neu_lot(lot_candidates, taxable_event_amount)

    def __seek_alt_lot(self, lot_candidates: AbstractAcquiredLotCandidates) -> Optional[AcquiredLotAndAmount]:
        selected, remaining = self.__find_non_exhausted_lot(lot_candidates, _REGIME_ALT)
        if selected is None:
            return None
        lot_candidates.clear_partial_amount(selected)
        return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining)

    def __seek_neu_lot(self, lot_candidates: AbstractAcquiredLotCandidates, taxable_event_amount: RP2Decimal) -> Optional[AcquiredLotAndAmount]:
        selected, remaining = self.__find_non_exhausted_lot(lot_candidates, _REGIME_NEU)
        if selected is None:
            return None
        pool_qty, pool_cost_total, _ = self.__neu_pool_state[id(lot_candidates)]
        pool_average: RP2Decimal = pool_cost_total / pool_qty if pool_qty > ZERO else ZERO
        consumed: RP2Decimal = taxable_event_amount if taxable_event_amount < remaining else remaining
        self.__deduct_from_neu_pool(lot_candidates, consumed, pool_average)
        lot_candidates.clear_partial_amount(selected)
        return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining, unit_cost_basis_override=pool_average)

    def __find_non_exhausted_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        regime: str,
    ) -> Tuple[Optional[InTransaction], RP2Decimal]:
        lots = lot_candidates.acquired_lot_list
        upper: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(upper + 1):
            lot: InTransaction = lots[i]
            if _classify_lot_regime(lot) != regime:
                continue
            if lot_candidates.has_partial_amount(lot):
                remaining: RP2Decimal = lot_candidates.get_partial_amount(lot)
                if remaining <= ZERO:
                    continue
                return lot, remaining
            return lot, lot.crypto_in
        return None, ZERO

    def __sync_neu_pool(self, lot_candidates: AbstractAcquiredLotCandidates) -> None:
        key: int = id(lot_candidates)
        pool_qty, pool_cost_total, last_synced = self.__neu_pool_state.get(key, (ZERO, ZERO, -1))
        lots = lot_candidates.acquired_lot_list
        upper: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(last_synced + 1, upper + 1):
            lot = lots[i]
            if _classify_lot_regime(lot) != _REGIME_NEU:
                continue
            pool_qty = pool_qty + lot.crypto_in
            pool_cost_total = pool_cost_total + lot.fiat_in_with_fee
        self.__neu_pool_state[key] = (pool_qty, pool_cost_total, upper)

    def __deduct_from_neu_pool(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        amount: RP2Decimal,
        pool_average: RP2Decimal,
    ) -> None:
        key: int = id(lot_candidates)
        pool_qty, pool_cost_total, last_synced = self.__neu_pool_state[key]
        pool_qty = pool_qty - amount
        pool_cost_total = pool_cost_total - amount * pool_average
        self.__neu_pool_state[key] = (pool_qty, pool_cost_total, last_synced)

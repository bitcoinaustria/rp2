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

from typing import Dict, Optional, Tuple

from rp2.abstract_accounting_method import (
    AbstractAcquiredLotCandidates,
    AbstractChronologicalAccountingMethod,
    AcquiredLotAndAmount,
    AcquiredLotCandidatesOrder,
)
from rp2.rp2_decimal import ZERO, RP2Decimal


# Moving-average (weighted running-average) accounting. Generic — usable by any country whose
# tax rules require or accept weighted-average cost basis (e.g. Austrian Neuvermögen under
# § 2 KryptowährungsVO). Lot pairing follows FIFO for the audit trail; the running pool
# average is surfaced per-disposal via `unit_cost_basis_override` on the returned
# `AcquiredLotAndAmount`, so individual lots retain their own `fiat_in_with_fee` for other
# consumers while `GainLoss.fiat_cost_basis` uses the pool average.
#
# Pool bookkeeping is per `AbstractAcquiredLotCandidates` instance — in practice one pool per
# (asset, year-mapping) because the accounting engine creates a fresh candidates container
# for each method entry. Disposals leave the running average unchanged (by construction:
# subtracting `amount * avg` from cost_total while subtracting `amount` from qty preserves
# the ratio). Only acquisitions move the average.
class AccountingMethod(AbstractChronologicalAccountingMethod):
    def __init__(self) -> None:
        super().__init__()
        # key = id(lot_candidates); value = (pool_qty, pool_cost_total, last_synced_to_index)
        self.__pool_state: Dict[int, Tuple[RP2Decimal, RP2Decimal, int]] = {}

    def lot_candidates_order(self) -> AcquiredLotCandidatesOrder:
        return AcquiredLotCandidatesOrder.OLDER_TO_NEWER

    def seek_non_exhausted_acquired_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
    ) -> Optional[AcquiredLotAndAmount]:
        self.__sync_pool(lot_candidates)

        fifo_result: Optional[AcquiredLotAndAmount] = super().seek_non_exhausted_acquired_lot(lot_candidates, taxable_event_amount)
        if fifo_result is None:
            return None

        pool_qty, pool_cost_total, _ = self.__pool_state[id(lot_candidates)]
        pool_average: RP2Decimal = pool_cost_total / pool_qty if pool_qty > ZERO else ZERO

        # The engine consumes min(taxable_event_amount, fifo_result.amount) from the pool this
        # call; residual (if any) stays for the next seek call and is accounted for then.
        consumed: RP2Decimal = taxable_event_amount if taxable_event_amount < fifo_result.amount else fifo_result.amount
        self.__deduct_from_pool(lot_candidates, consumed, pool_average)

        return AcquiredLotAndAmount(
            acquired_lot=fifo_result.acquired_lot,
            amount=fifo_result.amount,
            unit_cost_basis_override=pool_average,
        )

    def __sync_pool(self, lot_candidates: AbstractAcquiredLotCandidates) -> None:
        key: int = id(lot_candidates)
        pool_qty, pool_cost_total, last_synced = self.__pool_state.get(key, (ZERO, ZERO, -1))
        current_to: int = lot_candidates.to_index
        lots = lot_candidates.acquired_lot_list
        upper_bound: int = min(current_to, len(lots) - 1)
        for i in range(last_synced + 1, upper_bound + 1):
            lot = lots[i]
            pool_qty = pool_qty + lot.crypto_in
            pool_cost_total = pool_cost_total + lot.fiat_in_with_fee
        self.__pool_state[key] = (pool_qty, pool_cost_total, upper_bound)

    def __deduct_from_pool(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        amount: RP2Decimal,
        pool_average: RP2Decimal,
    ) -> None:
        key: int = id(lot_candidates)
        pool_qty, pool_cost_total, last_synced = self.__pool_state[key]
        pool_qty = pool_qty - amount
        pool_cost_total = pool_cost_total - amount * pool_average
        self.__pool_state[key] = (pool_qty, pool_cost_total, last_synced)

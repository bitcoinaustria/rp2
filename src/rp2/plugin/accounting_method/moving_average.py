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

from typing import Dict, List, Optional

from rp2.abstract_accounting_method import (
    AbstractAcquiredLotCandidates,
    AbstractChronologicalAccountingMethod,
    AcquiredLotAndAmount,
    AcquiredLotCandidatesOrder,
    PoolAcquiredLotCandidates,
)
from rp2.abstract_transaction import AbstractTransaction
from rp2.in_transaction import InTransaction
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2TypeError


# Single conventional pool id used by the generic moving-average method. Regime-aware
# methods (e.g. moving_average_at) use their own pool ids; this module is agnostic.
_DEFAULT_POOL: str = "default"


# Moving-average (weighted running-average) accounting. Generic — usable by any country whose
# tax rules require or accept weighted-average cost basis (e.g. Austrian Neuvermögen under
# § 2 KryptowährungsVO). Lot pairing follows FIFO for the audit trail; the running pool
# average is surfaced per-disposal via `unit_cost_basis_override` on the returned
# `AcquiredLotAndAmount`, so individual lots retain their own `fiat_in_with_fee` for other
# consumers while `GainLoss.fiat_cost_basis` uses the pool average.
#
# Pool bookkeeping lives on the `PoolAcquiredLotCandidates` container, not on the method —
# this means the method is stateless and can be safely reused across compute_tax runs, and
# pool state is naturally garbage-collected with its container. Disposals leave the running
# average unchanged (by construction: subtracting `amount * avg` from cost_total while
# subtracting `amount` from qty preserves the ratio). Only acquisitions move the average.
class AccountingMethod(AbstractChronologicalAccountingMethod):
    def create_lot_candidates(
        self, acquired_lot_list: List[InTransaction], acquired_lot_2_partial_amount: Dict[InTransaction, RP2Decimal]
    ) -> PoolAcquiredLotCandidates:
        return PoolAcquiredLotCandidates(self, acquired_lot_list, acquired_lot_2_partial_amount)

    def lot_candidates_order(self) -> AcquiredLotCandidatesOrder:
        return AcquiredLotCandidatesOrder.OLDER_TO_NEWER

    def seek_non_exhausted_acquired_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
        taxable_event: Optional[AbstractTransaction] = None,
    ) -> Optional[AcquiredLotAndAmount]:
        if not isinstance(lot_candidates, PoolAcquiredLotCandidates):
            raise RP2TypeError(
                f"Internal error: moving_average expects PoolAcquiredLotCandidates, got {type(lot_candidates).__name__}"
            )
        self.__sync_pool(lot_candidates)

        fifo_result: Optional[AcquiredLotAndAmount] = super().seek_non_exhausted_acquired_lot(lot_candidates, taxable_event_amount, taxable_event)
        if fifo_result is None:
            return None

        pool_qty, pool_cost_total = lot_candidates.get_pool(_DEFAULT_POOL)
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

    def __sync_pool(self, lot_candidates: PoolAcquiredLotCandidates) -> None:
        pool_qty, pool_cost_total = lot_candidates.get_pool(_DEFAULT_POOL)
        last_synced: int = lot_candidates.last_synced_index
        lots = lot_candidates.acquired_lot_list
        upper_bound: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(last_synced + 1, upper_bound + 1):
            lot = lots[i]
            pool_qty = pool_qty + lot.crypto_in
            pool_cost_total = pool_cost_total + lot.fiat_in_with_fee
        lot_candidates.set_pool(_DEFAULT_POOL, pool_qty, pool_cost_total)
        lot_candidates.set_last_synced_index(upper_bound)

    def __deduct_from_pool(
        self,
        lot_candidates: PoolAcquiredLotCandidates,
        amount: RP2Decimal,
        pool_average: RP2Decimal,
    ) -> None:
        pool_qty, pool_cost_total = lot_candidates.get_pool(_DEFAULT_POOL)
        pool_qty = pool_qty - amount
        pool_cost_total = pool_cost_total - amount * pool_average
        lot_candidates.set_pool(_DEFAULT_POOL, pool_qty, pool_cost_total)

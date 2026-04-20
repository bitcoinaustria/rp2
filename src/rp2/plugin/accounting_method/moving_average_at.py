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
from rp2.abstract_transaction import AbstractTransaction
from rp2.in_transaction import InTransaction
from rp2.plugin.country.at import (
    REGIME_ALT,
    REGIME_NEU,
    classify_lot_regime,
    event_has_explicit_regime,
    explicit_event_regime,
    has_swap_link,
    pool_id_from_notes,
    swap_link_id,
)
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2ValueError


# Austrian-specific moving-average method. Partitions lots into Altvermögen and Neuvermögen
# sub-pools by acquisition-date cutoff (2021-03-01 Europe/Vienna), with an explicit
# `at_regime=alt|neu` marker in `notes` overriding the date inference. Altvermögen disposals
# consume alt lots in FIFO order at their own cost basis (so the Spekulationsfrist can be
# derived in the report from `taxable_event.timestamp - acquired_lot.timestamp`). Neuvermögen
# disposals consume neu lots in FIFO order for the audit trail, but the cost basis surfaces
# as the Neuvermögen pool's running weighted average (gleitender Durchschnittspreis per
# § 2 KryptowährungsVO).
#
# Pool identity. Neuvermögen disposals can be further partitioned by an `at_pool=<id>` marker
# in notes. Lots without the marker land in AT_DEFAULT_POOL, so single-pool users do not need
# to emit it. Kassiber decides what a pool is (one wallet, a wallet group, all holdings).
# Altvermögen consumption stays FIFO across all alt lots (the pool marker is ignored for Alt:
# Austrian law applies universal FIFO to pre-2021 private holdings).
#
# Disambiguation. A disposal without an explicit `at_regime` marker is routed by the lot
# availability: if only Alt lots exist, Alt is consumed; if only Neu, Neu. If both regimes
# have lots that match the disposal's pool, the disposal is ambiguous and raises — the caller
# (Kassiber) must tag the disposal with `at_regime=alt|neu`. There is no silent preference.
class AccountingMethod(AbstractChronologicalAccountingMethod):
    def __init__(self) -> None:
        super().__init__()
        # key = (id(lot_candidates), pool_id); value = (pool_qty, pool_cost_total).
        # Only Neuvermögen lots contribute to this pool; Altvermögen is tracked per-lot via
        # the base framework's partial-amount mechanism.
        self.__neu_pool_state: Dict[Tuple[int, str], Tuple[RP2Decimal, RP2Decimal]] = {}
        # key = id(lot_candidates); value = last index synced into all Neu pools. A single
        # cursor is enough because lots arrive in chronological order and we fan out each
        # lot into its own pool during the walk.
        self.__neu_last_synced: Dict[int, int] = {}

    def lot_candidates_order(self) -> AcquiredLotCandidatesOrder:
        return AcquiredLotCandidatesOrder.OLDER_TO_NEWER

    def seek_non_exhausted_acquired_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
        taxable_event: Optional[AbstractTransaction] = None,
    ) -> Optional[AcquiredLotAndAmount]:
        self.__sync_neu_pools(lot_candidates)
        event_pool: str = pool_id_from_notes(taxable_event.notes if taxable_event is not None else None)

        if event_has_explicit_regime(taxable_event):
            regime: str = explicit_event_regime(taxable_event)
            if regime == REGIME_ALT:
                return self.__seek_alt_lot(lot_candidates)
            return self.__seek_neu_lot(lot_candidates, taxable_event_amount, taxable_event, event_pool)

        # No explicit regime: route by lot availability. If both regimes have lots for this
        # pool, refuse to guess — the caller must disambiguate. This is the inverse of a
        # silent "Alt first" preference.
        alt_available: bool = self.__any_lot_available(lot_candidates, REGIME_ALT, pool_filter=None)
        neu_available: bool = self.__any_lot_available(lot_candidates, REGIME_NEU, pool_filter=event_pool)
        if alt_available and neu_available:
            raise RP2ValueError(
                "Ambiguous Austrian disposal: both Altvermoegen and Neuvermoegen lots are available "
                f"(pool={event_pool}). Tag the disposal with `at_regime=alt` or `at_regime=neu` in notes. "
                f"Event: {taxable_event}"
            )
        if alt_available:
            return self.__seek_alt_lot(lot_candidates)
        return self.__seek_neu_lot(lot_candidates, taxable_event_amount, taxable_event, event_pool)

    def __seek_alt_lot(self, lot_candidates: AbstractAcquiredLotCandidates) -> Optional[AcquiredLotAndAmount]:
        selected, remaining = self.__find_non_exhausted_lot(lot_candidates, REGIME_ALT, pool_filter=None)
        if selected is None:
            return None
        lot_candidates.clear_partial_amount(selected)
        return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining)

    def __seek_neu_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
        taxable_event: Optional[AbstractTransaction],
        event_pool: str,
    ) -> Optional[AcquiredLotAndAmount]:
        selected, remaining = self.__find_non_exhausted_lot(lot_candidates, REGIME_NEU, pool_filter=event_pool)
        if selected is None:
            return None
        pool_qty, pool_cost_total = self.__neu_pool_state.get((id(lot_candidates), event_pool), (ZERO, ZERO))
        pool_average: RP2Decimal = pool_cost_total / pool_qty if pool_qty > ZERO else ZERO
        consumed: RP2Decimal = taxable_event_amount if taxable_event_amount < remaining else remaining
        # Pool depletes at pool_average regardless of how the gain/loss is reported. Depleting
        # `amount * pool_average` from cost_total leaves the running average unchanged by
        # construction, so swap neutrality and normal disposals preserve pool state identically.
        self.__deduct_from_neu_pool(lot_candidates, event_pool, consumed, pool_average)
        lot_candidates.clear_partial_amount(selected)
        if has_swap_link(taxable_event) and taxable_event is not None:
            # Validate the marker carries a non-empty id; an empty `at_swap_link=` would
            # silently force zero gain without Kassiber being able to pair the incoming leg.
            if swap_link_id(taxable_event) is None:
                raise RP2ValueError(
                    f"Empty `at_swap_link=` marker on disposal. The id is required so Kassiber can "
                    f"pair the incoming leg and carry the basis. Event: {taxable_event}"
                )
            # Tax-neutral Neu swap: override cost basis with the proceeds' per-unit value so
            # the GainLoss shows zero gain. The incoming leg's In leg is populated by Kassiber
            # with the carried basis (Kassiber pairs legs via the at_swap_link=<id> marker and
            # sets fiat_in_with_fee = consumed * pool_average on the incoming InTransaction).
            return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining, unit_cost_basis_override=taxable_event.spot_price)
        return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining, unit_cost_basis_override=pool_average)

    def __any_lot_available(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        regime: str,
        pool_filter: Optional[str],
    ) -> bool:
        selected, _remaining = self.__find_non_exhausted_lot(lot_candidates, regime, pool_filter)
        return selected is not None

    def __find_non_exhausted_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        regime: str,
        pool_filter: Optional[str],
    ) -> Tuple[Optional[InTransaction], RP2Decimal]:
        lots = lot_candidates.acquired_lot_list
        upper: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(upper + 1):
            lot: InTransaction = lots[i]
            if classify_lot_regime(lot) != regime:
                continue
            if pool_filter is not None and pool_id_from_notes(lot.notes) != pool_filter:
                continue
            if lot_candidates.has_partial_amount(lot):
                remaining: RP2Decimal = lot_candidates.get_partial_amount(lot)
                if remaining <= ZERO:
                    continue
                return lot, remaining
            return lot, lot.crypto_in
        return None, ZERO

    def __sync_neu_pools(self, lot_candidates: AbstractAcquiredLotCandidates) -> None:
        key: int = id(lot_candidates)
        last_synced: int = self.__neu_last_synced.get(key, -1)
        lots = lot_candidates.acquired_lot_list
        upper: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(last_synced + 1, upper + 1):
            lot = lots[i]
            if classify_lot_regime(lot) != REGIME_NEU:
                continue
            pool: str = pool_id_from_notes(lot.notes)
            pool_key: Tuple[int, str] = (key, pool)
            pool_qty, pool_cost_total = self.__neu_pool_state.get(pool_key, (ZERO, ZERO))
            pool_qty = pool_qty + lot.crypto_in
            pool_cost_total = pool_cost_total + lot.fiat_in_with_fee
            self.__neu_pool_state[pool_key] = (pool_qty, pool_cost_total)
        self.__neu_last_synced[key] = upper

    def __deduct_from_neu_pool(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        pool: str,
        amount: RP2Decimal,
        pool_average: RP2Decimal,
    ) -> None:
        pool_key: Tuple[int, str] = (id(lot_candidates), pool)
        pool_qty, pool_cost_total = self.__neu_pool_state[pool_key]
        pool_qty = pool_qty - amount
        pool_cost_total = pool_cost_total - amount * pool_average
        self.__neu_pool_state[pool_key] = (pool_qty, pool_cost_total)

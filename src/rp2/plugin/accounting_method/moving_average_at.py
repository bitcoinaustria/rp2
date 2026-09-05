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

from typing import Dict, List, Optional, Set, Tuple

from rp2.abstract_accounting_method import (
    AbstractAcquiredLotCandidates,
    AbstractChronologicalAccountingMethod,
    AcquiredLotAndAmount,
    AcquiredLotCandidatesOrder,
    PoolAcquiredLotCandidates,
)
from rp2.abstract_transaction import AbstractTransaction
from rp2.entry_types import TransactionType
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
from rp2.rp2_error import RP2TypeError, RP2ValueError


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
#
# Pool state lives on the `PoolAcquiredLotCandidates` container — one (qty, cost_total) entry
# per pool id. The method itself is stateless.
class AccountingMethod(AbstractChronologicalAccountingMethod):
    def get_open_position_basis(self, lot_candidates: AbstractAcquiredLotCandidates) -> Dict[InTransaction, RP2Decimal]:
        if not isinstance(lot_candidates, PoolAcquiredLotCandidates):
            raise RP2TypeError("Parameter 'lot_candidates' is not of type PoolAcquiredLotCandidates")
        lots = lot_candidates.acquired_lot_list[: lot_candidates.to_index + 1]
        for pool in {pool_id_from_notes(lot.notes) for lot in lots if classify_lot_regime(lot) == REGIME_NEU}:
            self.__sync_neu_pool(lot_candidates, pool)
        return {
            lot: (
                lot_candidates.pool_average(pool_id_from_notes(lot.notes)) * lot.crypto_in
                if classify_lot_regime(lot) == REGIME_NEU
                else lot_candidates.get_fiat_in_with_fee(lot)
            )
            for lot in lots
        }

    def create_lot_candidates(
        self,
        acquired_lot_list: List[InTransaction],
        acquired_lot_2_partial_amount: Dict[InTransaction, RP2Decimal],
        acquired_lot_2_fiat_in_with_fee_override: Optional[Dict[InTransaction, RP2Decimal]] = None,
    ) -> PoolAcquiredLotCandidates:
        return PoolAcquiredLotCandidates(self, acquired_lot_list, acquired_lot_2_partial_amount, acquired_lot_2_fiat_in_with_fee_override)

    def lot_candidates_order(self) -> AcquiredLotCandidatesOrder:
        return AcquiredLotCandidatesOrder.OLDER_TO_NEWER

    def seek_non_exhausted_acquired_lot(
        self,
        lot_candidates: AbstractAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
        taxable_event: Optional[AbstractTransaction] = None,
    ) -> Optional[AcquiredLotAndAmount]:
        if not isinstance(lot_candidates, PoolAcquiredLotCandidates):
            raise RP2TypeError(f"Internal error: moving_average_at expects PoolAcquiredLotCandidates, got {type(lot_candidates).__name__}")
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

    def __seek_alt_lot(self, lot_candidates: PoolAcquiredLotCandidates) -> Optional[AcquiredLotAndAmount]:
        selected, remaining = self.__find_non_exhausted_lot(lot_candidates, REGIME_ALT, pool_filter=None)
        if selected is None:
            return None
        lot_candidates.clear_partial_amount(selected)
        return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining)

    def __seek_neu_lot(
        self,
        lot_candidates: PoolAcquiredLotCandidates,
        taxable_event_amount: RP2Decimal,
        taxable_event: Optional[AbstractTransaction],
        event_pool: str,
    ) -> Optional[AcquiredLotAndAmount]:
        self.__sync_neu_pool(lot_candidates, event_pool)
        selected, remaining = self.__find_non_exhausted_lot(lot_candidates, REGIME_NEU, pool_filter=event_pool)
        if selected is None:
            # Returning None makes the engine raise the generic "Total in-transaction crypto value <
            # total taxable" exhaustion error. Before that, surface a far more actionable diagnostic
            # when the shortfall is specifically a pool mismatch: the disposal's pool is empty but
            # other Neu pools still hold lots — the usual cause is funds acquired in one pool/wallet
            # and disposed from another without re-tagging at_pool= (Kassiber pools per wallet).
            self.__raise_on_neu_pool_mismatch(lot_candidates, event_pool, taxable_event)
            return None
        pool_average: RP2Decimal = lot_candidates.pool_average(event_pool)
        consumed: RP2Decimal = taxable_event_amount if taxable_event_amount < remaining else remaining
        # Pool depletes at pool_average regardless of how the gain/loss is reported. deduct_from_pool
        # subtracts `amount * pool_average` from cost_total, leaving the running average unchanged by
        # construction, so swap neutrality and normal disposals preserve pool state identically.
        lot_candidates.deduct_from_pool(event_pool, consumed, pool_average)
        lot_candidates.clear_partial_amount(selected)
        if has_swap_link(taxable_event) and taxable_event is not None:
            # Validate the marker carries a non-empty id; an empty `at_swap_link=` would
            # silently force zero gain without Kassiber being able to pair the incoming leg.
            if swap_link_id(taxable_event) is None:
                raise RP2ValueError(
                    f"Empty `at_swap_link=` marker on disposal. The id is required so Kassiber can "
                    f"pair the incoming leg and carry the basis. Event: {taxable_event}"
                )
            # Swap neutrality is the § 27b Abs 3 Z 2 EStG carveout for crypto-to-crypto *sales*;
            # tagging a GIFT/DONATE/FEE/LOST/STAKING disposal with `at_swap_link=` would silently
            # zero out a disposal that has no pairable incoming leg. Cross-asset pairing stays
            # Kassiber's responsibility (per AGENTS.md), but the same-event kind check is cheap
            # and rejects the nonsensical combinations before they produce a zero-gain row.
            if taxable_event.transaction_type != TransactionType.SELL:
                raise RP2ValueError(
                    f"`at_swap_link=` marker on non-SELL disposal (transaction_type="
                    f"{taxable_event.transaction_type.name}). Crypto-to-crypto swap neutrality only "
                    f"applies to SELL-type disposals. Event: {taxable_event}"
                )
            # Tax-neutral Neu swap: override cost basis with the disposal's fee-aware per-unit
            # taxable proceeds so the outgoing GainLoss stays exactly at zero gain even when
            # crypto_balance_change includes a fee. The incoming leg is populated by Kassiber
            # with the carried basis (paired via at_swap_link=<id> and seeded onto the
            # incoming InTransaction as fiat_in_with_fee = crypto_out_no_fee * pool_average —
            # the fee portion is absorbed as expense, consistent with depleting the Neu pool
            # by crypto_out_with_fee * pool_average here).
            swap_unit_cost_basis: RP2Decimal = taxable_event.fiat_taxable_amount / taxable_event.crypto_balance_change
            return AcquiredLotAndAmount(
                acquired_lot=selected,
                amount=remaining,
                unit_cost_basis_override=swap_unit_cost_basis,
                taxable_event_unit_cost_basis=pool_average,
            )
        return AcquiredLotAndAmount(acquired_lot=selected, amount=remaining, unit_cost_basis_override=pool_average)

    def __raise_on_neu_pool_mismatch(
        self,
        lot_candidates: PoolAcquiredLotCandidates,
        event_pool: str,
        taxable_event: Optional[AbstractTransaction],
    ) -> None:
        other_pools: Set[str] = set()
        lots = lot_candidates.acquired_lot_list
        upper: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(upper + 1):
            lot: InTransaction = lots[i]
            if classify_lot_regime(lot) != REGIME_NEU:
                continue
            lot_pool: str = pool_id_from_notes(lot.notes)
            if lot_pool == event_pool:
                continue
            if lot_candidates.has_partial_amount(lot) and lot_candidates.get_partial_amount(lot) <= ZERO:
                continue
            other_pools.add(lot_pool)
        if other_pools:
            raise RP2ValueError(
                f"Neuvermoegen disposal tagged at_pool={event_pool!r} has no available lots in that pool, but other Neu pool(s) "
                f"{sorted(other_pools)} still hold lots. This usually means the disposal was tagged with a different pool than its "
                f"acquisition (e.g. funds moved between wallets/pools without re-tagging at_pool=). Event: {taxable_event}"
            )

    def __any_lot_available(
        self,
        lot_candidates: PoolAcquiredLotCandidates,
        regime: str,
        pool_filter: Optional[str],
    ) -> bool:
        selected, _remaining = self.__find_non_exhausted_lot(lot_candidates, regime, pool_filter)
        return selected is not None

    def __find_non_exhausted_lot(
        self,
        lot_candidates: PoolAcquiredLotCandidates,
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

    def __sync_neu_pool(self, lot_candidates: PoolAcquiredLotCandidates, pool: str) -> None:
        last_synced: int = lot_candidates.get_pool_last_synced_index(pool)
        lots = lot_candidates.acquired_lot_list
        upper: int = min(lot_candidates.to_index, len(lots) - 1)
        for i in range(last_synced + 1, upper + 1):
            lot = lots[i]
            if classify_lot_regime(lot) != REGIME_NEU:
                continue
            if pool_id_from_notes(lot.notes) != pool:
                continue
            pool_qty, pool_cost_total = lot_candidates.get_pool(pool)
            remaining = lot_candidates.get_partial_amount(lot) if lot_candidates.has_partial_amount(lot) else lot.crypto_in
            lot_candidates.set_pool(pool, pool_qty + remaining, pool_cost_total + lot_candidates.get_fiat_in_with_fee(lot) * remaining / lot.crypto_in)
        lot_candidates.set_pool_last_synced_index(pool, upper)

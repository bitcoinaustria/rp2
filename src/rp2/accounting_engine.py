# Copyright 2022 eprbell
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

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Iterator, List, NamedTuple, Optional

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import (
    AbstractAccountingMethod,
    AbstractAcquiredLotCandidates,
    AcquiredLotAndAmount,
    PoolAcquiredLotCandidates,
)
from rp2.abstract_transaction import AbstractTransaction
from rp2.configuration import Configuration
from rp2.in_transaction import InTransaction
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2RuntimeError, RP2TypeError, RP2ValueError


class AcquiredLotAndAmounts(NamedTuple):
    acquired_lot: Optional[InTransaction]
    taxable_event_amount: RP2Decimal
    acquired_lot_amount: RP2Decimal


class TaxableEventAndAcquiredLot(NamedTuple):
    taxable_event: AbstractTransaction
    acquired_lot: Optional[InTransaction]
    taxable_event_amount: RP2Decimal
    acquired_lot_amount: RP2Decimal
    # Per-unit cost basis override surfaced from the accounting method. Forwarded into
    # GainLoss so pool-based methods can supply their running average without touching lots.
    unit_cost_basis_override: Optional[RP2Decimal] = None
    # Per-unit basis associated with the taxable event itself. This is distinct from
    # unit_cost_basis_override: Austrian Neu swaps report zero gain using proceeds-per-unit,
    # but carry the source pool average to the paired incoming lot.
    taxable_event_unit_cost_basis: Optional[RP2Decimal] = None


@dataclass(frozen=True, eq=True)
class _AcquiredLotAndIndex:
    acquired_lot: InTransaction
    index: int


class _LotExhaustedException(Exception):
    def __init__(self, message: str = "") -> None:
        self.__message = message
        super().__init__(self.__message)

    def __repr__(self) -> str:
        return self.message

    @property
    def message(self) -> str:
        return self.__message


class TaxableEventsExhaustedException(_LotExhaustedException):
    pass


class AcquiredLotsExhaustedException(_LotExhaustedException):
    pass


class AccountingEngine:
    __taxable_event_iterator: Iterator[AbstractTransaction]
    __acquired_lot_list: List[InTransaction]
    __acquired_lot_avl: AVLTree[str, _AcquiredLotAndIndex]
    __acquired_lot_2_partial_amount: Dict[InTransaction, RP2Decimal]

    # Disambiguation is needed for transactions that have the same timestamp, because the avl tree class expects unique keys: 12 decimal digits express
    # 1 quadrillion, which should be enough to capture the maximum number of same-timestamp transactions in all reasonable cases.
    KEY_DISAMBIGUATOR_LENGTH: int = 12

    @classmethod
    def type_check(cls, name: str, instance: "AccountingEngine") -> "AccountingEngine":
        if not isinstance(name, str):
            raise RP2TypeError(f"Parameter name is not a string: {repr(name)}")
        if not isinstance(instance, cls):
            raise RP2TypeError(f"Parameter '{name}' is not of type {cls.__name__}: {instance}")
        return instance

    def __init__(self, years_2_methods: AVLTree[int, AbstractAccountingMethod]) -> None:
        self.__years_2_methods: AVLTree[int, AbstractAccountingMethod] = years_2_methods
        self.__years_2_lot_candidates: AVLTree[int, AbstractAcquiredLotCandidates] = AVLTree()
        if not self.__years_2_methods:
            raise RP2RuntimeError("Internal error: no accounting method defined")

    # Iterators yield transactions in ascending chronological order
    def initialize(
        self,
        taxable_event_iterator: Iterator[AbstractTransaction],
        acquired_lot_iterator: Iterator[InTransaction],
        acquired_lot_to_partial_amount: Optional[Dict[InTransaction, RP2Decimal]] = None,
        acquired_lot_to_fiat_in_with_fee_override: Optional[Dict[InTransaction, RP2Decimal]] = None,
    ) -> None:
        self.__taxable_event_iterator = taxable_event_iterator
        self.__acquired_lot_list = []
        self.__acquired_lot_avl: AVLTree[str, _AcquiredLotAndIndex] = AVLTree()
        self.__acquired_lot_2_partial_amount: Dict[InTransaction, RP2Decimal] = {} if acquired_lot_to_partial_amount is None else acquired_lot_to_partial_amount

        index: int = 0
        try:
            while True:
                acquired_lot: InTransaction = next(acquired_lot_iterator)
                self.__acquired_lot_list.append(acquired_lot)
                self.__acquired_lot_avl.insert_node(f"{self._get_avl_node_key(acquired_lot.timestamp, str(index))}", _AcquiredLotAndIndex(acquired_lot, index))
                index += 1
        except StopIteration:
            # End of acquired_lots
            pass

        if not self.__acquired_lot_avl.root:
            raise RP2RuntimeError("Internal error: AVL tree has no root node")

        # A redundant schedule boundary must reuse the running pool, not rebuild
        # its remaining inventory from historical acquisition costs.
        self.__years_2_lot_candidates = AVLTree()
        scheduled_methods: Dict[int, AbstractAccountingMethod] = {}
        to_visit = [self.__years_2_methods.root]
        while to_visit:
            node = to_visit.pop()
            if node is None:
                continue
            scheduled_methods[node.key] = node.value
            to_visit.extend([node.left, node.right])
        previous_method: Optional[AbstractAccountingMethod] = None
        previous_candidates: Optional[AbstractAcquiredLotCandidates] = None
        for year, method in sorted(scheduled_methods.items()):
            if previous_method is not None and previous_method.name == method.name:
                candidates = previous_candidates
            else:
                if isinstance(previous_candidates, PoolAcquiredLotCandidates):
                    raise RP2ValueError(
                        "Changing from a pool-based accounting method to a different method is unsupported: "
                        "remaining pool basis cannot be reconstructed from acquisition costs."
                    )
                candidates = method.create_lot_candidates(
                    self.__acquired_lot_list,
                    self.__acquired_lot_2_partial_amount,
                    acquired_lot_to_fiat_in_with_fee_override,
                )
            if candidates is None:
                raise RP2RuntimeError("Internal error: no lot candidates for accounting method")
            self.__years_2_lot_candidates.insert_node(year, candidates)
            previous_method = method
            previous_candidates = candidates

    def _disambiguator(self, internal_id: str) -> str:
        # The caller uses the acquired-list index, not the transaction row:
        # same-timestamp transactions retain stable insertion order, which can
        # differ from row order for library inputs and artificial transfer lots.
        try:
            value: int = int(internal_id)
        except ValueError:
            # The acquired-list index is always a stringified int; retain the defensive fallback.
            return f"0{internal_id:0>{self.KEY_DISAMBIGUATOR_LENGTH}}"
        if value >= 0:
            return f"0{value:0>{self.KEY_DISAMBIGUATOR_LENGTH}}"
        return f"1{-value:0>{self.KEY_DISAMBIGUATOR_LENGTH}}"

    # AVL tree node keys have this format: <timestamp>_<disambiguator>. The disambiguator separates
    # transactions that share a timestamp. Timestamp is in format "YYYYmmddHHMMSS.ffffff".
    def _get_avl_node_key(self, timestamp: datetime, internal_id: str) -> str:
        return f"{timestamp.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S.%f')}_{self._disambiguator(internal_id)}"

    # Produces a key strictly greater than every real lot key at the same timestamp: "2" sorts above
    # both the non-negative ("0...") and negative ("1...") disambiguator classes.
    def _get_avl_node_key_with_max_disambiguator(self, timestamp: datetime) -> str:
        return f"{timestamp.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S.%f')}_2"

    @property
    def years_2_methods(self) -> AVLTree[int, AbstractAccountingMethod]:
        return self.__years_2_methods

    def _get_accounting_method(self, year: int) -> AbstractAccountingMethod:
        method = self.__years_2_methods.find_max_value_less_than(year)
        if method is None:
            raise RP2RuntimeError(f"Internal error: no accounting method assigned for year {year}")
        if not isinstance(method, AbstractAccountingMethod):
            raise RP2RuntimeError(f"Internal error: accounting method assigned for year {year} is not of type AbstractAccountingMethod: {method}")
        return method

    def _set_partial_amount(self, acquired_lot: InTransaction, amount: RP2Decimal) -> None:
        self.__acquired_lot_2_partial_amount[acquired_lot] = amount

    def get_open_position_basis(self, to_date: date) -> Dict[InTransaction, RP2Decimal]:
        """Snapshot a date-bounded replay, including acquisitions after its last disposal."""
        if not isinstance(to_date, date):
            raise RP2TypeError("Parameter 'to_date' is not of type date")
        eligible_indices = [index for index, lot in enumerate(self.__acquired_lot_list) if lot.timestamp.date() <= to_date]
        if not eligible_indices:
            return {}
        method = self._get_accounting_method(to_date.year)
        lot_candidates = self.__years_2_lot_candidates.find_max_value_less_than(to_date.year)
        if lot_candidates is None:
            raise RP2RuntimeError("Internal error: no lot candidates found for report year")
        lot_candidates.set_to_index(eligible_indices[-1])
        return method.get_open_position_basis(lot_candidates)

    def set_acquired_lot_partial_amount(self, acquired_lot: InTransaction, amount: RP2Decimal) -> None:
        InTransaction.type_check("acquired_lot", acquired_lot)
        Configuration.type_check_positive_decimal("amount", amount)
        self._set_partial_amount(acquired_lot, amount)

    def get_next_taxable_event_and_amount(
        self,
        taxable_event: Optional[AbstractTransaction],
        acquired_lot: Optional[InTransaction],
        taxable_event_amount: RP2Decimal,
        acquired_lot_amount: RP2Decimal,
    ) -> TaxableEventAndAcquiredLot:
        new_acquired_lot: Optional[InTransaction] = acquired_lot
        new_acquired_lot_amount: RP2Decimal = acquired_lot_amount - taxable_event_amount if acquired_lot is not None else ZERO
        unit_cost_basis_override: Optional[RP2Decimal] = None
        taxable_event_unit_cost_basis: Optional[RP2Decimal] = None

        try:
            new_taxable_event: AbstractTransaction = next(self.__taxable_event_iterator)
        except StopIteration:
            raise TaxableEventsExhaustedException() from None
        new_taxable_event_amount: RP2Decimal = new_taxable_event.crypto_balance_change

        # If the new taxable event is newer than the old one (and it's not earn-typed) check if there is a newer acquired lot that
        # meets the accounting method criteria (but it's still older than the new taxable event).
        if taxable_event and taxable_event.timestamp < new_taxable_event.timestamp:
            if acquired_lot:
                self._set_partial_amount(acquired_lot, new_acquired_lot_amount)
            paired: TaxableEventAndAcquiredLot = self.get_acquired_lot_for_taxable_event(
                new_taxable_event, acquired_lot, new_taxable_event_amount, new_acquired_lot_amount
            )
            new_acquired_lot = paired.acquired_lot
            new_acquired_lot_amount = paired.acquired_lot_amount
            unit_cost_basis_override = paired.unit_cost_basis_override
            taxable_event_unit_cost_basis = paired.taxable_event_unit_cost_basis

        return TaxableEventAndAcquiredLot(
            taxable_event=new_taxable_event,
            acquired_lot=new_acquired_lot,
            taxable_event_amount=new_taxable_event_amount,
            acquired_lot_amount=new_acquired_lot_amount,
            unit_cost_basis_override=unit_cost_basis_override,
            taxable_event_unit_cost_basis=taxable_event_unit_cost_basis,
        )

    # After selecting the taxable event, RP2 calls this function to find the acquired_lot to pair with it.
    # Handles the pool-based unit_cost_basis_override returned by methods like moving_average_at:
    # it forwards into TaxableEventAndAcquiredLot so GainLoss can apply the pool average without
    # touching the lot. get_acquired_lot_for_timestamp (below) is the taxable-event-free path
    # used by global_allocation and does not participate in the override.
    def get_acquired_lot_for_taxable_event(
        self,
        taxable_event: AbstractTransaction,
        acquired_lot: Optional[InTransaction],  # pylint: disable=unused-argument
        taxable_event_amount: RP2Decimal,
        acquired_lot_amount: RP2Decimal,
    ) -> TaxableEventAndAcquiredLot:
        new_taxable_event_amount: RP2Decimal = taxable_event_amount - acquired_lot_amount
        acquired_lot_and_index: Optional[_AcquiredLotAndIndex] = self.__acquired_lot_avl.find_max_value_less_than(
            self._get_avl_node_key_with_max_disambiguator(taxable_event.timestamp)
        )
        if acquired_lot_and_index is not None:
            if acquired_lot_and_index.acquired_lot != self.__acquired_lot_list[acquired_lot_and_index.index]:
                raise RP2RuntimeError("Internal error: acquired_lot incongruence in accounting logic")
            method = self._get_accounting_method(taxable_event.timestamp.year)
            lot_candidates: Optional[AbstractAcquiredLotCandidates] = self.__years_2_lot_candidates.find_max_value_less_than(taxable_event.timestamp.year)
            # lot_candidates is 1:1 with acquired_lot_and_index, should always be True
            if lot_candidates:
                lot_candidates.set_to_index(acquired_lot_and_index.index)
                acquired_lot_and_amount: Optional[AcquiredLotAndAmount] = method.seek_non_exhausted_acquired_lot(
                    lot_candidates, new_taxable_event_amount, taxable_event=taxable_event
                )
                if acquired_lot_and_amount:
                    return TaxableEventAndAcquiredLot(
                        taxable_event=taxable_event,
                        acquired_lot=acquired_lot_and_amount.acquired_lot,
                        taxable_event_amount=new_taxable_event_amount,
                        acquired_lot_amount=acquired_lot_and_amount.amount,
                        unit_cost_basis_override=acquired_lot_and_amount.unit_cost_basis_override,
                        taxable_event_unit_cost_basis=acquired_lot_and_amount.taxable_event_unit_cost_basis,
                    )

        raise AcquiredLotsExhaustedException()

    def get_acquired_lot_for_timestamp(
        self,
        timestamp: datetime,
        acquired_lot: Optional[InTransaction],  # pylint: disable=unused-argument
        taxable_event_amount: RP2Decimal,
        acquired_lot_amount: RP2Decimal,
    ) -> AcquiredLotAndAmounts:
        new_taxable_event_amount: RP2Decimal = taxable_event_amount - acquired_lot_amount
        # Find the acquired_lot and index just before the timestamp: the index is used as an upper bound
        # in the search of acquired lot candidates (see set_to_index() below).
        acquired_lot_and_index: Optional[_AcquiredLotAndIndex] = self.__acquired_lot_avl.find_max_value_less_than(
            self._get_avl_node_key_with_max_disambiguator(timestamp)
        )
        if acquired_lot_and_index is None:
            raise AcquiredLotsExhaustedException()
        if acquired_lot_and_index.acquired_lot != self.__acquired_lot_list[acquired_lot_and_index.index]:
            raise RP2RuntimeError("Internal error: acquired_lot incongruence in accounting logic")
        method = self._get_accounting_method(timestamp.year)
        lot_candidates: Optional[AbstractAcquiredLotCandidates] = self.__years_2_lot_candidates.find_max_value_less_than(timestamp.year)
        # lot_candidates is 1:1 with acquired_lot_and_index, should always be True
        if not lot_candidates:
            raise RP2RuntimeError("Internal error: no lot candidates found for year")
        lot_candidates.set_to_index(acquired_lot_and_index.index)
        acquired_lot_and_amount: Optional[AcquiredLotAndAmount] = method.seek_non_exhausted_acquired_lot(lot_candidates, new_taxable_event_amount)
        if not acquired_lot_and_amount:
            raise AcquiredLotsExhaustedException()

        return AcquiredLotAndAmounts(
            acquired_lot=acquired_lot_and_amount.acquired_lot,
            taxable_event_amount=new_taxable_event_amount,
            acquired_lot_amount=acquired_lot_and_amount.amount,
        )

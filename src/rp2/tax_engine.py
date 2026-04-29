# Copyright 2021 eprbell
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

from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple, cast

from rp2.abstract_transaction import AbstractTransaction
from rp2.accounting_engine import (
    AccountingEngine,
    AcquiredLotsExhaustedException,
    TaxableEventAndAcquiredLot,
    TaxableEventsExhaustedException,
)
from rp2.computed_data import ComputedData
from rp2.configuration import MAX_DATE, MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.gain_loss_set import GainLossSet
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.logger import LOGGER
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2RuntimeError, RP2ValueError
from rp2.transaction_set import TransactionSet


class TaxableEventComputation(NamedTuple):
    taxable_event: AbstractTransaction
    gain_losses: Tuple[GainLoss, ...]
    taxable_event_unit_cost_basis: Optional[RP2Decimal] = None


def compute_tax(configuration: Configuration, accounting_engine: AccountingEngine, input_data: InputData) -> ComputedData:
    Configuration.type_check("configuration", configuration)
    AccountingEngine.type_check("accounting_engine", accounting_engine)
    InputData.type_check("input_data", input_data)

    cursor: TaxEngineCursor = TaxEngineCursor(configuration, accounting_engine, input_data)
    LOGGER.debug("%s: Created taxable event set", input_data.asset)
    while cursor.has_next():
        cursor.consume_next_taxable_event()
    LOGGER.debug("%s: Created gain-loss set", input_data.asset)

    return cursor.to_computed_data()


class TaxEngineCursor:
    def __init__(
        self,
        configuration: Configuration,
        accounting_engine: AccountingEngine,
        input_data: InputData,
        acquired_lot_2_fiat_in_with_fee_override: Optional[Dict[InTransaction, RP2Decimal]] = None,
    ) -> None:
        Configuration.type_check("configuration", configuration)
        AccountingEngine.type_check("accounting_engine", accounting_engine)
        InputData.type_check("input_data", input_data)

        self.__configuration: Configuration = configuration
        self.__input_data: InputData = input_data
        self.__unfiltered_taxable_event_set: TransactionSet = input_data.create_unfiltered_taxable_event_set(configuration)
        self.__taxable_event_list: List[AbstractTransaction] = [cast(AbstractTransaction, entry) for entry in self.__unfiltered_taxable_event_set]
        self.__taxable_event_index: int = 0
        self.__gain_loss_set: GainLossSet = GainLossSet(configuration, input_data.asset, MIN_DATE, MAX_DATE)
        self.__acquired_lot_2_fiat_in_with_fee_override: Dict[InTransaction, RP2Decimal] = (
            {} if acquired_lot_2_fiat_in_with_fee_override is None else acquired_lot_2_fiat_in_with_fee_override
        )
        self.__current_acquired_lot: Optional[InTransaction] = None
        self.__current_acquired_lot_amount: RP2Decimal = ZERO

        self.__accounting_engine: AccountingEngine = accounting_engine.__class__(accounting_engine.years_2_methods)
        acquired_lot_iterator: Iterator[InTransaction] = iter(cast(Iterable[InTransaction], input_data.unfiltered_in_transaction_set))
        self.__accounting_engine.initialize(
            iter([]),
            acquired_lot_iterator,
            acquired_lot_to_fiat_in_with_fee_override=self.__acquired_lot_2_fiat_in_with_fee_override,
        )

        self.__total_amount: RP2Decimal = ZERO

    @property
    def current_taxable_event(self) -> Optional[AbstractTransaction]:
        if self.__taxable_event_index >= len(self.__taxable_event_list):
            return None
        return self.__taxable_event_list[self.__taxable_event_index]

    @property
    def gain_loss_set(self) -> GainLossSet:
        return self.__gain_loss_set

    def has_next(self) -> bool:
        return self.current_taxable_event is not None

    def consume_next_taxable_event(self) -> TaxableEventComputation:
        first_taxable_event: Optional[AbstractTransaction] = self.current_taxable_event
        if first_taxable_event is None:
            raise TaxableEventsExhaustedException()

        gain_losses: List[GainLoss] = []
        taxable_event_unit_cost_basis: Optional[RP2Decimal] = None
        if self.__current_acquired_lot is not None:
            self.__accounting_engine.set_acquired_lot_partial_amount(self.__current_acquired_lot, self.__current_acquired_lot_amount)
        try:
            current: Optional[TaxableEventAndAcquiredLot] = self.__accounting_engine.get_acquired_lot_for_taxable_event(
                first_taxable_event,
                self.__current_acquired_lot,
                first_taxable_event.crypto_balance_change,
                ZERO,
            )
            while current is not None:
                taxable_event: AbstractTransaction = current.taxable_event
                acquired_lot: Optional[InTransaction] = current.acquired_lot
                taxable_event_amount: RP2Decimal = current.taxable_event_amount
                acquired_lot_amount: RP2Decimal = current.acquired_lot_amount
                unit_cost_basis_override: Optional[RP2Decimal] = current.unit_cost_basis_override
                if current.taxable_event_unit_cost_basis is not None:
                    if taxable_event_unit_cost_basis is not None and taxable_event_unit_cost_basis != current.taxable_event_unit_cost_basis:
                        raise RP2RuntimeError(
                            "Internal error: inconsistent taxable-event unit cost basis while processing "
                            f"{taxable_event}: {taxable_event_unit_cost_basis} != {current.taxable_event_unit_cost_basis}"
                        )
                    taxable_event_unit_cost_basis = current.taxable_event_unit_cost_basis

                # Type check values returned by accounting method plugin
                AbstractTransaction.type_check("taxable_event", taxable_event)
                if acquired_lot is None:
                    # There must always be at least one acquired_lot
                    raise RP2RuntimeError("Parameter 'acquired_lot' is None")
                InTransaction.type_check("acquired_lot", acquired_lot)
                Configuration.type_check_positive_decimal("taxable_event_amount", taxable_event_amount)
                Configuration.type_check_positive_decimal("acquired_lot_amount", acquired_lot_amount)

                if taxable_event.is_earning():
                    # Handle earnings first: they have no acquired-lot
                    gain_loss = GainLoss(self.__configuration, taxable_event_amount, taxable_event, None)
                    LOGGER.debug(
                        "tax_engine: taxable is earn: %s / %s + %s = %s: %s",
                        taxable_event_amount,
                        self.__total_amount,
                        taxable_event_amount,
                        self.__total_amount + taxable_event_amount,
                        gain_loss,
                    )
                    self.__total_amount += taxable_event_amount
                    self.__gain_loss_set.add_entry(gain_loss)
                    gain_losses.append(gain_loss)
                    self.__current_acquired_lot = acquired_lot
                    self.__current_acquired_lot_amount = acquired_lot_amount
                    self.__taxable_event_index += 1
                    break
                if taxable_event_amount == acquired_lot_amount:
                    gain_loss = GainLoss(
                        self.__configuration, taxable_event_amount, taxable_event, acquired_lot, unit_cost_basis_override=unit_cost_basis_override
                    )
                    LOGGER.debug(
                        "tax_engine: taxable == acquired: %s == %s / %s + %s = %s: %s",
                        taxable_event_amount,
                        acquired_lot_amount,
                        self.__total_amount,
                        taxable_event_amount,
                        self.__total_amount + taxable_event_amount,
                        gain_loss,
                    )
                    self.__total_amount += taxable_event_amount
                    self.__gain_loss_set.add_entry(gain_loss)
                    gain_losses.append(gain_loss)
                    self.__current_acquired_lot = acquired_lot
                    self.__current_acquired_lot_amount = ZERO
                    self.__taxable_event_index += 1
                    break
                if taxable_event_amount < acquired_lot_amount:
                    gain_loss = GainLoss(
                        self.__configuration, taxable_event_amount, taxable_event, acquired_lot, unit_cost_basis_override=unit_cost_basis_override
                    )
                    LOGGER.debug(
                        "tax_engine: taxable < acquired: %s < %s / %s + %s = %s: %s",
                        taxable_event_amount,
                        acquired_lot_amount,
                        self.__total_amount,
                        taxable_event_amount,
                        self.__total_amount + taxable_event_amount,
                        gain_loss,
                    )
                    self.__total_amount += taxable_event_amount
                    self.__gain_loss_set.add_entry(gain_loss)
                    gain_losses.append(gain_loss)
                    self.__current_acquired_lot = acquired_lot
                    self.__current_acquired_lot_amount = acquired_lot_amount - taxable_event_amount
                    self.__taxable_event_index += 1
                    break
                # taxable_amount > acquired_lot_amount
                gain_loss = GainLoss(self.__configuration, acquired_lot_amount, taxable_event, acquired_lot, unit_cost_basis_override=unit_cost_basis_override)
                LOGGER.debug(
                    "tax_engine: taxable > acquired: %s > %s / %s + %s = %s: %s",
                    taxable_event_amount,
                    acquired_lot_amount,
                    self.__total_amount,
                    acquired_lot_amount,
                    self.__total_amount + acquired_lot_amount,
                    gain_loss,
                )
                self.__total_amount += acquired_lot_amount
                self.__gain_loss_set.add_entry(gain_loss)
                gain_losses.append(gain_loss)
                current = self.__accounting_engine.get_acquired_lot_for_taxable_event(taxable_event, acquired_lot, taxable_event_amount, acquired_lot_amount)
        except AcquiredLotsExhaustedException:
            raise RP2ValueError("Total in-transaction crypto value < total taxable crypto value") from None

        return TaxableEventComputation(first_taxable_event, tuple(gain_losses), taxable_event_unit_cost_basis)

    def to_computed_data(self) -> ComputedData:
        return ComputedData(
            self.__input_data.asset,
            self.__unfiltered_taxable_event_set,
            self.__gain_loss_set,
            self.__input_data,
            self.__configuration.from_date,
            self.__configuration.to_date,
            in_transaction_2_fiat_in_with_fee_override=self.__acquired_lot_2_fiat_in_with_fee_override,
        )

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

"""Synthetic regressions separating acquisition, realized, and report-date basis."""

import unittest
from datetime import date
from typing import Dict, List, Optional, cast

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.abstract_transaction import AbstractTransaction
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MAX_DATE, MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.intra_transaction import IntraTransaction
from rp2.out_transaction import OutTransaction
from rp2.per_wallet_tax_engine import compute_tax_per_wallet
from rp2.plugin.accounting_method.fifo import AccountingMethod as FifoAccountingMethod
from rp2.plugin.accounting_method.moving_average import (
    AccountingMethod as MovingAverageAccountingMethod,
)
from rp2.plugin.country.us import US
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2TypeError, RP2ValueError
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet


class TestPr48BasisRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = Configuration("./config/test_data.ini", US())

    def test_acquisition_basis_override_boundary_validates_and_copies(self) -> None:
        lot = self._buy(1, "100", 1)
        invalid_values: List[object] = [[], {lot: "200"}, {"not-a-lot": RP2Decimal("200")}, {lot: RP2Decimal("-1")}]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises((RP2TypeError, RP2ValueError)):
                self._input([lot], [], [], cast(Dict[InTransaction, RP2Decimal], invalid))
        overrides = {lot: RP2Decimal("200")}
        data = self._input([lot], [], [], overrides)
        overrides[lot] = RP2Decimal("999")
        self.assertEqual(data.in_transaction_2_fiat_in_with_fee_override[lot], RP2Decimal("200"))

    def _buy(self, month: int, price: str, row: int) -> InTransaction:
        return InTransaction(
            self.configuration,
            f"2020-{month:02d}-{row:02d} 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal(price),
            crypto_in=RP2Decimal("1"),
            unique_id=f"buy-{row}",
            row=row,
        )

    def _sale(self, month: int, exchange: str, amount: str = "1") -> OutTransaction:
        return OutTransaction(
            self.configuration,
            f"2020-{month:02d}-15 08:00:00 +0000",
            "B1",
            exchange,
            "Bob",
            "Sell",
            spot_price=RP2Decimal("500"),
            crypto_out_no_fee=RP2Decimal(amount),
            crypto_fee=ZERO,
            unique_id=f"sale-{month}",
            row=20 + month,
        )

    def _transfer(self) -> IntraTransaction:
        return IntraTransaction(
            self.configuration,
            "2020-03-15 08:00:00 +0000",
            "B1",
            from_exchange="Coinbase",
            from_holder="Bob",
            to_exchange="Kraken",
            to_holder="Bob",
            spot_price=RP2Decimal("500"),
            crypto_sent=RP2Decimal("1"),
            crypto_received=RP2Decimal("1"),
            unique_id="transfer",
            row=40,
        )

    def _set(self, kind: str, entries: List[AbstractTransaction]) -> TransactionSet:
        result = TransactionSet(self.configuration, kind, "B1", MIN_DATE, MAX_DATE)
        for entry in entries:
            result.add_entry(entry)
        return result

    def _input(
        self,
        buys: List[InTransaction],
        sales: List[OutTransaction],
        transfers: List[IntraTransaction],
        overrides: Optional[Dict[InTransaction, RP2Decimal]] = None,
    ) -> InputData:
        return InputData(
            "B1",
            self._set("IN", list(buys)),
            self._set("OUT", list(sales)),
            self._set("INTRA", list(transfers)),
            from_date=self.configuration.from_date,
            to_date=self.configuration.to_date,
            in_transaction_2_fiat_in_with_fee_override=overrides,
        )

    @staticmethod
    def _engine(method: AbstractAccountingMethod) -> AccountingEngine:
        methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        methods.insert_node(MIN_DATE.year, method)
        return AccountingEngine(methods)

    @staticmethod
    def _open_basis(computed: ComputedData) -> RP2Decimal:
        total = ZERO
        for entry in computed.open_position_in_transaction_set:
            lot = cast(InTransaction, entry)
            basis = computed.get_open_position_in_transaction_fiat_in_with_fee(lot)
            actual = computed.get_in_transaction_actual_amount(lot)
            if actual is not None:
                total += basis * actual / lot.crypto_in
            else:
                total += basis * (RP2Decimal("1") - computed.get_open_position_in_lot_sold_percentage(lot))
        return total

    def test_future_transfer_and_sale_do_not_rewrite_historical_holdings(self) -> None:
        for cutoff in (date(2020, 2, 29), date(2020, 3, 31)):
            with self.subTest(cutoff=cutoff):
                self.configuration = Configuration("./config/test_data.ini", US(), to_date=cutoff)
                buys = [self._buy(1, "100", 1), self._buy(1, "300", 2)]
                data = self._input(buys, [self._sale(4, "Kraken")], [self._transfer()])
                method = MovingAverageAccountingMethod()
                result = compute_tax_per_wallet(self.configuration, self._engine(method), method, data)
                self.assertEqual(self._open_basis(result), RP2Decimal("400"))
                self.assertEqual(len(list(result.gain_loss_set)), 0)
                if cutoff.month == 2:
                    self.assertEqual(result.get_in_transaction_actual_amount(buys[0]), RP2Decimal("1"))
                    self.assertEqual(result.get_in_transaction_actual_amount(buys[1]), RP2Decimal("1"))

    def test_mixed_pooled_methods_fail_closed_before_transfer(self) -> None:
        for tax_method, transfer_method in (
            (FifoAccountingMethod(), MovingAverageAccountingMethod()),
            (MovingAverageAccountingMethod(), FifoAccountingMethod()),
        ):
            with self.subTest(tax=tax_method.name, transfer=transfer_method.name):
                data = self._input(
                    [self._buy(1, "100", 1), self._buy(1, "300", 2)],
                    [self._sale(2, "Coinbase")],
                    [self._transfer()],
                )
                with self.assertRaises(RP2ValueError):
                    compute_tax_per_wallet(self.configuration, self._engine(tax_method), transfer_method, data)

    def test_same_method_realized_and_transferred_basis_conserve_acquisitions(self) -> None:
        buys = [self._buy(1, "100", 1), self._buy(1, "300", 2)]
        data = self._input(buys, [self._sale(2, "Coinbase")], [self._transfer()])
        method = MovingAverageAccountingMethod()
        result = compute_tax_per_wallet(self.configuration, self._engine(method), method, data)
        realized = sum((cast(GainLoss, entry).fiat_cost_basis for entry in result.gain_loss_set), ZERO)
        self.assertEqual(realized, RP2Decimal("200"))
        self.assertEqual(self._open_basis(result), RP2Decimal("200"))
        self.assertEqual(result.get_in_transaction_fiat_in_with_fee(buys[0]), RP2Decimal("100"))
        self.assertEqual(result.get_in_transaction_fiat_in_with_fee(buys[1]), RP2Decimal("300"))

    def test_input_basis_override_remains_acquisition_basis(self) -> None:
        for per_wallet in (False, True):
            with self.subTest(per_wallet=per_wallet):
                buys = [self._buy(1, "100", 1), self._buy(1, "300", 2)]
                overrides: Dict[InTransaction, RP2Decimal] = {buys[0]: RP2Decimal("200")}
                expected_overrides = dict(overrides)
                data = self._input(buys, [self._sale(2, "Coinbase")], [], overrides)
                method = MovingAverageAccountingMethod()
                engine = self._engine(method)
                result = compute_tax_per_wallet(self.configuration, engine, method, data) if per_wallet else compute_tax(self.configuration, engine, data)
                self.assertEqual(self._open_basis(result), RP2Decimal("250"))
                self.assertEqual(result.get_in_transaction_fiat_in_with_fee(buys[0]), RP2Decimal("200"))
                self.assertEqual(result.get_in_transaction_fiat_in_with_fee(buys[1]), RP2Decimal("300"))
                self.assertEqual(overrides, expected_overrides)

    def test_acquisition_after_last_sale_updates_open_pool_only(self) -> None:
        buys = [self._buy(1, "100", 1), self._buy(1, "300", 2), self._buy(3, "600", 3)]
        method = MovingAverageAccountingMethod()
        result = compute_tax(self.configuration, self._engine(method), self._input(buys, [self._sale(2, "Coinbase")], []))
        self.assertEqual(self._open_basis(result), RP2Decimal("800"))
        for lot in buys[1:]:
            self.assertEqual(result.get_open_position_in_transaction_fiat_in_with_fee(lot), RP2Decimal("400"))
            self.assertEqual(result.get_in_transaction_fiat_in_with_fee(lot), lot.fiat_in_with_fee)


if __name__ == "__main__":
    unittest.main()

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

import unittest
from typing import Dict, List, Optional, Tuple, cast

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.intra_transaction import IntraTransaction
from rp2.out_transaction import OutTransaction
from rp2.per_wallet_tax_engine import compute_tax_per_wallet
from rp2.plugin.accounting_method.fifo import AccountingMethod as FIFO
from rp2.plugin.accounting_method.hifo import AccountingMethod as HIFO
from rp2.plugin.accounting_method.lifo import AccountingMethod as LIFO
from rp2.plugin.accounting_method.lofo import AccountingMethod as LOFO
from rp2.plugin.accounting_method.moving_average import (
    AccountingMethod as MovingAverage,
)
from rp2.plugin.accounting_method.moving_average_at import (
    AccountingMethod as MovingAverageAT,
)
from rp2.plugin.country.us import US
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2ValueError
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet

_ASSET = "B1"
_EXCHANGE = "Coinbase"
_HOLDER = "Bob"
_BUY_TIME = "2023-01-01 00:00:00 +0000"
_SELL_TIME = "2023-03-01 00:00:00 +0000"
_NEXT_SELL_TIME = "2024-03-01 00:00:00 +0000"


class TestAccountingBasisRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = Configuration("./config/test_data.ini", US())

    def _buy(self, row: int, basis: str, timestamp: str = _BUY_TIME) -> InTransaction:
        return InTransaction(
            self.configuration,
            timestamp,
            _ASSET,
            _EXCHANGE,
            _HOLDER,
            "Buy",
            spot_price=RP2Decimal(basis),
            crypto_in=RP2Decimal("1"),
            row=row,
        )

    def _sell(self, row: int, timestamp: str = _SELL_TIME, exchange: str = _EXCHANGE) -> OutTransaction:
        return OutTransaction(
            self.configuration,
            timestamp,
            _ASSET,
            exchange,
            _HOLDER,
            "Sell",
            spot_price=RP2Decimal("500"),
            crypto_out_no_fee=RP2Decimal("1"),
            crypto_fee=ZERO,
            row=row,
        )

    def _input(
        self,
        buys: List[InTransaction],
        sells: List[OutTransaction],
        overrides: Optional[Dict[InTransaction, RP2Decimal]] = None,
        transfers: Optional[List[IntraTransaction]] = None,
    ) -> InputData:
        in_set = TransactionSet(self.configuration, "IN", _ASSET)
        out_set = TransactionSet(self.configuration, "OUT", _ASSET)
        intra_set = TransactionSet(self.configuration, "INTRA", _ASSET)
        for buy in buys:
            in_set.add_entry(buy)
        for sell in sells:
            out_set.add_entry(sell)
        for transfer in transfers or []:
            intra_set.add_entry(transfer)
        return InputData(_ASSET, in_set, out_set, intra_set, in_transaction_2_fiat_in_with_fee_override=overrides)

    @staticmethod
    def _engine(schedule: List[Tuple[int, AbstractAccountingMethod]]) -> AccountingEngine:
        methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        for year, method in schedule:
            methods.insert_node(year, method)
        return AccountingEngine(methods)

    @staticmethod
    def _gains(computed: ComputedData) -> List[GainLoss]:
        return [cast(GainLoss, entry) for entry in computed.gain_loss_set]

    def test_lot_methods_use_effective_basis_in_gain_loss(self) -> None:
        for method in (FIFO(), LIFO(), HIFO(), LOFO()):
            for basis in ("200", "0"):
                with self.subTest(method=method.name, basis=basis):
                    buy = self._buy(1, "100")
                    computed = compute_tax(
                        self.configuration,
                        self._engine([(MIN_DATE.year, method)]),
                        self._input([buy], [self._sell(2)], {buy: RP2Decimal(basis)}),
                    )
                    gain = self._gains(computed)[0]
                    self.assertIs(gain.acquired_lot, buy)
                    self.assertEqual(gain.fiat_cost_basis, RP2Decimal(basis))
                    self.assertEqual(gain.fiat_gain, RP2Decimal("500") - RP2Decimal(basis))
                    self.assertEqual(computed.get_in_transaction_fiat_in_with_fee(buy), RP2Decimal(basis))

    def test_austrian_alt_lot_uses_effective_basis(self) -> None:
        for basis in ("200", "0"):
            with self.subTest(basis=basis):
                buy = self._buy(1, "100", "2020-01-01 00:00:00 +0000")
                computed = compute_tax(
                    self.configuration,
                    self._engine([(MIN_DATE.year, MovingAverageAT())]),
                    self._input([buy], [self._sell(2, "2020-02-01 00:00:00 +0000")], {buy: RP2Decimal(basis)}),
                )
                gain = self._gains(computed)[0]
                self.assertIs(gain.acquired_lot, buy)
                self.assertEqual(gain.fiat_cost_basis, RP2Decimal(basis))
                self.assertEqual(gain.fiat_gain, RP2Decimal("500") - RP2Decimal(basis))
                self.assertEqual(computed.get_in_transaction_fiat_in_with_fee(buy), RP2Decimal(basis))

    def test_cost_ordering_uses_effective_basis(self) -> None:
        for method, selected in ((HIFO(), 0), (LOFO(), 1)):
            with self.subTest(method=method.name):
                buys = [self._buy(1, "100"), self._buy(2, "300")]
                expected_basis = RP2Decimal("400") if selected == 0 else RP2Decimal("300")
                computed = compute_tax(
                    self.configuration,
                    self._engine([(MIN_DATE.year, method)]),
                    self._input(buys, [self._sell(3)], {buys[0]: RP2Decimal("400")}),
                )
                gain = self._gains(computed)[0]
                self.assertIs(gain.acquired_lot, buys[selected])
                self.assertEqual(gain.fiat_cost_basis, expected_basis)

    def test_transfer_carries_effective_basis_and_cost_order(self) -> None:
        for method, expected_basis in ((FIFO(), "400"), (HIFO(), "400"), (LOFO(), "300")):
            with self.subTest(method=method.name):
                buys = [self._buy(1, "100"), self._buy(2, "300")]
                transfer = IntraTransaction(
                    self.configuration,
                    "2023-02-01 00:00:00 +0000",
                    _ASSET,
                    _EXCHANGE,
                    _HOLDER,
                    "Kraken",
                    _HOLDER,
                    spot_price=RP2Decimal("500"),
                    crypto_sent=RP2Decimal("1"),
                    crypto_received=RP2Decimal("1"),
                    row=3,
                )
                data = self._input(buys, [self._sell(4, exchange="Kraken")], {buys[0]: RP2Decimal("400")}, [transfer])
                computed = compute_tax_per_wallet(self.configuration, self._engine([(MIN_DATE.year, method)]), method, data)
                gain = self._gains(computed)[0]
                self.assertEqual(gain.fiat_cost_basis, RP2Decimal(expected_basis))
                self.assertIsNotNone(gain.acquired_lot)
                assert gain.acquired_lot is not None
                self.assertEqual(computed.get_in_transaction_fiat_in_with_fee(gain.acquired_lot), RP2Decimal(expected_basis))

    def test_redundant_pool_schedule_boundary_preserves_average(self) -> None:
        for method_type in (MovingAverage, MovingAverageAT):
            with self.subTest(method=method_type.__name__):
                buys = [self._buy(1, "100"), self._buy(2, "300")]
                data = self._input(buys, [self._sell(3), self._sell(4, _NEXT_SELL_TIME)])
                computed = compute_tax(
                    self.configuration,
                    self._engine([(MIN_DATE.year, method_type()), (2024, method_type())]),
                    data,
                )
                bases = [gain.fiat_cost_basis for gain in self._gains(computed)]
                expected_bases = [RP2Decimal("200"), RP2Decimal("200")]
                self.assertEqual(bases, expected_bases)

    def test_redundant_pool_schedule_preserves_open_position_basis(self) -> None:
        for method_type in (MovingAverage, MovingAverageAT):
            with self.subTest(method=method_type.__name__):
                buys = [self._buy(1, "100"), self._buy(2, "300")]
                computed = compute_tax(
                    self.configuration,
                    self._engine([(MIN_DATE.year, method_type()), (2024, method_type())]),
                    self._input(buys, [self._sell(3)]),
                )
                self.assertEqual(computed.get_open_position_in_transaction_fiat_in_with_fee(buys[1]), RP2Decimal("200"))
                self.assertEqual(computed.get_in_transaction_fiat_in_with_fee(buys[1]), RP2Decimal("300"))

    def test_departure_from_pool_fails_closed(self) -> None:
        for following_method in (FIFO(), MovingAverageAT()):
            with self.subTest(following_method=following_method.name):
                data = self._input([self._buy(1, "100"), self._buy(2, "300")], [self._sell(3), self._sell(4, _NEXT_SELL_TIME)])
                with self.assertRaisesRegex(RP2ValueError, "Changing from a pool-based accounting method"):
                    compute_tax(
                        self.configuration,
                        self._engine([(MIN_DATE.year, MovingAverage()), (2024, following_method)]),
                        data,
                    )

    def test_same_timestamp_rows_preserve_input_order_and_all_inventory(self) -> None:
        buys = [self._buy(2, "100"), self._buy(1, "300")]
        computed = compute_tax(
            self.configuration,
            self._engine([(MIN_DATE.year, FIFO())]),
            self._input(buys, [self._sell(3), self._sell(4, _NEXT_SELL_TIME)]),
        )
        gains = self._gains(computed)
        selected_lots = [gain.acquired_lot for gain in gains]
        bases = [gain.fiat_cost_basis for gain in gains]
        self.assertEqual(selected_lots, buys)
        expected_bases = [RP2Decimal("100"), RP2Decimal("300")]
        self.assertEqual(bases, expected_bases)

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
from typing import List, Tuple

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.plugin.accounting_method.moving_average import AccountingMethod
from rp2.plugin.country.us import US
from rp2.rp2_decimal import RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet

_ASSET: str = "B1"


def _rp2_decimal(value: str) -> RP2Decimal:
    return RP2Decimal(value)


class TestMovingAverage(unittest.TestCase):
    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        # Country choice is irrelevant for cost-basis math; we pick US because its config file
        # already exists in repo fixtures. The accounting engine is wired to moving_average below.
        cls._configuration = Configuration("./config/test_data.ini", US())

    def setUp(self) -> None:
        self.maxDiff = None  # pylint: disable=invalid-name

    # ------------------------------------------------------------------ helpers ----

    def _make_engine(self) -> AccountingEngine:
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree[int, AbstractAccountingMethod]()
        years_2_methods.insert_node(MIN_DATE.year, AccountingMethod())
        return AccountingEngine(years_2_methods)

    def _buy(self, row: int, timestamp: str, crypto_in: str, spot_price: str) -> InTransaction:
        return InTransaction(
            self._configuration,
            timestamp,
            _ASSET,
            "Coinbase",
            "Bob",
            "BUY",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_in),
            fiat_fee=_rp2_decimal("0"),
            row=row,
        )

    def _sell(self, row: int, timestamp: str, crypto_out: str, spot_price: str) -> OutTransaction:
        return OutTransaction(
            self._configuration,
            timestamp,
            _ASSET,
            "Coinbase",
            "Bob",
            "SELL",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_out),
            _rp2_decimal("0"),
            row=row,
        )

    def _compute(self, in_txs: List[InTransaction], out_txs: List[OutTransaction]) -> ComputedData:
        in_set: TransactionSet = TransactionSet(self._configuration, "IN", _ASSET)
        for in_tx in in_txs:
            in_set.add_entry(in_tx)
        out_set: TransactionSet = TransactionSet(self._configuration, "OUT", _ASSET)
        for out_tx in out_txs:
            out_set.add_entry(out_tx)
        intra_set: TransactionSet = TransactionSet(self._configuration, "INTRA", _ASSET)
        input_data: InputData = InputData(_ASSET, in_set, out_set, intra_set)
        return compute_tax(self._configuration, self._make_engine(), input_data)

    def _gain_loss_list(self, computed_data: ComputedData) -> List[GainLoss]:
        return [entry for entry in computed_data.gain_loss_set if isinstance(entry, GainLoss)]

    def _assert_decimal_equal(self, actual: RP2Decimal, expected: str) -> None:
        self.assertEqual(actual, _rp2_decimal(expected), f"expected {expected}, got {actual}")

    # ------------------------------------------------------------------ tests ------

    def test_basic_moving_average_two_acquisitions_one_disposal(self) -> None:
        # Buy 1 BTC @ 100, Buy 1 BTC @ 300 → pool (2, 400, avg 200).
        # Sell 0.5 BTC @ 400 → cost_basis = 0.5 * 200 = 100, proceeds 200, gain 100.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="400"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        self._assert_decimal_equal(gains[0].crypto_amount, "0.5")
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")
        self._assert_decimal_equal(gains[0].fiat_gain, "100")

    def test_multiple_disposals_preserve_running_average(self) -> None:
        # After both buys: pool (2, 400, avg 200). Disposals at 500 each; avg must stay 200
        # for both because the spec says disposals don't move the running average.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500"),
            self._sell(row=4, timestamp="2023-04-01 00:00:00 +0000", crypto_out="0.3", spot_price="500"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        # Both use avg 200.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")  # 0.5 * 200
        self._assert_decimal_equal(gains[1].fiat_cost_basis, "60")  # 0.3 * 200

    def test_acquisition_between_disposals_moves_average(self) -> None:
        # Buy 1 @ 100 → (1, 100, avg 100).
        # Sell 0.5 @ 500 → cost_basis = 50, pool (0.5, 50, avg 100).
        # Buy 1 @ 300 → pool (1.5, 350, avg 233.333...).
        # Sell 0.5 @ 500 → cost_basis = 0.5 * 233.333... = 116.666...
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_out="0.5", spot_price="500"),
            self._sell(row=4, timestamp="2023-04-01 00:00:00 +0000", crypto_out="0.5", spot_price="500"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "50")
        # Second disposal: cost_basis = 0.5 * (350/1.5) = 175/1.5 ≈ 116.66666...
        expected_second = _rp2_decimal("350") / _rp2_decimal("3")
        self.assertEqual(gains[1].fiat_cost_basis, expected_second)

    def test_disposal_exceeding_single_lot_splits_into_sub_gainlosses(self) -> None:
        # Pool (2, 400, avg 200). Sell 1.5 in one event → engine splits into two sub-GainLoss,
        # both using avg 200. Sum of sub-costs should equal 1.5 * 200 = 300.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="1.5", spot_price="500"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        amounts_and_costs: List[Tuple[RP2Decimal, RP2Decimal]] = [(g.crypto_amount, g.fiat_cost_basis) for g in gains]
        # First sub consumes the older lot fully (1.0), second consumes 0.5 of the newer lot.
        self._assert_decimal_equal(amounts_and_costs[0][0], "1")
        self._assert_decimal_equal(amounts_and_costs[0][1], "200")
        self._assert_decimal_equal(amounts_and_costs[1][0], "0.5")
        self._assert_decimal_equal(amounts_and_costs[1][1], "100")
        total_cost = amounts_and_costs[0][1] + amounts_and_costs[1][1]
        self._assert_decimal_equal(total_cost, "300")

    def test_lot_pairing_follows_fifo_order_for_audit_trail(self) -> None:
        # The cost basis uses the pool average, but the GainLoss.acquired_lot must still point
        # to real lots in FIFO order so the audit trail makes sense.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="1.5", spot_price="500"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        assert gains[0].acquired_lot is not None and gains[1].acquired_lot is not None
        self.assertEqual(gains[0].acquired_lot.row, 1)
        self.assertEqual(gains[1].acquired_lot.row, 2)

    def test_fifo_output_unchanged_without_override(self) -> None:
        # Regression guard: when no override is supplied (the default FIFO/LIFO/HIFO/LOFO path),
        # GainLoss.fiat_cost_basis must match the original lot-based formula. This test builds
        # a GainLoss directly without override and checks the value.
        lot = self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100")
        disposal = self._sell(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_out="0.5", spot_price="500")
        gain_loss_no_override = GainLoss(self._configuration, _rp2_decimal("0.5"), disposal, lot)
        self._assert_decimal_equal(gain_loss_no_override.fiat_cost_basis, "50")  # 0.5 * 100

        gain_loss_with_override = GainLoss(self._configuration, _rp2_decimal("0.5"), disposal, lot, unit_cost_basis_override=_rp2_decimal("200"))
        self._assert_decimal_equal(gain_loss_with_override.fiat_cost_basis, "100")  # 0.5 * 200


if __name__ == "__main__":
    unittest.main()

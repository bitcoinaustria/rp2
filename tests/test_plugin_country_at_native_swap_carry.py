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
from typing import Dict, List, Optional, Sequence

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
from rp2.plugin.accounting_method.moving_average_at import AccountingMethod
from rp2.plugin.country.at import (
    AT,
    AtDisposalCategory,
    classify_disposal,
    collect_at_swap_link_pairs,
)
from rp2.plugin.country.at_native_tax_engine import compute_native_at_tax
from rp2.rp2_decimal import RP2Decimal
from rp2.rp2_error import RP2ValueError
from rp2.transaction_set import TransactionSet


def _rp2_decimal(value: str) -> RP2Decimal:
    return RP2Decimal(value)


class TestNativeATSwapCarry(unittest.TestCase):
    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", AT())

    def setUp(self) -> None:
        self.maxDiff = None  # pylint: disable=invalid-name

    def _make_engine(self) -> AccountingEngine:
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree[int, AbstractAccountingMethod]()
        years_2_methods.insert_node(MIN_DATE.year, AccountingMethod())
        return AccountingEngine(years_2_methods)

    def _buy(
        self,
        asset: str,
        row: int,
        timestamp: str,
        crypto_in: str,
        spot_price: str,
        notes: Optional[str] = None,
    ) -> InTransaction:
        return InTransaction(
            self._configuration,
            timestamp,
            asset,
            "Coinbase",
            "Bob",
            "BUY",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_in),
            fiat_fee=_rp2_decimal("0"),
            row=row,
            notes=notes,
        )

    def _sell(
        self,
        asset: str,
        row: int,
        timestamp: str,
        crypto_out: str,
        spot_price: str,
        notes: Optional[str] = None,
        crypto_fee: str = "0",
    ) -> OutTransaction:
        return OutTransaction(
            self._configuration,
            timestamp,
            asset,
            "Coinbase",
            "Bob",
            "SELL",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_out),
            _rp2_decimal(crypto_fee),
            row=row,
            notes=notes,
        )

    def _move(
        self,
        asset: str,
        row: int,
        timestamp: str,
        crypto_sent: str,
        crypto_received: str,
        spot_price: str,
    ) -> IntraTransaction:
        return IntraTransaction(
            self._configuration,
            timestamp,
            asset,
            "Coinbase",
            "Bob",
            "Kraken",
            "Bob",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_sent),
            _rp2_decimal(crypto_received),
            row=row,
        )

    def _input_data(
        self,
        asset: str,
        in_txs: Sequence[InTransaction],
        out_txs: Sequence[OutTransaction],
        intra_txs: Sequence[IntraTransaction] = (),
    ) -> InputData:
        in_set: TransactionSet = TransactionSet(self._configuration, "IN", asset)
        for in_tx in in_txs:
            in_set.add_entry(in_tx)
        out_set: TransactionSet = TransactionSet(self._configuration, "OUT", asset)
        for out_tx in out_txs:
            out_set.add_entry(out_tx)
        intra_set: TransactionSet = TransactionSet(self._configuration, "INTRA", asset)
        for intra_tx in intra_txs:
            intra_set.add_entry(intra_tx)
        return InputData(asset, in_set, out_set, intra_set)

    def _compute(self, asset_to_input_data: Dict[str, InputData]) -> Dict[str, ComputedData]:
        return compute_native_at_tax(
            self._configuration, self._make_engine(), asset_to_input_data, collect_at_swap_link_pairs(list(asset_to_input_data.values()))
        )

    def _gain_loss_list(self, computed_data: ComputedData) -> List[GainLoss]:
        return [entry for entry in computed_data.gain_loss_set if isinstance(entry, GainLoss)]

    def _assert_decimal_equal(self, actual: RP2Decimal, expected: str) -> None:
        self.assertEqual(actual, _rp2_decimal(expected), f"expected {expected}, got {actual}")

    def test_b1_to_b2_neu_swap_carries_source_pool_basis_to_destination_pool(self) -> None:
        b1_in = [
            self._buy("B1", 1, "2023-01-01 00:00:00 +0000", "1", "100"),
            self._buy("B1", 2, "2023-02-01 00:00:00 +0000", "1", "300"),
        ]
        b1_out = [self._sell("B1", 3, "2023-03-01 00:00:00 +0000", "0.5", "1000", notes="at_swap_link=swap-1")]
        b2_incoming = self._buy("B2", 1, "2023-03-01 00:00:00 +0000", "2", "1000", notes="at_swap_link=swap-1")
        b2_out = [self._sell("B2", 2, "2023-04-01 00:00:00 +0000", "1", "100")]

        computed = self._compute(
            {
                "B1": self._input_data("B1", b1_in, b1_out),
                "B2": self._input_data("B2", [b2_incoming], b2_out),
            }
        )

        b1_gains = self._gain_loss_list(computed["B1"])
        self.assertEqual(classify_disposal(b1_gains[0]), AtDisposalCategory.NEU_SWAP)
        self._assert_decimal_equal(b1_gains[0].fiat_gain, "0")
        self._assert_decimal_equal(computed["B2"].get_in_transaction_fiat_in_with_fee(b2_incoming), "100")
        self._assert_decimal_equal(self._gain_loss_list(computed["B2"])[0].fiat_cost_basis, "50")

    def test_b2_to_b1_neu_swap_is_symmetric(self) -> None:
        b2_in = [
            self._buy("B2", 1, "2023-01-01 00:00:00 +0000", "2", "50"),
            self._buy("B2", 2, "2023-02-01 00:00:00 +0000", "2", "150"),
        ]
        b2_out = [self._sell("B2", 3, "2023-03-01 00:00:00 +0000", "1", "1000", notes="at_swap_link=swap-2")]
        b1_incoming = self._buy("B1", 1, "2023-03-01 00:00:00 +0000", "0.25", "8000", notes="at_swap_link=swap-2")
        b1_out = [self._sell("B1", 2, "2023-04-01 00:00:00 +0000", "0.125", "1000")]

        computed = self._compute(
            {
                "B1": self._input_data("B1", [b1_incoming], b1_out),
                "B2": self._input_data("B2", b2_in, b2_out),
            }
        )

        self._assert_decimal_equal(computed["B1"].get_in_transaction_fiat_in_with_fee(b1_incoming), "100")
        self._assert_decimal_equal(self._gain_loss_list(computed["B1"])[0].fiat_cost_basis, "50")
        self._assert_decimal_equal(self._gain_loss_list(computed["B2"])[0].fiat_gain, "0")

    def test_same_timestamp_swap_chain_uses_carried_incoming_as_next_source_basis(self) -> None:
        swap_timestamp = "2023-03-01 00:00:00 +0000"
        b1_in = [
            self._buy("B1", 1, "2023-01-01 00:00:00 +0000", "1", "100"),
            self._buy("B1", 2, "2023-02-01 00:00:00 +0000", "1", "300"),
        ]
        b1_out = [self._sell("B1", 3, swap_timestamp, "0.5", "1000", notes="at_swap_link=chain-1")]
        b2_incoming = self._buy("B2", 1, swap_timestamp, "2", "1000", notes="at_swap_link=chain-1")
        b2_out = [self._sell("B2", 2, swap_timestamp, "2", "500", notes="at_swap_link=chain-2")]
        b3_incoming = self._buy("B3", 1, swap_timestamp, "10", "100", notes="at_swap_link=chain-2")
        b3_out = [self._sell("B3", 2, "2023-04-01 00:00:00 +0000", "5", "20")]

        computed = self._compute(
            {
                "B1": self._input_data("B1", b1_in, b1_out),
                "B2": self._input_data("B2", [b2_incoming], b2_out),
                "B3": self._input_data("B3", [b3_incoming], b3_out),
            }
        )

        self._assert_decimal_equal(computed["B2"].get_in_transaction_fiat_in_with_fee(b2_incoming), "100")
        self._assert_decimal_equal(computed["B3"].get_in_transaction_fiat_in_with_fee(b3_incoming), "100")
        self._assert_decimal_equal(self._gain_loss_list(computed["B2"])[0].fiat_gain, "0")
        self._assert_decimal_equal(self._gain_loss_list(computed["B3"])[0].fiat_cost_basis, "50")

    def test_same_asset_transfer_fee_before_cross_asset_swap_preserves_source_pool_average(self) -> None:
        b1_in = [
            self._buy("B1", 1, "2023-01-01 00:00:00 +0000", "1", "100"),
            self._buy("B1", 2, "2023-02-01 00:00:00 +0000", "1", "300"),
        ]
        b1_move = [self._move("B1", 3, "2023-02-15 00:00:00 +0000", "0.20", "0.19", "1000")]
        b1_out = [self._sell("B1", 4, "2023-03-01 00:00:00 +0000", "0.5", "1000", notes="at_swap_link=swap-after-move")]
        b2_incoming = self._buy("B2", 1, "2023-03-01 00:00:00 +0000", "2", "1000", notes="at_swap_link=swap-after-move")
        b2_out = [self._sell("B2", 2, "2023-04-01 00:00:00 +0000", "1", "100")]

        computed = self._compute(
            {
                "B1": self._input_data("B1", b1_in, b1_out, b1_move),
                "B2": self._input_data("B2", [b2_incoming], b2_out),
            }
        )

        self._assert_decimal_equal(computed["B2"].get_in_transaction_fiat_in_with_fee(b2_incoming), "100")
        self._assert_decimal_equal(self._gain_loss_list(computed["B2"])[0].fiat_cost_basis, "50")

    def test_alt_outgoing_swap_marker_realizes_normally_and_does_not_force_carry(self) -> None:
        b1_in = [self._buy("B1", 1, "2020-06-01 00:00:00 +0000", "1", "100")]
        b1_out = [
            self._sell(
                "B1",
                2,
                "2023-06-01 00:00:00 +0000",
                "0.5",
                "500",
                notes="at_regime=alt at_swap_link=alt-bookkeeping",
            )
        ]
        b2_incoming = self._buy("B2", 1, "2023-06-01 00:00:00 +0000", "1", "250", notes="at_regime=alt at_swap_link=alt-bookkeeping")
        b2_out = [self._sell("B2", 2, "2023-07-01 00:00:00 +0000", "0.5", "300", notes="at_regime=alt")]

        computed = self._compute(
            {
                "B1": self._input_data("B1", b1_in, b1_out),
                "B2": self._input_data("B2", [b2_incoming], b2_out),
            }
        )

        self._assert_decimal_equal(self._gain_loss_list(computed["B1"])[0].fiat_gain, "200")
        self._assert_decimal_equal(computed["B2"].get_in_transaction_fiat_in_with_fee(b2_incoming), "250")

    def test_same_timestamp_reciprocal_swaps_in_independent_pools_do_not_deadlock(self) -> None:
        swap_timestamp = "2023-03-01 00:00:00 +0000"
        b1_base = self._buy("B1", 1, "2023-01-01 00:00:00 +0000", "1", "100", notes="at_pool=b1-source")
        b1_incoming = self._buy("B1", 2, swap_timestamp, "0.25", "1000", notes="at_pool=b1-destination at_swap_link=b2-to-b1")
        b1_out = [self._sell("B1", 3, swap_timestamp, "0.5", "1000", notes="at_pool=b1-source at_swap_link=b1-to-b2")]
        b2_base = self._buy("B2", 1, "2023-01-01 00:00:00 +0000", "1", "200", notes="at_pool=b2-source")
        b2_incoming = self._buy("B2", 2, swap_timestamp, "0.25", "1000", notes="at_pool=b2-destination at_swap_link=b1-to-b2")
        b2_out = [self._sell("B2", 3, swap_timestamp, "0.5", "1000", notes="at_pool=b2-source at_swap_link=b2-to-b1")]

        computed = self._compute(
            {
                "B1": self._input_data("B1", [b1_base, b1_incoming], b1_out),
                "B2": self._input_data("B2", [b2_base, b2_incoming], b2_out),
            }
        )

        self._assert_decimal_equal(self._gain_loss_list(computed["B1"])[0].fiat_gain, "0")
        self._assert_decimal_equal(self._gain_loss_list(computed["B2"])[0].fiat_gain, "0")
        self._assert_decimal_equal(computed["B1"].get_in_transaction_fiat_in_with_fee(b1_incoming), "100")
        self._assert_decimal_equal(computed["B2"].get_in_transaction_fiat_in_with_fee(b2_incoming), "50")

    def test_same_timestamp_reciprocal_swap_cycle_raises(self) -> None:
        swap_timestamp = "2023-03-01 00:00:00 +0000"
        b1_in = [
            self._buy("B1", 1, "2023-01-01 00:00:00 +0000", "1", "100"),
            self._buy("B1", 2, swap_timestamp, "0.25", "1000", notes="at_swap_link=b2-to-b1"),
        ]
        b1_out = [self._sell("B1", 3, swap_timestamp, "0.5", "1000", notes="at_swap_link=b1-to-b2")]
        b2_in = [
            self._buy("B2", 1, "2023-01-01 00:00:00 +0000", "1", "100"),
            self._buy("B2", 2, swap_timestamp, "0.25", "1000", notes="at_swap_link=b1-to-b2"),
        ]
        b2_out = [self._sell("B2", 3, swap_timestamp, "0.5", "1000", notes="at_swap_link=b2-to-b1")]

        with self.assertRaisesRegex(RP2ValueError, "Unable to order Austrian swap-linked taxable events"):
            self._compute(
                {
                    "B1": self._input_data("B1", b1_in, b1_out),
                    "B2": self._input_data("B2", b2_in, b2_out),
                }
            )


if __name__ == "__main__":
    unittest.main()

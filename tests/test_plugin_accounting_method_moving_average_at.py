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
from typing import List, Optional

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.plugin.accounting_method.moving_average_at import AccountingMethod
from rp2.plugin.country.at import AT
from rp2.rp2_decimal import RP2Decimal
from rp2.rp2_error import RP2ValueError
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet

_ASSET: str = "B1"


def _rp2_decimal(value: str) -> RP2Decimal:
    return RP2Decimal(value)


class TestMovingAverageAT(unittest.TestCase):
    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        # AT country wiring is irrelevant for the method's math, but it keeps the config
        # semantically aligned with the Austrian plugin surface.
        cls._configuration = Configuration("./config/test_data.ini", AT())

    def setUp(self) -> None:
        self.maxDiff = None  # pylint: disable=invalid-name

    # ------------------------------------------------------------------ helpers ----

    def _make_engine(self) -> AccountingEngine:
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree[int, AbstractAccountingMethod]()
        years_2_methods.insert_node(MIN_DATE.year, AccountingMethod())
        return AccountingEngine(years_2_methods)

    def _buy(self, row: int, timestamp: str, crypto_in: str, spot_price: str, notes: Optional[str] = None) -> InTransaction:
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
            notes=notes,
        )

    def _sell(
        self,
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
            _ASSET,
            "Coinbase",
            "Bob",
            "SELL",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_out),
            _rp2_decimal(crypto_fee),
            row=row,
            notes=notes,
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

    def test_altvermoegen_disposal_uses_own_lot_cost_basis(self) -> None:
        # Alt lot (pre-cutoff, 2020-06-01), disposal at 2023-06-01. Cost basis = lot's own
        # (no pool-average override), so holding-period computation in the report is exact.
        in_txs = [
            self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
        ]
        out_txs = [
            self._sell(row=2, timestamp="2023-06-01 00:00:00 +0000", crypto_out="0.5", spot_price="400", notes="at_regime=alt"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        self._assert_decimal_equal(gains[0].crypto_amount, "0.5")
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "50")  # 0.5 * 100
        self._assert_decimal_equal(gains[0].fiat_gain, "150")
        assert gains[0].acquired_lot is not None
        self.assertEqual(gains[0].acquired_lot.row, 1)

    def test_neuvermoegen_disposal_uses_pool_average(self) -> None:
        # Two Neu buys (post-cutoff), one Neu disposal (default regime). Pool avg = 200.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="400"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")  # 0.5 * 200
        self._assert_decimal_equal(gains[0].fiat_gain, "100")

    def test_mixed_pools_routed_by_explicit_event_regime_marker(self) -> None:
        # Alt lot and Neu lot coexist. Two disposals, each explicitly tagged.
        in_txs = [
            self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100"),  # alt by date
            self._buy(row=2, timestamp="2023-06-01 00:00:00 +0000", crypto_in="1", spot_price="500"),  # neu by date
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-07-01 00:00:00 +0000", crypto_out="0.5", spot_price="700", notes="at_regime=alt"),
            self._sell(row=4, timestamp="2023-08-01 00:00:00 +0000", crypto_out="0.5", spot_price="700", notes="at_regime=neu"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        # First (alt): cost basis 0.5 * 100 = 50.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "50")
        assert gains[0].acquired_lot is not None
        self.assertEqual(gains[0].acquired_lot.row, 1)
        # Second (neu): pool has only lot 2 @ 500, avg 500, cost basis 0.5 * 500 = 250.
        self._assert_decimal_equal(gains[1].fiat_cost_basis, "250")
        assert gains[1].acquired_lot is not None
        self.assertEqual(gains[1].acquired_lot.row, 2)

    def test_explicit_lot_regime_marker_overrides_date_inference(self) -> None:
        # A pre-cutoff lot tagged `at_regime=neu` goes into the Neu pool despite its date —
        # this covers the case where a user declares a pre-2021 lot as part of Neuvermögen
        # (e.g. after a Swap broke Altvermögen status; Phase 4 wires the swap flow proper).
        in_txs = [
            self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100", notes="at_regime=neu"),
        ]
        out_txs = [
            self._sell(row=2, timestamp="2023-06-01 00:00:00 +0000", crypto_out="0.5", spot_price="400"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        # Despite the lot's pre-cutoff date, it's classified Neu. Pool has only this lot, avg 100.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "50")  # 0.5 * 100

    def test_neu_pool_ignores_alt_lots(self) -> None:
        # A 1000-EUR alt lot should NOT contaminate the neu pool average. Explicit
        # `at_regime=neu` on the disposal overrides the default alt-first preference.
        in_txs = [
            self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="1000"),  # alt
            self._buy(row=2, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),  # neu
            self._buy(row=3, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),  # neu
        ]
        out_txs = [
            self._sell(row=4, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_regime=neu"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        # Neu pool: (1+1, 100+300, avg 200). Cost basis 0.5 * 200 = 100.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")

    def test_unmarked_disposal_with_mixed_pools_raises(self) -> None:
        # Both Alt and Neu lots available, disposal unmarked → ambiguity. The AT method
        # refuses to silently pick one regime over the other; Kassiber must disambiguate.
        in_txs = [
            self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100"),  # alt
            self._buy(row=2, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="300"),  # neu
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-06-01 00:00:00 +0000", crypto_out="0.5", spot_price="500"),
        ]
        with self.assertRaisesRegex(RP2ValueError, "Ambiguous Austrian disposal"):
            self._compute(in_txs, out_txs)

    def test_default_unmarked_disposal_falls_back_to_neu_when_no_alt(self) -> None:
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500")]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")  # 0.5 * pool_avg_200

    def test_alt_fifo_across_multiple_alt_lots(self) -> None:
        # Three alt lots; alt disposal consumes first FIFO-wise.
        in_txs = [
            self._buy(row=1, timestamp="2019-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2019-06-01 00:00:00 +0000", crypto_in="1", spot_price="200"),
            self._buy(row=3, timestamp="2020-01-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=4, timestamp="2023-06-01 00:00:00 +0000", crypto_out="1.5", spot_price="500", notes="at_regime=alt"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        # First GainLoss consumes lot 1 fully (cost_basis 100), second consumes 0.5 of lot 2 (cost_basis 100).
        assert gains[0].acquired_lot is not None and gains[1].acquired_lot is not None
        self.assertEqual(gains[0].acquired_lot.row, 1)
        self._assert_decimal_equal(gains[0].crypto_amount, "1")
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")
        self.assertEqual(gains[1].acquired_lot.row, 2)
        self._assert_decimal_equal(gains[1].crypto_amount, "0.5")
        self._assert_decimal_equal(gains[1].fiat_cost_basis, "100")  # 0.5 * 200

    def test_neu_swap_produces_zero_gain(self) -> None:
        # Neu swap (outgoing leg). at_swap_link marker in notes → cost basis = proceeds → gain 0.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_swap_link=swap-42"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        # Proceeds = 0.5 * 500 = 250; cost basis matches → gain 0.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "250")
        self._assert_decimal_equal(gains[0].fiat_gain, "0")

    def test_neu_swap_with_fee_produces_zero_gain(self) -> None:
        # Fee-bearing Neu swap still needs a zero-gain outgoing row. fiat_taxable_amount is
        # based on crypto_out, while crypto_balance_change includes the fee.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(
                row=3,
                timestamp="2023-03-01 00:00:00 +0000",
                crypto_out="0.5",
                crypto_fee="0.1",
                spot_price="500",
                notes="at_swap_link=swap-fee",
            ),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        self._assert_decimal_equal(gains[0].crypto_amount, "0.6")
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "250")
        self._assert_decimal_equal(gains[0].fiat_gain, "0")

    def test_neu_swap_preserves_pool_average_for_later_disposals(self) -> None:
        # Pool (2, 400, avg 200). Swap-out of 1 BTC at spot 999 (the swap price is economically
        # irrelevant — pool must stay at avg 200). Later non-swap disposal sees the preserved avg.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="1", spot_price="999", notes="at_swap_link=x"),
            self._sell(row=4, timestamp="2023-04-01 00:00:00 +0000", crypto_out="0.5", spot_price="500"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 2)
        # First is the swap with zero gain.
        self._assert_decimal_equal(gains[0].fiat_gain, "0")
        # Second is a regular disposal: pool avg should still be 200.
        self._assert_decimal_equal(gains[1].fiat_cost_basis, "100")  # 0.5 * 200

    def test_alt_swap_marker_ignored_realizes_normally(self) -> None:
        # Austrian law: swapping Altvermögen breaks Alt status and IS taxable. The marker is
        # only honored for Neu disposals; on Alt it's a no-op and we realize a normal gain.
        in_txs = [
            self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
        ]
        out_txs = [
            self._sell(
                row=2,
                timestamp="2023-06-01 00:00:00 +0000",
                crypto_out="0.5",
                spot_price="500",
                notes="at_regime=alt at_swap_link=ignored",
            ),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        # Alt disposal with lot's own cost basis: 0.5 * 100 = 50. Proceeds 250 → gain 200.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "50")
        self._assert_decimal_equal(gains[0].fiat_gain, "200")

    def test_neu_pool_partitioning_by_at_pool(self) -> None:
        # Two Neu buys in different pools (A @ 100, B @ 1000). Disposal tagged `at_pool=A`
        # must use pool A's average (100), not a cross-pool blended average.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100", notes="at_pool=A"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="1000", notes="at_pool=B"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_pool=A at_regime=neu"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        # Pool A contains only lot 1 @ 100 → cost basis 0.5 * 100 = 50.
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "50")
        assert gains[0].acquired_lot is not None
        self.assertEqual(gains[0].acquired_lot.row, 1)

    def test_default_pool_is_independent_from_named_pool(self) -> None:
        # One lot in default pool, one in named pool; disposal in default pool sees only
        # the default-pool lot's average.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="200"),
            self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="800", notes="at_pool=cold"),
        ]
        out_txs = [
            self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_regime=neu"),
        ]
        gains = self._gain_loss_list(self._compute(in_txs, out_txs))
        self.assertEqual(len(gains), 1)
        self._assert_decimal_equal(gains[0].fiat_cost_basis, "100")  # 0.5 * 200

    def test_empty_swap_link_id_raises(self) -> None:
        # `at_swap_link=` with no id is a Kassiber bug: silently forcing zero gain without
        # a pairable id hides data loss. The method refuses to guess.
        in_txs = [
            self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
        ]
        out_txs = [
            self._sell(row=2, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_swap_link="),
        ]
        with self.assertRaisesRegex(RP2ValueError, "Empty `at_swap_link=` marker"):
            self._compute(in_txs, out_txs)

    def test_neu_acquisition_after_neu_disposal_moves_average(self) -> None:
        # Phase-2 interleaving scenario, but on the AT method and through the Neu pool only.
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
        # Second disposal: pool = (0.5, 50, 100) + new lot (1, 300) = (1.5, 350, avg 350/1.5).
        expected_second: RP2Decimal = _rp2_decimal("350") / _rp2_decimal("3")
        self.assertEqual(gains[1].fiat_cost_basis, expected_second)


if __name__ == "__main__":
    unittest.main()

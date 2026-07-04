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

from rp2.abstract_accounting_method import fee_inclusive_unit_cost_basis
from rp2.configuration import Configuration
from rp2.in_transaction import InTransaction
from rp2.plugin.accounting_method.hifo import AccountingMethod as HifoAccountingMethod
from rp2.plugin.accounting_method.lofo import AccountingMethod as LofoAccountingMethod
from rp2.plugin.country.us import US
from rp2.rp2_decimal import RP2Decimal


# HIFO/LOFO must rank lots by fee-inclusive per-unit cost basis, not by fee-exclusive spot_price.
# See https://github.com/bitcoinaustria/rp2/issues/11 (mirrors upstream eprbell/rp2#150).
class TestAccountingMethodFeeBasis(unittest.TestCase):
    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", US())

    def _buy(self, row: int, spot_price: str, crypto_in: str, fiat_fee: str) -> InTransaction:
        return InTransaction(
            self._configuration,
            "2023-01-01 00:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "BUY",
            RP2Decimal(spot_price),
            RP2Decimal(crypto_in),
            fiat_fee=RP2Decimal(fiat_fee),
            row=row,
        )

    def test_fee_inclusive_unit_cost_basis_includes_fee(self) -> None:
        # Same spot_price and amount, different acquisition fee -> different fee-inclusive per-unit basis.
        no_fee: InTransaction = self._buy(1, "100", "2", "0")
        with_fee: InTransaction = self._buy(2, "100", "2", "20")
        self.assertEqual(fee_inclusive_unit_cost_basis(no_fee), RP2Decimal("100"))  # 200 / 2
        self.assertEqual(fee_inclusive_unit_cost_basis(with_fee), RP2Decimal("110"))  # (200 + 20) / 2

    def test_hifo_orders_by_fee_inclusive_cost(self) -> None:
        # Two lots identical except for the fee: under the old spot_price ordering their keys were equal;
        # now HIFO ranks the higher fee-inclusive cost first (heapq is a min-heap, so the smaller key sorts first).
        method: HifoAccountingMethod = HifoAccountingMethod()
        cheap: InTransaction = self._buy(1, "100", "2", "0")  # per-unit 100
        pricey: InTransaction = self._buy(2, "100", "2", "20")  # per-unit 110
        self.assertLess(method.sort_key(pricey), method.sort_key(cheap))
        self.assertLess(method.taxable_event_sort_key(pricey), method.taxable_event_sort_key(cheap))

    def test_lofo_orders_by_fee_inclusive_cost(self) -> None:
        method: LofoAccountingMethod = LofoAccountingMethod()
        cheap: InTransaction = self._buy(1, "100", "2", "0")  # per-unit 100
        pricey: InTransaction = self._buy(2, "100", "2", "20")  # per-unit 110
        self.assertLess(method.sort_key(cheap), method.sort_key(pricey))
        self.assertLess(method.taxable_event_sort_key(cheap), method.taxable_event_sort_key(pricey))


if __name__ == "__main__":
    unittest.main()

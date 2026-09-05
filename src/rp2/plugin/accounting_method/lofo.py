# Copyright 2024 eprbell
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

from typing import Optional

from rp2.abstract_accounting_method import (
    AbstractFeatureBasedAccountingMethod,
    AcquiredLotSortKey,
    fee_inclusive_unit_cost_basis,
)
from rp2.in_transaction import InTransaction
from rp2.rp2_decimal import RP2Decimal


# LOFO (Lowest In, First Out) plugin. In this method the lot with lowest cost of purchase is the first to be used.
# "Cost" is the fee-inclusive per-unit cost basis (lowest first), not the fee-exclusive spot_price: per IRS
# guidance cost basis includes acquisition fees. See issue #11 / upstream eprbell/rp2#150.
class AccountingMethod(AbstractFeatureBasedAccountingMethod):
    def sort_key(self, lot: InTransaction) -> AcquiredLotSortKey:
        return AcquiredLotSortKey(fee_inclusive_unit_cost_basis(lot), lot.timestamp.timestamp(), lot.row)

    def taxable_event_sort_key(self, lot: InTransaction) -> AcquiredLotSortKey:
        return AcquiredLotSortKey(fee_inclusive_unit_cost_basis(lot), lot.cost_basis_timestamp.timestamp(), lot.row)

    def sort_key_with_basis(self, lot: InTransaction, fiat_in_with_fee: Optional[RP2Decimal], taxable_event: bool) -> AcquiredLotSortKey:
        key = super().sort_key_with_basis(lot, fiat_in_with_fee, taxable_event)
        if fiat_in_with_fee is None:
            return key
        return key._replace(cost_key=fiat_in_with_fee / lot.crypto_in)

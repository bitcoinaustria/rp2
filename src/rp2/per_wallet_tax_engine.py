# Copyright 2025 eprbell
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

# Per-wallet tax engine. Runs transfer analysis to partition the universal input
# into per-wallet InputData, computes tax for each wallet independently, then
# merges the per-wallet ComputedData back into a single ComputedData that
# report generators can consume unchanged.
#
# See the design doc: https://github.com/eprbell/rp2/wiki/Adding-Per%E2%80%90Wallet-Application-to-RP2

from typing import Dict, cast

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.abstract_entry import AbstractEntry
from rp2.abstract_transaction import AbstractTransaction
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MAX_DATE, MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.gain_loss_set import GainLossSet
from rp2.in_transaction import Account, InTransaction
from rp2.input_data import InputData
from rp2.logger import LOGGER
from rp2.rp2_decimal import RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet
from rp2.transfer_analyzer import TransferAnalyzer


def compute_tax_per_wallet(
    configuration: Configuration,
    accounting_engine: AccountingEngine,
    transfer_semantics: AbstractAccountingMethod,
    universal_input_data: InputData,
) -> ComputedData:
    Configuration.type_check("configuration", configuration)
    AccountingEngine.type_check("accounting_engine", accounting_engine)
    InputData.type_check("universal_input_data", universal_input_data)

    asset: str = universal_input_data.asset

    # Step 1: split universal input into per-wallet inputs using transfer analysis.
    analyzer = TransferAnalyzer(configuration, transfer_semantics, universal_input_data)
    wallet_2_input_data: Dict[Account, InputData] = analyzer.analyze()
    LOGGER.info("Per-wallet tax engine: %s wallets after transfer analysis for %s", len(wallet_2_input_data), asset)

    # Step 2: run the tax engine once per wallet with a fresh accounting engine.
    # Step 3: union the per-wallet transaction and gain/loss sets into merged sets.
    merged_in_set: TransactionSet = TransactionSet(configuration, "IN", asset, MIN_DATE, MAX_DATE)
    merged_out_set: TransactionSet = TransactionSet(configuration, "OUT", asset, MIN_DATE, MAX_DATE)
    merged_intra_set: TransactionSet = TransactionSet(configuration, "INTRA", asset, MIN_DATE, MAX_DATE)
    merged_actual_amounts: Dict[InTransaction, RP2Decimal] = {}
    merged_taxable_events: TransactionSet = TransactionSet(configuration, "MIXED", asset, MIN_DATE, MAX_DATE)
    merged_gain_loss_set: GainLossSet = GainLossSet(configuration, asset, MIN_DATE, MAX_DATE)

    for account, per_wallet_input in wallet_2_input_data.items():
        LOGGER.debug("Per-wallet tax engine: computing tax for %s", account)
        fresh_engine: AccountingEngine = AccountingEngine(accounting_engine.years_2_methods)
        per_wallet_computed: ComputedData = compute_tax(configuration, fresh_engine, per_wallet_input)

        _extend_transaction_set(merged_in_set, per_wallet_input.unfiltered_in_transaction_set)
        _extend_transaction_set(merged_out_set, per_wallet_input.unfiltered_out_transaction_set)
        _extend_transaction_set(merged_intra_set, per_wallet_input.unfiltered_intra_transaction_set)
        merged_actual_amounts.update(per_wallet_input.in_transaction_2_actual_amount)

        taxable_event_set, gain_loss_set = per_wallet_computed.get_unfiltered_taxable_event_and_gain_loss_set()
        _extend_transaction_set(merged_taxable_events, taxable_event_set)
        _extend_gain_loss_set(merged_gain_loss_set, gain_loss_set)

    merged_input_data: InputData = InputData(
        asset,
        merged_in_set,
        merged_out_set,
        merged_intra_set,
        in_transaction_2_actual_amount=merged_actual_amounts,
        from_date=configuration.from_date,
        to_date=configuration.to_date,
    )

    return ComputedData(
        asset,
        merged_taxable_events,
        merged_gain_loss_set,
        merged_input_data,
        configuration.from_date,
        configuration.to_date,
    )


def _extend_transaction_set(target: TransactionSet, source: TransactionSet) -> None:
    entry: AbstractEntry
    for entry in source:
        transaction: AbstractTransaction = cast(AbstractTransaction, entry)
        target.add_entry(transaction)


def _extend_gain_loss_set(target: GainLossSet, source: GainLossSet) -> None:
    entry: AbstractEntry
    for entry in source:
        gain_loss: GainLoss = cast(GainLoss, entry)
        target.add_entry(gain_loss)

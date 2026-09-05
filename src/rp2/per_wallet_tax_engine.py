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

from datetime import datetime
from typing import Dict, cast

from rp2.abstract_accounting_method import (
    AbstractAccountingMethod,
    PoolAcquiredLotCandidates,
)
from rp2.abstract_entry import AbstractEntry
from rp2.abstract_transaction import AbstractTransaction
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MAX_DATE, MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.gain_loss_set import GainLossSet
from rp2.in_transaction import Account, InTransaction
from rp2.input_data import InputData
from rp2.intra_transaction import IntraTransaction
from rp2.logger import LOGGER
from rp2.out_transaction import OutTransaction
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2ValueError
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
    _reject_fee_bearing_intra_transactions(universal_input_data)
    pool_transfer_semantics = isinstance(transfer_semantics.create_lot_candidates([], {}), PoolAcquiredLotCandidates)
    _reject_pool_disposals_after_transfers(universal_input_data, accounting_engine, pool_transfer_semantics)
    _reject_mixed_pool_methods(universal_input_data, accounting_engine, transfer_semantics, pool_transfer_semantics)

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
    merged_fiat_basis_overrides: Dict[InTransaction, RP2Decimal] = {}
    merged_open_position_basis: Dict[InTransaction, RP2Decimal] = {}
    merged_taxable_events: TransactionSet = TransactionSet(configuration, "MIXED", asset, MIN_DATE, MAX_DATE)
    merged_gain_loss_set: GainLossSet = GainLossSet(configuration, asset, MIN_DATE, MAX_DATE)

    for account, per_wallet_input in wallet_2_input_data.items():
        LOGGER.debug("Per-wallet tax engine: computing tax for %s", account)
        fresh_engine: AccountingEngine = AccountingEngine(accounting_engine.years_2_methods)
        # Analyzer inputs retain acquisition basis only. Report averages are
        # separate snapshots and must never seed chronological taxable replay.
        per_wallet_computed: ComputedData = compute_tax(configuration, fresh_engine, per_wallet_input)
        taxable_event_set, gain_loss_set = per_wallet_computed.get_unfiltered_taxable_event_and_gain_loss_set()

        _extend_transaction_set(merged_in_set, per_wallet_input.unfiltered_in_transaction_set)
        _extend_transaction_set(merged_out_set, per_wallet_input.unfiltered_out_transaction_set)
        _extend_transaction_set(merged_intra_set, per_wallet_input.unfiltered_intra_transaction_set)
        merged_actual_amounts.update(analyzer.get_open_position_actual_amounts(account))
        for entry in per_wallet_input.unfiltered_in_transaction_set:
            in_transaction = cast(InTransaction, entry)
            merged_fiat_basis_overrides[in_transaction] = per_wallet_computed.get_in_transaction_fiat_in_with_fee(in_transaction)
            merged_open_position_basis[in_transaction] = per_wallet_computed.get_open_position_in_transaction_fiat_in_with_fee(in_transaction)
        if pool_transfer_semantics:
            # The analyzer includes non-taxable principal movements that the
            # taxable replay does not consume. Its report snapshot is bounded
            # to the same cutoff as the actual quantities above.
            merged_open_position_basis.update(analyzer.get_open_position_basis(account))

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
        in_transaction_2_fiat_in_with_fee_override=merged_fiat_basis_overrides,
    )

    return ComputedData(
        asset,
        merged_taxable_events,
        merged_gain_loss_set,
        merged_input_data,
        configuration.from_date,
        configuration.to_date,
        in_transaction_2_fiat_in_with_fee_override=merged_fiat_basis_overrides,
        open_position_in_transaction_2_fiat_in_with_fee_override=merged_open_position_basis,
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


def _reject_fee_bearing_intra_transactions(input_data: InputData) -> None:
    for entry in input_data.unfiltered_intra_transaction_set:
        intra_transaction = cast(IntraTransaction, entry)
        if intra_transaction.crypto_fee == ZERO:
            continue
        raise RP2ValueError(
            "Fee-bearing intra-transactions are unsupported with per_wallet application "
            "because their fee basis cannot be allocated chronologically without a shared accounting cursor."
        )


def _reject_pool_disposals_after_transfers(input_data: InputData, accounting_engine: AccountingEngine, pool_transfer_semantics: bool) -> None:
    # A fee-free MOVE is not a taxable event, so the second pass cannot deplete
    # a source pool at its transfer time. A later disposal would therefore use
    # the wrong pool after intervening acquisitions. Final-position overrides
    # cannot repair that history: doing so changes earlier taxable events.
    first_transfer_by_account: Dict[Account, datetime] = {}
    for intra_entry in input_data.unfiltered_intra_transaction_set:
        transfer = cast(IntraTransaction, intra_entry)
        if transfer.is_self_transfer():
            continue
        account = Account(transfer.from_exchange, transfer.from_holder)
        first_transfer_by_account[account] = min(first_transfer_by_account.get(account, transfer.timestamp), transfer.timestamp)
    for entry in input_data.unfiltered_out_transaction_set:
        disposal = cast(OutTransaction, entry)
        method = accounting_engine.years_2_methods.find_max_value_less_than(disposal.timestamp.year)
        if not pool_transfer_semantics and not (method is not None and isinstance(method.create_lot_candidates([], {}), PoolAcquiredLotCandidates)):
            continue
        transfer_time = first_transfer_by_account.get(Account(disposal.exchange, disposal.holder))
        if transfer_time is not None and transfer_time <= disposal.timestamp:
            raise RP2ValueError(
                "Pool-based per_wallet application does not support a source-wallet disposal after an outgoing transfer. "
                "These events require a shared chronological accounting cursor. Use universal application."
            )


def _reject_mixed_pool_methods(
    input_data: InputData,
    accounting_engine: AccountingEngine,
    transfer_semantics: AbstractAccountingMethod,
    pool_transfer_semantics: bool,
) -> None:
    if not any(not cast(IntraTransaction, entry).is_self_transfer() for entry in input_data.unfiltered_intra_transaction_set):
        return
    for entry in input_data.unfiltered_out_transaction_set:
        method = accounting_engine.years_2_methods.find_max_value_less_than(entry.timestamp.year)
        if method is None or method.name == transfer_semantics.name:
            continue
        if pool_transfer_semantics or isinstance(method.create_lot_candidates([], {}), PoolAcquiredLotCandidates):
            raise RP2ValueError(
                "Mixed pool-based tax and transfer methods are unsupported with per_wallet application: "
                "their independent replays cannot conserve basis across disposals and transfers. Use universal application."
            )

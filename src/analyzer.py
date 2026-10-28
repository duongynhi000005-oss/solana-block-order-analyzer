"""
Solana Block Order Analyzer - Core Block Analysis Engine
Empirical analysis of transaction ordering, priority fees, compute units, account contention, and Jito bundles.
"""

import sys
import json
import argparse
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict
import struct

from .utils import (
    COMPUTE_BUDGET_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    JITO_TIP_ACCOUNTS,
    b58decode,
    rpc_call
)
from .leader_tracker import SolanaLeaderTracker

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

@dataclass
class ParsedTransaction:
    index: int
    signature: str
    is_success: bool
    cu_limit: int
    cu_price_micro_lamports: int  # Priority fee rate
    base_fee_lamports: int
    priority_fee_lamports: int
    tip_lamports: int
    is_jito_tip: bool
    bundle_id: Optional[int]
    write_locks: List[str]
    read_locks: List[str]

@dataclass
class ContendedAccountMetric:
    account: str
    tx_count: int
    spearman_rho: float
    is_strictly_ordered: bool
    inversions: int

@dataclass
class BlockAnalysisReport:
    slot: int
    blockhash: str
    parent_slot: int
    block_time: Optional[int]
    leader_pubkey: str
    validator_client: str
    total_transactions: int
    vote_transactions: int
    non_vote_transactions: int
    jito_bundles_count: int
    jito_bundle_txs_count: int
    total_priority_fees_sol: float
    total_tips_sol: float
    global_spearman_rho: float
    contended_accounts_evaluated: int
    mean_contended_rho: float
    total_write_lock_conflicts: int
    contended_metrics: List[ContendedAccountMetric]

class SolanaBlockAnalyzer:
    def __init__(self, rpc_url: str = DEFAULT_RPC_URL):
        self.rpc_url = rpc_url
        self.leader_tracker = SolanaLeaderTracker(rpc_url)

    def fetch_block(self, slot: int) -> Dict[str, Any]:
        """Fetch full block payload with maxSupportedTransactionVersion=0."""
        params = [
            slot,
            {
                "encoding": "json",
                "maxSupportedTransactionVersion": 0,
                "transactionDetails": "full",
                "rewards": False
            }
        ]
        res = rpc_call(self.rpc_url, "getBlock", params)
        if not res:
            raise ValueError(f"Block at slot {slot} not found or skipped by cluster.")
        return res

    def _parse_compute_budget(self, instructions: List[dict], account_keys: List[str]) -> Tuple[int, int]:
        """Extract SetComputeUnitLimit and SetComputeUnitPrice from instruction list."""
        cu_limit = 200_000  # Default Solana CU limit
        cu_price = 0        # Default 0 micro-lamports

        for ix in instructions:
            program_idx = ix.get("programIdIndex")
            if program_idx is not None and program_idx < len(account_keys):
                prog_id = account_keys[program_idx]
            else:
                prog_id = ix.get("programId")

            if prog_id == COMPUTE_BUDGET_PROGRAM_ID:
                data_str = ix.get("data", "")
                try:
                    data_bytes = b58decode(data_str)
                    if len(data_bytes) >= 1:
                        disc = data_bytes[0]
                        if disc == 0x02 and len(data_bytes) >= 5:  # SetComputeUnitLimit
                            cu_limit = struct.unpack("<I", data_bytes[1:5])[0]
                        elif disc == 0x03 and len(data_bytes) >= 9:  # SetComputeUnitPrice
                            cu_price = struct.unpack("<Q", data_bytes[1:9])[0]
                except Exception:
                    pass

        return cu_limit, cu_price

    def _check_jito_tip(self, instructions: List[dict], account_keys: List[str]) -> Tuple[bool, int]:
        """Detect transfer to any official Jito Tip account."""
        total_tip = 0
        is_tip = False

        for ix in instructions:
            prog_idx = ix.get("programIdIndex")
            prog_id = account_keys[prog_idx] if (prog_idx is not None and prog_idx < len(account_keys)) else ix.get("programId")

            # Check for tip accounts in instruction accounts
            ix_accounts = ix.get("accounts", [])
            for acc_idx in ix_accounts:
                if acc_idx < len(account_keys):
                    target_acc = account_keys[acc_idx]
                    if target_acc in JITO_TIP_ACCOUNTS:
                        is_tip = True
                        # Parse standard SystemProgram transfer data if available
                        data_str = ix.get("data", "")
                        try:
                            data_bytes = b58decode(data_str)
                            if len(data_bytes) >= 12 and struct.unpack("<I", data_bytes[:4])[0] == 2:
                                lamports = struct.unpack("<Q", data_bytes[4:12])[0]
                                total_tip += lamports
                        except Exception:
                            pass

        return is_tip, total_tip

    def parse_block_transactions(self, block: dict) -> List[ParsedTransaction]:
        """Parse all transactions in block into structured ParsedTransaction instances."""
        raw_txs = block.get("transactions", [])
        parsed_list: List[ParsedTransaction] = []

        for idx, tx_entry in enumerate(raw_txs):
            meta = tx_entry.get("meta") or {}
            tx = tx_entry.get("transaction") or {}
            message = tx.get("message") or {}
            signatures = tx.get("signatures") or ["unknown"]
            sig = signatures[0] if signatures else "unknown"

            is_success = meta.get("err") is None

            # Resolve account keys
            header = message.get("header") or {}
            num_req_sigs = header.get("numRequiredSignatures", 0)
            num_ro_signed = header.get("numReadonlySignedAccounts", 0)
            num_ro_unsigned = header.get("numReadonlyUnsignedAccounts", 0)

            static_keys = message.get("accountKeys") or []
            if isinstance(static_keys[0], dict):
                static_keys = [k.get("pubkey", "") for k in static_keys]

            loaded_addresses = meta.get("loadedAddresses") or {}
            loaded_writable = loaded_addresses.get("writable") or []
            loaded_readonly = loaded_addresses.get("readonly") or []

            all_accounts = list(static_keys) + list(loaded_writable) + list(loaded_readonly)

            # Partition into read and write locks
            num_static = len(static_keys)
            num_rw_signed = max(0, num_req_sigs - num_ro_signed)
            num_rw_unsigned = max(0, (num_static - num_req_sigs) - num_ro_unsigned)

            write_locks: List[str] = []
            read_locks: List[str] = []

            for i in range(num_rw_signed):
                if i < num_static:
                    write_locks.append(static_keys[i])
            for i in range(num_rw_signed, num_req_sigs):
                if i < num_static:
                    read_locks.append(static_keys[i])
            for i in range(num_req_sigs, num_req_sigs + num_rw_unsigned):
                if i < num_static:
                    write_locks.append(static_keys[i])
            for i in range(num_req_sigs + num_rw_unsigned, num_static):
                if i < num_static:
                    read_locks.append(static_keys[i])

            write_locks.extend(loaded_writable)
            read_locks.extend(loaded_readonly)

            instructions = message.get("instructions") or []
            cu_limit, cu_price = self._parse_compute_budget(instructions, all_accounts)

            is_jito_tip, tip_lamports = self._check_jito_tip(instructions, all_accounts)

            base_fee = 5000 * max(1, num_req_sigs)
            priority_fee = int((cu_limit * cu_price) / 1_000_000)

            parsed_list.append(ParsedTransaction(
                index=idx,
                signature=sig,
                is_success=is_success,
                cu_limit=cu_limit,
                cu_price_micro_lamports=cu_price,
                base_fee_lamports=base_fee,
                priority_fee_lamports=priority_fee,
                tip_lamports=tip_lamports,
                is_jito_tip=is_jito_tip,
                bundle_id=None,
                write_locks=write_locks,
                read_locks=read_locks
            ))

        # Bundle identification pass (heuristics based on tip boundaries and consecutive shared locks)
        self._tag_jito_bundles(parsed_list)
        return parsed_list

    def _tag_jito_bundles(self, txs: List[ParsedTransaction]):
        """Tag contiguous transaction sequences terminating in a Jito tip as atomic bundles."""
        current_bundle_id = 1
        i = 0
        while i < len(txs):
            if txs[i].is_jito_tip:
                # Search backwards for contiguous searcher bundle members (max 5 txs per Jito bundle spec)
                start_idx = max(0, i - 4)
                # Form bundle from start_idx up to i
                for k in range(start_idx, i + 1):
                    if txs[k].bundle_id is None:
                        txs[k].bundle_id = current_bundle_id
                current_bundle_id += 1
            i += 1

    @staticmethod
    def calculate_spearman_rho(ranks_x: List[float], ranks_y: List[float]) -> float:
        """Compute Spearman rank correlation coefficient between two ranked vectors."""
        n = len(ranks_x)
        if n < 2:
            return 0.0
        d_sq_sum = sum((rx - ry) ** 2 for rx, ry in zip(ranks_x, ranks_y))
        denom = n * (n ** 2 - 1)
        if denom == 0:
            return 0.0
        return 1.0 - (6.0 * d_sq_sum) / denom

    def analyze_block(self, slot: int) -> BlockAnalysisReport:
        """Run complete empirical ordering and architectural analysis on block."""
        block = self.fetch_block(slot)
        parsed_txs = self.parse_block_transactions(block)

        leader_info = self.leader_tracker.resolve_slot_leader(slot)
        validator_client = leader_info.validator.client_family if leader_info.validator else "Unknown"

        # Separate votes vs non-votes (votes typically have 1 write lock: Vote program)
        non_votes = [tx for tx in parsed_txs if not any("Vote111111111111111111111111111111111111111" in acc for acc in tx.write_locks)]
        vote_count = len(parsed_txs) - len(non_votes)

        # Global priority fee gradient across non-vote transactions
        # Expected ideal order: highest CU price first (index 0), so index ranks vs fee ranks should have negative correlation
        if len(non_votes) >= 2:
            indices = [float(tx.index) for tx in non_votes]
            fees = [float(tx.cu_price_micro_lamports) for tx in non_votes]
            # Convert to ordinal ranks
            idx_ranks = [float(r) for r in sorted(range(len(indices)), key=lambda k: indices[k])]
            fee_ranks = [float(r) for r in sorted(range(len(fees)), key=lambda k: -fees[k])]
            global_rho = self.calculate_spearman_rho(idx_ranks, fee_ranks)
        else:
            global_rho = 0.0

        # Contended account serialization analysis
        account_tx_map: Dict[str, List[ParsedTransaction]] = defaultdict(list)
        for tx in non_votes:
            for acc in tx.write_locks:
                account_tx_map[acc].append(tx)

        contended_metrics: List[ContendedAccountMetric] = []
        conflicts_count = 0
        rho_sum = 0.0
        evaluated_accounts = 0

        for acc, acc_txs in account_tx_map.items():
            if len(acc_txs) >= 2:
                conflicts_count += (len(acc_txs) * (len(acc_txs) - 1)) // 2
                acc_indices = [float(tx.index) for tx in acc_txs]
                acc_fees = [float(tx.cu_price_micro_lamports) for tx in acc_txs]

                r_idx = [float(r) for r in sorted(range(len(acc_indices)), key=lambda k: acc_indices[k])]
                r_fee = [float(r) for r in sorted(range(len(acc_fees)), key=lambda k: -acc_fees[k])]
                rho = self.calculate_spearman_rho(r_idx, r_fee)

                # Count pair inversions
                inversions = 0
                for a in range(len(acc_fees)):
                    for b in range(a + 1, len(acc_fees)):
                        if acc_fees[a] < acc_fees[b]:  # Lower fee included before higher fee
                            inversions += 1

                is_strictly_ordered = (inversions == 0)
                contended_metrics.append(ContendedAccountMetric(
                    account=acc,
                    tx_count=len(acc_txs),
                    spearman_rho=round(rho, 4),
                    is_strictly_ordered=is_strictly_ordered,
                    inversions=inversions
                ))
                rho_sum += rho
                evaluated_accounts += 1

        mean_contended_rho = (rho_sum / evaluated_accounts) if evaluated_accounts > 0 else 0.0

        # Totals
        total_p_fee = sum(tx.priority_fee_lamports for tx in parsed_txs) / 1e9
        total_tip = sum(tx.tip_lamports for tx in parsed_txs) / 1e9
        bundles = set(tx.bundle_id for tx in parsed_txs if tx.bundle_id is not None)
        bundle_txs = sum(1 for tx in parsed_txs if tx.bundle_id is not None)

        return BlockAnalysisReport(
            slot=slot,
            blockhash=block.get("blockhash", ""),
            parent_slot=block.get("parentSlot", 0),
            block_time=block.get("blockTime"),
            leader_pubkey=leader_info.leader_pubkey,
            validator_client=validator_client,
            total_transactions=len(parsed_txs),
            vote_transactions=vote_count,
            non_vote_transactions=len(non_votes),
            jito_bundles_count=len(bundles),
            jito_bundle_txs_count=bundle_txs,
            total_priority_fees_sol=round(total_p_fee, 6),
            total_tips_sol=round(total_tip, 6),
            global_spearman_rho=round(global_rho, 4),
            contended_accounts_evaluated=evaluated_accounts,
            mean_contended_rho=round(mean_contended_rho, 4),
            total_write_lock_conflicts=conflicts_count,
            contended_metrics=sorted(contended_metrics, key=lambda m: -m.tx_count)[:10]
        )

def main():
    parser = argparse.ArgumentParser(description="Solana Validator Transaction Ordering Analyzer")
    parser.add_argument("--rpc", default=DEFAULT_RPC_URL, help="Solana JSON-RPC URL")
    parser.add_argument("--slot", type=int, help="Target slot to analyze")
    parser.add_argument("--output", help="Optional output JSON path")
    args = parser.parse_args()

    analyzer = SolanaBlockAnalyzer(args.rpc)
    target_slot = args.slot
    if not target_slot:
        epoch_info = rpc_call(args.rpc, "getEpochInfo")
        target_slot = epoch_info.get("absoluteSlot", 0) - 10
        print(f"Targeting confirmed slot {target_slot}...")

    report = analyzer.analyze_block(target_slot)

    print("\n" + "=" * 65)
    print(f"  SOLANA BLOCK TRANSACTION ORDERING REPORT - SLOT {report.slot}")
    print("=" * 65)
    print(f"  Validator Client:      {report.validator_client}")
    print(f"  Leader Pubkey:         {report.leader_pubkey}")
    print(f"  Total Transactions:    {report.total_transactions} ({report.non_vote_transactions} non-vote, {report.vote_transactions} votes)")
    print(f"  Jito Bundles Detected: {report.jito_bundles_count} ({report.jito_bundle_txs_count} txs)")
    print(f"  Priority Fees (SOL):   {report.total_priority_fees_sol:.6f} SOL")
    print(f"  Jito Tips (SOL):       {report.total_tips_sol:.6f} SOL")
    print("-" * 65)
    print("  EMPIRICAL ORDERING CORRELATIONS:")
    print(f"  Global Priority Gradient (Spearman rho): {report.global_spearman_rho}")
    print(f"  Contended Accounts Evaluated:           {report.contended_accounts_evaluated}")
    print(f"  Mean Contended Account Rho:             {report.mean_contended_rho}")
    print(f"  Total Write-Lock Conflict Pairs:        {report.total_write_lock_conflicts}")
    print("=" * 65)

    if report.contended_metrics:
        print("\nTop Contended Writable Accounts (Write-Lock Inversion Heatmap):")
        print(f"  {'Account Pubkey':<46} {'Txs':<5} {'Rho':<8} {'Inversions'}")
        for m in report.contended_metrics[:5]:
            print(f"  {m.account:<46} {m.tx_count:<5} {m.spearman_rho:<8} {m.inversions}")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2)
        print(f"\nReport written to {args.output}")

if __name__ == "__main__":
    main()

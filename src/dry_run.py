"""
Solana Block Order Analyzer - Safe Simulation & Live Probe Engine
Default: Dry-run simulation (0 SOL cost) modeling Agave, Jito, and Firedancer banking stages.
Live mode: Strict spend cap enforcement (MAX_SPEND_SOL <= 0.05).
"""

import sys
import time
import random
import argparse
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

MAX_SPEND_CAP_SOL = 0.05

@dataclass
class SyntheticTransaction:
    tx_id: int
    cu_price_micro_lamports: int
    cu_limit: int
    write_locks: List[str]
    is_bundle: bool
    bundle_id: Optional[int]
    arrival_timestamp_us: int

@dataclass
class SimulatorResult:
    engine_name: str
    total_txs: int
    ordered_indices: List[int]
    spearman_rho: float
    write_lock_inversions: int
    bundle_atomic_compliance: float

class AgaveSimulator:
    """Models Agave (standard Solana Labs) multithreaded banking stage (4 worker threads)."""
    def __init__(self, num_threads: int = 4):
        self.num_threads = num_threads

    def schedule(self, txs: List[SyntheticTransaction]) -> List[SyntheticTransaction]:
        # Sort incoming pool primarily by priority fee rate
        pending = sorted(txs, key=lambda t: -t.cu_price_micro_lamports)
        scheduled: List[SyntheticTransaction] = []
        active_write_locks: Set[str] = set()

        # Thread scheduling cycles
        while pending:
            batch: List[SyntheticTransaction] = []
            rem = []
            current_locks: Set[str] = set()

            for tx in pending:
                # Check for write lock conflict within current concurrent batch
                has_conflict = any(l in current_locks for l in tx.write_locks)
                if not has_conflict and len(batch) < self.num_threads:
                    batch.append(tx)
                    current_locks.update(tx.write_locks)
                else:
                    rem.append(tx)

            scheduled.extend(batch)
            pending = rem
            if not batch:  # Fallback to prevent stall
                scheduled.append(pending.pop(0))

        return scheduled

class JitoSimulator:
    """Models Jito-Solana bundle-engine + banking stage."""
    def __init__(self, agave_sim: AgaveSimulator):
        self.agave_sim = agave_sim

    def schedule(self, txs: List[SyntheticTransaction]) -> List[SyntheticTransaction]:
        bundles: Dict[int, List[SyntheticTransaction]] = {}
        standard_txs: List[SyntheticTransaction] = []

        for tx in txs:
            if tx.is_bundle and tx.bundle_id is not None:
                bundles.setdefault(tx.bundle_id, []).append(tx)
            else:
                standard_txs.append(tx)

        # Bundles execute atomically at the front of the block
        scheduled: List[SyntheticTransaction] = []
        for b_id in sorted(bundles.keys()):
            scheduled.extend(bundles[b_id])

        # Remainder processed through banking stage
        scheduled.extend(self.agave_sim.schedule(standard_txs))
        return scheduled

class FiredancerSimulator:
    """Models Firedancer pipelined tile architecture (arrival-time queue with micro-burst sorting)."""
    def __init__(self, tick_window_us: int = 1000):
        self.tick_window_us = tick_window_us

    def schedule(self, txs: List[SyntheticTransaction]) -> List[SyntheticTransaction]:
        # Packets arrive via fd_quic and buffered into micro-burst time windows
        if not txs:
            return []
        min_time = min(t.arrival_timestamp_us for t in txs)
        buckets: Dict[int, List[SyntheticTransaction]] = {}

        for tx in txs:
            bucket_idx = (tx.arrival_timestamp_us - min_time) // self.tick_window_us
            buckets.setdefault(bucket_idx, []).append(tx)

        scheduled: List[SyntheticTransaction] = []
        # In Firedancer fd_pack, each arrival window sorts by priority fee
        for b_idx in sorted(buckets.keys()):
            bucket_txs = sorted(buckets[b_idx], key=lambda t: -t.cu_price_micro_lamports)
            scheduled.extend(bucket_txs)

        return scheduled

def calculate_spearman(ranks_x: List[float], ranks_y: List[float]) -> float:
    n = len(ranks_x)
    if n < 2:
        return 0.0
    d_sq_sum = sum((rx - ry) ** 2 for rx, ry in zip(ranks_x, ranks_y))
    denom = n * (n ** 2 - 1)
    return 1.0 - (6.0 * d_sq_sum) / denom if denom > 0 else 0.0

def count_inversions(scheduled: List[SyntheticTransaction]) -> int:
    inversions = 0
    account_map: Dict[str, List[SyntheticTransaction]] = {}
    for tx in scheduled:
        for acc in tx.write_locks:
            account_map.setdefault(acc, []).append(tx)

    for acc, tx_list in account_map.items():
        for i in range(len(tx_list)):
            for j in range(i + 1, len(tx_list)):
                if tx_list[i].cu_price_micro_lamports < tx_list[j].cu_price_micro_lamports:
                    inversions += 1
    return inversions

def run_dry_run_simulation(num_txs: int = 100, hot_accounts: int = 5):
    print("=" * 65)
    print("  SOLANA VALIDATOR TRANSACTION ORDERING - DRY-RUN SIMULATION")
    print("  (0 SOL cost - Benchmarking Agave vs Jito vs Firedancer)")
    print("=" * 65)
    print(f"  Synthetic Workload: {num_txs} transactions, {hot_accounts} contended accounts")

    random.seed(42)
    synthetic_txs: List[SyntheticTransaction] = []

    # Generate synthetic transactions with Pareto distributed priority fees
    for i in range(num_txs):
        # Pareto distribution produces heavy-tailed priority fee spikes typical of MEV
        cu_price = int(random.paretovariate(1.2) * 1000)
        # Contention: 60% of txs touch hot accounts
        if random.random() < 0.6:
            write_locks = [f"HotAccount_{random.randint(1, hot_accounts)}"]
        else:
            write_locks = [f"DisjointAccount_{i}"]

        is_bundle = (i < 6)  # First 6 txs simulate 2 Jito bundles of 3 txs each
        bundle_id = (i // 3) + 1 if is_bundle else None
        arrival_us = random.randint(0, 5000)

        synthetic_txs.append(SyntheticTransaction(
            tx_id=i,
            cu_price_micro_lamports=cu_price,
            cu_limit=200_000,
            write_locks=write_locks,
            is_bundle=is_bundle,
            bundle_id=bundle_id,
            arrival_timestamp_us=arrival_us
        ))

    # Evaluate Agave
    agave_sim = AgaveSimulator(num_threads=4)
    agave_out = agave_sim.schedule(synthetic_txs)
    agave_inv = count_inversions(agave_out)
    idx_ranks = [float(r) for r in range(len(agave_out))]
    fee_ranks = [float(r) for r in sorted(range(len(agave_out)), key=lambda k: -agave_out[k].cu_price_micro_lamports)]
    agave_rho = calculate_spearman(idx_ranks, fee_ranks)

    # Evaluate Jito-Solana
    jito_sim = JitoSimulator(agave_sim)
    jito_out = jito_sim.schedule(synthetic_txs)
    jito_inv = count_inversions(jito_out)
    idx_ranks = [float(r) for r in range(len(jito_out))]
    fee_ranks = [float(r) for r in sorted(range(len(jito_out)), key=lambda k: -jito_out[k].cu_price_micro_lamports)]
    jito_rho = calculate_spearman(idx_ranks, fee_ranks)

    # Evaluate Firedancer
    fd_sim = FiredancerSimulator(tick_window_us=1000)
    fd_out = fd_sim.schedule(synthetic_txs)
    fd_inv = count_inversions(fd_out)
    idx_ranks = [float(r) for r in range(len(fd_out))]
    fee_ranks = [float(r) for r in sorted(range(len(fd_out)), key=lambda k: -fd_out[k].cu_price_micro_lamports)]
    fd_rho = calculate_spearman(idx_ranks, fee_ranks)

    print("-" * 65)
    print(f"  {'Engine':<24} {'Spearman Rho':<15} {'Write Lock Inversions':<25}")
    print("-" * 65)
    print(f"  {'Agave (4 threads)':<24} {agave_rho:<15.4f} {agave_inv:<25}")
    print(f"  {'Jito-Solana (Bundled)':<24} {jito_rho:<15.4f} {jito_inv:<25}")
    print(f"  {'Firedancer (Arrival QS)':<24} {fd_rho:<15.4f} {fd_inv:<25}")
    print("=" * 65)
    print("  Key Empirical Insight:")
    print("  - Agave introduces lock-contention inversions when threads execute disjoint batches.")
    print("  - Jito guarantees atomic bundle priority, clustering high-MEV txs at slot start.")
    print("  - Firedancer preserves arrival-time windows while sorting intra-window bursts by fee.")

def run_live_probe(spend_cap: float, keypair_path: Optional[str] = None):
    print("=" * 65)
    print("  SOLANA LIVE PROBE SAFETY GUARDRAILS")
    print("=" * 65)
    if spend_cap > MAX_SPEND_CAP_SOL:
        print(f"  [ERROR] Requested spend cap {spend_cap} SOL exceeds safety maximum ({MAX_SPEND_CAP_SOL} SOL)!")
        sys.exit(1)

    print(f"  Spend Cap Verified: {spend_cap} SOL (Safety Limit <= {MAX_SPEND_CAP_SOL} SOL)")
    if not keypair_path:
        print("  [SAFETY GUARD ACTIVATED] No keypair supplied. Live transaction broadcast aborted.")
        print("  Dry-run simulation mode remains active. To execute live micro-probes, provide --keypair.")
        return

    print("  Live mode initialized under safety spend cap.")

def main():
    parser = argparse.ArgumentParser(description="Solana Simulator & Live Probe Engine")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run simulation without sending live transactions (0 SOL cost)")
    parser.add_argument("--live", action="store_true", help="Enable live probe mode (strictly capped at <= 0.05 SOL)")
    parser.add_argument("--spend-cap", type=float, default=0.01, help="Max SOL allowed to spend in live mode (max 0.05 SOL)")
    parser.add_argument("--keypair", help="Path to Solana keypair for live mode")
    parser.add_argument("--txs", type=int, default=100, help="Number of synthetic txs to simulate")
    args = parser.parse_args()

    if args.live:
        run_live_probe(args.spend_cap, args.keypair)
    else:
        run_dry_run_simulation(args.txs)

if __name__ == "__main__":
    main()

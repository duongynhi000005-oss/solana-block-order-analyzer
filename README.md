# Inside the Block: Solana Validator Transaction Ordering Analyzer

An empirical measurement toolkit and architectural simulation engine studying how Solana validators order transactions across **Agave**, **Jito-Solana**, and **Firedancer**.

Built for the **Superteam Earn ($2,500 USDC)** research challenge: *Inside the Block: How Solana Validators Order Transactions*.

---

## 1. Executive Summary & Research Motivation

On Solana, transactions are not ordered by a single global sequential mempool. Instead, transaction execution order within a block is governed by:
1. **Network Packet Arrival Order (QUIC/TPU)**: Physical arrival time at the designated slot leader.
2. **Validator Client Architecture**:
   - **Agave (Anza / Solana Labs)**: Multithreaded banking stage (default 4–6 worker threads) executing non-conflicting transactions in parallel with account write-lock contention.
   - **Jito-Solana**: Custom bundle engine evaluating MEV auction tips and prepending/interleaving atomic bundles before standard TPU packets.
   - **Firedancer (Jump Crypto)**: Hardware-optimized pipeline tiles (`fd_quic` -> `fd_verify` -> `fd_dedup` -> `fd_pack` -> `fd_bank`) utilizing arrival-time queuing and micro-burst priority sorting.
3. **Compute Budget & Priority Fees**: Micro-lamports per Compute Unit (`SetComputeUnitPrice` and `SetComputeUnitLimit`).

This repository provides **mathematical metrics, empirical block parsers, validator client trackers, and an architectural simulator** to quantify transaction ordering behavior.

---

## 2. Comparative Client Architectures

| Dimension | Agave (Solana Labs) | Jito-Solana | Firedancer / Frankendancer |
| :--- | :--- | :--- | :--- |
| **Banking Stage Model** | Multithreaded Parallel Workers (4-6 threads) | Bundle Engine + Agave Banking Stage | Monolithic Pipelined Tiles (`fd_pack`) |
| **Contention Resolution** | Concurrent execution; conflicting write locks serialize across thread dispatch | Bundles execute atomically; remainder follows Agave locking | Hardware ring buffers (`mcache`); microsecond arrival windows |
| **MEV Channel** | Gossip TPU / Public QUIC only | Private Mempool Relayer + Bundle Auction | High-throughput TPU QUIC (`fd_quic`) |
| **Incentive Mechanism** | Priority Fee ($F_{pri} / CU$) | Priority Fee + Direct Tip Account Transfers | Priority Fee ($F_{pri} / CU$) |
| **Ordering Predictability**| Non-deterministic under high write-lock contention | Deterministic for bundles; variable for standard TPU | High fidelity to physical arrival time |

---

## 3. Empirical Metrics & Methodology

### 3.1 Intra-Block Priority Fee Gradient (Spearman's $\rho$)
Measures the monotonic correlation between the transaction's execution index in the block ($r_{idx}$) and its priority fee rate ($r_{fee}$):

$$\rho = 1 - \frac{6 \sum (r_{idx} - r_{fee})^2}{n(n^2 - 1)}$$

- $\rho \approx 1.0$: Strict priority fee ordering (highest fee always executed first).
- $\rho \approx 0.0$: Fee-independent ordering (pure arrival time or random scheduling).
- $\rho < 0.0$: Inversion-heavy scheduling.

### 3.2 Contended Write-Lock Inversion Analysis
For any contended write-lock account $A$ accessed by multiple transactions:
An **inversion** is defined as any pair $(i, j)$ with $i < j$ (transaction $i$ ordered before $j$) where:

$$FeeRate(tx_i) < FeeRate(tx_j)$$

Under Agave's multithreaded banking stage, when two transactions touch disjoint sets of accounts, they run concurrently on different threads. If a high-fee transaction $T_H$ waits for a lock held by a previous transaction on Thread 1, a lower-fee transaction $T_L$ can execute immediately on Thread 2, resulting in an observable intra-block fee inversion.

### 3.3 Jito Bundle Detection Heuristics
The analyzer scans every transaction for instructions invoking `11111111111111111111111111111111` (System Program) transferring lamports to any of the 8 canonical Jito Tip Accounts:
- `96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5`
- `HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe`
- `Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY`
- `ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49`
- `DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh`
- `ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt`
- `DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL`
- `3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT`

Sequences terminating in these tip transfers are categorized as atomic bundles.

---

## 4. Quick Start & Execution

### Option A: 1-Click Run via Docker Compose
```bash
docker-compose up --build
```

### Option B: Standalone Python 3.12+
Install dependencies:
```bash
pip install -r requirements.txt
```

#### 1. Dry-Run Architectural Simulation (0 SOL Cost)
Simulate synthetic transaction workloads across Agave, Jito, and Firedancer:
```bash
python -m src.dry_run --txs 100
```
*Output demonstrates the comparative write-lock inversion rate across client models.*

#### 2. Analyze Live On-Chain Blocks
Fetch and evaluate real mainnet blocks via JSON-RPC:
```bash
python -m src.analyzer --slot 300000000 --output block_report.json
```

#### 3. Resolve Slot Leader & Validator Client Family
Identify whether a designated slot was produced by Agave, Jito-Solana, or Firedancer:
```bash
python -m src.leader_tracker --current
```

#### 4. Safety Guardrails for Live Probe Mode
To test arrival vs inclusion orders on Devnet/Mainnet:
```bash
python -m src.dry_run --live --spend-cap 0.01 --keypair ~/.config/solana/id.json
```
> **Safety Guard**: Hard cap enforced at `<= 0.05 SOL`. Rejects execution if fee limits exceed safety boundaries.

---

## 5. Repository Structure

```
+-- docker-compose.yml       # 1-Click reproducible container deployment
+-- Dockerfile               # Python 3.12-slim runtime environment
+-- requirements.txt         # Core dependencies (requests, numpy, scipy, tabulate, rich)
+-- README.md                # Comprehensive research documentation
+-- LICENSE                  # MIT License
+-- src
    +-- __init__.py          # Package initialization
    +-- utils.py             # Base58 decoder, Jito tip addresses, RPC client
    +-- leader_tracker.py    # Cluster gossip & leader schedule resolver
    +-- analyzer.py          # Core block parsing & Spearman correlation engine
    +-- dry_run.py           # Multi-client simulator & safety-guarded probe
```

---

## 6. License
MIT License. Open source research contributions welcome.

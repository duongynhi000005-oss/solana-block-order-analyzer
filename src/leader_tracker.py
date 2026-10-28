"""
Solana Block Order Analyzer - Leader Schedule & Validator Client Tracker
Tracks slot leaders and maps validator identities to client software (Agave, Jito-Solana, Firedancer).
"""

import sys
import argparse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from .utils import rpc_call

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

@dataclass
class ValidatorMetadata:
    identity_pubkey: str
    vote_pubkey: str
    version: str
    client_family: str  # "Agave", "Jito-Solana", "Firedancer/Frankendancer", "Unknown"
    feature_set: Optional[int] = None

@dataclass
class SlotLeaderInfo:
    slot: int
    leader_pubkey: str
    validator: Optional[ValidatorMetadata]

class SolanaLeaderTracker:
    def __init__(self, rpc_url: str = DEFAULT_RPC_URL):
        self.rpc_url = rpc_url
        self._cluster_nodes: Dict[str, dict] = {}
        self._vote_accounts: Dict[str, dict] = {}
        self._leader_schedule: Dict[str, List[int]] = {}
        self._epoch_info: Optional[dict] = None
        self._cached_epoch: int = -1

    def refresh_cluster_metadata(self):
        """Fetch gossip cluster nodes and vote accounts to build validator client profiles."""
        nodes = rpc_call(self.rpc_url, "getClusterNodes") or []
        self._cluster_nodes = {n.get("pubkey"): n for n in nodes if "pubkey" in n}

        vote_res = rpc_call(self.rpc_url, "getVoteAccounts") or {}
        current_votes = vote_res.get("current", [])
        delinq_votes = vote_res.get("delinquent", [])
        self._vote_accounts = {}
        for v in current_votes + delinq_votes:
            self._vote_accounts[v.get("nodePubkey")] = v

    def classify_client(self, version_str: str, node_info: Optional[dict] = None) -> str:
        """Classify validator software implementation based on version string and cluster gossip flags."""
        v_lower = (version_str or "").lower()
        if "firedancer" in v_lower or "frankendancer" in v_lower or "fd" in v_lower:
            return "Firedancer/Frankendancer"
        if "jito" in v_lower:
            return "Jito-Solana"
        if "agave" in v_lower or "anza" in v_lower:
            return "Agave"
        if "solana" in v_lower or v_lower.startswith("1.") or v_lower.startswith("2."):
            # Solana Labs lineage is now maintained as Agave by Anza
            return "Agave"
        return "Unknown"

    def get_validator_metadata(self, identity_pubkey: str) -> ValidatorMetadata:
        """Retrieve client metadata for a given validator identity public key."""
        if not self._cluster_nodes:
            self.refresh_cluster_metadata()

        node_info = self._cluster_nodes.get(identity_pubkey, {})
        vote_info = self._vote_accounts.get(identity_pubkey, {})

        version = node_info.get("version", "Unknown")
        feature_set = node_info.get("featureSet")
        vote_pubkey = vote_info.get("votePubkey", "Unknown")
        client_family = self.classify_client(version, node_info)

        return ValidatorMetadata(
            identity_pubkey=identity_pubkey,
            vote_pubkey=vote_pubkey,
            version=version,
            client_family=client_family,
            feature_set=feature_set
        )

    def load_leader_schedule(self, slot: Optional[int] = None) -> Dict[str, List[int]]:
        """Fetch and cache the leader schedule for the epoch containing slot."""
        epoch_info = rpc_call(self.rpc_url, "getEpochInfo")
        self._epoch_info = epoch_info
        current_epoch = epoch_info.get("epoch", 0)

        if self._cached_epoch != current_epoch or not self._leader_schedule:
            schedule = rpc_call(self.rpc_url, "getLeaderSchedule", [slot] if slot else [])
            self._leader_schedule = schedule or {}
            self._cached_epoch = current_epoch

        return self._leader_schedule

    def resolve_slot_leader(self, slot: int) -> SlotLeaderInfo:
        """Identify the validator leader assigned to a specific slot."""
        if not self._leader_schedule:
            self.load_leader_schedule(slot)

        epoch_info = self._epoch_info or rpc_call(self.rpc_url, "getEpochInfo")
        slot_index = epoch_info.get("slotIndex", 0)
        absolute_slot = epoch_info.get("absoluteSlot", 0)
        epoch_start_slot = absolute_slot - slot_index

        slot_offset = slot - epoch_start_slot

        # Look up which validator has this slot_offset
        matched_leader = None
        for pubkey, offsets in self._leader_schedule.items():
            if slot_offset in offsets:
                matched_leader = pubkey
                break

        if not matched_leader:
            # Fallback: query slot leaders directly
            slot_leaders = rpc_call(self.rpc_url, "getSlotLeaders", [slot, 1])
            if slot_leaders and len(slot_leaders) > 0:
                matched_leader = slot_leaders[0]

        validator_meta = self.get_validator_metadata(matched_leader) if matched_leader else None
        return SlotLeaderInfo(slot=slot, leader_pubkey=matched_leader or "Unknown", validator=validator_meta)

def main():
    parser = argparse.ArgumentParser(description="Solana Leader Schedule & Client Tracker")
    parser.add_argument("--rpc", default=DEFAULT_RPC_URL, help="Solana JSON-RPC URL")
    parser.add_argument("--slot", type=int, help="Target slot to inspect")
    parser.add_argument("--current", action="store_true", help="Inspect current slot")
    args = parser.parse_args()

    tracker = SolanaLeaderTracker(args.rpc)
    target_slot = args.slot
    if args.current or not target_slot:
        epoch_info = rpc_call(args.rpc, "getEpochInfo")
        target_slot = epoch_info.get("absoluteSlot", 0)
        print(f"Current Epoch: {epoch_info.get('epoch')}, Current Slot: {target_slot}")

    info = tracker.resolve_slot_leader(target_slot)
    print(f"\nSlot {info.slot} Leader Information:")
    print(f"  Leader Pubkey:  {info.leader_pubkey}")
    if info.validator:
        print(f"  Vote Pubkey:    {info.validator.vote_pubkey}")
        print(f"  Client Family:  {info.validator.client_family}")
        print(f"  Version:        {info.validator.version}")
        print(f"  Feature Set:    {info.validator.feature_set}")
    else:
        print("  Validator client info could not be resolved from cluster gossip.")

if __name__ == "__main__":
    main()

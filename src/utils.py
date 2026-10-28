"""
Solana Block Order Analyzer - Utility Functions and Constants
"""

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

# Program IDs
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

# Known Official Jito Tip Payment Accounts
JITO_TIP_ACCOUNTS = {
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
}

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58decode(s: str) -> bytes:
    """Decode Base58 string to raw bytes without external dependencies."""
    n = 0
    for char in s:
        idx = B58_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"Invalid character {char} in Base58 string")
        n = n * 58 + idx
    res = []
    while n > 0:
        res.append(n & 0xFF)
        n >>= 8
    pad = 0
    for char in s:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + bytes(reversed(res))

def b58encode(b: bytes) -> str:
    """Encode raw bytes to Base58 string."""
    n = int.from_bytes(b, "big")
    res = []
    while n > 0:
        n, mod = divmod(n, 58)
        res.append(B58_ALPHABET[mod])
    pad = 0
    for byte in b:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(res))

def rpc_call(url: str, method: str, params: Optional[List[Any]] = None, max_retries: int = 3) -> Dict[str, Any]:
    """Execute a Solana JSON-RPC call with retry logic and exponential backoff."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or []
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SolanaBlockOrderAnalyzer/1.0"
    }

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "error" in data:
                    raise RuntimeError(f"RPC Error: {data['error']}")
                return data.get("result")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(1.5 ** attempt)
    return {}

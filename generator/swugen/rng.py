"""Deterministic randomness (FR-06): the same seed, utility and minute always
produce the same random sequence, independent of when or how often the
generator runs."""
import hashlib
import random
from datetime import datetime


def minute_rng(seed: int, utility_id: str, minute: datetime, stream: str = "") -> random.Random:
    key = f"{seed}|{utility_id}|{minute.strftime('%Y-%m-%dT%H:%M')}|{stream}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))


def poisson(rng: random.Random, lam: float) -> int:
    """Small-lambda Poisson sample (Knuth); lambda here is events per minute."""
    if lam <= 0:
        return 0
    import math
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1

#!/usr/bin/env python3
"""Compare TCP connect latency from this host to the configured Neon database and every Neon region."""

import argparse
from collections.abc import Callable
from pathlib import Path
import re
import socket
import statistics
import time
from urllib.parse import urlsplit


GATEWAY_DIR = Path(__file__).resolve().parent
NEON_AWS_REGIONS = (
    "us-east-1", "us-east-2", "us-west-2", "eu-central-1",
    "eu-west-2", "ap-southeast-1", "ap-southeast-2", "sa-east-1",
)
NEON_HOST = re.compile(r"^ep-[a-z0-9-]+(?:\.c-\d+)?\.(?P<region>[a-z]{2}-[a-z]+-\d)\.aws\.neon\.tech$")


def neon_region(hostname: str) -> str | None:
    match = NEON_HOST.fullmatch(hostname or "")
    return f"aws-{match['region']}" if match else None


def connect_ms(host: str, port: int = 5432, timeout: float = 5.0) -> float:
    start = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout):
        return (time.perf_counter() - start) * 1000


def median_latency(host: str, samples: int, probe: Callable[[str], float] = connect_ms) -> float | None:
    measured = []
    for _ in range(samples):
        try:
            measured.append(probe(host))
        except OSError:
            continue
    return statistics.median(measured) if measured else None


def configured_database_host(env_file: Path) -> str | None:
    if not env_file.is_file():
        return None
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = raw_line.strip().partition("=")
        if separator and key.strip() == "DATABASE_URL":
            return urlsplit(value.strip().strip("'\"")).hostname
    return None


def rank_regions(samples: int, probe: Callable[[str], float] = connect_ms) -> list[tuple[str, float | None]]:
    # Regional wildcard DNS resolves any endpoint-shaped name to the region's Neon proxy.
    results = [
        (f"aws-{region}", median_latency(f"ep-latency-probe-00000000.{region}.aws.neon.tech", samples, probe))
        for region in NEON_AWS_REGIONS
    ]
    return sorted(results, key=lambda item: float("inf") if item[1] is None else item[1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--env-file", default=str(GATEWAY_DIR / ".env"))
    args = parser.parse_args()

    host = configured_database_host(Path(args.env_file))
    if host:
        latency = median_latency(host, args.samples)
        shown = "unreachable" if latency is None else f"{latency:.1f} ms"
        print(f"Configured database: {neon_region(host) or 'non-Neon host'} ({shown})")
    print("Neon region latency from this host (median TCP connect):")
    ranked = rank_regions(args.samples)
    for region, latency in ranked:
        print(f"  {region:<20} {'unreachable' if latency is None else f'{latency:7.1f} ms'}")
    if ranked and ranked[0][1] is not None:
        print(f"Closest region: {ranked[0][0]}")


if __name__ == "__main__":
    main()

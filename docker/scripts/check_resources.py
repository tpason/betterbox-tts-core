#!/usr/bin/env python3
"""
Check system resources (CPU, RAM, GPU VRAM) and decide how many crawler
workers to run. Exit code 0 = safe to run, 1 = skip this pass.

Usage:
  python docker/scripts/check_resources.py              # human-readable log line
  python docker/scripts/check_resources.py --workers-only   # print recommended workers count
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def read_cpu_percent(interval: float = 0.5) -> float:
    def read_stat() -> tuple[int, int]:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + vals[4]  # idle + iowait
        total = sum(vals)
        return idle, total

    idle1, total1 = read_stat()
    time.sleep(interval)
    idle2, total2 = read_stat()
    delta_total = total2 - total1
    delta_idle = idle2 - idle1
    if delta_total == 0:
        return 0.0
    return round(100.0 * (1.0 - delta_idle / delta_total), 1)


def read_mem_free_gb() -> float:
    mem: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
    available_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    return round(available_kb / (1024 * 1024), 2)


def read_gpu_vram_free_gb() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            if lines:
                return round(int(lines[0]) / 1024, 2)
    except Exception:
        pass
    return None


def recommend_workers(
    cpu_percent: float,
    mem_free_gb: float,
    max_workers: int,
    max_cpu_percent: float,
) -> int:
    # Crawling is I/O-bound (HTTP), so CPU rarely limits it.
    # But high CPU usually means polish/audio is running alongside.
    if cpu_percent >= max_cpu_percent:
        return max(1, max_workers // 4)
    if cpu_percent >= max_cpu_percent * 0.75:
        return max(1, max_workers // 2)

    if mem_free_gb < 0.5:
        return 1
    if mem_free_gb < 1.5:
        return max(1, max_workers // 2)

    return max_workers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-workers",
        type=int,
        default=int(os.environ.get("CRAWLER_WORKERS", 4)),
        help="Upper cap on workers to recommend.",
    )
    parser.add_argument(
        "--min-free-ram-gb",
        type=float,
        default=float(os.environ.get("CRAWLER_MIN_FREE_RAM_GB", "1.0")),
        help="Minimum free RAM in GB to allow running.",
    )
    parser.add_argument(
        "--max-cpu-percent",
        type=float,
        default=float(os.environ.get("CRAWLER_MAX_CPU_PERCENT", "85")),
        help="CPU % above which workers are reduced.",
    )
    parser.add_argument(
        "--cpu-measure-seconds",
        type=float,
        default=0.5,
    )
    parser.add_argument(
        "--workers-only",
        action="store_true",
        help="Print only the recommended workers number, then exit.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
    )
    args = parser.parse_args()

    cpu = read_cpu_percent(args.cpu_measure_seconds)
    mem_free = read_mem_free_gb()
    gpu_free = read_gpu_vram_free_gb()
    workers = recommend_workers(cpu, mem_free, args.max_workers, args.max_cpu_percent)
    should_run = mem_free >= args.min_free_ram_gb

    result = {
        "cpu_percent": cpu,
        "mem_free_gb": mem_free,
        "gpu_vram_free_gb": gpu_free,
        "workers_recommended": workers,
        "should_run": should_run,
    }

    if args.workers_only:
        print(workers)
    elif args.as_json:
        print(json.dumps(result))
    else:
        gpu_str = f" gpu_free={gpu_free}GB" if gpu_free is not None else ""
        status = "OK" if should_run else "SKIP"
        print(
            f"[resources] {status} cpu={cpu}% mem_free={mem_free}GB{gpu_str}"
            f" → workers={workers}",
            flush=True,
        )

    sys.exit(0 if should_run else 1)


if __name__ == "__main__":
    main()

"""CPU / RAM / GPU guards for long-running pipeline workers.

Used by story_quality_pipeline auto mode to avoid OOM crashes.
Defaults target Ollama qwen3:14b (~10GB VRAM) + headroom for OS.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float
    ram_free_mb: int
    vram_free_mb: int  # -1 if no GPU
    gpu_util_percent: int = -1

    def summary(self) -> str:
        parts = [f"CPU {self.cpu_percent:.0f}%", f"RAM {self.ram_free_mb}MB free"]
        if self.vram_free_mb >= 0:
            parts.append(f"VRAM {self.vram_free_mb}MB free")
        if self.gpu_util_percent >= 0:
            parts.append(f"GPU util {self.gpu_util_percent}%")
        return ", ".join(parts)


@dataclass(frozen=True)
class ResourceThresholds:
    min_vram_mb: int = 10240
    min_ram_mb: int = 4096
    max_cpu_percent: float = 85.0

    @classmethod
    def polish(cls) -> ResourceThresholds:
        return cls(
            min_vram_mb=_env_int("QUALITY_MIN_VRAM_MB", 10240),
            min_ram_mb=_env_int("QUALITY_MIN_RAM_MB", 4096),
            max_cpu_percent=_env_float("QUALITY_MAX_CPU_PCT", 85.0),
        )

    @classmethod
    def qa_deterministic(cls) -> ResourceThresholds:
        return cls(
            min_vram_mb=_env_int("QUALITY_QA_MIN_VRAM_MB", 2048),
            min_ram_mb=_env_int("QUALITY_QA_MIN_RAM_MB", 2048),
            max_cpu_percent=_env_float("QUALITY_QA_MAX_CPU_PCT", 90.0),
        )

    @classmethod
    def qa_llm(cls) -> ResourceThresholds:
        return cls.polish()

    @classmethod
    def tts_vieneu(cls) -> ResourceThresholds:
        """VieNeu v3 on CUDA — needs ~6GB VRAM + idle CPU."""
        return cls(
            min_vram_mb=_env_int("TTS_MIN_VRAM_MB", 6144),
            min_ram_mb=_env_int("TTS_MIN_RAM_MB", 6148),
            max_cpu_percent=_env_float("TTS_MAX_CPU_PCT", 70.0),
        )

    @classmethod
    def tts_cpu(cls) -> ResourceThresholds:
        """VieNeu on CPU — RAM-heavy; refuse to run when CPU already loaded."""
        return cls(
            min_vram_mb=0,
            min_ram_mb=_env_int("TTS_CPU_MIN_RAM_MB", 8192),
            max_cpu_percent=_env_float("TTS_CPU_MAX_CPU_PCT", 55.0),
        )


def read_cpu_percent(interval: float = 0.5) -> float:
    def read_stat() -> tuple[int, int]:
        with open("/proc/stat", encoding="utf-8") as f:
            parts = f.readline().split()
        vals = [int(x) for x in parts[1:]]
        idle = vals[3] + vals[4]
        return idle, sum(vals)

    idle1, total1 = read_stat()
    time.sleep(interval)
    idle2, total2 = read_stat()
    delta_total = total2 - total1
    if delta_total <= 0:
        return 0.0
    return round(100.0 * (1.0 - (idle2 - idle1) / delta_total), 1)


def read_ram_free_mb() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().available // (1024 * 1024))
    except Exception:
        mem: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
        kb = mem.get("MemAvailable", mem.get("MemFree", 0))
        return int(kb // 1024)


def read_vram_free_mb() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            timeout=10,
            text=True,
        )
        values = [int(v.strip()) for v in out.strip().splitlines() if v.strip().isdigit()]
        return min(values) if values else -1
    except Exception:
        return -1


def read_gpu_util_percent() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            timeout=10,
            text=True,
        )
        values = [int(v.strip()) for v in out.strip().splitlines() if v.strip().isdigit()]
        return max(values) if values else -1
    except Exception:
        return -1


def snapshot() -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_percent=read_cpu_percent(0.3),
        ram_free_mb=read_ram_free_mb(),
        vram_free_mb=read_vram_free_mb(),
        gpu_util_percent=read_gpu_util_percent(),
    )


def check_safe(
    snap: ResourceSnapshot,
    thresholds: ResourceThresholds,
    *,
    require_gpu: bool = True,
    max_gpu_util_percent: float = 0.0,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if snap.ram_free_mb < thresholds.min_ram_mb:
        reasons.append(f"RAM {snap.ram_free_mb}MB < {thresholds.min_ram_mb}MB")
    if snap.cpu_percent > thresholds.max_cpu_percent:
        reasons.append(f"CPU {snap.cpu_percent:.0f}% > {thresholds.max_cpu_percent:.0f}%")
    if thresholds.min_vram_mb > 0:
        if snap.vram_free_mb < 0:
            if require_gpu:
                reasons.append("no GPU / nvidia-smi unavailable")
        elif snap.vram_free_mb < thresholds.min_vram_mb:
            reasons.append(f"VRAM {snap.vram_free_mb}MB < {thresholds.min_vram_mb}MB")
    if max_gpu_util_percent > 0 and snap.gpu_util_percent >= 0:
        if snap.gpu_util_percent > max_gpu_util_percent:
            reasons.append(f"GPU util {snap.gpu_util_percent}% > {max_gpu_util_percent:.0f}%")
    return not reasons, reasons


def ollama_loaded_models(base_url: str) -> list[str]:
    if not requests:
        return []
    try:
        resp = requests.get(base_url.rstrip("/") + "/api/ps", timeout=10)
        if resp.ok:
            return [str(m.get("name", "")) for m in resp.json().get("models", [])]
    except Exception:
        pass
    return []


def unload_ollama_model(base_url: str, model: str, log: LogFn = _default_log) -> None:
    if not requests or not model:
        return
    try:
        requests.post(
            base_url.rstrip("/") + "/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=30,
        )
        log(f"[RESOURCE] unload requested: {model}")
    except Exception as exc:
        log(f"[RESOURCE] could not unload {model}: {exc}")


def unload_all_ollama_models(base_url: str, log: LogFn = _default_log) -> None:
    for model in ollama_loaded_models(base_url):
        if model:
            unload_ollama_model(base_url, model, log=log)


def _pgrep_python_lines() -> list[str]:
    try:
        out = subprocess.check_output(["pgrep", "-af", "python"], text=True, timeout=5)
        my_pid = str(os.getpid())
        lines: list[str] = []
        for line in out.splitlines():
            pid = line.split(None, 1)[0] if line.strip() else ""
            if pid == my_pid:
                continue
            lines.append(line)
        return lines
    except Exception:
        return []


def competing_ollama_users() -> list[str]:
    """Processes that reload Ollama models and block TTS VRAM."""
    patterns = (
        "polish_worker",
        "translate_chapters_from_db",
        "repolish_story_from_db",
        "crawl_skydemonorder_chapters",
        "story_quality_pipeline",
    )
    found: list[str] = []
    for line in _pgrep_python_lines():
        for pat in patterns:
            if pat in line:
                found.append(pat)
                break
        else:
            if "crawl_" in line and "chapters" in line and "qwen3" in line:
                found.append("crawl_translate")
    return found


def competing_gpu_workers() -> list[str]:
    """Other heavy pipeline processes that should finish before TTS."""
    patterns = (
        "audio_segment_worker",
        "audio_worker_vieneu",
        "audio_worker_viterbox",
        "generate_chapter_audio",
    )
    found: list[str] = []
    for line in _pgrep_python_lines():
        if "story_quality_pipeline" in line:
            continue
        for pat in patterns:
            if pat in line:
                found.append(pat)
                break
    return found


def wait_until_safe(
    thresholds: ResourceThresholds,
    *,
    label: str = "",
    poll_seconds: int = 30,
    max_wait_seconds: int = 0,
    ollama_url: str = "",
    unload_models: list[str] | None = None,
    unload_all_ollama: bool = False,
    wait_for_workers: bool = True,
    wait_for_ollama_users: bool = True,
    require_gpu: bool = True,
    max_gpu_util_percent: float = 0.0,
    log: LogFn = _default_log,
) -> ResourceSnapshot:
    """Block until CPU/RAM/VRAM are safe. max_wait_seconds=0 → wait indefinitely."""
    prefix = f"[RESOURCE:{label}] " if label else "[RESOURCE] "
    started = time.monotonic()
    deadline = started + max_wait_seconds if max_wait_seconds > 0 else None
    unload_models = unload_models or []

    while True:
        if unload_all_ollama and ollama_url:
            unload_all_ollama_models(ollama_url, log=log)
        else:
            for model in unload_models:
                loaded = ollama_loaded_models(ollama_url)
                if any(model in m for m in loaded):
                    unload_ollama_model(ollama_url, model, log=log)
                    time.sleep(5)

        worker_hits = competing_gpu_workers() if wait_for_workers else []
        ollama_hits = competing_ollama_users() if wait_for_ollama_users else []
        snap = snapshot()
        ok, reasons = check_safe(
            snap,
            thresholds,
            require_gpu=require_gpu,
            max_gpu_util_percent=max_gpu_util_percent,
        )
        if worker_hits:
            ok = False
            reasons.append(f"workers running: {', '.join(sorted(set(worker_hits)))}")
        if ollama_hits:
            ok = False
            reasons.append(f"ollama users: {', '.join(sorted(set(ollama_hits)))}")

        if ok:
            log(f"{prefix}OK — {snap.summary()}")
            return snap

        elapsed = int(time.monotonic() - started)
        if deadline is not None and time.monotonic() >= deadline:
            raise RuntimeError(
                f"{prefix}timeout {max_wait_seconds}s — {'; '.join(reasons)} ({snap.summary()})"
            )

        log(f"{prefix}wait {poll_seconds}s — {'; '.join(reasons)} ({snap.summary()}, {elapsed}s)")
        time.sleep(poll_seconds)

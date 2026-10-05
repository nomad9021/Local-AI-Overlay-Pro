"""
GPU stats abstraction — NVIDIA (pynvml) with best-effort AMD fallback,
cross-platform (Windows + Linux).

Usage:
    reader = get_gpu_reader()
    stats = reader.read()   # GPUStats, or GPUStats(available=False) if nothing worked

NVIDIA is tried first on any OS via pynvml (nvidia-ml-py package) since it's
reliable and identical cross-platform. AMD support is genuinely best-effort:
- Linux: amdsmi/pyrsmi (require ROCm installed) or a rocm-smi CLI fallback.
- Windows: pyadl gives load% only; VRAM comes from WMI GPU perf counters,
  which are known to read 0 on a cold first query and have a documented
  Microsoft bug on some Windows 10 builds affecting accuracy. Treat Windows+AMD
  VRAM numbers as unverified until checked against real hardware.
"""
import platform
import subprocess
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class GPUStats:
    name: str = "GPU"
    gpu_pct: float = 0.0
    vram_used_gb: float = 0.0
    vram_total_gb: float = 0.0
    available: bool = True


class GPUReader(ABC):
    @abstractmethod
    def init(self) -> bool:
        """Return True if this reader can be used on this machine."""

    @abstractmethod
    def read(self) -> "GPUStats | None":
        """Return current stats, or None on transient read failure."""

    def process_sm_utilization(self) -> "dict | None":
        """Per-PID GPU SM utilization percentages, or None if unsupported.

        Optional: only the NVIDIA reader implements it. model_state.py uses it
        to tell which Ollama model runner is generating when a runner's /slots
        endpoint isn't available, so returning None just costs that fallback.
        """
        return None

    def shutdown(self) -> None:
        pass


class NvidiaGPUReader(GPUReader):
    """All NVIDIA GPUs on the machine, aggregated into one GPUStats.

    Multi-GPU boxes (common on the servers the agent runs on) report the mean
    utilization and the summed VRAM across every device, with the name prefixed
    by the device count — Ollama spreads large models across GPUs, so a
    single-device reading would understate what's in use.
    """
    # Lookback for per-process utilization samples; must exceed the monitor's
    # poll interval so a generating runner is never missed between ticks.
    PROC_LOOKBACK_S = 2.5

    def __init__(self):
        self._pynvml = None
        self._handles = []

    def init(self) -> bool:
        try:
            import pynvml
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in range(count)]
            if not handles:
                return False
            self._pynvml = pynvml
            self._handles = handles
            return True
        except Exception:
            self._pynvml = None
            self._handles = []
            return False

    def read(self) -> "GPUStats | None":
        if self._pynvml is None:
            return None
        try:
            nvml = self._pynvml
            utils, used, total = [], 0, 0
            for handle in self._handles:
                utils.append(float(nvml.nvmlDeviceGetUtilizationRates(handle).gpu))
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                used += mem.used
                total += mem.total
            name = nvml.nvmlDeviceGetName(self._handles[0])
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if len(self._handles) > 1:
                name = f"{len(self._handles)}x {name}"
            return GPUStats(
                name=name,
                gpu_pct=sum(utils) / len(utils),
                vram_used_gb=used / (1024 ** 3),
                vram_total_gb=total / (1024 ** 3),
                available=True,
            )
        except Exception:
            return None

    def process_sm_utilization(self) -> "dict | None":
        """{pid: sm_percent} for processes that used any GPU very recently.

        NVML only returns samples newer than the timestamp passed in, and an
        idle process produces no sample at all — so a PID missing from the
        result means "not generating", which is exactly the signal wanted here.
        A lookback slightly longer than the poll interval keeps a busy runner
        from flickering to absent between ticks. Raises NVML_ERROR_NOT_FOUND
        when nothing at all touched the GPU, which is a normal idle state, not
        an error worth surfacing.
        """
        if self._pynvml is None:
            return None
        since_us = int((time.time() - self.PROC_LOOKBACK_S) * 1_000_000)
        usage = {}
        answered = False
        for handle in self._handles:
            try:
                samples = self._pynvml.nvmlDeviceGetProcessUtilization(handle, since_us)
                answered = True
            except Exception:
                continue
            for sample in samples or []:
                try:
                    pid = int(sample.pid)
                    usage[pid] = max(int(sample.smUtil), usage.get(pid, 0))
                except Exception:
                    continue
        return usage if answered else None

    def shutdown(self) -> None:
        if self._pynvml is not None:
            try:
                self._pynvml.nvmlShutdown()
            except Exception:
                pass


class AMDGPUReaderLinux(GPUReader):
    def __init__(self):
        self._backend = None  # "amdsmi" | "rocm-smi-cli"
        self._amdsmi = None
        self._handle = None

    def init(self) -> bool:
        try:
            import amdsmi
            amdsmi.amdsmi_init()
            handles = amdsmi.amdsmi_get_processor_handles()
            if handles:
                self._amdsmi = amdsmi
                self._handle = handles[0]
                self._backend = "amdsmi"
                return True
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["rocm-smi", "--showuse", "--showmeminfo", "vram", "--json"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                self._backend = "rocm-smi-cli"
                return True
        except Exception:
            pass

        return False

    def read(self) -> "GPUStats | None":
        if self._backend == "amdsmi":
            return self._read_amdsmi()
        if self._backend == "rocm-smi-cli":
            return self._read_rocm_smi_cli()
        return None

    def _read_amdsmi(self) -> "GPUStats | None":
        try:
            amdsmi = self._amdsmi
            engine_usage = amdsmi.amdsmi_get_gpu_activity(self._handle)
            vram_info = amdsmi.amdsmi_get_gpu_vram_usage(self._handle)
            name = amdsmi.amdsmi_get_gpu_asic_info(self._handle).get("market_name", "AMD GPU")
            return GPUStats(
                name=name,
                gpu_pct=float(engine_usage.get("gfx_activity", 0)),
                vram_used_gb=vram_info.get("vram_used", 0) / 1024,
                vram_total_gb=vram_info.get("vram_total", 0) / 1024,
                available=True,
            )
        except Exception:
            return None

    def _read_rocm_smi_cli(self) -> "GPUStats | None":
        try:
            result = subprocess.run(
                ["rocm-smi", "--showuse", "--showmeminfo", "vram", "--json"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            card = next(iter(data.values()))
            gpu_pct = float(card.get("GPU use (%)", 0))
            used_bytes = float(card.get("VRAM Total Used Memory (B)", 0))
            total_bytes = float(card.get("VRAM Total Memory (B)", 0))
            return GPUStats(
                name="AMD GPU",
                gpu_pct=gpu_pct,
                vram_used_gb=used_bytes / (1024 ** 3),
                vram_total_gb=total_bytes / (1024 ** 3),
                available=True,
            )
        except Exception:
            return None


class AMDGPUReaderWindows(GPUReader):
    """Best-effort: pyadl for load%, WMI perf counters for VRAM.

    VRAM readings via WMI are unverified against real AMD+Windows hardware —
    known to read 0 on a cold first query and to have a documented Microsoft
    accuracy bug on some Windows 10 builds. If VRAM can't be read reliably,
    only gpu_pct is populated and vram fields stay 0 with available=True
    (partial data is still better than nothing for the load% row).
    """

    def __init__(self):
        self._pyadl_device = None
        self._wmi_conn = None

    def init(self) -> bool:
        ok = False
        try:
            from pyadl import ADLManager
            devices = ADLManager.getInstance().getDevices()
            if devices:
                self._pyadl_device = devices[0]
                ok = True
        except Exception:
            self._pyadl_device = None

        try:
            import wmi
            self._wmi_conn = wmi.WMI(namespace="root\\CIMV2")
        except Exception:
            self._wmi_conn = None

        return ok or self._wmi_conn is not None

    def read(self) -> "GPUStats | None":
        gpu_pct = 0.0
        vram_used_gb = 0.0
        vram_total_gb = 0.0

        if self._pyadl_device is not None:
            try:
                gpu_pct = float(self._pyadl_device.getCurrentUsage())
            except Exception:
                pass

        if self._wmi_conn is not None:
            try:
                counters = self._wmi_conn.Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory()
                used = sum(int(c.DedicatedUsage) for c in counters if c.DedicatedUsage)
                if used > 0:
                    vram_used_gb = used / (1024 ** 3)
            except Exception:
                pass

        if gpu_pct == 0.0 and vram_used_gb == 0.0:
            return None

        return GPUStats(
            name="AMD GPU",
            gpu_pct=gpu_pct,
            vram_used_gb=vram_used_gb,
            vram_total_gb=vram_total_gb,
            available=True,
        )


class NullGPUReader(GPUReader):
    def init(self) -> bool:
        return True

    def read(self) -> "GPUStats | None":
        return GPUStats(available=False)


def get_gpu_reader() -> GPUReader:
    """Detect and return the first usable GPU reader: NVIDIA -> AMD -> Null."""
    candidates = [NvidiaGPUReader()]

    system = platform.system()
    if system == "Linux":
        candidates.append(AMDGPUReaderLinux())
    elif system == "Windows":
        candidates.append(AMDGPUReaderWindows())

    for candidate in candidates:
        try:
            if candidate.init():
                return candidate
        except Exception:
            continue

    return NullGPUReader()

# -*- coding: utf-8 -*-
"""多管件并行开模调度器 (batch_runner.py)

设计要点（为什么是"进程池"而不是"线程池"）
------------------------------------------------
1. **隔离**：OCCT 的布尔运算/几何内核带大量全局状态，线程间并发不安全。
   本调度器让每个任务跑在**独立子进程**里（`mold_worker.py`），进程间只用文件通信：
   一个任务把 OCC 跑崩（段错误）或跑死（布尔卡住），只是**该进程退出**，
   主进程、Web 服务、其它任务全部不受影响 —— 这是线程池给不了的。
2. **真并行**：任务是 CPU 密集型（布尔 + 曲面三角化），Python 线程受 GIL 限制
   只能并发 IO；子进程各占一个物理核，才是真正的墙钟时间缩短。
3. **资源不打架**：
   · 每进程强制 `OMP_NUM_THREADS=1` 等线程上限 —— 否则 N 个进程 × 16 线程会互相抢占，
     反而比串行更慢（线程颠簸）；
   · worker 数量同时受**物理核数**与**可用内存**约束（OCC 单进程常驻数百 MB）；
   · 每任务独立输出目录 + 独立 ASCII 临时文件名（uuid），无写冲突；
   · 每任务独立 stdout/stderr 日志文件，不共享文件描述符。
4. **可终止**：单任务超时直接 terminate 进程，OCC 卡死不再拖住整批。

对外接口:
    jobs = [MoldJob(...), ...]
    outcomes = run_jobs(jobs, workers=None, work_root=None, progress=cb)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
WORKER_SCRIPT = PROJECT_ROOT / "mold_worker.py"

DEFAULT_TIMEOUT_S = float(os.environ.get("MOLD_JOB_TIMEOUT", "900"))
MAX_WORKERS_CAP = int(os.environ.get("MOLD_MAX_WORKERS", "6"))
PER_WORKER_RAM_MB = int(os.environ.get("MOLD_PER_WORKER_RAM_MB", "1200"))

# Windows: 子进程不要弹黑框
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 跨批次全局并发闸门：多个批次同时提交时，全进程总数仍不超过 MAX_WORKERS_CAP，
# 否则「2 批 × 6 进程」= 12 个 OCC 进程抢 8 个物理核，比串行还慢。
_GLOBAL_LOCK = threading.Lock()
_GLOBAL_SEM: Optional[threading.BoundedSemaphore] = None


def _global_slots() -> threading.BoundedSemaphore:
    global _GLOBAL_SEM
    with _GLOBAL_LOCK:
        if _GLOBAL_SEM is None:
            _GLOBAL_SEM = threading.BoundedSemaphore(max(1, MAX_WORKERS_CAP))
        return _GLOBAL_SEM


# --------------------------------------------------------------------------- 环境探测
def available_ram_mb() -> Optional[int]:
    """可用物理内存（MB）；取不到返回 None。避免 psutil 依赖。"""
    try:
        if os.name == "nt":
            import ctypes

            class _MEMSTATUS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _MEMSTATUS()
            st.dwLength = ctypes.sizeof(_MEMSTATUS)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return int(st.ullAvailPhys // (1024 * 1024))
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:                       # noqa: BLE001
        return None
    return None


def physical_cores() -> int:
    """物理核数估计（超线程按 2 折算），至少 1。"""
    logical = os.cpu_count() or 2
    return max(1, logical // 2)


def plan_workers(n_jobs: int, max_workers: Optional[int] = None) -> int:
    """按「任务数 / 物理核 / 可用内存 / 上限」四项取最小，得出并发数。"""
    if n_jobs <= 1:
        return 1
    limits = [n_jobs, physical_cores(), MAX_WORKERS_CAP]
    ram = available_ram_mb()
    if ram:
        limits.append(max(1, int(ram // PER_WORKER_RAM_MB)))
    if max_workers:
        limits.append(int(max_workers))
    return max(1, min(int(v) for v in limits))


def _worker_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """子进程环境：单线程数学库（避免线程颠簸）+ UTF-8 + 项目路径。"""
    env = os.environ.copy()
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        env[k] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    paths = [str(PROJECT_ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.pop("MOLD_WORKER_DEPTH", None)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


# --------------------------------------------------------------------------- 数据模型
@dataclass
class MoldJob:
    """一个待生成任务（一个管件 = 一个任务）。"""

    job_id: str
    part_path: str
    prefix: str
    output_dir: str
    margin: Tuple[float, float, float] = (20.0, 20.0, 20.0)
    parting_surface: str = "plane"
    parting_z: Optional[float] = None
    verify: bool = False
    with_core: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    label: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["margin"] = list(self.margin)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MoldJob":
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        if "margin" in kw:
            m = list(kw["margin"])
            kw["margin"] = tuple(float(v) for v in (m + [20.0] * 3)[:3])
        return cls(**kw)


@dataclass
class JobOutcome:
    """一个任务的执行结果（成功 / 失败 / 超时 / 崩溃，四态之一）。"""

    job_id: str
    prefix: str = ""
    label: str = ""
    state: str = "failed"            # done | failed | timeout | crashed
    ok: bool = False
    elapsed_s: float = 0.0
    out_dir: str = ""
    paths: List[dict] = field(default_factory=list)
    summary: Dict[str, object] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)
    error: Optional[str] = None
    returncode: Optional[int] = None
    log_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- 调度
def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except Exception:                       # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:                       # noqa: BLE001
        pass


def _tail(path: str, lines: int = 40) -> List[str]:
    """取日志尾部（只读最后 64 KB，避免大日志拖慢轮询）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > 65536:
                fh.seek(size - 65536)
            data = fh.read().decode("utf-8", errors="replace")
        return [ln.rstrip() for ln in data.splitlines() if ln.strip()][-lines:]
    except OSError:
        return []


def tail_log(path: str, lines: int = 40) -> List[str]:
    return _tail(path, lines)


def _run_one(job: MoldJob, index: int, total: int, work_root: Path,
             progress: Optional[Callable[[dict], None]],
             extra_env: Optional[Dict[str, str]] = None) -> JobOutcome:
    job_dir = work_root / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job_json = job_dir / "job.json"
    result_json = job_dir / "result.json"
    log_path = job_dir / "worker.log"

    with open(job_json, "w", encoding="utf-8") as fh:
        json.dump(job.to_dict(), fh, ensure_ascii=False, indent=2)
    if result_json.exists():
        result_json.unlink()

    # 全局名额：拿不到就一直等（此时前端看到的是"排队"而不是"运行中"）
    sem = _global_slots()
    with sem:
        return _run_one_locked(job, index, total, progress, extra_env,
                               job_json, result_json, log_path)


def _run_one_locked(job: MoldJob, index: int, total: int,
                    progress: Optional[Callable[[dict], None]],
                    extra_env: Optional[Dict[str, str]],
                    job_json: Path, result_json: Path, log_path: Path) -> JobOutcome:
    if progress:
        try:
            progress({"event": "start", "job_id": job.job_id, "label": job.label,
                      "index": index, "total": total, "state": "running"})
        except Exception:                   # noqa: BLE001
            pass

    cmd = [sys.executable, "-X", "utf8", str(WORKER_SCRIPT), "--job", str(job_json)]
    t0 = time.perf_counter()
    timed_out = False
    rc: Optional[int] = None
    with open(log_path, "wb") as log_fh:
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), stdout=log_fh, stderr=subprocess.STDOUT,
            env=_worker_env(extra_env), creationflags=_CREATE_NO_WINDOW,
        )
        try:
            rc = proc.wait(timeout=(job.timeout_s or None))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            rc = proc.returncode
        except KeyboardInterrupt:           # 主进程被中断 -> 别留下孤儿进程
            _kill(proc)
            raise
    elapsed = round(time.perf_counter() - t0, 1)

    payload: Dict[str, object] = {}
    if result_json.exists():
        try:
            with open(result_json, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            payload = {}

    if timed_out:
        state, ok = "timeout", False
        error = (f"超出 {job.timeout_s:.0f}s 未完成，已终止该工作进程"
                 f"（成品可能仍在 OCC 布尔里卡住；可加大 timeout 或改用 --no-verify）")
    elif rc != 0 or not payload.get("ok"):
        state, ok = "failed", False
        error = (payload.get("error")
                 or f"工作进程异常退出（returncode={rc}），详见日志")
    else:
        state, ok = "done", True
        error = None

    outcome = JobOutcome(
        job_id=job.job_id,
        prefix=str(payload.get("prefix") or job.prefix or ""),
        label=job.label or job.prefix,
        state=state,
        ok=ok,
        elapsed_s=float(payload.get("elapsed_s") or elapsed),
        out_dir=str(payload.get("out_dir") or job.output_dir),
        paths=list(payload.get("files") or []),
        summary=dict(payload.get("summary") or {}),
        warnings=list(payload.get("warnings") or []),
        metadata=dict(payload.get("metadata") or {}),
        error=error,
        returncode=rc,
        log_path=str(log_path),
    )
    if progress:
        try:
            progress({"event": "done", "job_id": job.job_id, "label": outcome.label,
                      "index": index, "total": total, "state": state,
                      "ok": ok, "elapsed_s": outcome.elapsed_s, "error": error})
        except Exception:                   # noqa: BLE001
            pass
    return outcome


def run_jobs(jobs: List[MoldJob],
             workers: Optional[int] = None,
             work_root: Optional[str] = None,
             progress: Optional[Callable[[dict], None]] = None,
             extra_env: Optional[Dict[str, str]] = None,
             cleanup: bool = False) -> List[JobOutcome]:
    """并行执行一批模具生成任务，返回与 `jobs` **同序**的结果列表。

    参数:
        workers:   并发进程数；None = 自动（见 `plan_workers`）。
        work_root: 控制文件根目录（job.json / worker.log / result.json）；
                   None = 临时目录。**不用于放成品**（成品在 job.output_dir）。
        progress:  回调，事件 dict（start / done），供 Web 层刷新进度。
        cleanup:   结束后删除控制目录（日志随之删除；调试时保持 False）。
    """
    if not jobs:
        return []

    n = len(jobs)
    w = plan_workers(n, workers)
    root = Path(work_root) if work_root else Path(tempfile.mkdtemp(prefix="mold_batch_"))
    root.mkdir(parents=True, exist_ok=True)

    if progress:
        try:
            progress({"event": "batch_start", "total": n, "workers": w,
                      "work_root": str(root)})
        except Exception:                   # noqa: BLE001
            pass

    outcomes: Dict[str, JobOutcome] = {}
    lock = threading.Lock()

    def _task(job: MoldJob, idx: int) -> None:
        try:
            out = _run_one(job, idx, n, root, progress, extra_env)
        except Exception as exc:            # noqa: BLE001 —— 调度层兜底，绝不让单任务拖垮整批
            out = JobOutcome(job_id=job.job_id, prefix=job.prefix,
                             label=job.label or job.prefix, state="crashed",
                             error=f"调度异常: {type(exc).__name__}: {exc}")
        with lock:
            outcomes[job.job_id] = out

    if w == 1:
        for i, job in enumerate(jobs):
            _task(job, i)
    else:
        with ThreadPoolExecutor(max_workers=w, thread_name_prefix="mold-sup") as pool:
            list(pool.map(lambda p: _task(p[1], p[0]), list(enumerate(jobs))))

    ordered = [outcomes[j.job_id] for j in jobs if j.job_id in outcomes]

    if progress:
        done = sum(1 for o in ordered if o.ok)
        try:
            progress({"event": "batch_done", "total": n, "workers": w,
                      "done": done, "failed": n - done,
                      "wall_s": round(sum(o.elapsed_s for o in ordered), 1)})
        except Exception:                   # noqa: BLE001
            pass

    if cleanup:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    return ordered


def batch_summary(outcomes: List[JobOutcome], wall_s: float) -> dict:
    """汇总统计：串行耗时估算 vs 实际墙钟，给出加速比。"""
    total_cpu = round(sum(o.elapsed_s for o in outcomes), 1)
    done = sum(1 for o in outcomes if o.ok)
    return {
        "total": len(outcomes),
        "done": done,
        "failed": len(outcomes) - done,
        "serial_est_s": total_cpu,           # 逐件串行大致耗时（各件耗时之和）
        "wall_s": round(wall_s, 1),
        "speedup": round(total_cpu / wall_s, 2) if wall_s > 0 else None,
    }


if __name__ == "__main__":
    # 自测：对同一件做 2 次并行（仅验证调度链路可用）
    import argparse

    ap = argparse.ArgumentParser(description="批量开模调度器自测")
    ap.add_argument("--part", required=True)
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--out", default="./_batch_selftest")
    a = ap.parse_args()

    items = [MoldJob(job_id=f"selftest_{i}", part_path=os.path.abspath(a.part),
                     prefix=f"selftest{i}", output_dir=os.path.join(a.out, f"j{i}"))
             for i in range(a.n)]
    t0 = time.perf_counter()
    res = run_jobs(items, workers=a.workers, progress=lambda e: print(e))
    print(json.dumps(batch_summary(res, time.perf_counter() - t0),
                     ensure_ascii=False, indent=2))

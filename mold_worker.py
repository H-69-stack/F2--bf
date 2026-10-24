# -*- coding: utf-8 -*-
"""单件模具生成工作进程 (mold_worker.py)

一个进程只做**一件事**：读一份 job.json -> 跑完整条管件开模管线 -> 写 result.json。

为什么要有这一层：
  * OCCT 的布尔运算**不是线程安全的**，同进程多线程并发跑开模会互相污染
    （本工程在 `--parallel` 上已实测到"偶有不稳"）。用**独立进程**跑，每个进程
    有自己的 OCC 内核实例、自己的堆，进程间零共享 -> 天然隔离。
  * 单件生成被拖到独立进程后，主 Web 服务不再加载几何 -> 某件把 OCC 跑崩/跑死
    只会让**这一个工作进程**退出，服务照常运行，其余任务不受影响。
  * 进程间只通过**文件**交换数据（STEP / JSON），无可序列化几何对象问题
    （TopoDS_Shape 无法 pickle）。

调用方式（由 batch_runner 拉起，一般不用手敲）：
    python mold_worker.py --job <控制目录>/job.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 产出文件清单（顺序即前端展示顺序）：(paths 里的 key, 名称, 说明)
FILE_SPEC = (
    ("upper", "上模", "型腔内壁 = 管件外壁"),
    ("lower", "下模", "与上模在同一分模面处切开"),
    ("core_pin", "芯棒", "内孔全长、端面与管件端口齐平"),
    ("mold4_assembly", "四件套装配体", "上模 + 产品 + 芯棒 + 下模（合模状态）"),
    ("metadata", "元数据 JSON", "分模高度 / 体积 / 耗时 / 警告"),
)


def _write_json(path: str, obj) -> None:
    """原子写 JSON（先写 .tmp 再 replace，避免父进程读到半个文件）。"""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def _file_entries(paths: dict) -> list:
    """把导出路径整理成可下载条目（url 由 Web 层补，worker 不管路由）。"""
    out = []
    for key, label, note in FILE_SPEC:
        p = paths.get(key)
        if not p or not os.path.isfile(p):
            continue
        try:
            size_mb = round(os.path.getsize(p) / 1048576.0, 2)
        except OSError:
            size_mb = 0.0
        out.append({"key": key, "label": label, "note": note,
                    "name": os.path.basename(p),
                    "path": os.path.abspath(p), "size_mb": size_mb})
    return out


def _summarize(product, pipe, meta: dict) -> dict:
    """前端 `/api/mold_task` 用的摘要（字段与旧版 _work() 保持一致）。"""
    return {
        "product_size_mm": [round(v, 2) for v in
                            (product.bbox[3] - product.bbox[0],
                             product.bbox[4] - product.bbox[1],
                             product.bbox[5] - product.bbox[2])],
        "product_volume_mm3": round(float(product.volume), 1),
        "core_volume_mm3": round(float(pipe.core_volume), 1),
        "upper_volume_mm3": round(float(pipe.upper_volume), 1),
        "lower_volume_mm3": round(float(pipe.lower_volume), 1),
        "parting_kind": pipe.parting_kind,
        "parting_z_mm": round(float(pipe.parting_z_mean), 2),
        "trim_via": (meta.get("pipe_stats", {}) or {}).get("envelope", {}).get("via"),
        "upper_height_mm": meta.get("upper_mold_height_mm"),
        "lower_height_mm": meta.get("lower_mold_height_mm"),
    }


def run_job(job: dict) -> dict:
    """执行一个模具生成任务，返回结果载荷（不写盘，便于进程内复用/单测）。"""
    from main import generate_mold          # 延迟导入：让进程启动阶段尽量轻

    out_dir = os.path.abspath(job["output_dir"])
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.perf_counter()
    res = generate_mold(
        part_path=job["part_path"],
        margin=tuple(float(v) for v in (job.get("margin") or (20.0, 20.0, 20.0))),
        output_dir=out_dir,
        prefix=job.get("prefix") or "mold",
        parting_surface=job.get("parting_surface") or "plane",
        parting_z=(None if job.get("parting_z") in (None, "", "auto")
                   else float(job["parting_z"])),
        verify=bool(job.get("verify", False)),
        with_core=bool(job.get("with_core", True)),
    )
    elapsed = round(time.perf_counter() - t0, 1)

    pipe = res["pipe"]
    product = res["product"]
    meta = {}
    mpath = (res.get("paths") or {}).get("metadata")
    if mpath and os.path.isfile(mpath):
        try:
            with open(mpath, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}

    return {
        "ok": True,
        "job_id": job["job_id"],
        "prefix": job.get("prefix"),
        "elapsed_s": elapsed,
        "out_dir": out_dir,
        "files": _file_entries(res.get("paths") or {}),
        "summary": _summarize(product, pipe, meta),
        "warnings": list(pipe.warnings or []),
        "metadata": meta,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="单件管件开模工作进程")
    parser.add_argument("--job", required=True, help="job.json 路径")
    parser.add_argument("--result", default=None,
                        help="result.json 输出路径（默认与 job.json 同目录）")
    args = parser.parse_args(argv)

    job_path = os.path.abspath(args.job)
    result_path = args.result or os.path.join(os.path.dirname(job_path), "result.json")

    with open(job_path, encoding="utf-8") as fh:
        job = json.load(fh)

    from io_utils import ensure_utf8_stdout
    ensure_utf8_stdout()
    print(f"[worker {os.getpid()}] 开始: {job.get('prefix')} <- {job.get('part_path')}",
          flush=True)
    try:
        payload = run_job(job)
        print(f"[worker {os.getpid()}] 完成: {payload['prefix']}，{payload['elapsed_s']} s",
              flush=True)
        _write_json(result_path, payload)
        return 0
    except Exception as exc:                       # noqa: BLE001 —— 任何异常都要落成结果文件
        tb = traceback.format_exc()
        print(tb, flush=True)
        _write_json(result_path, {
            "ok": False,
            "job_id": job.get("job_id"),
            "prefix": job.get("prefix"),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": tb,
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

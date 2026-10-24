# -*- coding: utf-8 -*-
"""管件硬模成型 —— 极简 Web 服务 (web_box_generator/app.py)

只做三件事（**没有三维查看**，不加载 three.js）：
    1. 上传产品数模（STEP / IGES / STL）
    2. 生成管件模具 -> 上模 / 下模 / 芯棒 / 四件套装配体，逐个下载
    3. 镜像 / 旋转模型 -> 下载；另可"只算推荐分模面高度"（不跑布尔）

启动：
    venv\\Scripts\\python app.py --port 5002
或双击项目根目录的 启动Web服务.bat，浏览器打开 http://127.0.0.1:5002

接口一览：
    GET  /                       页面
    POST /api/upload             上传数模 -> {file, original_name, part_number, size_mb}
    POST /api/mold_generate      开工（异步）-> {task_id}
    POST /api/pipe_plane_suggest 只算推荐分模面高度（异步）-> {task_id}
    GET  /api/task_status?id=    轮询进度 / 结果 / 日志
    GET  /api/download?task=&file=  下载生成件（只允许任务自己的输出目录）
    POST /api/generate_mirror    镜像/旋转（同步）-> {name, url, ...}
    GET  /api/download_mirror?name= 下载镜像/旋转结果
"""

from __future__ import annotations

import argparse
import contextlib
import io as _io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid

from flask import Flask, jsonify, render_template, request, send_file

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from batch_runner import (MoldJob, available_ram_mb, physical_cores,   # noqa: E402
                         plan_workers, run_jobs, tail_log)
from io_utils import ensure_utf8_stdout                 # noqa: E402
from naming_utils import part_number_from_filename      # noqa: E402

ensure_utf8_stdout()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024      # 单文件上限 300 MB

UPLOAD_DIR = os.path.join(_HERE, "uploads")
OUT_ROOT = os.path.join(UPLOAD_DIR, "output")
MIRROR_DIR = os.path.join(UPLOAD_DIR, "mirror_output")
for _d in (UPLOAD_DIR, OUT_ROOT, MIRROR_DIR):
    os.makedirs(_d, exist_ok=True)

ALLOWED_EXTS = (".stp", ".step", ".iges", ".igs", ".stl")
_ERROR_LOG = os.path.join(_HERE, "error.log")


# --------------------------------------------------------------------------- 工具
def _log_error(text: str) -> None:
    """把异常写进 error.log（同时打一份到控制台），方便事后排查。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {stamp} =====\n{text}\n")
    except OSError:
        pass
    try:
        print(f"[错误] {text.splitlines()[0] if text else ''}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _json_safe(obj):
    """把 numpy / OCC 之类"不是标准 JSON 类型"的东西转成普通类型。"""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if obj == obj and abs(obj) != float("inf") else None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    try:                                     # numpy 标量/数组等
        import numpy as np

        if isinstance(obj, np.ndarray):
            return _json_safe(obj.tolist())
        if isinstance(obj, np.generic):
            return _json_safe(obj.item())
    except Exception:  # noqa: BLE001
        pass
    return str(obj)


def _safe_name(name: str, fallback: str = "part") -> str:
    """文件名安全化（去掉路径分隔符与 Windows 非法字符）。"""
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    return name or fallback


def _uploaded_path(name: str) -> str:
    """把"上传文件名"解析成 uploads 目录下的绝对路径（越界返回空串）。"""
    safe = os.path.basename(str(name or ""))
    if not safe:
        return ""
    path = os.path.join(UPLOAD_DIR, safe)
    if os.path.isfile(path):
        return path
    return ""


def _human_size(path: str) -> float:
    try:
        return round(os.path.getsize(path) / 1048576.0, 2)
    except OSError:
        return 0.0


# --------------------------------------------------------------------------- 异步任务
_TASKS: dict = {}
_TASK_KEEP = 12
_NOISE = ("******", " Step File Name", "** WorkSession", "** Transferring")


def _task_new(label: str) -> str:
    tid = uuid.uuid4().hex[:12]
    _TASKS[tid] = {"id": tid, "label": label, "state": "running",
                   "t0": time.time(), "t1": None, "log": _io.StringIO(),
                   "log_file": None, "result": None, "error": None}
    # 只保留最近若干个已完成任务
    while len(_TASKS) > _TASK_KEEP:
        done = [t for t in _TASKS.values() if t["state"] != "running"]
        if not done:
            break
        _TASKS.pop(min(done, key=lambda t: t["t0"])["id"], None)
    return tid


def _task_start(tid: str, fn) -> None:
    def _run():
        t = _TASKS[tid]
        try:
            with contextlib.redirect_stdout(t["log"]):
                result = fn()
            t["result"] = _json_safe(result)
            t["state"] = "done"
        except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成任务错误，不能只留在线程里
            tb = traceback.format_exc()
            t["error"] = f"{type(exc).__name__}: {exc}"
            t["state"] = "error"
            _log_error(f"[{t['label']}] {tb}")
        finally:
            t["t1"] = time.time()

    threading.Thread(target=_run, daemon=True).start()


def _task_log(t: dict, limit: int = 80) -> list:
    """取任务日志尾部。

    几何计算跑在**子进程**里，日志落在该任务的 worker.log；这里优先读文件，
    这样生成过程中日志就能实时看到（子进程 stdout 不经过主进程）。
    """
    fp = t.get("log_file")
    if fp and os.path.isfile(fp):
        lines = tail_log(fp, limit * 2)
        keep = [ln for ln in lines if not ln.lstrip().startswith(_NOISE)]
        if keep:
            return keep[-limit:]
    lines = t["log"].getvalue().splitlines()
    keep = [ln.rstrip() for ln in lines
            if ln.strip() and not ln.lstrip().startswith(_NOISE)]
    return keep[-limit:]


@app.route("/api/task_status")
def api_task_status():
    t = _TASKS.get(request.args.get("id", ""))
    if t is None:
        return jsonify({"error": "任务不存在（可能服务重启过）"}), 404
    return jsonify({
        "id": t["id"], "label": t["label"], "state": t["state"],
        "elapsed_s": round((t["t1"] or time.time()) - t["t0"], 1),
        "log": _task_log(t), "result": t["result"], "error": t["error"],
    })


# --------------------------------------------------------------------------- 页面
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """上传产品数模。返回管件编号（默认输出前缀）与存储文件名。"""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "没有收到文件"}), 400
    name = _safe_name(f.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"不支持的格式 {ext or '(无扩展名)'}，"
                                 f"请上传 {' / '.join(ALLOWED_EXTS)}"}), 400
    # 存成 "<12位hex>_原名"：避免重名覆盖，同时 naming_utils 会剥掉这个前缀
    save_name = f"{uuid.uuid4().hex[:12]}_{name}"
    save_path = os.path.join(UPLOAD_DIR, save_name)
    f.save(save_path)
    if os.path.getsize(save_path) == 0:
        os.remove(save_path)
        return jsonify({"error": "上传的文件是空的"}), 400
    return jsonify({
        "file": save_name,
        "original_name": name,
        "part_number": part_number_from_filename(name),
        "size_mb": _human_size(save_path),
        "ext": ext,
        "stl_note": ("STL 是多面体近似：型腔会是平面小面。要光滑曲面请用 STEP。"
                     if ext == ".stl" else ""),
    })


# --------------------------------------------------------------------------- 管件模具
def _files_with_url(entries: list, task: str, job: str = None) -> list:
    """给 worker 产出的文件条目补下载 URL（worker 不知道路由，只报路径）。"""
    out = []
    for e in entries or []:
        name = e.get("name") or os.path.basename(str(e.get("path") or ""))
        if not name:
            continue
        url = f"/api/download?task={task}&file={name}"
        if job:
            url += f"&job={job}"
        out.append({"key": e.get("key"), "label": e.get("label"),
                    "note": e.get("note"), "name": name,
                    "size_mb": e.get("size_mb"), "url": url})
    return out


def _parse_margin(value):
    """返回 (余量三元组, 错误信息)；未提供则用默认 20/20/20。"""
    if value in (None, "", []):
        return (20.0, 20.0, 20.0), None
    try:
        m = tuple(float(v) for v in list(value)[:3])
        if len(m) != 3:
            raise ValueError
        return m, None
    except (TypeError, ValueError):
        return None, "模芯余量要 3 个数字"


def _parse_parting_z(value):
    """返回 (数值或 None, 错误信息)。'' / None / 'auto' 都表示自动推荐。"""
    if value in (None, "", "auto"):
        return None, None
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, "分模面高度必须是数字（或留空用自动推荐值）"


def _parse_timeout(value) -> float:
    """单件超时秒数；不给或非法时用 900s（OCC 卡死兜底）。"""
    try:
        v = float(value)
        return v if v > 0 else 900.0
    except (TypeError, ValueError):
        return 900.0


@app.route("/api/mold_generate", methods=["POST"])
def api_mold_generate():
    """生成管件模具（异步任务；几何计算跑在独立子进程里）。

    请求 JSON:
        file            上传后的存储文件名
        prefix          输出前缀（默认取管件编号）
        parting_surface "plane"（水平面，默认）/ "silhouette"（侧影随形）
        parting_z       水平分模面高度 mm（不给 = 自动推荐）
        margin          [mx,my,mz] 模芯余量
        verify          true = 做开模/顶出干涉校验（慢几分钟；默认 false）
        with_core       true(默认) = 出芯棒与四件套
        timeout         单件超时秒数（默认 900）
    """
    d = request.get_json(silent=True) or {}
    abs_src = _uploaded_path(d.get("file"))
    if not abs_src:
        return jsonify({"error": "请先上传产品数模"}), 400

    prefix = _safe_name(d.get("prefix") or "", "")
    if not prefix:
        prefix = part_number_from_filename(abs_src)

    parting = str(d.get("parting_surface") or "plane").lower()
    if parting not in ("plane", "silhouette"):
        parting = "plane"

    pz, err = _parse_parting_z(d.get("parting_z"))
    if err:
        return jsonify({"error": err}), 400
    margin, err = _parse_margin(d.get("margin"))
    if err:
        return jsonify({"error": "模芯余量要 3 个数字"}), 400

    verify = bool(d.get("verify", False))
    with_core = bool(d.get("with_core", True))
    out_dir = os.path.join(OUT_ROOT, time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])

    tid = _task_new(f"生成管件模具：{prefix}")
    job = MoldJob(job_id="j1", part_path=abs_src, prefix=prefix, output_dir=out_dir,
                  margin=margin, parting_surface=parting, parting_z=pz,
                  verify=verify, with_core=with_core,
                  timeout_s=_parse_timeout(d.get("timeout")),
                  label=os.path.basename(abs_src))
    work_root = os.path.join(out_dir, "_job")
    _TASKS[tid]["log_file"] = os.path.join(work_root, "j1", "worker.log")

    def _work():
        outs = run_jobs([job], workers=1, work_root=work_root)
        out = outs[0]
        if not out.ok:
            raise RuntimeError(out.error or "生成失败（详见日志）")
        payload = out.to_dict()
        return {
            "prefix": payload["prefix"],
            "out_dir": payload["out_dir"],
            "elapsed_s": payload["elapsed_s"],
            "files": _files_with_url(payload["paths"], tid),
            "summary": payload["summary"],
            "warnings": payload["warnings"],
            "metadata": payload["metadata"],
        }

    _task_start(tid, _work)
    return jsonify({"task_id": tid, "async": True, "state": "running", "out_dir": out_dir})


# --------------------------------------------------------------------------- 批量（多件并行）
_BATCHES: dict = {}
_BATCH_KEEP = 6
_BATCH_LOCK = threading.Lock()


def _batch_new(total: int, workers: int, work_root: str, items: list) -> str:
    bid = uuid.uuid4().hex[:12]
    with _BATCH_LOCK:
        _BATCHES[bid] = {"id": bid, "state": "running", "t0": time.time(), "t1": None,
                         "total": total, "workers": workers, "work_root": work_root,
                         "wall_s": None, "summary": None,
                         "items": {it["job_id"]: it for it in items}}
        while len(_BATCHES) > _BATCH_KEEP:
            done = [b for b in _BATCHES.values() if b["state"] != "running"]
            if not done:
                break
            _BATCHES.pop(min(done, key=lambda b: b["t0"])["id"], None)
    return bid


@app.route("/api/system_info")
def api_system_info():
    """给前端参考的并发能力（自动并发数是怎么算出来的）。"""
    ram = available_ram_mb()
    return jsonify({
        "logical_cpus": os.cpu_count(),
        "physical_cores": physical_cores(),
        "available_ram_mb": ram,
        "suggest_workers_4": plan_workers(4),
        "suggest_workers_8": plan_workers(8),
        "note": "并发数 = min(任务数, 物理核, 可用内存/单进程预算, 上限6)；可用环境变量 MOLD_MAX_WORKERS 调整",
    })


@app.route("/api/batch_generate", methods=["POST"])
def api_batch_generate():
    """**多管件并行生成**：一次提交多个数模，各自独立进程、互不影响。

    请求 JSON:
        files  [{"file": 上传后存储名, "prefix": 可选前缀}, ...]
        workers 并发进程数（null = 自动）
        其余参数同 /api/mold_generate（整批共用）
    """
    d = request.get_json(silent=True) or {}
    raw_items = d.get("files") or []
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"error": "请提交至少一个产品数模（files）"}), 400
    if len(raw_items) > 32:
        return jsonify({"error": "单批最多 32 个管件（再多的请分批提交）"}), 400

    parting = str(d.get("parting_surface") or "plane").lower()
    if parting not in ("plane", "silhouette"):
        parting = "plane"
    pz, err = _parse_parting_z(d.get("parting_z"))
    if err:
        return jsonify({"error": err}), 400
    margin, err = _parse_margin(d.get("margin"))
    if err:
        return jsonify({"error": err}), 400
    verify = bool(d.get("verify", False))
    with_core = bool(d.get("with_core", True))
    timeout_s = _parse_timeout(d.get("timeout"))

    batch_dir = os.path.join(OUT_ROOT, time.strftime("%Y%m%d_%H%M%S") + "_batch_"
                             + uuid.uuid4().hex[:6])
    work_root = os.path.join(batch_dir, "_jobs")

    jobs = []
    items = []
    used = set()
    for i, it in enumerate(raw_items):
        src = _uploaded_path((it or {}).get("file"))
        if not src:
            return jsonify({"error": f"第 {i + 1} 个文件没找到（请重新上传）"}), 400
        raw = _safe_name((it or {}).get("prefix") or "", "") or part_number_from_filename(src)
        prefix = raw
        n = 2
        while prefix in used:                       # 前缀重复 -> 输出目录会撞车
            prefix = f"{raw}_{n}"
            n += 1
        used.add(prefix)
        jid = f"j{i + 1}"
        jobs.append(MoldJob(job_id=jid, part_path=src, prefix=prefix,
                            output_dir=os.path.join(batch_dir, prefix),
                            margin=margin, parting_surface=parting, parting_z=pz,
                            verify=verify, with_core=with_core,
                            timeout_s=timeout_s, label=os.path.basename(src)))
        items.append({"job_id": jid, "file": os.path.basename(src), "prefix": prefix,
                      "state": "queued", "elapsed_s": None, "error": None,
                      "files": [], "summary": {}, "warnings": [],
                      "out_dir": os.path.join(batch_dir, prefix)})

    workers = plan_workers(len(jobs), d.get("workers"))
    bid = _batch_new(len(jobs), workers, work_root, items)

    def _on_progress(ev: dict) -> None:
        b = _BATCHES.get(bid)
        if b is None:
            return
        jid = ev.get("job_id")
        if not jid:
            return
        with _BATCH_LOCK:
            it = b["items"].get(jid)
            if it is None:
                return
            if ev.get("event") == "start":
                it["state"] = "running"
            elif ev.get("event") == "done":
                it["state"] = ev.get("state") or ("done" if ev.get("ok") else "failed")
                it["elapsed_s"] = ev.get("elapsed_s")
                it["error"] = ev.get("error")

    def _run_batch():
        b = _BATCHES[bid]
        try:
            outs = run_jobs(jobs, workers=workers, work_root=work_root, progress=_on_progress)
            wall = time.time() - b["t0"]
            with _BATCH_LOCK:
                for o in outs:
                    it = b["items"].get(o.job_id)
                    if it is None:
                        continue
                    d2 = o.to_dict()
                    it.update({"state": o.state, "elapsed_s": o.elapsed_s,
                               "error": o.error, "out_dir": o.out_dir,
                               "summary": d2["summary"], "warnings": d2["warnings"],
                               "files": _files_with_url(d2["paths"], bid, o.job_id),
                               "log_file": o.log_path, "prefix": o.prefix})
                ok_n = sum(1 for o in outs if o.ok)
                b["wall_s"] = round(wall, 1)
                b["summary"] = {
                    "total": len(outs), "done": ok_n, "failed": len(outs) - ok_n,
                    "serial_est_s": round(sum(o.elapsed_s for o in outs), 1),
                    "speedup": round(sum(o.elapsed_s for o in outs) / wall, 2) if wall > 0 else None,
                    "workers": workers,
                }
                b["state"] = "done" if ok_n == len(outs) else "done_with_errors"
        except Exception as exc:  # noqa: BLE001
            _log_error(traceback.format_exc())
            with _BATCH_LOCK:
                b["state"] = "error"
                b["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            b["t1"] = time.time()

    threading.Thread(target=_run_batch, daemon=True).start()
    return jsonify({"batch_id": bid, "total": len(jobs), "workers": workers,
                    "async": True, "state": "running"})


@app.route("/api/batch_status")
def api_batch_status():
    b = _BATCHES.get(request.args.get("id", ""))
    if b is None:
        return jsonify({"error": "批次不存在（可能服务重启过）"}), 404
    with _BATCH_LOCK:
        items = []
        for it in b["items"].values():
            item = {k: it.get(k) for k in
                    ("job_id", "file", "prefix", "state", "elapsed_s", "error",
                     "files", "summary", "warnings")}
            # 运行中的件：实时读它自己的日志（每件日志物理隔离，不会串台）
            if it.get("state") in ("running", "queued"):
                fp = it.get("log_file") or os.path.join(
                    b["work_root"], it["job_id"], "worker.log")
                item["log"] = tail_log(fp, 40) if os.path.isfile(str(fp)) else []
            items.append(item)
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "timeout": 0}
        for it in b["items"].values():
            st = it.get("state") or "queued"
            counts[st if st in counts else "failed"] += 1
        return jsonify({
            "id": b["id"], "state": b["state"], "total": b["total"],
            "workers": b["workers"],
            "elapsed_s": round((b["t1"] or time.time()) - b["t0"], 1),
            "wall_s": b["wall_s"], "summary": b["summary"],
            "counts": counts, "items": items,
            "error": b.get("error"),
        })


@app.route("/api/pipe_plane_suggest", methods=["POST"])
def api_pipe_plane_suggest():
    """只算"推荐水平分模面高度 + 安全区间"（不跑布尔，异步任务，几十秒）。"""
    d = request.get_json(silent=True) or {}
    abs_src = _uploaded_path(d.get("file"))
    if not abs_src:
        return jsonify({"error": "请先上传产品数模"}), 400
    margin = d.get("margin") or (20.0, 20.0, 20.0)
    try:
        margin = tuple(float(v) for v in list(margin)[:3])
    except (TypeError, ValueError):
        margin = (20.0, 20.0, 20.0)

    tid = _task_new("推荐分模面高度")

    def _work():
        from main import suggest_pipe_plane

        t0 = time.time()
        info = suggest_pipe_plane(part_path=abs_src, margin=margin)
        return {"suggest": info, "elapsed_s": round(time.time() - t0, 1)}

    _task_start(tid, _work)
    return jsonify({"task_id": tid, "async": True, "state": "running"})


def _resolve_out_dir(task_id: str, job_id: str = None) -> str:
    """定位一个任务的输出目录：批量按 (批次, 件) 取，单件按任务取。

    只认「服务自己分配过的目录」，配合文件名 basename 化，杜绝路径穿越。
    """
    if job_id:
        b = _BATCHES.get(task_id)
        if b:
            it = b["items"].get(job_id)
            if it:
                return str(it.get("out_dir") or "")
    t = _TASKS.get(task_id)
    if t and t.get("result"):
        return str((t["result"] or {}).get("out_dir") or "")
    b = _BATCHES.get(task_id)                  # 兼容：批量下载不带 job 时不放行
    return ""


@app.route("/api/download")
def api_download():
    """下载某个任务的输出文件（只允许该任务自己的输出目录）。"""
    name = os.path.basename(request.args.get("file", ""))
    out_dir = _resolve_out_dir(request.args.get("task", ""), request.args.get("job") or None)
    if not out_dir or not name:
        return "任务不存在或还没完成", 404
    path = os.path.realpath(os.path.join(out_dir, name))
    if not path.startswith(os.path.realpath(out_dir) + os.sep) or not os.path.isfile(path):
        return "文件不存在", 404
    return send_file(path, as_attachment=True, download_name=name)


# --------------------------------------------------------------------------- 镜像 / 旋转
@app.route("/api/generate_mirror", methods=["POST"])
def api_generate_mirror():
    """镜像 / 绕轴旋转（可叠加：先镜像后旋转），导出一个 STEP 供下载。"""
    d = request.get_json(silent=True) or {}
    abs_src = _uploaded_path(d.get("file"))
    if not abs_src:
        return jsonify({"error": "请先上传产品数模"}), 400

    do_mirror = bool(d.get("mirror", True))
    do_rotate = bool(d.get("rotate", False))
    if not (do_mirror or do_rotate):
        return jsonify({"error": "镜像和旋转至少要选一个"}), 400

    plane = str(d.get("plane") or "xOy")
    if plane not in ("xOy", "xOz", "yOz"):
        plane = "xOy"
    axis = str(d.get("axis") or "z").lower()
    if axis not in ("x", "y", "z"):
        axis = "z"
    direction = "cw" if str(d.get("direction") or "cw").lower().startswith(("cw", "顺")) else "ccw"
    try:
        degrees = float(d.get("degrees", 90.0))
    except (TypeError, ValueError):
        return jsonify({"error": "旋转角度必须是数字"}), 400

    try:
        from mirror_generator import MirrorGenerator

        t0 = time.time()
        res = MirrorGenerator().generate(
            abs_src, plane=plane, output_dir=MIRROR_DIR,
            center_on_centroid=bool(d.get("center_on_centroid", True)),
            rotate=do_rotate, axis=axis, degrees=degrees, direction=direction,
            mirror=do_mirror,
        )
    except Exception as exc:  # noqa: BLE001
        _log_error(traceback.format_exc())
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    name = os.path.basename(res["output_path"])
    return jsonify({
        "ok": True, "name": name, "size_mb": _human_size(res["output_path"]),
        "url": f"/api/download_mirror?name={name}",
        "tag": res.get("tag"), "mirrored": res.get("mirrored"), "rotated": res.get("rotated"),
        "rotate_axis": res.get("rotate_axis"), "rotate_deg": res.get("rotate_deg"),
        "rotate_dir": res.get("rotate_dir"), "plane_label": res.get("plane_label"),
        "bbox_size": res.get("bbox", {}).get("size"),
        "elapsed_s": round(time.time() - t0, 1),
    })


@app.route("/api/download_mirror")
def api_download_mirror():
    name = os.path.basename(request.args.get("name", ""))
    path = os.path.join(MIRROR_DIR, name)
    if not name or not os.path.isfile(path):
        return "文件不存在", 404
    return send_file(path, as_attachment=True, download_name=name)


# --------------------------------------------------------------------------- 兜底
@app.errorhandler(Exception)
def _handle_uncaught(exc):
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return exc
    _log_error(traceback.format_exc())
    if request.path.startswith("/api/"):
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return "服务器内部错误，详见 web_box_generator/error.log", 500


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="管件硬模成型 —— 极简 Web 服务")
    parser.add_argument("--port", type=int, default=5002, help="端口（默认 5002）")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    args = parser.parse_args(argv)
    print("=" * 62)
    print("  管件硬模成型 —— 上传 / 生成（上模·下模·芯棒·四件套）/ 镜像旋转")
    print(f"  浏览器打开: http://{args.host}:{args.port}")
    print("=" * 62, flush=True)
    app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

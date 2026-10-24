# 管件硬模成型 · 模具自动生成系统

输入一个**管件产品数模**（STEP / IGES / STL），自动生成硬模成型模具的四个零件，并可下载：

| 输出 | 说明 |
|---|---|
| `{编号}_upper_mold.step` | **上模**：型腔内壁 = 管件外壁 |
| `{编号}_lower_mold.step` | **下模**：与上模在同一分模面处切开 |
| `{编号}_core_pin.step` | **芯棒**：内孔全长，端面与管件端口齐平 |
| `{编号}_mold4_assembly.step` | **四件套装配体**：上模 + 产品 + 芯棒 + 下模（合模状态） |
| `{编号}_metadata.json` | 分模高度 / 各件体积 / 耗时 / 警告（供核对） |

另外提供 **镜像 / 旋转**（模型方向不合适时先变换再开模）。

> 本系统已按需求**精简**：只保留管件硬模成型这一条管线，**没有三维查看**（不加载 three.js、
> 没有剖切/测量/特征识别等面板）。板件折弯管线、旧版修复器、独立芯棒设计模块已从仓库移除
> （本机留在 `_legacy/`，历史版本仍在 git 里）。
> 管件模具的技术细节、历次优化与实测数据见 [`PIPE_MOLD_README.md`](PIPE_MOLD_README.md)。

---

## 一、运行方式

### Web（推荐）
```bat
:: 双击项目根目录的 启动Web服务.bat，或：
venv\Scripts\python.exe web_box_generator\app.py --port 5002
```
浏览器打开 <http://127.0.0.1:5002>，页面上四步：
1. **上传产品数模** → 自动识别管件编号（用作输出前缀）
2. **生成管件模具** → 填分模面高度（可点「取推荐值」），点「开始生成」→ 进度日志 → 四个下载按钮
3. **批量生成（多管件并行）** → 一次选多个数模，改前缀、选并发数 → 一张表看每件状态/用时/下载/日志
4. **镜像 / 旋转** → 生成一个新的 STEP 供下载

### 命令行
```bat
:: 单件
venv\Scripts\python.exe main.py --part <产品.stp> [--parting-z 40] [--prefix 24TK_1324-31] ^
    [--parting-surface plane|silhouette] [--margin 20 20 20] [--no-verify] [--no-core] [--parallel]

:: 多件并行（每个管件一个独立子进程）
venv\Scripts\python.exe main.py --parts <产品1.stp> <产品2.stp> <产品3.stp> ^
    [--jobs 4] [--timeout 900] [--output ./output] [--no-verify]
```
`--part` / `--parts` 至少给一个（可混用）。`--jobs` 不给则按 物理核 / 可用内存 自动定；
多件时成品落在 `{--output}/{前缀}/` 下，同名前缀自动加 `_2` 后缀。`--no-verify` 跳过开模/顶出干涉校验，快几分钟。

### 依赖
`requirements.txt`（Windows 虚拟环境 `venv/`）：`cadquery-ocp`（提供 `OCP`，OCCT 7.8）、`cadquery`、`flask`、`numpy`。
```bat
venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **中文路径**：OCC 底层文件 API 不支持非 ASCII 路径。所有读写统一走 `io_utils.py`
> （读前复制到 ASCII 临时目录、写后由 Python 移回），**新增直接调用 OCC 读写文件的代码必须复用它**。

---

## 二、系统怎么生成的（一句话版）

1. 读产品 → 摆正到**模芯块**正中（管件长轴沿 X）；
2. 型腔 = 产品**外形包络**（把内孔填实），所以型腔内壁与管件外壁严丝合缝；
3. 填实内孔的那块料正好就是**芯棒**（挖包络顺带切出来，不额外花时间）；
4. 按**分模面**把型腔模切成上模 / 下模：
   * **水平面**（默认，最好加工）：高度可由用户指定，也可用自动推荐值；
   * **侧影随形面**：沿产品侧影线（曲面，更随形，加工麻烦）；
5. 导出上模 / 下模 / 芯棒 / 四件套（都已在**输入数模的原始坐标系**里，可直接和原产品装配）。

**分模面高度怎么定**：开模方向是 +Z，分型线要落在产品表面法向水平处。程序用表面采样
（不跑布尔，几十秒）算出"安全区间"和推荐高度；区间上下限颠倒时说明该件**任何单一水平面**
都会在部分站位留台阶（斜切端面 / 局部倒扣），此时可以自己填一个高度，或改用侧影随形面。

---

## 三、Web 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| POST | `/api/upload` | 上传数模（`file`），返回 `{file, part_number, size_mb}` |
| POST | `/api/mold_generate` | 生成模具（异步）→ `{task_id}` |
| POST | `/api/batch_generate` | **多件并行生成**（异步）→ `{batch_id, total, workers}` |
| GET | `/api/batch_status?id=` | 轮询整批：`{state, counts, summary, items[]}`（每件含日志/成品/错误） |
| GET | `/api/system_info` | 本机并发能力（物理核 / 可用内存 / 建议并发数） |
| POST | `/api/pipe_plane_suggest` | 只算推荐分模面高度（异步）→ `{task_id}` |
| GET | `/api/task_status?id=` | 轮询：`{state, log, result, error}` |
| GET | `/api/download?task=&file=` | 下载生成件（批量时再加 `&job=<件id>`；只允许该任务的输出目录） |
| POST | `/api/generate_mirror` | 镜像 / 旋转（同步）→ `{name, url, ...}` |
| GET | `/api/download_mirror?name=` | 下载镜像 / 旋转结果 |

生成参数（`/api/mold_generate` 的 JSON）：

```json
{ "file": "上传后的存储名",
  "prefix": "24TK_1324-31",
  "parting_surface": "plane",        // plane=水平面 / silhouette=侧影随形
  "parting_z": 40.0,                 // 留空或 null = 自动推荐
  "margin": [20, 20, 20],
  "verify": false,                   // true = 做开模/顶出干涉校验（慢几分钟）
  "with_core": true,                 // 是否出芯棒 + 四件套
  "timeout": 900 }                   // 单件超时秒数（超时只终止该件）
```

批量参数（`/api/batch_generate`）在上述基础上把 `file` 换成数组，并多一个 `workers`：

```json
{ "files": [{"file": "存储名1", "prefix": "24TK_1324-2"},
            {"file": "存储名2", "prefix": "24TK_2052-2"}],
  "workers": 4,                      // null = 自动
  "parting_surface": "plane", "margin": [20,20,20],
  "verify": false, "with_core": true }
```

---

## 四、文件清单

| 文件 | 作用 |
|---|---|
| `main.py` | 管线入口：`generate_mold()` / `generate_batch()` / `suggest_pipe_plane()` / 命令行 |
| `pipe_mold.py` | **核心**：型腔包络、分模（水平面/侧影随形）、芯棒提取、开模校验 |
| `batch_runner.py` | **并行调度器**：子进程池、并发配额、超时终止、结果汇总 |
| `mold_worker.py` | **单件工作进程**：读 job.json → 跑管线 → 写 result.json（进程隔离的落点） |
| `core_block_generator.py` | 按产品包围盒 + 余量算模芯块尺寸 |
| `part_aligner.py` | 产品在模芯里的居中定位 |
| `cavity_cutter.py` | 布尔运算封装（带 FuzzyValue 容错重试） |
| `result_exporter.py` | 导出 STEP（AP214，含 schema 降级）+ JSON 元数据 |
| `model_reader.py` / `mesh_solid.py` | 读 STEP / IGES / STL（STL 先缝合网格成实体） |
| `mirror_generator.py` | 镜像（xOy/xOz/yOz，可过质心）/ 绕轴旋转（顺逆时针） |
| `naming_utils.py` | 从文件名提取管件编号作为输出前缀 |
| `geometry_utils.py` / `errors.py` / `io_utils.py` | 体积/包围盒、异常类型、中文路径与 OCC 的安全读写 |
| `web_box_generator/app.py` | 极简 Flask 服务（上传 / 单件 / **批量** / 下载 / 镜像旋转） |
| `web_box_generator/templates/index.html` | 单文件页面（无三方前端库） |
| `requirements.txt` | 依赖版本清单 |
| `启动Web服务.bat` | 一键启动（端口 5002） |
| `PIPE_MOLD_README.md` | 管件模具的技术说明：算法、OCCT 坑、历次优化与实测数据 |
| `24TK_*.stp` / `r0.STL` | 测试用管件数模 |

---

## 五、多管件并行批量生成

**一句话**：一个管件 = 一个独立子进程；进程之间只用文件交换数据（STEP / JSON），
所以任何一件崩溃、卡死、超时都只损失它自己，其余件和服务照常。

### 5.1 为什么是"进程池"而不是"线程池"

OCCT 的布尔运算和几何内核带大量全局状态，**同进程多线程并发不安全**（本工程在
`--parallel` 上实测过"偶有不稳"）。Python 侧还有 GIL：CPU 密集任务用线程只是假并发。
所以并行必须由**独立进程**承担 —— 各进程一个 OCC 内核实例、一个堆，天然隔离。

### 5.2 调度方式（三层配额）

| 层 | 规则 | 作用 |
|---|---|---|
| 批内并发 | `w = min(任务数, 物理核, 可用内存/1.2GB, MOLD_MAX_WORKERS=6)` | 不让并发数超过机器真实能力 |
| 跨批全局闸门 | 进程内 `BoundedSemaphore(6)` | 多批同时提交时，全进程总数仍 ≤ 6（否则 2 批 × 6 = 12 进程抢 8 核，比串行还慢） |
| 每进程线程数 | `OMP/MKL/OPENBLAS/NUMEXPR_NUM_THREADS=1` | 避免 N 进程 × 16 线程互相颠簸 |

没抢到全局名额的任务在前端显示"排队"而不是"运行中"（语义诚实，也便于判断瓶颈）。
单件少的时候自动降并发；`workers=1` 即串行，可做对照实验。

### 5.3 隔离机制（5 条，每件一套）

1. **进程**：`mold_worker.py` 独立进程，几何对象不跨进程（`TopoDS_Shape` 本来就不可 pickle）；
2. **目录**：每件自己的 `output_dir`（`{批目录}/{前缀}/`），同批前缀重复自动加 `_2`；
3. **日志**：每件自己的 `worker.log`（父进程不重定向、不共享 fd），前端按件实时 tail；
4. **临时文件**：`io_utils` 的 ASCII 中转文件名带 uuid，并发写不撞车；
5. **结果文件**：`result.json` 先写 `.tmp` 再 `os.replace`，父进程永远读不到半个文件。

**失败四态**：`done` / `failed`（进程返回非 0 或未产出结果）/ `timeout`（超时被终止）/
`crashed`（调度层兜底）。单件失败不影响同批其它件，失败原因逐件回传。

### 5.4 资源冲突怎么避开

- **CPU**：按住物理核数（不是 16 线程）定并发；跨批再压一层全局闸门。
- **内存**：按可用物理内存 / 单进程预算（默认 1.2 GB）反推上限，防 OOM 连带拖死整批。
- **线程颠簸**：见 5.2 第三条，单进程内部强制单线程数学库。
- **磁盘写冲突**：目录隔离 + 文件名带前缀；`/api/download` 只放行本任务目录内的
  basename（`realpath` 前缀校验，已实测 `../../app.py` 返回 404）。
- **卡死**：每件独立 `timeout`（默认 900s），到时 `kill` 该进程，其余件继续。
- **OCC 全局静态量**（如 `write.step.schema`）：进程级，不跨进程串味。

### 5.5 实测（本机 Ryzen 7 6800H，8 物理核 / 16 线程 / 31 GB，verify=False）

| 场景 | 串行墙钟 | 并行墙钟 | 加速 | 说明 |
|---|---|---|---|---|
| 4 份同一件（2052-2，单件 25~30 s） | 115.0 s | **32.4 s**（4 进程） | **3.55x** | 并行效率 89%，接近线性 |
| 2 件不同件（1324-2 61 s + 2052-2 29 s） | 89.9 s | **63.2 s**（2 进程） | **1.42x** | 墙钟 = 最长单件，被长件锁死 |

**预期效率怎么估**：`墙钟 ≈ ceil(N / w) × 单件均时 + 约 2 s 进程启动开销`。
所以：
* 件数与并发数相当、且单件耗时接近 → **接近线性**（实测 3.55/4 ≈ 89%）；
* 耗时不均 → 加速比由**最慢那件**决定（Amdahl），此时把慢件排在最前面提交收益最大；
* 件数远大于并发数 → 摊薄后同样趋近 `w` 倍。

单件本身也改走子进程了（即使只提交一件），换来的是：**Web 服务不再加载几何**，
某件把 OCC 跑崩/跑死不会带崩服务；同时日志可以实时看。

> 个例：`24TK_1324-1_ROTATE_x_CCW_90deg` 单件跑到 7 分半仍停在布尔运算里（已手动终止，
> 未取到最终耗时），属 OCC 边界个案 —— 这类件正是 `timeout` 存在的意义。

---

## 六、已知限制（如实说明）

1. **管壁有侧向开口**（三通口、侧窗）时，内孔腔从洞里与主体相连，两端截断在原理上切不掉 —
   这种结构需要**侧向抽芯（斜顶/滑块）**，本程序不生成侧抽芯，只会在 `metadata.mold_warnings`
   里明确提示。
2. **斜切端面 / 局部倒扣 + 水平面**：任何单一水平面都会在部分站位留下台阶或钩料（几何决定的），
   要完全随形请用「侧影随形」分模面。
3. **包围盒虚胖**：`Bnd_Box` 对 BSpline 面会撑大（`24TK_1324-31` 实测虚胖 2×25 mm），
   影响模芯尺寸与上报的"产品尺寸"（多约 7% 材料）；型腔贴合、分模、芯棒都不受影响。
4. **OCC 的 STEP 写出器**对个别 BSpline 件会轻微重逼近（0.001 mm 量级），交付文件自洽。
5. **输出不自动清理**：`web_box_generator/uploads/` 与 `output/` 只增不减，批量跑多了会占几个 GB；
   需要定期手动清理（或加定时任务）。
6. **任务状态在内存里**：`_TASKS` / `_BATCHES` 是进程内字典，服务重启后旧批次查不到（成品文件还在盘上）。
7. **单批上限 32 件**、全局并发上限 6 个进程（`MOLD_MAX_WORKERS` 可调）；再多请分批提交。
8. **几何内核未改动**：本次只优化了编排/隔离层，`pipe_mold.py` 的算法一行没动 ——
   分模、包络、芯棒的行为与之前完全一致。

# 项目长期记忆 · 管件硬模成型模具生成系统

## 架构约定（改动前必读）
- 分层：`web_box_generator/app.py`（Flask）→ `main.py`（编排）→ `pipe_mold.py`（几何内核，2515 行）→ 基础能力（model_reader / core_block_generator / part_aligner / cavity_cutter / result_exporter）→ 横切（io_utils / errors / geometry_utils / naming_utils）
- **几何内核 OCC 非线程安全**：任何并行都必须走进程，禁止在同进程内用线程并发跑布尔。
- 单件生成统一走 `mold_worker.py` 子进程（file-in/file-out）；Web 进程**不加载几何**。
- 并行编排统一走 `batch_runner.run_jobs()`，不要在别处另造线程池。
- 坐标系：内部计算在"模芯坐标系"（产品居中），导出前 `_shift_shape` 反向平移回输入数模原始坐标（main.py 导出段）。
- 中文路径：所有 OCC 读写必须经 `io_utils`（ASCII 临时中转），新代码禁止直接调 OCC 文件 API。

## 并发参数
- `w = min(任务数, 物理核, 可用内存/1.2GB, MOLD_MAX_WORKERS)`，默认上限 6；跨批共用全局 Semaphore。
- 子进程强制 `OMP_NUM_THREADS=1` 等；单件超时默认 900s（`MOLD_JOB_TIMEOUT`）。

## 代码风格
- 中文注释/文档字符串，与现有文件一致；错误用 `errors.py` 的异常体系（带 stage）。
- 不主动删除 `web_box_generator/uploads/` 下的用户数据。

## 待办（按优先级）
1. 端到端冒烟脚本（4 个测试件 → 断言体积/警告基线），补上后才有资格动 `pipe_mold.py`
2. 拆 `pipe_mold.py`（采样 / 截面 / 端口探测 / 布尔 / 校验）
3. 上传与输出目录的定期清理；任务态持久化（当前全内存，重启丢失）

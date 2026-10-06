# runbook_kernel — 纯 Python Jupyter 协议 5.3 后端内核

面向研发场景的极简 Jupyter 内核: 用 Jupyter 客户端执行 Python 单元并保留变量。
仅依赖 `pyzmq` 与 `jupyter_client`(复用其 `Session` 编解码与 HMAC 校验),
**不启动、不代理 ipykernel**, 内核协议 5.3 由本项目直接实现。

## 运行环境

- Python 3.14.4
- pyzmq 27.1.0
- jupyter_client 8.6.3

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
```

## 快速开始

```bash
# 1. 准备连接文件(端口、密钥)
.venv/bin/python - <<'PY'
from jupyter_client.connect import write_connection_file
write_connection_file("connection.json", ip="127.0.0.1", key=b"my-secret")
PY

# 2. 启动内核(只在环回地址绑定)
.venv/bin/python -m runbook_kernel -f connection.json

# 3. 用 Jupyter 客户端连接(锁定依赖内, 无需额外安装 jupyter_console):
.venv/bin/python - <<'PY'
from jupyter_client.blocking import BlockingKernelClient
client = BlockingKernelClient()
client.load_connection_file("connection.json")
client.start_channels()
client.wait_for_ready(timeout=20)
mid = client.execute("print('hello'); 6 * 7")
print(client.get_shell_msg(timeout=5)["content"])   # execute_reply
client.shutdown()
PY
```

## 测试与演示

```bash
# 集成测试: 真实 jupyter_client 驱动内核子进程
.venv/bin/python -m pytest

# 演示: 执行、异常、中断、关闭的完整过程
.venv/bin/python examples/demo.py

# 演示: Notebook 实验 API(参数化重跑、异常、超时)
.venv/bin/python examples/run_experiment.py
```

## Notebook 实验 API: 按参数重跑 .ipynb

研发场景: 同一个实验笔记, 用不同参数反复重跑, 每次产出一份独立的结果笔记。
不依赖 ipykernel / nbclient / papermill —— 每次运行启动一个**本项目的**新内核
子进程, 经 jupyter_client 顺序执行, 运行间变量完全隔离。

```python
from runbook_kernel import run_notebook

result = run_notebook(
    "experiment.ipynb",          # 输入(nbformat 4), 永不修改
    "runs/exp-lr0.01.ipynb",     # 结果笔记, 原子写入(临时文件 + os.replace)
    parameters={"lr": 0.01, "epochs": 10},   # dict 或 JSON 字符串
    cell_timeout=60.0,           # 单元超时秒数, None 表示不限
    continue_on_error=False,     # 单元异常是否继续(默认停止)
)
print(result.status)             # "ok" | "error" | "timeout"
```

命令行等价形式:

```bash
.venv/bin/python -m runbook_kernel.experiment experiment.ipynb runs/out.ipynb \
    --params '{"lr": 0.01, "epochs": 10}' --cell-timeout 60
```

**参数注入(papermill 风格)**

- 笔记中有且仅有一个 code 单元可带 `parameters` 标签; 提供了非空参数时
  该单元必须存在, 否则规划阶段直接报错(不启动内核)。
- 先执行该单元的默认代码, 再在**紧随其后**的注入单元(带
  `injected-parameters` 标签)里赋覆盖值。
- 参数名须为非关键字 Python 标识符; 值限于有限 JSON(拒绝 NaN/Infinity
  与非 JSON 类型); 字符串经 `repr` 安全注入, 是数据而非可执行代码。

**执行与输出**

- 支持 nbformat 4, 单元 source 为字符串或字符串数组; 单元 ID、元数据、
  Markdown、raw 与附件原样保留, 旧输出与执行计数先清零再执行。
- 每个单元的 stdout / stderr / 末表达式(text/plain)/ 异常按父消息 ID
  归集, 写成标准 nbformat 输出; 对应 `execute_reply` 与 `idle` 都收到
  才推进下一单元, 迟到消息不会归入下一单元。

**失败行为**

| 情形 | 行为 |
| --- | --- |
| 单元异常(默认) | 停止执行, 状态 `error`; 失败单元含 error 输出, 后续单元保持空输出与空计数 |
| 单元异常 + `continue_on_error=True` | 跑完全程, 状态仍为 `error`, 每个失败单元各自记录 error 输出 |
| 单元超时 | 经 control 通道中断当前单元并终止实验, 状态 `timeout`; 已收输出(含中断前的部分输出)保留, 未执行单元保持空输出与空计数 |
| 规划/参数不合法 | 抛出 `NotebookPlanError`, 不启动内核、不写结果笔记 |

成功、异常、超时都会原子写出独立的结果笔记; 返回前回收本次的内核
进程、连接文件与套接字。

## 目录结构(跨文件协作)

```
runbook_kernel/
  __main__.py     入口: python -m runbook_kernel -f connection.json
  connection.py   连接 JSON 解析; 强制环回地址绑定
  security.py     认证与路由: 复用 jupyter_client Session 的编解码与 HMAC 校验
  kernel.py       内核主体: 通道线程、串行执行循环、消息分发、关闭
  execution.py    执行上下文: 共享命名空间、输出转发、可中断执行
  iopub.py        输出发布: status / execute_input / stream / execute_result / error
  heartbeat.py    心跳线程: REP 回显, 与执行完全解耦
  supervisor.py   进程监管: 信号处理、生命周期、资源回收
  notebook.py     笔记规划: 校验 nbformat 4、注入参数覆盖单元、清输出
  collect.py      消息收集: 按父消息 ID 归集单元产出, reply+idle 双终态
  experiment.py   运行监管: run_notebook API 与 CLI、内核生命周期、原子写出
tests/test_kernel.py      内核集成测试(真实 jupyter_client)
tests/test_experiment.py  实验 API 测试(规划、参数、输出、失败与超时)
examples/demo.py            交互式内核演示
examples/run_experiment.py  实验 API 演示(产物在 examples/out/)
```

协作关系: `supervisor` 驱动 `kernel` 的生命周期; `kernel` 经 `security`
收发并校验消息, 把执行请求交给主线程的 `execution` 执行上下文, 执行产出经
`iopub` 实时发布; `heartbeat` 独立应答存活探测。

实验链路: `experiment.run_notebook` 先交给 `notebook` 做规划(校验、注入、
清输出), 再启动新内核进程, 逐单元经 `collect` 按父消息 ID 收集产出并写成
标准笔记输出, 最后原子写出结果笔记并回收本次全部资源。内核的交互能力
(jupyter_client 直连、中断、关闭)完全保留, 与实验 API 互不干扰。

## 协议行为

- **通道**: shell / control / iopub / stdin / heartbeat 全部绑定在环回地址。
  stdin 仅绑定, 内核永不发起 `input_request`; 用户代码里的 `input()` 立即报错,
  不会挂起内核。
- **认证**: 所有请求经 HMAC 签名校验, 坏签名/重放消息直接丢弃; 回复沿原路由
  idents 返回并保留父消息关联, 多客户端不串路由。
- **执行**: 单元串行执行、共享命名空间; 末尾表达式以 `text/plain` 返回
  `execute_result`; stdout/stderr 按行实时发布; 异常返回 `ename`/`evalue`/
  `traceback`, 之后内核继续可用。
- **消息流**: 每个请求按协议发布 `busy → (execute_input → stream/result/error)
  → idle`; `execution_count` 遵守 `silent` 与 `store_history` 语义
  (`silent` 强制 `store_history=False` 并抑制该单元的全部 IOPub 输出)。
- **中断**: control 通道 `interrupt_request` 借助 SIGINT 把 `KeyboardInterrupt`
  投递进主线程当前单元 —— 只结束当前单元、保留已有变量; SIGINT 处理器只在
  中断确实瞄准当前单元时才抛出, 迟到信号直接吞掉, 不误伤随后单元。单元的
  终态(`execute_reply` + `idle`)只由执行线程发出, 每单元恰好一次,
  完成与中断竞争不会产生重复终态。
- **关闭**: `shutdown_request`(shell 或 control)回复后回收线程与套接字并退出;
  3 秒看门狗兜底保证进程一定被回收。SIGTERM 走同样的优雅关闭路径。

## 设计说明

- **执行与通信分离**: 主线程串行执行单元; shell / control / heartbeat 各自
  独立线程; IOPub 经带锁的发布器发送。长运算期间心跳与控制通道照常响应。
- **为什么用 SIGINT 而不是跨线程异步异常**: Python 信号处理器只在主线程
  执行, 配合"目标单元号"武装检查, 可以精确命中当前单元; `time.sleep` 等
  阻塞调用在主线程可被信号立即打断。
- **安全模型**: 仅面向可信本机代码 —— 不做沙盒; 只监听环回地址; 依赖连接
  文件密钥做消息认证。

## 已知限制(明确不支持)

- 用户表达式(`user_expressions` 一律返回 `{}`)、富媒体(只发 `text/plain`)
- comm / debugger / 完整代码分析(`is_complete` 恒返回 `complete`)
- IPython 魔法命令(内核执行的是纯 Python)
- stdin 交互输入

"""运行监管: 一次实验一个内核子进程, 负责生命周期、原子写出与资源回收。

协作关系: notebook 负责规划(校验/注入/清输出), collect 负责按父消息
ID 收集单单元产出, 本模块把二者串成一次完整实验 —— 启动本项目的新
内核进程, 顺序执行, 收尾写出结果笔记并回收全部资源。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from jupyter_client.blocking import BlockingKernelClient
from jupyter_client.connect import write_connection_file

from .collect import collect_cell
from .notebook import NotebookPlanError, prepare_notebook, source_to_str

log = logging.getLogger("runbook_kernel.experiment")

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ExperimentResult:
    """一次实验的终态。"""

    status: str  # "ok" | "error" | "timeout"
    output_path: Path
    failed_cell: int | None = None  # 第一个失败/超时单元在笔记中的下标
    ename: str = ""
    evalue: str = ""
    executed_cells: int = 0  # 实际执行的 code 单元数
    total_cells: int = 0  # 计划执行的 code 单元数
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def run_notebook(
    input_path,
    output_path,
    *,
    parameters=None,
    cell_timeout: float | None = None,
    continue_on_error: bool = False,
    startup_timeout: float = 20.0,
    interrupt_grace: float = 2.0,
) -> ExperimentResult:
    """按参数重跑一个 .ipynb 实验, 原子写出独立的结果笔记。

    - input_path:   输入笔记(nbformat 4), 永不修改
    - output_path:  结果笔记, 成功/异常/超时都经临时文件原子替换写入
    - parameters:   dict 或 JSON 字符串; 非空时笔记里必须有且仅有一个
                    带 parameters 标签的 code 单元 —— 先执行它的默认代码,
                    再执行紧随其后注入的覆盖值单元
    - cell_timeout: 单元超时秒数(None 不限); 超时则中断并终止整个实验,
                    已收输出保留, 未执行单元保持空输出与空计数
    - continue_on_error: 单元异常默认停止; 为 True 时继续执行后续单元

    每次调用都启动本项目的新内核进程, 运行间变量完全隔离; 返回前
    回收本次的进程、连接文件与套接字。
    """
    plan = prepare_notebook(input_path, parameters)
    output_path = Path(output_path)
    started = time.monotonic()
    result = ExperimentResult(status="ok", output_path=output_path, total_cells=len(plan.code_cell_indices))

    workdir = Path(tempfile.mkdtemp(prefix="runbook-experiment-"))
    conn_file = workdir / "connection.json"
    write_connection_file(str(conn_file), ip="127.0.0.1", key=os.urandom(32).hex().encode())
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(  # noqa: S603 - 启动的是本项目自己的内核
        [sys.executable, "-m", "runbook_kernel", "-f", str(conn_file)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = BlockingKernelClient()
    client.load_connection_file(str(conn_file))
    client.start_channels()
    try:
        client.wait_for_ready(timeout=startup_timeout)
        cells = plan.notebook["cells"]
        for index in plan.code_cell_indices:
            cell = cells[index]
            msg_id = client.execute(source_to_str(cell.get("source", "")))
            collected = collect_cell(client, msg_id, timeout=cell_timeout, interrupt_grace=interrupt_grace)
            cell["outputs"] = collected.outputs
            cell["execution_count"] = collected.execution_count
            result.executed_cells += 1
            if collected.timed_out:
                result.status = "timeout"
                result.failed_cell = index
                result.ename = collected.ename or "TimeoutError"
                result.evalue = collected.evalue or f"单元超过 {cell_timeout}s 未完结, 已中断"
                break
            if collected.status == "error":
                if result.failed_cell is None:
                    result.failed_cell = index
                    result.ename = collected.ename
                    result.evalue = collected.evalue
                if not continue_on_error:
                    result.status = "error"
                    break
        if result.failed_cell is not None and result.status == "ok":
            result.status = "error"  # continue_on_error 下跑完全程但有单元失败
    finally:
        _reap_kernel(proc, client)
        shutil.rmtree(workdir, ignore_errors=True)
    result.elapsed = time.monotonic() - started
    _atomic_write_json(output_path, plan.notebook)
    return result


def _reap_kernel(proc: subprocess.Popen, client: BlockingKernelClient) -> None:
    """回收本次实验的内核进程与客户端套接字(优雅优先, 强制兜底)。"""
    try:
        if proc.poll() is None:
            try:
                client.shutdown()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("内核未响应关闭, 发送 SIGTERM")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log.warning("SIGTERM 无效, 强制 SIGKILL")
                    proc.kill()
                    proc.wait(timeout=3)
    except Exception:
        log.warning("关闭内核时出现异常, 强制终止", exc_info=True)
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        try:
            client.stop_channels()
        except Exception:
            pass


def _atomic_write_json(path: Path, data: dict) -> None:
    """同目录临时文件 + os.replace: 任何时刻结果笔记要么不存在, 要么完整。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    """CLI: python -m runbook_kernel.experiment 输入.ipynb 输出.ipynb [选项]"""
    parser = argparse.ArgumentParser(
        prog="runbook_kernel.experiment",
        description="按参数重跑 .ipynb 实验(每次启动新内核, 原子写出结果笔记)",
    )
    parser.add_argument("input", help="输入笔记路径(不被修改)")
    parser.add_argument("output", help="结果笔记输出路径(原子写入)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--params", help="JSON 参数字符串, 如 '{\"lr\": 0.01}'")
    group.add_argument("--params-file", help="JSON 参数文件路径")
    parser.add_argument("--cell-timeout", type=float, default=None, metavar="秒", help="单元超时秒数(默认不限)")
    parser.add_argument("--continue-on-error", action="store_true", help="单元异常后继续执行后续单元")
    args = parser.parse_args(argv)

    parameters = None
    if args.params_file:
        parameters = Path(args.params_file).read_text(encoding="utf-8")
    elif args.params is not None:
        parameters = args.params
    try:
        result = run_notebook(
            args.input,
            args.output,
            parameters=parameters,
            cell_timeout=args.cell_timeout,
            continue_on_error=args.continue_on_error,
        )
    except NotebookPlanError as exc:
        print(f"规划失败: {exc}", file=sys.stderr)
        return 2
    print(f"[{result.status}] 执行 {result.executed_cells}/{result.total_cells} 个单元, 耗时 {result.elapsed:.1f}s -> {result.output_path}")
    if result.failed_cell is not None:
        print(f"首个失败单元 #{result.failed_cell}: {result.ename}: {result.evalue}", file=sys.stderr)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

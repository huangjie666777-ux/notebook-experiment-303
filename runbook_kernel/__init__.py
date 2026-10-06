"""runbook_kernel — 纯 Python 实现的 Jupyter 协议 5.3 后端内核。

仅依赖 pyzmq 与 jupyter_client(复用其 Session 编解码与 HMAC 校验),
不启动、不代理 ipykernel。

除交互式内核外, 包顶层直接暴露 Notebook 实验 API:
run_notebook 按参数重跑 .ipynb 并原子写出结果笔记。
"""

__version__ = "0.1.0"

__all__ = ["ExperimentResult", "NotebookPlanError", "__version__", "run_notebook"]


def __getattr__(name):
    """惰性导出实验 API: 内核子进程(python -m runbook_kernel)不必加载客户端栈。"""
    if name in ("ExperimentResult", "run_notebook"):
        from . import experiment

        return getattr(experiment, name)
    if name == "NotebookPlanError":
        from .notebook import NotebookPlanError

        return NotebookPlanError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

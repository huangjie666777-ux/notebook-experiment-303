"""演示 Notebook 实验 API: 按参数重跑 .ipynb, 以及异常与超时行为。

用法:  .venv/bin/python examples/run_experiment.py
产物:  examples/out/ 下的源笔记与各次运行的结果笔记(输入永不被修改)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 仓库未安装, 直接从源码树导入

from runbook_kernel import run_notebook

OUT_DIR = Path(__file__).resolve().parent / "out"


def build_source_notebook(path: Path) -> None:
    """构造一个带 parameters 标签的实验笔记(含 markdown 附件与 raw 单元)。"""
    nb = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "cells": [
            {
                "cell_type": "markdown",
                "id": "intro",
                "metadata": {},
                "source": "# 参数化实验\n学习率扫描: 同一笔记, 不同参数重跑。\n",
                "attachments": {"notes.txt": {"text/plain": "5Zu95a6J5pa95qGj"}},
            },
            {
                "cell_type": "code",
                "id": "params",
                "metadata": {"tags": ["parameters"]},
                "source": "learning_rate = 0.01\nepochs = 2\nrun_name = 'baseline'\n",
                "outputs": [],
                "execution_count": None,
            },
            {
                "cell_type": "code",
                "id": "train",
                "metadata": {},
                "source": (
                    "loss = 1.0\n"
                    "for epoch in range(epochs):\n"
                    "    loss *= 1 - learning_rate\n"
                    "    print(f'[{run_name}] epoch {epoch}: loss={loss:.4f}')\n"
                ),
                "outputs": [],
                "execution_count": None,
            },
            {
                "cell_type": "code",
                "id": "summary",
                "metadata": {},
                "source": "{'run': run_name, 'lr': learning_rate, 'final_loss': round(loss, 6)}\n",
                "outputs": [],
                "execution_count": None,
            },
            {"cell_type": "raw", "id": "raw-note", "metadata": {}, "source": "raw 单元原样保留\n"},
        ],
    }
    path.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")


def show_result(result) -> None:
    print(f"   [{result.status}] 执行 {result.executed_cells}/{result.total_cells} 个单元, "
          f"耗时 {result.elapsed:.1f}s -> {result.output_path.name}")
    nb = json.loads(result.output_path.read_text(encoding="utf-8"))
    for cell in nb["cells"]:
        if cell["cell_type"] != "code":
            continue
        for output in cell.get("outputs", []):
            kind = output["output_type"]
            if kind == "stream":
                for line in output["text"].rstrip().splitlines():
                    print(f"      stdout| {line}")
            elif kind == "execute_result":
                print(f"      result| {output['data']['text/plain']}")
            elif kind == "error":
                print(f"      error | {output['ename']}: {output['evalue']}")
    print()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = OUT_DIR / "experiment.ipynb"
    build_source_notebook(src)
    print(f"== 源笔记: {src} (parameters 单元带默认值, 含 markdown 附件与 raw 单元)==\n")

    print("== 1. 同一笔记, 两组参数各跑一次(每次新内核, 变量隔离)==")
    r1 = run_notebook(src, OUT_DIR / "run-baseline.ipynb",
                      parameters={"learning_rate": 0.1, "epochs": 3, "run_name": "baseline"})
    show_result(r1)
    r2 = run_notebook(src, OUT_DIR / "run-fast.ipynb",
                      parameters={"learning_rate": 0.5, "epochs": 3, "run_name": "fast"})
    show_result(r2)

    print("== 2. 异常: 默认停止, 后续单元保持空输出 ==")
    err_nb = json.loads(src.read_text(encoding="utf-8"))
    err_nb["cells"].insert(3, {
        "cell_type": "code", "id": "boom", "metadata": {},
        "source": "raise RuntimeError('模拟实验中途失败')\n", "outputs": [], "execution_count": None,
    })
    err_src = OUT_DIR / "experiment-error.ipynb"
    err_src.write_text(json.dumps(err_nb, ensure_ascii=False, indent=1), encoding="utf-8")
    r3 = run_notebook(err_src, OUT_DIR / "run-error.ipynb", parameters={"run_name": "will-fail"})
    show_result(r3)
    cells = json.loads((OUT_DIR / "run-error.ipynb").read_text(encoding="utf-8"))["cells"]
    last = cells[-2]
    print(f"      失败之后的单元: outputs={last['outputs']} execution_count={last['execution_count']}\n")

    print("== 3. 超时: 中断当前单元并终止实验, 已收输出保留 ==")
    slow_nb = json.loads(src.read_text(encoding="utf-8"))
    slow_nb["cells"][2]["source"] = "import time\nprint('开始长运算...')\ntime.sleep(300)\n"
    slow_src = OUT_DIR / "experiment-slow.ipynb"
    slow_src.write_text(json.dumps(slow_nb, ensure_ascii=False, indent=1), encoding="utf-8")
    r4 = run_notebook(slow_src, OUT_DIR / "run-timeout.ipynb", cell_timeout=1.0)
    show_result(r4)

    print(f"全部产物在 {OUT_DIR}/ , 源笔记未被修改。")


if __name__ == "__main__":
    main()

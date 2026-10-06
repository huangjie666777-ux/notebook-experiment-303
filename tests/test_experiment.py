"""Notebook 实验 API 测试: 规划校验、参数注入、输出收集、失败与超时行为。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from runbook_kernel import NotebookPlanError, run_notebook
from runbook_kernel.notebook import prepare_notebook

# ---------------------------------------------------------------- 构造工具


def code_cell(source, *, cell_id=None, tags=None, outputs=None, count=None):
    cell = {
        "cell_type": "code",
        "metadata": {"tags": tags} if tags else {},
        "source": source,
        "outputs": outputs if outputs is not None else [],
        "execution_count": count,
    }
    if cell_id is not None:
        cell["id"] = cell_id
    return cell


def markdown_cell(source, *, cell_id=None, attachments=None, metadata=None):
    cell = {"cell_type": "markdown", "metadata": metadata or {}, "source": source}
    if cell_id is not None:
        cell["id"] = cell_id
    if attachments is not None:
        cell["attachments"] = attachments
    return cell


def raw_cell(source, *, cell_id=None):
    cell = {"cell_type": "raw", "metadata": {}, "source": source}
    if cell_id is not None:
        cell["id"] = cell_id
    return cell


def make_notebook(cells, *, minor=5, nbformat=4):
    return {
        "nbformat": nbformat,
        "nbformat_minor": minor,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "cells": cells,
    }


def write_nb(tmp_path, notebook, name="input.ipynb"):
    path = tmp_path / name
    path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")
    return path


def read_nb(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def stream_texts(cell, name="stdout"):
    return "".join(o.get("text", "") for o in cell["outputs"] if o.get("output_type") == "stream" and o.get("name") == name)


# ---------------------------------------------------------------- 成功路径


def test_parameterized_run_end_to_end(tmp_path):
    """参数注入、旧输出清除、结构保留、输入文件不变。"""
    attachment = {"hello.txt": {"text/plain": "aGVsbG8="}}
    nb = make_notebook(
        [
            markdown_cell("# 实验\n", cell_id="md-1", attachments=attachment, metadata={"tags": ["keep-me"], "custom": 1}),
            code_cell("x = 1\nname = 'default'\n", cell_id="params", tags=["parameters"]),
            code_cell(
                "import sys\nprint(f'x={x}')\nprint('warn', file=sys.stderr)\nname\n",
                cell_id="report",
                outputs=[{"output_type": "stream", "name": "stdout", "text": "stale"}],
                count=99,
            ),
            raw_cell("raw 文本\n", cell_id="raw-1"),
        ]
    )
    src = write_nb(tmp_path, nb)
    before = src.read_bytes()
    out_path = tmp_path / "runs" / "result.ipynb"

    result = run_notebook(src, out_path, parameters={"x": 41, "name": "实验A"}, cell_timeout=30)

    assert result.status == "ok" and result.ok
    assert result.executed_cells == 3 and result.total_cells == 3
    assert src.read_bytes() == before, "输入文件被修改"

    out = read_nb(out_path)
    cells = out["cells"]
    # markdown / raw / 附件 / 元数据 / 单元 ID 原样保留
    assert cells[0]["cell_type"] == "markdown"
    assert cells[0]["id"] == "md-1"
    assert cells[0]["attachments"] == attachment
    assert cells[0]["metadata"] == {"tags": ["keep-me"], "custom": 1}
    assert cells[-1]["cell_type"] == "raw" and cells[-1]["id"] == "raw-1"
    # 覆盖值单元紧插在 parameters 单元之后
    assert cells[1]["id"] == "params" and cells[1]["execution_count"] == 1
    injected = cells[2]
    assert injected["cell_type"] == "code"
    assert "injected-parameters" in injected["metadata"]["tags"]
    assert injected["id"].startswith("injected-parameters-")
    assert "x = 41" in injected["source"] and "name = '实验A'" in injected["source"]
    assert injected["execution_count"] == 2
    # 报告单元: 旧输出被清除, 新输出齐全, 计数连续
    report = cells[3]
    assert report["execution_count"] == 3
    assert stream_texts(report) == "x=41\n"
    assert stream_texts(report, "stderr") == "warn\n"
    results = [o for o in report["outputs"] if o["output_type"] == "execute_result"]
    assert len(results) == 1 and results[0]["data"]["text/plain"] == "'实验A'"
    assert results[0]["execution_count"] == 3


def test_source_as_string_list(tmp_path):
    """source 为字符串数组的单元照常执行, 且写回时保持原形态。"""
    nb = make_notebook([code_cell(["a = 2\n", "b = 3\n", "print(a * b)\n"])])
    src = write_nb(tmp_path, nb)
    result = run_notebook(src, tmp_path / "out.ipynb")
    assert result.status == "ok"
    cell = read_nb(tmp_path / "out.ipynb")["cells"][0]
    assert isinstance(cell["source"], list), "source 形态不应被改写"
    assert stream_texts(cell) == "6\n"


def test_empty_parameters_run_defaults_without_injection(tmp_path):
    """不给参数时: 不注入覆盖单元, parameters 单元按默认值执行。"""
    nb = make_notebook(
        [
            code_cell("x = 7\n", tags=["parameters"]),
            code_cell("print(x)\n"),
        ]
    )
    src = write_nb(tmp_path, nb)
    result = run_notebook(src, tmp_path / "out.ipynb")
    assert result.status == "ok"
    cells = read_nb(tmp_path / "out.ipynb")["cells"]
    assert len(cells) == 2, "不应注入覆盖单元"
    assert stream_texts(cells[1]) == "7\n"


def test_runs_are_isolated(tmp_path):
    """每次运行启动新内核: 上一次的全局变量不会泄漏到下一次。"""
    nb1 = make_notebook([code_cell("shared_marker = 'run1'\n")])
    nb2 = make_notebook([code_cell("print('shared_marker' in dir())\n")])
    src1 = write_nb(tmp_path, nb1, "one.ipynb")
    src2 = write_nb(tmp_path, nb2, "two.ipynb")
    assert run_notebook(src1, tmp_path / "out1.ipynb").status == "ok"
    assert run_notebook(src2, tmp_path / "out2.ipynb").status == "ok"
    assert stream_texts(read_nb(tmp_path / "out2.ipynb")["cells"][0]) == "False\n"


def test_string_parameter_stays_data(tmp_path):
    """字符串参数经 repr 注入: 是可打印的数据, 不是可执行代码。"""
    payload = f"__import__('os').system('touch {tmp_path}/pwned')"
    nb = make_notebook(
        [
            code_cell("cmd = ''\n", tags=["parameters"]),
            code_cell("print(repr(cmd))\n"),
        ]
    )
    src = write_nb(tmp_path, nb)
    result = run_notebook(src, tmp_path / "out.ipynb", parameters={"cmd": payload})
    assert result.status == "ok"
    assert not (tmp_path / "pwned").exists(), "字符串参数被执行了"
    cell = read_nb(tmp_path / "out.ipynb")["cells"][-1]
    assert payload in stream_texts(cell), "字符串内容应原样保留"


# ---------------------------------------------------------------- 失败行为


def test_error_stops_by_default(tmp_path):
    nb = make_notebook(
        [
            code_cell("print('first')\n"),
            code_cell("1 / 0\n"),
            code_cell("print('third')\n"),
        ]
    )
    src = write_nb(tmp_path, nb)
    result = run_notebook(src, tmp_path / "out.ipynb")
    assert result.status == "error"
    assert result.failed_cell == 1
    assert result.ename == "ZeroDivisionError"
    assert result.executed_cells == 2
    cells = read_nb(tmp_path / "out.ipynb")["cells"]
    errors = [o for o in cells[1]["outputs"] if o["output_type"] == "error"]
    assert len(errors) == 1 and errors[0]["ename"] == "ZeroDivisionError"
    assert any("ZeroDivisionError" in line for line in errors[0]["traceback"])
    # 未执行单元保持空输出与空计数
    assert cells[2]["outputs"] == [] and cells[2]["execution_count"] is None


def test_continue_on_error(tmp_path):
    nb = make_notebook(
        [
            code_cell("1 / 0\n"),
            code_cell("print('after-error')\n"),
        ]
    )
    src = write_nb(tmp_path, nb)
    result = run_notebook(src, tmp_path / "out.ipynb", continue_on_error=True)
    assert result.status == "error", "有单元失败, 即使跑完全程也应标记 error"
    assert result.executed_cells == 2
    cells = read_nb(tmp_path / "out.ipynb")["cells"]
    assert cells[1]["execution_count"] == 2
    assert stream_texts(cells[1]) == "after-error\n"


def test_timeout_interrupts_and_terminates(tmp_path):
    nb = make_notebook(
        [
            code_cell("print('before')\n"),
            code_cell("import time\nprint('start')\ntime.sleep(60)\n"),
            code_cell("print('never')\n"),
        ]
    )
    src = write_nb(tmp_path, nb)
    t0 = time.monotonic()
    result = run_notebook(src, tmp_path / "out.ipynb", cell_timeout=1.0)
    elapsed = time.monotonic() - t0
    assert result.status == "timeout"
    assert result.failed_cell == 1
    assert elapsed < 30, "超时后实验必须终止, 不能等单元自然结束"
    cells = read_nb(tmp_path / "out.ipynb")["cells"]
    # 已收输出保留: 超时单元中断前的 stdout 与 KeyboardInterrupt 都在
    assert stream_texts(cells[1]) == "start\n"
    assert any(o["output_type"] == "error" and o["ename"] == "KeyboardInterrupt" for o in cells[1]["outputs"])
    # 未执行单元保持空输出与空计数
    assert cells[2]["outputs"] == [] and cells[2]["execution_count"] is None


# ---------------------------------------------------------------- 规划校验(不启动内核, 快速)


def test_parameters_require_tagged_cell(tmp_path):
    src = write_nb(tmp_path, make_notebook([code_cell("x = 1\n")]))
    with pytest.raises(NotebookPlanError, match="parameters"):
        prepare_notebook(src, {"x": 2})


def test_multiple_parameters_cells_rejected(tmp_path):
    src = write_nb(tmp_path, make_notebook([code_cell("a = 1\n", tags=["parameters"]), code_cell("b = 2\n", tags=["parameters"])]))
    with pytest.raises(NotebookPlanError, match="只能标记一个"):
        prepare_notebook(src)


def test_parameters_tag_on_non_code_rejected(tmp_path):
    src = write_nb(tmp_path, make_notebook([markdown_cell("# t\n", metadata={"tags": ["parameters"]})]))
    with pytest.raises(NotebookPlanError, match="code 单元"):
        prepare_notebook(src)


@pytest.mark.parametrize("bad_name", ["class", "for", "1x", "with space", "", "x-y"])
def test_bad_parameter_names_rejected(tmp_path, bad_name):
    src = write_nb(tmp_path, make_notebook([code_cell("x = 1\n", tags=["parameters"])]))
    with pytest.raises(NotebookPlanError, match="参数名"):
        prepare_notebook(src, {bad_name: 1})


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_rejected(tmp_path, bad_value):
    src = write_nb(tmp_path, make_notebook([code_cell("x = 1\n", tags=["parameters"])]))
    with pytest.raises(NotebookPlanError, match="有限"):
        prepare_notebook(src, {"x": bad_value})


@pytest.mark.parametrize(
    "bad_value",
    [object(), (1, 2), {"a": {1: "x"}}, b"bytes"],
    ids=["object", "tuple", "non-str-key", "bytes"],
)
def test_non_json_values_rejected(tmp_path, bad_value):
    src = write_nb(tmp_path, make_notebook([code_cell("x = 1\n", tags=["parameters"])]))
    with pytest.raises(NotebookPlanError):
        prepare_notebook(src, {"x": bad_value})


def test_nested_json_values_accepted(tmp_path):
    src = write_nb(tmp_path, make_notebook([code_cell("cfg = {}\n", tags=["parameters"])]))
    plan = prepare_notebook(src, {"cfg": {"layers": [64, 32], "lr": 0.01, "name": "exp", "flag": True, "none": None}})
    injected = plan.notebook["cells"][1]
    assert "injected-parameters" in injected["metadata"]["tags"]
    assert "cfg = {'layers': [64, 32], 'lr': 0.01, 'name': 'exp', 'flag': True, 'none': None}" in injected["source"]


def test_parameters_as_json_string(tmp_path):
    src = write_nb(tmp_path, make_notebook([code_cell("x = 1\n", tags=["parameters"])]))
    plan = prepare_notebook(src, '{"x": 5}')
    assert "x = 5" in plan.notebook["cells"][1]["source"]


def test_unsupported_nbformat_rejected(tmp_path):
    src = write_nb(tmp_path, make_notebook([code_cell("1\n")], nbformat=3))
    with pytest.raises(NotebookPlanError, match="nbformat 4"):
        prepare_notebook(src)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(NotebookPlanError, match="读取失败"):
        prepare_notebook(tmp_path / "nope.ipynb")


def test_injected_cell_id_only_for_nbformat_45(tmp_path):
    """nbformat_minor < 5 的笔记没有单元 ID 概念, 注入单元也不强造。"""
    src = write_nb(tmp_path, make_notebook([code_cell("x = 1\n", tags=["parameters"])], minor=2))
    plan = prepare_notebook(src, {"x": 2})
    assert "id" not in plan.notebook["cells"][1]

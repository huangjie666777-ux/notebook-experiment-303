"""笔记规划: 载入校验 nbformat 4 笔记、注入参数覆盖单元、规划执行顺序。

纯数据变换, 不接触内核与套接字: 读入 .ipynb(JSON), 产出"已清输出、
已注入覆盖单元"的笔记深拷贝与待执行 code 单元下标。输入文件永不修改;
单元 ID、元数据、Markdown、raw 与附件经深拷贝原样保留。
"""
from __future__ import annotations

import copy
import json
import keyword
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PARAMETERS_TAG = "parameters"
INJECTED_TAG = "injected-parameters"


class NotebookPlanError(ValueError):
    """笔记或参数不合法(启动内核之前抛出, 不产生结果笔记)。"""


@dataclass
class NotebookPlan:
    """一次实验的执行计划。"""

    notebook: dict  # 深拷贝后的笔记: 已清输出/计数, 已按需注入参数覆盖单元
    code_cell_indices: list[int]  # 按执行顺序的 code 单元下标


def source_to_str(source) -> str:
    """nbformat 的 source 允许字符串或字符串数组, 统一成字符串。"""
    if isinstance(source, str):
        return source
    if isinstance(source, list) and all(isinstance(part, str) for part in source):
        return "".join(source)
    raise NotebookPlanError(f"非法的单元 source: {type(source).__name__}")


def _validate_json_value(value, path: str) -> None:
    """值必须是有限 JSON: null/bool/int/float(有限)/str/list/dict(str 键)。"""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NotebookPlanError(f"参数 {path} 不是有限数值: {value!r}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _validate_json_value(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NotebookPlanError(f"参数 {path} 的键不是字符串: {key!r}")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise NotebookPlanError(f"参数 {path} 不是 JSON 值: {type(value).__name__}")


def validate_parameters(parameters) -> dict:
    """校验并规范化参数, 返回 {名字: 值}(保持给定顺序)。

    接受 dict 或 JSON 字符串; 参数名须为非关键字 Python 标识符,
    值限于有限 JSON(拒绝 NaN/Infinity 与非 JSON 类型)。
    """
    if parameters is None:
        return {}
    if isinstance(parameters, (str, bytes)):
        try:
            parameters = json.loads(parameters)
        except json.JSONDecodeError as exc:
            raise NotebookPlanError(f"参数不是合法 JSON: {exc}") from exc
    if not isinstance(parameters, Mapping):
        raise NotebookPlanError(f"参数必须是 JSON 对象, 收到: {type(parameters).__name__}")
    normalized = {}
    for name, value in parameters.items():
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise NotebookPlanError(f"非法参数名(须为非关键字 Python 标识符): {name!r}")
        _validate_json_value(value, name)
        normalized[name] = value
    return normalized


def _override_cell_source(parameters: dict) -> str:
    """生成覆盖值单元源码。

    值已校验只含有限 JSON 类型, repr 得到的是纯字面量:
    字符串被安全引用转义, 不会变成可执行代码。
    """
    lines = ["# 由 runbook_kernel 注入的参数覆盖值(自动生成, 请勿手改)"]
    lines.extend(f"{name} = {value!r}" for name, value in parameters.items())
    return "\n".join(lines) + "\n"


def _cell_tags(cell: dict) -> list:
    metadata = cell.get("metadata")
    if not isinstance(metadata, dict):
        return []
    tags = metadata.get("tags", [])
    return tags if isinstance(tags, list) else []


def prepare_notebook(input_path, parameters=None) -> NotebookPlan:
    """载入输入笔记并生成执行计划。

    - 仅支持 nbformat 4; 单元 source 支持字符串或字符串数组
    - 深拷贝后清除所有 code 单元的旧输出与执行计数, 输入文件不变
    - parameters 标签至多标记一个 code 单元; 参数非空时该单元必须存在
    - 覆盖值单元紧插在该单元之后: 先跑默认值, 再被覆盖
    """
    path = Path(input_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise NotebookPlanError(f"{path}: 读取失败: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise NotebookPlanError(f"{path}: 不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise NotebookPlanError(f"{path}: 顶层必须是 JSON 对象")
    if raw.get("nbformat") != 4:
        raise NotebookPlanError(f"{path}: 仅支持 nbformat 4, 收到 {raw.get('nbformat')!r}")
    minor = raw.get("nbformat_minor", 0)
    if not isinstance(raw.get("cells"), list):
        raise NotebookPlanError(f"{path}: 缺少 cells 数组")

    params = validate_parameters(parameters)

    notebook = copy.deepcopy(raw)
    tagged = []
    for index, cell in enumerate(notebook["cells"]):
        if not isinstance(cell, dict):
            raise NotebookPlanError(f"第 {index} 个单元不是 JSON 对象")
        source_to_str(cell.get("source", ""))  # 提前校验 source 形态
        if PARAMETERS_TAG in _cell_tags(cell):
            tagged.append((index, cell))
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None

    if len(tagged) > 1:
        raise NotebookPlanError(f"parameters 标签只能标记一个单元, 发现 {len(tagged)} 个")
    if tagged and tagged[0][1].get("cell_type") != "code":
        raise NotebookPlanError("parameters 标签只能标记 code 单元")
    if params and not tagged:
        raise NotebookPlanError("提供了参数, 但笔记中没有带 parameters 标签的 code 单元")

    if params:
        anchor = tagged[0][0]
        override = {
            "cell_type": "code",
            "metadata": {"tags": [INJECTED_TAG]},
            "source": _override_cell_source(params),
            "outputs": [],
            "execution_count": None,
        }
        if isinstance(minor, int) and minor >= 5:
            override["id"] = f"injected-parameters-{uuid.uuid4().hex[:8]}"
        notebook["cells"].insert(anchor + 1, override)

    code_indices = [i for i, cell in enumerate(notebook["cells"]) if cell.get("cell_type") == "code"]
    return NotebookPlan(notebook=notebook, code_cell_indices=code_indices)

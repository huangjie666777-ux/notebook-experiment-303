"""消息收集: 按父消息 ID 归集一个单元的执行产出。

每个单元发出一个 execute_request 后, 同时监听 shell / iopub / control
三条通道, 只认 parent_header.msg_id 等于本次请求的消息 —— 迟到或外源
消息(如中断请求自己的 busy/idle)直接丢弃, 不会归入本单元, 更不会漏进
下一单元。execute_reply 与 status idle 都收到, 单元才算完结, 缺一不可。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from queue import Empty

import zmq

POLL_SECONDS = 0.05  # 轮询步长: 兼顾实时性与空转开销


@dataclass
class CollectedCell:
    """一个单元收集到的终态与标准笔记输出(nbformat 4)。"""

    status: str = "ok"  # "ok" | "error"
    execution_count: int | None = None
    outputs: list[dict] = field(default_factory=list)
    ename: str = ""
    evalue: str = ""
    timed_out: bool = False


class _Collector:
    """单个单元的消息归集器(绑定一个 parent msg_id)。"""

    def __init__(self, client, msg_id: str) -> None:
        self.client = client
        self.msg_id = msg_id
        self.cell = CollectedCell()
        self.got_reply = False
        self.got_idle = False
        self._kind = {
            client.shell_channel.socket: "shell",
            client.iopub_channel.socket: "iopub",
            client.control_channel.socket: "control",
        }
        self._poller = zmq.Poller()
        for sock in self._kind:
            self._poller.register(sock, zmq.POLLIN)

    @property
    def done(self) -> bool:
        """execute_reply 与 idle 都到齐才完结。"""
        return self.got_reply and self.got_idle

    def pump(self, timeout: float) -> None:
        """在 timeout 秒内收取并归集消息。"""
        events = dict(self._poller.poll(max(0, int(timeout * 1000))))
        for sock in events:
            kind = self._kind[sock]
            while True:  # 排空当前就绪的全部消息
                try:
                    msg = self._get_msg(kind)
                except Empty:
                    break
                self._handle(kind, msg)

    def _get_msg(self, kind: str):
        channel = getattr(self.client, f"{kind}_channel")
        return channel.get_msg(timeout=0)

    def _handle(self, kind: str, msg: dict) -> None:
        parent = msg.get("parent_header", {}).get("msg_id")
        if parent != self.msg_id:
            return  # 迟到/外源消息: 不归入本单元
        if kind == "shell":
            if msg.get("msg_type") == "execute_reply":
                self._on_reply(msg.get("content") or {})
        elif kind == "iopub":
            self._on_iopub(msg.get("msg_type"), msg.get("content") or {})

    # ---- shell ----

    def _on_reply(self, content: dict) -> None:
        self.got_reply = True
        status = content.get("status")
        self.cell.status = "error" if status == "error" else "ok"
        count = content.get("execution_count")
        self.cell.execution_count = count if isinstance(count, int) else None
        if self.cell.status == "error":
            self.cell.ename = str(content.get("ename", ""))
            self.cell.evalue = str(content.get("evalue", ""))

    # ---- iopub ----

    def _on_iopub(self, msg_type: str, content: dict) -> None:
        if msg_type == "status":
            if content.get("execution_state") == "idle":
                self.got_idle = True
        elif msg_type == "stream":
            self._append_stream(content.get("name", "stdout"), content.get("text", ""))
        elif msg_type in ("execute_result", "display_data"):
            self.cell.outputs.append(
                {
                    "output_type": msg_type,
                    "execution_count": content.get("execution_count"),
                    "data": content.get("data", {}),
                    "metadata": content.get("metadata", {}),
                }
            )
        elif msg_type == "error":
            self.cell.outputs.append(
                {
                    "output_type": "error",
                    "ename": content.get("ename", ""),
                    "evalue": content.get("evalue", ""),
                    "traceback": content.get("traceback", []),
                }
            )

    def _append_stream(self, name: str, text: str) -> None:
        """连续的同名流合并成一条输出(与 Jupyter 习惯一致)。"""
        outputs = self.cell.outputs
        if outputs and outputs[-1].get("output_type") == "stream" and outputs[-1].get("name") == name:
            outputs[-1]["text"] += text
        else:
            outputs.append({"output_type": "stream", "name": name, "text": text})


def _send_interrupt(client) -> None:
    """经 control 通道请求中断当前单元(回复由轮询循环顺带排空)。"""
    client.control_channel.send(client.session.msg("interrupt_request", {}))


def collect_cell(client, msg_id: str, timeout: float | None = None, interrupt_grace: float = 2.0) -> CollectedCell:
    """收集一个 execute_request(msg_id)的全部产出, 直到终态到齐。

    timeout 为 None 表示不限时; 超时则先经 control 通道中断当前单元,
    再给 interrupt_grace 秒宽限收尾(通常能补收到 KeyboardInterrupt 的
    error 输出与 execute_reply), 之后无论是否到齐都返回 timed_out=True,
    已收到的输出原样保留。
    """
    collector = _Collector(client, msg_id)
    deadline = None if timeout is None else time.monotonic() + timeout
    while not collector.done:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            break
        collector.pump(POLL_SECONDS if remaining is None else min(POLL_SECONDS, remaining))
    if collector.done:
        return collector.cell

    _send_interrupt(client)
    grace_deadline = time.monotonic() + interrupt_grace
    while not collector.done:
        remaining = grace_deadline - time.monotonic()
        if remaining <= 0:
            break
        collector.pump(min(POLL_SECONDS, remaining))
    collector.cell.timed_out = True
    return collector.cell

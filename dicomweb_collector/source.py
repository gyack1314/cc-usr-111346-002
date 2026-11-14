"""来源适配器接口：采集端只通过它接触研究影像接口。

适配器负责真实的HTTP；采集端负责身份、版本、边界与落盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Protocol, Tuple


@dataclass(frozen=True)
class Capabilities:
    """来源能力声明。

    ordered_frame_parts=True 表示来源有明确契约：帧请求的响应部件
    严格按请求URL中帧号列表的顺序返回。只有这时采集端才允许使用
    请求顺序模式；否则部件必须自带Content-Location。
    """

    ordered_frame_parts: bool = False


@dataclass
class SourceResponse:
    """一次响应的元信息与字节流。

    generation：源当前的冻结代；etag：该资源的强ETag（"W/"前缀为弱）。
    history：重定向链中每一跳的URL（不含最终URL），用于逐跳范围检查。
    body：字节块迭代器，采集端按块消费，不假设可整体入内存。
    """

    url: str  # 最终地址（重定向后）
    status: int
    headers: Dict[str, str]
    body: Iterable[bytes]
    generation: Optional[str] = None
    etag: Optional[str] = None
    history: Tuple[str, ...] = field(default_factory=tuple)

    def strong_etag(self) -> Optional[str]:
        if self.etag and not self.etag.startswith("W/"):
            return self.etag
        return None


class SourceAdapter(Protocol):
    """研究影像接口的适配器协议。"""

    capabilities: Capabilities

    def fetch(self, url: str, headers: Dict[str, str]) -> SourceResponse:
        """GET一个地址。采集端只在同源时放入会话凭据。"""
        ...

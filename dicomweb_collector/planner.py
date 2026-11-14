"""帧请求规划：把"还缺哪些帧"变成一组HTTP请求。

规划只决定请求如何合并/拆分；帧身份不依赖请求边界，
部件身份由Content-Location或可证明的请求顺序在接收端确定，
因此合并/拆分请求不改变最终帧身份。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from .errors import MetadataError
from .identity import InstanceKey
from .net import frames_url


@dataclass(frozen=True)
class FrameRequest:
    """一次帧请求：单实例、有序帧号列表、对应URL。"""

    request_id: str
    instance: InstanceKey
    frame_numbers: Tuple[int, ...]
    url: str
    order_provable: bool  # 请求本身枚举了顺序且来源契约保证按序返回


class FrameRequestPlanner:
    def __init__(self, base_url: str, max_frames_per_request: int = 128,
                 ordered_parts: bool = False):
        if max_frames_per_request < 1:
            raise ValueError("max_frames_per_request必须为正")
        self._base = base_url.rstrip("/")
        self._max = max_frames_per_request
        self._ordered = ordered_parts

    def plan(self, needed: Mapping[InstanceKey, Tuple[int, ...]],
             number_of_frames: Mapping[InstanceKey, int]) -> List[FrameRequest]:
        """needed：每实例待取帧号（已排序去重）。空集实例不产生请求。"""
        requests: List[FrameRequest] = []
        counter = 0
        for key in sorted(needed):
            frames = tuple(needed[key])
            if not frames:
                continue
            total = number_of_frames[key]
            for n in frames:
                if n < 1 or n > total:
                    raise MetadataError(
                        f"请求帧号{n}越出实例{key.sop_instance_uid}的NumberOfFrames={total}")
            for start in range(0, len(frames), self._max):
                chunk = frames[start : start + self._max]
                counter += 1
                requests.append(FrameRequest(
                    request_id=f"rq-{counter:04d}",
                    instance=key,
                    frame_numbers=chunk,
                    url=frames_url(self._base, key, chunk),
                    order_provable=self._ordered,
                ))
        return requests

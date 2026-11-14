"""研究、序列、实例、帧的身份与版本绑定。

身份从请求URL派生，沿采集流水线一直保留到落盘文件名与清单记录；
版本绑定（generation + 元数据强ETag）决定缓存能否复用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"


@dataclass(frozen=True, order=True)
class InstanceKey:
    """实例身份：study/series/sop三元组。"""

    study_uid: str
    series_uid: str
    sop_instance_uid: str

    def frame(self, number: int) -> "FrameKey":
        return FrameKey(self.study_uid, self.series_uid, self.sop_instance_uid, number)

    def as_tuple(self) -> tuple:
        return (self.study_uid, self.series_uid, self.sop_instance_uid)


@dataclass(frozen=True, order=True)
class FrameKey:
    """帧身份：实例身份加1起始的帧号。"""

    study_uid: str
    series_uid: str
    sop_instance_uid: str
    frame_number: int

    def __post_init__(self):
        if self.frame_number < 1:
            raise ValueError("帧号从1起")

    @property
    def instance(self) -> InstanceKey:
        return InstanceKey(self.study_uid, self.series_uid, self.sop_instance_uid)

    def label(self) -> str:
        return f"{self.study_uid}/{self.series_uid}/{self.sop_instance_uid}/frames/{self.frame_number}"


@dataclass(frozen=True)
class VersionTag:
    """可验证的版本绑定：源generation加实例元数据强ETag。

    两者齐备才允许宣称"同版"；任一缺失只能标一致性未知（version=None）。
    """

    generation: str
    metadata_etag: str


@dataclass(frozen=True)
class Provenance:
    """帧的出处：来自哪一个响应的哪一个部件（或批量正文的哪一段）。"""

    kind: str  # "multipart-part" | "bulk-slice" | "inline-binary"
    source_url: str
    request_id: Optional[str] = None
    part_index: Optional[int] = None
    content_location: Optional[str] = None
    offset: Optional[int] = None
    response_generation: Optional[str] = None
    response_etag: Optional[str] = None

    def describe(self) -> str:
        if self.kind == "multipart-part":
            return f"{self.request_id}#part{self.part_index}"
        if self.kind == "bulk-slice":
            return f"bulk:{self.source_url}@{self.offset}"
        return f"inline:{self.source_url}"


@dataclass(frozen=True)
class FrameRecord:
    """已核验帧的登记：身份、版本、内容摘要、长度、出处。"""

    key: FrameKey
    version: Optional[VersionTag]
    sha256: str
    length: int  # 像素字节数，不含偶数填充
    padding: int  # 填充字节数（0或1），不算像素
    provenance: Provenance


@dataclass(frozen=True)
class InstanceRecord:
    """实例登记：全部所需帧核验后才产生。"""

    key: InstanceKey
    version: Optional[VersionTag]
    required_frames: tuple
    verified_frames: tuple
    full_instance: bool  # 所需帧是否等于实例全部帧
    consistency: str  # "bound" | "unknown"

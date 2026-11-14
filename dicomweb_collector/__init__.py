"""研究影像DICOMweb逐帧采集端。

把研究、序列、实例、帧的身份从请求一直保留到落盘；不分析影像内容。
模块划分：
- dicomjson：DICOM JSON元数据解析与冻结目录
- planner：帧请求规划（合并/拆分不改变帧身份）
- mime / receiver：流式multipart解析与部件身份判定
- store：临时对象、已核验帧、清单与恢复
- collector：编排、版本绑定、范围与凭据控制、统计
- check：独立帧清单检查器
"""
from .collector import (
    Collector,
    CollectorConfig,
    InstanceRequest,
    RunReport,
    VIA_BULK,
    VIA_FRAMES,
)
from .dicomjson import MetadataCatalog
from .errors import (
    AmbiguousPartError,
    CollectorError,
    FrameConflictError,
    FrameLayoutError,
    IncompleteInstanceError,
    MetadataError,
    MimeError,
    PartIdentityError,
    ScopeViolationError,
    TransferSyntaxError,
    UnexpectedPartError,
    VersionMismatchError,
)
from .identity import (
    EXPLICIT_VR_LE,
    FrameKey,
    FrameRecord,
    InstanceKey,
    InstanceRecord,
    Provenance,
    VersionTag,
)
from .net import SourceScope
from .source import Capabilities, SourceAdapter, SourceResponse
from .store import FrameStore

__all__ = [
    "AmbiguousPartError",
    "Capabilities",
    "Collector",
    "CollectorConfig",
    "CollectorError",
    "EXPLICIT_VR_LE",
    "FrameConflictError",
    "FrameKey",
    "FrameLayoutError",
    "FrameRecord",
    "FrameStore",
    "IncompleteInstanceError",
    "InstanceKey",
    "InstanceRecord",
    "InstanceRequest",
    "MetadataCatalog",
    "MetadataError",
    "MimeError",
    "PartIdentityError",
    "Provenance",
    "RunReport",
    "ScopeViolationError",
    "SourceAdapter",
    "SourceResponse",
    "SourceScope",
    "TransferSyntaxError",
    "UnexpectedPartError",
    "VIA_BULK",
    "VIA_FRAMES",
    "VersionMismatchError",
    "VersionTag",
]

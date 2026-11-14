"""采集端的错误分类。

所有错误都拒绝静默降级：身份不明、版本不符、长度不符一律抛出，
不允许猜测或顶替。
"""


class CollectorError(Exception):
    """采集端错误基类。"""


class MetadataError(CollectorError):
    """DICOM JSON元数据缺失、为空或与限定条件不符。"""


class TransferSyntaxError(CollectorError):
    """传输语法不是限定的未压缩Explicit VR Little Endian。"""


class MimeError(CollectorError):
    """multipart/related结构错误：边界、头部或截断。"""


class PartIdentityError(CollectorError):
    """部件身份无法确定或自相矛盾。"""


class AmbiguousPartError(PartIdentityError):
    """部件既没有Content-Location也没有可证明的顺序依据。"""


class UnexpectedPartError(PartIdentityError):
    """部件属于非请求实例或范围外帧号。"""


class FrameConflictError(CollectorError):
    """同一帧的两个部件字节不同。"""


class FrameLayoutError(CollectorError):
    """帧长度与元数据目录冻结的像素布局不符。"""


class VersionMismatchError(CollectorError):
    """元数据、帧或缓存不属于同一冻结代。"""


class ScopeViolationError(CollectorError):
    """BulkDataURI或重定向越出配置的研究源范围。"""


class IncompleteInstanceError(CollectorError):
    """所需帧未全部核验，实例不得登记为完整。"""

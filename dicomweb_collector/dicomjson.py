"""DICOM JSON元数据解析与冻结的元数据目录。

限定：未压缩Explicit VR Little Endian、BitsAllocated 8或16、单样本灰度。
不实现压缩编解码，不做完整Part10解析。

标签状态分别留存：标签缺失(missing)、空Value(empty)、零长度值(zero_length)、
InlineBinary(inline_binary)、BulkDataURI(bulk_data)、正常值(value)。
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

from .errors import MetadataError, TransferSyntaxError
from .identity import EXPLICIT_VR_LE, InstanceKey, VersionTag

# 标签状态
MISSING = "missing"
EMPTY = "empty"
ZERO_LENGTH = "zero_length"
VALUE = "value"
INLINE_BINARY = "inline_binary"
BULK_DATA = "bulk_data"

TAG_ROWS = "00280010"
TAG_COLUMNS = "00280011"
TAG_BITS_ALLOCATED = "00280100"
TAG_SAMPLES_PER_PIXEL = "00280002"
TAG_PHOTOMETRIC = "00280004"
TAG_NUMBER_OF_FRAMES = "00280008"
TAG_PIXEL_DATA = "7FE00010"
TAG_STUDY_UID = "0020000D"
TAG_SERIES_UID = "0020000E"
TAG_SOP_UID = "00080018"

_MONOCHROME = ("MONOCHROME1", "MONOCHROME2")


@dataclass(frozen=True)
class TagValue:
    """一个标签的解析结果，状态与原始内容分开留存。"""

    kind: str
    vr: Optional[str] = None
    values: Optional[tuple] = None
    inline_binary: Optional[bytes] = None
    bulk_data_uri: Optional[str] = None


def parse_tag(entry) -> TagValue:
    """解析DICOM JSON的一个标签条目，区分空Value、零长度值与载体类型。"""
    if not isinstance(entry, dict):
        raise MetadataError("标签条目不是对象")
    if not entry:
        # 整个条目为空对象：标签在但无任何内容，按空Value留存
        return TagValue(EMPTY, vr=None, values=())
    vr = entry.get("vr")
    if not isinstance(vr, str) or not vr:
        raise MetadataError("标签缺少vr")
    has_value = "Value" in entry
    has_inline = "InlineBinary" in entry
    has_bulk = "BulkDataURI" in entry
    carriers = sum([has_value and bool(entry["Value"]), has_inline, has_bulk])
    if carriers > 1:
        raise MetadataError(f"标签{vr}同时携带多种值载体")
    if has_bulk:
        uri = entry["BulkDataURI"]
        if not isinstance(uri, str) or not uri:
            raise MetadataError("BulkDataURI为空")
        return TagValue(BULK_DATA, vr=vr, bulk_data_uri=uri)
    if has_inline:
        raw = entry["InlineBinary"]
        if not isinstance(raw, str):
            raise MetadataError("InlineBinary不是字符串")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MetadataError("InlineBinary不是有效base64") from exc
        if len(data) == 0:
            return TagValue(ZERO_LENGTH, vr=vr, inline_binary=data)
        return TagValue(INLINE_BINARY, vr=vr, inline_binary=data)
    if not has_value or not entry["Value"]:
        # "Value": [] 或Value键缺失：标签在但为空
        return TagValue(EMPTY, vr=vr, values=())
    values = entry["Value"]
    if not isinstance(values, list):
        raise MetadataError("Value不是数组")
    if any(v == "" or v == {} or v == [] for v in values):
        # 零长度值与空Value、标签缺失分别留存
        return TagValue(ZERO_LENGTH, vr=vr, values=tuple(values))
    return TagValue(VALUE, vr=vr, values=tuple(values))


def parse_dataset(dataset_json) -> Mapping:
    """解析整个DICOM JSON数据集为 标签->TagValue 的只读映射。"""
    if not isinstance(dataset_json, dict):
        raise MetadataError("数据集不是JSON对象")
    tags = {}
    for tag, entry in dataset_json.items():
        if not isinstance(tag, str) or len(tag) != 8:
            raise MetadataError(f"标签键{tag!r}不是8位十六进制形式")
        tags[tag.upper()] = parse_tag(entry)
    return MappingProxyType(tags)


@dataclass(frozen=True)
class PixelDataSource:
    """像素载体：BulkDataURI、InlineBinary或缺失。"""

    kind: str  # BULK_DATA | INLINE_BINARY | MISSING | EMPTY | ZERO_LENGTH
    uri: Optional[str] = None
    data: Optional[bytes] = None


@dataclass(frozen=True)
class InstanceDescriptor:
    """由元数据目录冻结的每实例布局；采集期间不可变。"""

    key: InstanceKey
    rows: int
    columns: int
    bits_allocated: int
    samples_per_pixel: int
    photometric: Optional[str]
    number_of_frames: int
    transfer_syntax: str
    pixel_data: PixelDataSource
    assumed: tuple = ()  # 缺失/为空而按约定默认的标签名，明示不静默

    @property
    def frame_payload_bytes(self) -> int:
        """每帧像素字节数，不含填充。"""
        return self.rows * self.columns * self.samples_per_pixel * (self.bits_allocated // 8)

    @property
    def padded_frame_bytes(self) -> int:
        """每帧在线字节数：允许偶数长度填充，但填充不算像素。"""
        n = self.frame_payload_bytes
        return n if n % 2 == 0 else n + 1


def _require(tags: Mapping, tag: str, what: str) -> TagValue:
    tv = tags.get(tag)
    if tv is None:
        raise MetadataError(f"{what}标签{tag}缺失")
    if tv.kind in (EMPTY, ZERO_LENGTH):
        raise MetadataError(f"{what}标签{tag}为空或零长度")
    return tv


def _one_int(tv: TagValue, what: str) -> int:
    if tv.kind != VALUE or tv.values is None or len(tv.values) != 1:
        raise MetadataError(f"{what}不是单值")
    value = tv.values[0]
    if isinstance(value, bool):
        raise MetadataError(f"{what}不是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise MetadataError(f"{what}不是整数") from exc
    raise MetadataError(f"{what}不是整数")


def _one_str(tv: TagValue, what: str) -> str:
    if tv.kind != VALUE or tv.values is None or len(tv.values) != 1:
        raise MetadataError(f"{what}不是单值")
    value = tv.values[0]
    if not isinstance(value, str) or not value:
        raise MetadataError(f"{what}不是非空字符串")
    return value


def build_descriptor(
    key: InstanceKey,
    tags: Mapping,
    transfer_syntax: str,
) -> InstanceDescriptor:
    """从解析后的标签构建冻结描述符；身份以请求为准，元数据UID只做交叉核对。

    只接受：Explicit VR Little Endian、BitsAllocated 8/16、单样本灰度。
    """
    if transfer_syntax != EXPLICIT_VR_LE:
        raise TransferSyntaxError(f"不支持的传输语法{transfer_syntax}，仅接受{EXPLICIT_VR_LE}")
    # 元数据内UID若存在则必须与请求身份一致，身份本身取自请求
    for tag, expect, what in (
        (TAG_STUDY_UID, key.study_uid, "StudyInstanceUID"),
        (TAG_SERIES_UID, key.series_uid, "SeriesInstanceUID"),
        (TAG_SOP_UID, key.sop_instance_uid, "SOPInstanceUID"),
    ):
        tv = tags.get(tag)
        if tv is not None and tv.kind == VALUE and tv.values:
            if _one_str(tv, what) != expect:
                raise MetadataError(f"{what}与请求身份不符")

    rows = _one_int(_require(tags, TAG_ROWS, "Rows"), "Rows")
    columns = _one_int(_require(tags, TAG_COLUMNS, "Columns"), "Columns")
    bits = _one_int(_require(tags, TAG_BITS_ALLOCATED, "BitsAllocated"), "BitsAllocated")
    if rows < 1 or columns < 1:
        raise MetadataError("Rows/Columns必须为正")
    if bits not in (8, 16):
        raise MetadataError(f"BitsAllocated={bits}，仅接受8或16")

    assumed = []
    samples_tag = tags.get(TAG_SAMPLES_PER_PIXEL)
    if samples_tag is None or samples_tag.kind in (EMPTY, ZERO_LENGTH):
        samples = 1  # 单样本默认，记入assumed明示
        assumed.append("SamplesPerPixel")
    else:
        samples = _one_int(samples_tag, "SamplesPerPixel")
    if samples != 1:
        raise MetadataError(f"SamplesPerPixel={samples}，仅接受单样本灰度")

    photometric_tag = tags.get(TAG_PHOTOMETRIC)
    if photometric_tag is None or photometric_tag.kind in (EMPTY, ZERO_LENGTH):
        photometric = None
    else:
        photometric = _one_str(photometric_tag, "PhotometricInterpretation")
        if photometric not in _MONOCHROME:
            raise MetadataError(
                f"PhotometricInterpretation={photometric}，仅接受单样本灰度")

    nf_tag = tags.get(TAG_NUMBER_OF_FRAMES)
    if nf_tag is None:
        number_of_frames = 1
    elif nf_tag.kind in (EMPTY, ZERO_LENGTH):
        raise MetadataError("NumberOfFrames为空或零长度")
    else:
        number_of_frames = _one_int(nf_tag, "NumberOfFrames")
        if number_of_frames < 1:
            raise MetadataError("NumberOfFrames必须为正")

    pd = tags.get(TAG_PIXEL_DATA)
    if pd is None:
        pixel = PixelDataSource(MISSING)
    elif pd.kind == BULK_DATA:
        pixel = PixelDataSource(BULK_DATA, uri=pd.bulk_data_uri)
    elif pd.kind == INLINE_BINARY:
        pixel = PixelDataSource(INLINE_BINARY, data=pd.inline_binary)
    else:
        pixel = PixelDataSource(pd.kind)

    return InstanceDescriptor(
        key=key,
        rows=rows,
        columns=columns,
        bits_allocated=bits,
        samples_per_pixel=samples,
        photometric=photometric,
        number_of_frames=number_of_frames,
        transfer_syntax=transfer_syntax,
        pixel_data=pixel,
        assumed=tuple(assumed),
    )


@dataclass(frozen=True)
class CatalogEntry:
    """目录条目：冻结描述符 + 版本绑定 + 元数据来源地址。"""

    key: InstanceKey
    descriptor: InstanceDescriptor
    version: Optional[VersionTag]  # None表示一致性未知
    metadata_url: str
    tags: Mapping  # 解析后的全部标签，供审计


class MetadataCatalog:
    """冻结的元数据目录：一次采集运行内不可变。

    目录代(generation)在构建时确定；各条目generation不一致即拒绝。
    """

    def __init__(self, entries):
        self._entries = dict(entries)
        generations = {e.version.generation for e in self._entries.values() if e.version}
        if len(generations) > 1:
            from .errors import VersionMismatchError

            raise VersionMismatchError(f"元数据目录跨代：{sorted(generations)}")
        self._generation = generations.pop() if generations else None

    @property
    def generation(self) -> Optional[str]:
        return self._generation

    def entry(self, key: InstanceKey) -> CatalogEntry:
        try:
            return self._entries[key]
        except KeyError:
            raise MetadataError(f"实例{key.sop_instance_uid}不在元数据目录中") from None

    def __contains__(self, key: InstanceKey) -> bool:
        return key in self._entries

    def __iter__(self):
        return iter(self._entries.values())

"""部件接收：把multipart响应的每个部件钉到帧身份上。

身份判定顺序：
1. Content-Location解析为规范帧地址——优先依据；
2. 请求顺序模式——仅当请求枚举了有序帧列表且来源契约保证按序返回；
3. 两者都有时必须一致，都没有时拒绝歧义，不猜。

每个部件同时核对：Content-Type的transfer-syntax、声明长度与实际长度、
以及元数据目录冻结的像素布局（允许偶数填充，填充不算像素）。
"""
from __future__ import annotations

from typing import List, Optional

from .dicomjson import MetadataCatalog
from .errors import (
    AmbiguousPartError,
    FrameLayoutError,
    MimeError,
    PartIdentityError,
    TransferSyntaxError,
    UnexpectedPartError,
)
from .identity import EXPLICIT_VR_LE, FrameKey, FrameRecord, Provenance
from .mime import MultipartParser, RawPartHeaders
from .net import parse_content_type, parse_frame_location
from .planner import FrameRequest
from .source import SourceResponse
from .store import FrameStore


class _PartAssembler:
    """一个部件的接收状态：身份、声明长度、临时对象。"""

    def __init__(self, receiver: "PartReceiver", key: FrameKey,
                 content_location: Optional[str], declared_length: Optional[int],
                 part_index: int):
        self.receiver = receiver
        self.key = key
        self.content_location = content_location
        self.declared_length = declared_length
        self.part_index = part_index
        self.spool = receiver._store.new_part_file()
        self.length = 0
        self.record: Optional[FrameRecord] = None
        self.duplicate = False

    def write(self, data: bytes) -> None:
        self.spool.write(data)
        self.length += len(data)

    def close(self) -> None:
        self.receiver._commit(self)

    def abort(self) -> None:
        self.spool.close()


class PartReceiver:
    """消费一个帧响应，返回本次核验的帧记录。"""

    def __init__(self, catalog: MetadataCatalog, store: FrameStore,
                 request: FrameRequest, response: SourceResponse):
        self._catalog = catalog
        self._store = store
        self._request = request
        self._response = response
        self._entry = catalog.entry(request.instance)
        media_type, params = parse_content_type(response.headers.get("Content-Type"))
        if media_type != "multipart/related":
            raise MimeError(f"帧响应不是multipart/related：{media_type}")
        boundary = params.get("boundary")
        if not boundary:
            raise MimeError("multipart缺少boundary参数")
        self._check_transfer_syntax(params.get("transfer-syntax"), "multipart")
        self._boundary = boundary
        self._declared_total = response.headers.get("Content-Length")
        self._assemblers: List[_PartAssembler] = []
        self._order_cursor = 0
        self.duplicates = 0

    # ------------------------------------------------------------------ API
    def consume(self) -> List[FrameRecord]:
        parser = MultipartParser(self._boundary, self._open_part)
        total = 0
        try:
            for chunk in self._response.body:
                total += len(chunk)
                parser.feed(chunk)
            parser.finish()
        except BaseException:
            for assembler in self._assemblers:
                assembler.abort()
            raise
        if self._declared_total is not None:
            try:
                declared = int(self._declared_total)
            except ValueError:
                raise MimeError("响应Content-Length不是整数") from None
            if declared != total:
                raise MimeError(
                    f"响应长度不符：声明{declared}字节，实收{total}字节")
        records = [a.record for a in self._assemblers if a.record is not None]
        self.duplicates = sum(1 for a in self._assemblers if a.duplicate)
        return records

    # ------------------------------------------------------------- identity
    def _open_part(self, headers: RawPartHeaders) -> _PartAssembler:
        part_index = len(self._assemblers)
        request = self._request

        location = headers.get("Content-Location")
        key_from_location: Optional[FrameKey] = None
        if location is not None:
            key_from_location = parse_frame_location(location, request.url)
            if key_from_location is None:
                raise PartIdentityError(
                    f"Content-Location不是规范帧地址：{location!r}")
            if key_from_location.instance != request.instance:
                raise UnexpectedPartError(
                    f"部件属于非请求实例：{key_from_location.label()}")
            if key_from_location.frame_number not in request.frame_numbers:
                raise UnexpectedPartError(
                    f"部件帧号越出请求范围：{key_from_location.label()}")

        key_from_order: Optional[FrameKey] = None
        if request.order_provable:
            if self._order_cursor >= len(request.frame_numbers):
                raise UnexpectedPartError("部件数量超过请求帧数")
            number = request.frame_numbers[self._order_cursor]
            key_from_order = request.instance.frame(number)
        self._order_cursor += 1

        if key_from_location and key_from_order and key_from_location != key_from_order:
            raise PartIdentityError(
                f"Content-Location={key_from_location.label()}与请求顺序"
                f"={key_from_order.label()}矛盾")
        key = key_from_location or key_from_order
        if key is None:
            raise AmbiguousPartError(
                f"第{part_index}个部件既无Content-Location也无可证明的顺序依据，拒绝猜测")

        part_type, part_params = parse_content_type(
            headers.get("Content-Type") or "application/octet-stream")
        if part_type != "application/octet-stream":
            raise MimeError(f"帧部件Content-Type应为application/octet-stream：{part_type}")
        self._check_transfer_syntax(part_params.get("transfer-syntax"), "部件")

        declared = headers.get("Content-Length")
        declared_length: Optional[int] = None
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                raise MimeError("部件Content-Length不是整数") from None

        assembler = _PartAssembler(self, key, location, declared_length, part_index)
        self._assemblers.append(assembler)
        return assembler

    # --------------------------------------------------------------- commit
    def _commit(self, assembler: _PartAssembler) -> None:
        desc = self._entry.descriptor
        actual = assembler.length
        if assembler.declared_length is not None and assembler.declared_length != actual:
            raise FrameLayoutError(
                f"帧{assembler.key.label()}部件长度不符："
                f"声明{assembler.declared_length}字节，实收{actual}字节")
        payload_len = desc.frame_payload_bytes
        padded_len = desc.padded_frame_bytes
        if actual == payload_len:
            padding = 0
        elif actual == padded_len and padded_len != payload_len:
            padding = 1  # 偶数长度填充：允许，但不算像素
        else:
            raise FrameLayoutError(
                f"帧{assembler.key.label()}长度{actual}与冻结布局不符"
                f"（像素{payload_len}字节，允许填充到{padded_len}字节）")
        assembler.spool.seek(0)
        payload = assembler.spool.read(payload_len)
        assembler.spool.close()
        provenance = Provenance(
            kind="multipart-part",
            source_url=self._response.url,
            request_id=self._request.request_id,
            part_index=assembler.part_index,
            content_location=assembler.content_location,
            response_generation=self._response.generation,
            response_etag=self._response.etag,
        )
        record, duplicate = self._store.commit_part(
            assembler.key, self._entry.version, payload, padding, provenance)
        assembler.record = record
        assembler.duplicate = duplicate

    # --------------------------------------------------------------- checks
    @staticmethod
    def _check_transfer_syntax(value: Optional[str], where: str) -> None:
        if value is not None and value != EXPLICIT_VR_LE:
            raise TransferSyntaxError(
                f"{where}的transfer-syntax={value}，仅接受{EXPLICIT_VR_LE}")

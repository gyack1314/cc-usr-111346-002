"""采集编排：元数据目录 -> 帧请求规划 -> 部件接收 -> 实例登记。

流程不变量：
1. 先取元数据并冻结目录代；帧、批量正文、缓存都必须属于同一冻结代；
2. 恢复只复用已核验且版本相符的帧；
3. 全部所需帧核验后才登记实例；任何一步失败都抛出，不静默降级。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin

from .dicomjson import (
    BULK_DATA,
    INLINE_BINARY,
    CatalogEntry,
    MetadataCatalog,
    build_descriptor,
    parse_dataset,
)
from .errors import (
    CollectorError,
    FrameLayoutError,
    MetadataError,
    ScopeViolationError,
    VersionMismatchError,
)
from .identity import (
    FrameKey,
    FrameRecord,
    InstanceKey,
    InstanceRecord,
    Provenance,
    VersionTag,
)
from .net import SourceScope, metadata_url
from .planner import FrameRequestPlanner
from .receiver import PartReceiver
from .source import SourceAdapter, SourceResponse
from .store import FrameStore

VIA_FRAMES = "frames"
VIA_BULK = "bulk"


@dataclass(frozen=True)
class CollectorConfig:
    base_url: str
    scope: SourceScope
    credentials: Optional[Mapping[str, str]] = None  # 会话凭据，仅同源发送
    max_frames_per_request: int = 128


@dataclass(frozen=True)
class InstanceRequest:
    """采集请求：实例身份 + 所需帧（None为全部）+ 取帧路径。"""

    key: InstanceKey
    frames: Optional[Tuple[int, ...]] = None
    via: str = VIA_FRAMES


@dataclass
class RequestStat:
    """一次实际请求的统计。"""

    purpose: str  # "metadata" | "frames" | "bulk"
    url: str
    status: int
    bytes_received: int = 0
    generation: Optional[str] = None
    etag: Optional[str] = None
    credentials_sent: bool = False


@dataclass
class InstanceReport:
    key: InstanceKey
    required_frames: Tuple[int, ...]
    reused_frames: Tuple[int, ...]  # 恢复自已核验缓存
    fetched_frames: Tuple[int, ...]  # 本次新取
    record: InstanceRecord


@dataclass
class RunReport:
    """一次采集运行的完整账目：版本、一致性、请求、字节、每帧出处。"""

    generation: Optional[str]
    consistency: str  # "bound" | "unknown"
    requests: List[RequestStat]
    frames: Dict[FrameKey, FrameRecord]
    instances: List[InstanceReport]
    duplicates: int = 0

    @property
    def bytes_received(self) -> int:
        return sum(r.bytes_received for r in self.requests)

    def provenance_map(self) -> Dict[str, str]:
        """每帧来自哪一个响应部件。"""
        return {key.label(): rec.provenance.describe()
                for key, rec in sorted(self.frames.items())}


class Collector:
    def __init__(self, adapter: SourceAdapter, store: FrameStore,
                 config: CollectorConfig):
        self._adapter = adapter
        self._store = store
        self._config = config
        self._stats: List[RequestStat] = []
        self._consistency_unknown = False
        self._bulk_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------ API
    def acquire(self, requests: List[InstanceRequest]) -> RunReport:
        if not requests:
            raise CollectorError("采集请求为空")
        self._bulk_cache.clear()  # 批量正文缓存仅限单次运行，跨运行必须重取
        catalog, entries = self._build_catalog(requests)
        required = self._required_frames(requests, catalog)
        self._store.save_catalog_snapshot(catalog, required)

        frames: Dict[FrameKey, FrameRecord] = {}
        reused: Dict[InstanceKey, Tuple[int, ...]] = {}
        needed: Dict[InstanceKey, Tuple[int, ...]] = {}
        for key, frame_numbers in required.items():
            entry = catalog.entry(key)
            have = set()
            for n in frame_numbers:
                record = self._store.verified_record(key.frame(n), entry.version)
                if record is not None:
                    frames[key.frame(n)] = record
                    have.add(n)
            reused[key] = tuple(sorted(have))
            needed[key] = tuple(n for n in frame_numbers if n not in have)

        via_by_key: Dict[InstanceKey, str] = {}
        for request in requests:
            if request.via not in (VIA_FRAMES, VIA_BULK):
                raise CollectorError(f"未知取帧路径：{request.via}")
            if request.key in via_by_key and via_by_key[request.key] != request.via:
                raise CollectorError("同一实例的取帧路径不一致")
            via_by_key[request.key] = request.via

        fetched: Dict[InstanceKey, List[int]] = {k: [] for k in required}
        duplicates = 0
        for key, todo in needed.items():
            if not todo:
                continue
            if via_by_key[key] == VIA_FRAMES:
                duplicates += self._acquire_via_frames(catalog, key, todo, frames)
            else:
                self._acquire_via_bulk(catalog, key, todo, frames)
            fetched[key].extend(todo)

        instances: List[InstanceReport] = []
        for request in requests:
            key = request.key
            entry = catalog.entry(key)
            record = self._store.register_instance(
                key, entry.version, required[key],
                entry.descriptor.number_of_frames)
            instances.append(InstanceReport(
                key=key,
                required_frames=tuple(required[key]),
                reused_frames=reused[key],
                fetched_frames=tuple(sorted(fetched[key])),
                record=record,
            ))

        report = RunReport(
            generation=catalog.generation,
            consistency="unknown" if self._consistency_unknown else "bound",
            requests=list(self._stats),
            frames=frames,
            instances=instances,
            duplicates=duplicates,
        )
        self._write_run_report(report)
        return report

    # -------------------------------------------------------------- catalog
    def _build_catalog(self, requests: List[InstanceRequest]):
        entries: Dict[InstanceKey, CatalogEntry] = {}
        for request in requests:
            key = request.key
            if key in entries:
                continue
            url = metadata_url(self._config.base_url.rstrip("/"), key)
            response = self._fetch(url, "metadata")
            try:
                body = b"".join(response.body)
            except Exception as exc:
                raise MetadataError(f"元数据响应读取失败：{url}") from exc
            try:
                document = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise MetadataError(f"元数据不是有效DICOM JSON：{url}") from exc
            if isinstance(document, list):
                if len(document) != 1:
                    raise MetadataError("实例元数据应只含一个数据集")
                document = document[0]
            tags = parse_dataset(document)
            transfer_syntax = response.headers.get("X-Transfer-Syntax")
            if transfer_syntax is None:
                raise MetadataError("来源未声明实例传输语法，拒绝假定")
            descriptor = build_descriptor(key, tags, transfer_syntax)
            generation = response.generation
            etag = response.strong_etag()
            if generation is None or etag is None:
                version = None
                self._consistency_unknown = True
            else:
                version = VersionTag(generation, etag)
            entries[key] = CatalogEntry(
                key=key, descriptor=descriptor, version=version,
                metadata_url=response.url, tags=tags)
        catalog = MetadataCatalog(entries)
        return catalog, entries

    @staticmethod
    def _required_frames(requests, catalog) -> Dict[InstanceKey, Tuple[int, ...]]:
        required: Dict[InstanceKey, Tuple[int, ...]] = {}
        for request in requests:
            total = catalog.entry(request.key).descriptor.number_of_frames
            if request.frames is None:
                frame_numbers = tuple(range(1, total + 1))
            else:
                frame_numbers = tuple(sorted(set(request.frames)))
                for n in frame_numbers:
                    if n < 1 or n > total:
                        raise MetadataError(
                            f"请求帧号{n}越出NumberOfFrames={total}")
            merged = set(required.get(request.key, ()))
            merged.update(frame_numbers)
            required[request.key] = tuple(sorted(merged))
        return required

    # --------------------------------------------------------------- frames
    def _acquire_via_frames(self, catalog, key: InstanceKey, todo,
                            frames) -> int:
        planner = FrameRequestPlanner(
            self._config.base_url,
            max_frames_per_request=self._config.max_frames_per_request,
            ordered_parts=self._adapter.capabilities.ordered_frame_parts,
        )
        plans = planner.plan(
            {key: todo},
            {key: catalog.entry(key).descriptor.number_of_frames},
        )
        duplicates = 0
        for plan in plans:
            response = self._fetch(plan.url, "frames")
            self._check_response_version(catalog, response, "帧响应")
            receiver = PartReceiver(catalog, self._store, plan, response)
            records = receiver.consume()
            duplicates += receiver.duplicates
            for record in records:
                frames[record.key] = record
        return duplicates

    # ----------------------------------------------------------------- bulk
    def _acquire_via_bulk(self, catalog, key: InstanceKey, todo,
                          frames) -> None:
        entry = catalog.entry(key)
        desc = entry.descriptor
        source = desc.pixel_data
        if source.kind == INLINE_BINARY:
            blob = source.data
            source_url = entry.metadata_url
            kind = "inline-binary"
            response = None
        elif source.kind == BULK_DATA:
            # BulkDataURI可相对元数据响应地址；逐跳解析并限制在研究源范围
            url = urljoin(entry.metadata_url, source.uri)
            self._config.scope.check(url)
            cached = self._bulk_cache.get(url)
            if cached is not None:
                blob, source_url, response = cached
            else:
                response = self._fetch(url, "bulk")
                self._config.scope.check_hops(list(response.history) + [response.url])
                self._check_response_version(catalog, response, "批量正文")
                spool = self._store.new_part_file()
                try:
                    for chunk in response.body:
                        spool.write(chunk)
                    spool.seek(0)
                    blob = spool.read()
                finally:
                    spool.close()
                source_url = response.url
                self._bulk_cache[url] = (blob, source_url, response)
            kind = "bulk-slice"
        else:
            raise MetadataError(
                f"实例{key.sop_instance_uid}的PixelData无可用载体"
                f"（状态{source.kind}），无法走BulkData路径")

        expected_total = desc.padded_frame_bytes * desc.number_of_frames
        if len(blob) != expected_total:
            raise FrameLayoutError(
                f"实例{key.sop_instance_uid}像素载体{len(blob)}字节，"
                f"应为{expected_total}字节（{desc.number_of_frames}帧）")
        for n in todo:
            offset = (n - 1) * desc.padded_frame_bytes
            payload = blob[offset : offset + desc.frame_payload_bytes]
            padding = desc.padded_frame_bytes - desc.frame_payload_bytes
            provenance = Provenance(
                kind=kind,
                source_url=source_url,
                offset=offset,
                response_generation=response.generation if response else None,
                response_etag=response.etag if response else None,
            )
            record, _ = self._store.commit_part(
                key.frame(n), entry.version, payload, padding, provenance)
            frames[record.key] = record

    # -------------------------------------------------------------- version
    def _check_response_version(self, catalog, response: SourceResponse,
                                what: str) -> None:
        generation = response.generation
        if generation is None or response.strong_etag() is None:
            # 没有可验证的版本绑定：只能标一致性未知
            self._consistency_unknown = True
        if (generation is not None and catalog.generation is not None
                and generation != catalog.generation):
            raise VersionMismatchError(
                f"{what}属于代{generation}，元数据目录冻结于代{catalog.generation}")

    # ---------------------------------------------------------------- fetch
    def _fetch(self, url: str, purpose: str) -> SourceResponse:
        self._config.scope.check(url)
        headers: Dict[str, str] = {}
        credentials_sent = False
        if self._config.credentials and self._config.scope.is_session_origin(url):
            headers.update(self._config.credentials)
            credentials_sent = True
        response = self._adapter.fetch(url, headers)
        if credentials_sent:
            # 重定向不得把会话凭据带出同源
            for hop in list(response.history) + [response.url]:
                if not self._config.scope.is_session_origin(hop):
                    raise ScopeViolationError(
                        f"重定向越出同源而凭据已发送：{hop}")
        if response.status != 200:
            raise CollectorError(f"GET {url} 返回状态{response.status}")
        stat = RequestStat(
            purpose=purpose, url=url, status=response.status,
            generation=response.generation, etag=response.etag,
            credentials_sent=credentials_sent)
        self._stats.append(stat)
        response.body = self._counting(response.body, stat)
        return response

    @staticmethod
    def _counting(body, stat: RequestStat):
        for chunk in body:
            stat.bytes_received += len(chunk)
            yield chunk

    # --------------------------------------------------------------- report
    def _write_run_report(self, report: RunReport) -> None:
        document = {
            "generation": report.generation,
            "consistency": report.consistency,
            "bytes_received": report.bytes_received,
            "requests": [
                {
                    "purpose": r.purpose,
                    "url": r.url,
                    "status": r.status,
                    "bytes_received": r.bytes_received,
                    "generation": r.generation,
                    "etag": r.etag,
                    "credentials_sent": r.credentials_sent,
                }
                for r in report.requests
            ],
            "frames": {
                key.label(): {
                    "sha256": rec.sha256,
                    "length": rec.length,
                    "padding": rec.padding,
                    "provenance": rec.provenance.describe(),
                }
                for key, rec in sorted(report.frames.items())
            },
            "instances": [
                {
                    "key": "/".join(inst.key.as_tuple()),
                    "required_frames": list(inst.required_frames),
                    "reused_frames": list(inst.reused_frames),
                    "fetched_frames": list(inst.fetched_frames),
                    "full_instance": inst.record.full_instance,
                    "consistency": inst.record.consistency,
                }
                for inst in report.instances
            ],
        }
        (self._store.root / "run_report.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

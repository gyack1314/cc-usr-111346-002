"""帧落盘与清单：临时对象、已核验帧、实例登记、中断恢复。

不变量：
- 部件先写临时对象，核验（长度、布局、摘要、版本）后才进入frames/；
- 同帧重复部件：字节相同去重，字节不同即冲突；
- 所需帧未全部核验，实例不得登记完整，缺帧不以空文件顶替；
- 恢复只复用已核验且版本相符的帧；tmp/里的半个部件一律丢弃。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

from .errors import FrameConflictError, FrameLayoutError, IncompleteInstanceError
from .identity import (
    FrameKey,
    FrameRecord,
    InstanceKey,
    InstanceRecord,
    Provenance,
    VersionTag,
)


def _safe(uid: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in uid)


class FrameStore:
    def __init__(self, root, part_memory_budget: int = 1 << 20):
        self.root = Path(root)
        self.frames_dir = self.root / "frames"
        self.tmp_dir = self.root / "tmp"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        # 上次中断留下的半个部件：不从未知半个MIME部件继续拼，直接清除
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._part_memory_budget = part_memory_budget
        self._manifest_path = self.root / "manifest.jsonl"
        self._records: Dict[FrameKey, FrameRecord] = {}
        self._instances: Dict[InstanceKey, InstanceRecord] = {}
        self._load_manifest()

    # ------------------------------------------------------------ manifest
    def _append(self, entry: dict) -> None:
        with self._manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def _load_manifest(self) -> None:
        if not self._manifest_path.exists():
            return
        with self._manifest_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("type") == "frame":
                    key = FrameKey(*entry["key"])
                    version = entry.get("version")
                    record = FrameRecord(
                        key=key,
                        version=VersionTag(**version) if version else None,
                        sha256=entry["sha256"],
                        length=entry["length"],
                        padding=entry["padding"],
                        provenance=Provenance(**entry["provenance"]),
                    )
                    self._records[key] = record

    # ------------------------------------------------------------- snapshot
    def save_catalog_snapshot(self, catalog, required) -> None:
        """保存冻结目录与所需帧集合，供独立检查器脱离采集代码复核。"""
        snapshot = {
            "generation": catalog.generation,
            "entries": [
                {
                    "key": list(entry.key.as_tuple()),
                    "version": (
                        {"generation": entry.version.generation,
                         "metadata_etag": entry.version.metadata_etag}
                        if entry.version else None
                    ),
                    "metadata_url": entry.metadata_url,
                    "descriptor": {
                        "rows": entry.descriptor.rows,
                        "columns": entry.descriptor.columns,
                        "bits_allocated": entry.descriptor.bits_allocated,
                        "samples_per_pixel": entry.descriptor.samples_per_pixel,
                        "photometric": entry.descriptor.photometric,
                        "number_of_frames": entry.descriptor.number_of_frames,
                        "transfer_syntax": entry.descriptor.transfer_syntax,
                        "assumed": list(entry.descriptor.assumed),
                        "frame_payload_bytes": entry.descriptor.frame_payload_bytes,
                        "padded_frame_bytes": entry.descriptor.padded_frame_bytes,
                    },
                }
                for entry in catalog
            ],
        }
        (self.root / "catalog.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        spec = {
            "required": {
                "/".join(key.as_tuple()): sorted(frames)
                for key, frames in required.items()
            }
        }
        (self.root / "run_spec.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- frames
    def frame_path(self, key: FrameKey) -> Path:
        return (
            self.frames_dir
            / _safe(key.study_uid)
            / _safe(key.series_uid)
            / _safe(key.sop_instance_uid)
            / f"{key.frame_number:06d}.raw"
        )

    def new_part_file(self):
        """当前部件的临时对象；内存受预算控制，超出即落临时文件。"""
        return tempfile.SpooledTemporaryFile(
            max_size=self._part_memory_budget, mode="w+b", dir=self.tmp_dir)

    def verified_record(self, key: FrameKey,
                        version: Optional[VersionTag]) -> Optional[FrameRecord]:
        """只认已核验且版本相符的帧；文件与摘要再核一遍。"""
        record = self._records.get(key)
        if record is None or record.version != version or version is None:
            return None
        path = self.frame_path(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) != record.length:
            return None
        if hashlib.sha256(data).hexdigest() != record.sha256:
            return None
        return record

    def commit_part(self, key: FrameKey, version: Optional[VersionTag],
                    payload: bytes, padding: int,
                    provenance: Provenance) -> Tuple[FrameRecord, bool]:
        """登记一个已核验部件。返回(记录, 是否重复)。

        同帧同版本已有记录：字节相同去重，不同即冲突。
        """
        digest = hashlib.sha256(payload).hexdigest()
        existing = self._records.get(key)
        if existing is not None and existing.version == version:
            if existing.sha256 == digest and existing.length == len(payload):
                return existing, True
            raise FrameConflictError(
                f"帧{key.label()}的两个部件字节不同，拒绝合并")
        path = self.frame_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.tmp_dir)
        try:
            with open(fd, "wb", closefd=True) as fh:
                fh.write(payload)
                fh.flush()
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        record = FrameRecord(
            key=key, version=version, sha256=digest,
            length=len(payload), padding=padding, provenance=provenance)
        self._records[key] = record
        self._append({
            "type": "frame",
            "key": [key.study_uid, key.series_uid, key.sop_instance_uid,
                    key.frame_number],
            "version": (
                {"generation": version.generation,
                 "metadata_etag": version.metadata_etag} if version else None),
            "sha256": digest,
            "length": len(payload),
            "padding": padding,
            "provenance": {
                "kind": provenance.kind,
                "source_url": provenance.source_url,
                "request_id": provenance.request_id,
                "part_index": provenance.part_index,
                "content_location": provenance.content_location,
                "offset": provenance.offset,
                "response_generation": provenance.response_generation,
                "response_etag": provenance.response_etag,
            },
        })
        return record, False

    # ----------------------------------------------------------- instances
    def _current_record(self, key: FrameKey,
                        version: Optional[VersionTag]) -> Optional[FrameRecord]:
        """本次运行已提交的帧记录（含一致性未知的版本）。"""
        record = self._records.get(key)
        if record is None or record.version != version:
            return None
        return record

    def register_instance(self, key: InstanceKey, version: Optional[VersionTag],
                          required_frames, total_frames: int) -> InstanceRecord:
        """全部所需帧核验后才登记；缺帧即拒绝，不以空文件顶替。"""
        required = tuple(sorted(required_frames))
        missing = [n for n in required
                   if self._current_record(key.frame(n), version) is None]
        if missing:
            raise IncompleteInstanceError(
                f"实例{key.sop_instance_uid}缺帧{missing}，不得登记完整")
        record = InstanceRecord(
            key=key,
            version=version,
            required_frames=required,
            verified_frames=required,
            full_instance=len(required) == total_frames,
            consistency="bound" if version is not None else "unknown",
        )
        self._instances[key] = record
        self._append({
            "type": "instance_complete",
            "key": list(key.as_tuple()),
            "version": (
                {"generation": version.generation,
                 "metadata_etag": version.metadata_etag} if version else None),
            "required_frames": list(required),
            "full_instance": record.full_instance,
            "consistency": record.consistency,
        })
        return record

    def instance_record(self, key: InstanceKey) -> Optional[InstanceRecord]:
        return self._instances.get(key)

    def frame_records(self) -> Dict[FrameKey, FrameRecord]:
        return dict(self._records)

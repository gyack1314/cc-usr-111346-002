"""独立帧清单检查器：不信任采集代码，按落盘资料重新核验。

读取catalog.json（冻结目录）、run_spec.json（所需帧）、manifest.jsonl（登记），
逐帧重算SHA256、核对版本绑定、长度与出处字段，报告完整性。
入口：python -m dicomweb_collector.check <store_root>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .identity import FrameKey, InstanceKey, VersionTag


class CheckProblem:
    def __init__(self, message: str):
        self.message = message

    def __repr__(self):  # pragma: no cover
        return f"CheckProblem({self.message!r})"


def _load_json(path: Path, problems, what):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(CheckProblem(f"{what}不可读取：{exc}"))
        return None


def check_store(store_root) -> dict:
    """复核一个落盘目录；返回可JSON序列化的报告。"""
    root = Path(store_root)
    problems = []
    notes = []

    catalog = _load_json(root / "catalog.json", problems, "冻结目录")
    spec = _load_json(root / "run_spec.json", problems, "所需帧清单")
    if catalog is None or spec is None:
        return {"ok": False, "problems": [p.message for p in problems]}

    entries = {}
    for raw in catalog.get("entries", []):
        key = InstanceKey(*raw["key"])
        version = raw.get("version")
        entries[key] = {
            "version": VersionTag(**version) if version else None,
            "descriptor": raw["descriptor"],
        }
    generation = catalog.get("generation")

    manifest_path = root / "manifest.jsonl"
    frame_records = {}
    instance_records = []
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    problems.append(CheckProblem(f"清单第{line_number}行不是有效JSON"))
                    continue
                if entry.get("type") == "frame":
                    frame_records[FrameKey(*entry["key"])] = entry
                elif entry.get("type") == "instance_complete":
                    instance_records.append(entry)
    else:
        problems.append(CheckProblem("缺少manifest.jsonl"))

    checked_frames = 0
    total_bytes = 0
    consistency = {}
    required = spec.get("required", {})
    for instance_label, frame_numbers in sorted(required.items()):
        key = InstanceKey(*instance_label.split("/"))
        entry = entries.get(key)
        if entry is None:
            problems.append(CheckProblem(f"实例{instance_label}不在冻结目录中"))
            continue
        version = entry["version"]
        expected_len = entry["descriptor"]["frame_payload_bytes"]
        instance_consistency = "bound" if version is not None else "unknown"
        for number in sorted(frame_numbers):
            frame_key = key.frame(number)
            record = frame_records.get(frame_key)
            if record is None:
                problems.append(CheckProblem(f"帧{frame_key.label()}没有登记记录"))
                continue
            record_version = record.get("version")
            if (record_version is None) != (version is None) or (
                version is not None
                and record_version != {
                    "generation": version.generation,
                    "metadata_etag": version.metadata_etag}
            ):
                problems.append(CheckProblem(
                    f"帧{frame_key.label()}的版本绑定与冻结目录不符"))
                continue
            path = (root / "frames" / _safe(key.study_uid) / _safe(key.series_uid)
                    / _safe(key.sop_instance_uid) / f"{number:06d}.raw")
            try:
                data = path.read_bytes()
            except OSError:
                problems.append(CheckProblem(f"帧{frame_key.label()}文件缺失"))
                continue
            if len(data) != expected_len or len(data) != record["length"]:
                problems.append(CheckProblem(
                    f"帧{frame_key.label()}长度{len(data)}与冻结布局"
                    f"{expected_len}不符"))
                continue
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                problems.append(CheckProblem(f"帧{frame_key.label()}摘要不符"))
                continue
            provenance = record.get("provenance") or {}
            kind = provenance.get("kind")
            if kind == "multipart-part":
                if provenance.get("request_id") is None or provenance.get("part_index") is None:
                    problems.append(CheckProblem(
                        f"帧{frame_key.label()}缺少部件出处"))
                    continue
            elif kind == "bulk-slice":
                if provenance.get("offset") is None or not provenance.get("source_url"):
                    problems.append(CheckProblem(
                        f"帧{frame_key.label()}缺少批量正文出处"))
                    continue
            elif kind != "inline-binary":
                problems.append(CheckProblem(
                    f"帧{frame_key.label()}出处类型未知：{kind}"))
                continue
            checked_frames += 1
            total_bytes += len(data)
        consistency[instance_label] = instance_consistency

    registered = {tuple(r.get("key", ())) for r in instance_records}
    for instance_label in required:
        key_tuple = tuple(instance_label.split("/"))
        if key_tuple not in registered:
            problems.append(CheckProblem(f"实例{instance_label}未登记完整"))

    return {
        "ok": not problems,
        "generation": generation,
        "frames_checked": checked_frames,
        "frames_bytes": total_bytes,
        "consistency": consistency,
        "problems": [p.message for p in problems],
        "notes": notes,
    }


def _safe(uid: str) -> str:
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in uid)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="独立复核帧清单与落盘字节")
    parser.add_argument("store_root")
    args = parser.parse_args(argv)
    report = check_store(args.store_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())

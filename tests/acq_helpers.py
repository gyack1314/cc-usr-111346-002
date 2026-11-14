"""采集端测试的公共装置：内存来源适配器、DICOM JSON与multipart构造。"""
import hashlib
import json
from dataclasses import dataclass, field

from dicomweb_collector.identity import EXPLICIT_VR_LE, InstanceKey
from dicomweb_collector.source import Capabilities, SourceResponse

BASE = "https://imaging.invalid/dicomweb"
ORIGIN = "https://imaging.invalid"
KEY = InstanceKey("1.2.3", "1.2.3.4", "1.2.3.4.5")
KEY2 = InstanceKey("1.2.3", "1.2.3.4", "1.2.3.4.6")


@dataclass
class FetchedRequest:
    url: str
    headers: dict


class FakeAdapter:
    """按路由表应答的来源适配器；记录每次请求的URL与头部。"""

    def __init__(self, generation="g1", ordered=False):
        self.generation = generation
        self.capabilities = Capabilities(ordered_frame_parts=ordered)
        self.routes = {}
        self.requests = []

    def route(self, url, *, body, headers=None, etag='"r1"', generation="same",
              status=200, history=(), final_url=None, chunk=13):
        self.routes[url] = {
            "body": body, "headers": headers or {}, "etag": etag,
            "generation": generation, "status": status,
            "history": tuple(history), "final_url": final_url, "chunk": chunk,
        }

    def fetch(self, url, headers):
        self.requests.append(FetchedRequest(url, dict(headers)))
        if url not in self.routes:
            raise AssertionError(f"未登记的请求地址：{url}")
        spec = self.routes[url]
        body = spec["body"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        chunk = spec["chunk"] or len(body) or 1

        def pieces():
            for start in range(0, len(body), chunk):
                yield body[start:start + chunk]

        generation = spec["generation"]
        if generation == "same":
            generation = self.generation
        return SourceResponse(
            url=spec["final_url"] or url,
            status=spec["status"],
            headers=dict(spec["headers"]),
            body=pieces(),
            generation=generation,
            etag=spec["etag"],
            history=spec["history"],
        )

    def requested_urls(self, purpose_substring=""):
        return [r.url for r in self.requests if purpose_substring in r.url]


def make_metadata(key=KEY, frames=4, rows=2, cols=3, bits=8, samples=1,
                  photometric="MONOCHROME2", pixel="bulk", bulk_uri="px",
                  extra=None):
    """构造实例级DICOM JSON。pixel: "bulk" | "inline" | None。"""
    dataset = {
        "0020000D": {"vr": "UI", "Value": [key.study_uid]},
        "0020000E": {"vr": "UI", "Value": [key.series_uid]},
        "00080018": {"vr": "UI", "Value": [key.sop_instance_uid]},
        "00280010": {"vr": "US", "Value": [rows]},
        "00280011": {"vr": "US", "Value": [cols]},
        "00280100": {"vr": "US", "Value": [bits]},
        "00280002": {"vr": "US", "Value": [samples]},
    }
    if frames != 1:
        dataset["00280008"] = {"vr": "IS", "Value": [str(frames)]}
    if photometric is not None:
        dataset["00280004"] = {"vr": "CS", "Value": [photometric]}
    if pixel == "bulk":
        dataset["7FE00010"] = {"vr": "OW", "BulkDataURI": bulk_uri}
    elif pixel == "inline":
        import base64

        payload = pixel_blob(key, frames, rows * cols * (bits // 8))
        dataset["7FE00010"] = {
            "vr": "OW",
            "InlineBinary": base64.b64encode(payload).decode("ascii"),
        }
    if extra:
        dataset.update(extra)
    return json.dumps([dataset])


def frame_bytes(key, number, length):
    """确定性的帧字节：同帧恒同、异帧恒异。"""
    seed = hashlib.sha256(
        f"{key.sop_instance_uid}#{number}".encode("utf-8")).digest()
    return (seed * (length // len(seed) + 1))[:length]


def pixel_blob(key, frames, payload_len, pad=True):
    """多帧拼接的像素载体：每帧像素 + 偶数填充。"""
    padded = payload_len if payload_len % 2 == 0 else payload_len + 1
    out = bytearray()
    for n in range(1, frames + 1):
        out += frame_bytes(key, n, payload_len)
        out += b"\x00" * (padded - payload_len if pad else 0)
    return bytes(out)


def part_headers(key, number, transfer_syntax=EXPLICIT_VR_LE, base=BASE,
                 content_length=None):
    headers = [
        ("Content-Type", f"application/octet-stream; transfer-syntax={transfer_syntax}"),
        ("Content-Location",
         f"{base}/studies/{key.study_uid}/series/{key.series_uid}"
         f"/instances/{key.sop_instance_uid}/frames/{number}"),
    ]
    if content_length is not None:
        headers.append(("Content-Length", str(content_length)))
    return headers


def build_multipart(boundary, parts):
    """parts: [(headers, body)]；按给定顺序拼装。"""
    out = bytearray()
    for headers, body in parts:
        out += b"--" + boundary + b"\r\n"
        for name, value in headers:
            out += name.encode("ascii") + b": " + value.encode("utf-8") + b"\r\n"
        out += b"\r\n"
        out += body
        out += b"\r\n"
    out += b"--" + boundary + b"--\r\n"
    return bytes(out)


def frames_response_body(key, numbers, payload_len, boundary=b"bnd",
                         order=None, transfer_syntax=EXPLICIT_VR_LE):
    """按order（默认=numbers）顺序装帧的multipart正文。"""
    order = order if order is not None else numbers
    parts = [
        (part_headers(key, n, transfer_syntax=transfer_syntax),
         frame_bytes(key, n, payload_len))
        for n in order
    ]
    return build_multipart(boundary, parts)


def multipart_content_type(boundary="bnd", transfer_syntax=EXPLICIT_VR_LE):
    return (f'multipart/related; type="application/octet-stream"; '
            f'boundary={boundary}; transfer-syntax={transfer_syntax}')


def metadata_headers():
    return {
        "Content-Type": "application/dicom+json",
        "X-Transfer-Syntax": EXPLICIT_VR_LE,
    }


def metadata_url(key, base=BASE):
    return (f"{base}/studies/{key.study_uid}/series/{key.series_uid}"
            f"/instances/{key.sop_instance_uid}/metadata")


def frames_url(key, numbers, base=BASE):
    frames = ",".join(str(n) for n in numbers)
    return (f"{base}/studies/{key.study_uid}/series/{key.series_uid}"
            f"/instances/{key.sop_instance_uid}/frames/{frames}")

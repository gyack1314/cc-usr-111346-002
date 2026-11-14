"""HTTP层面的解析与范围控制：Content-Type、规范帧地址、研究源范围。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlsplit

from .errors import MimeError, ScopeViolationError
from .identity import FrameKey, InstanceKey

_FRAME_PATH = re.compile(
    r"/studies/([^/]+)/series/([^/]+)/instances/([^/]+)/frames/([0-9]+)$"
)


def parse_content_type(value: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """解析Content-Type为(小写媒体类型, 参数)；支持引号与转义。"""
    if not value:
        raise MimeError("缺少Content-Type")
    parts = []
    current = []
    in_quote = False
    escaped = False
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\" and in_quote:
            escaped = True
        elif ch == '"':
            in_quote = not in_quote
        elif ch == ";" and not in_quote:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if in_quote:
        raise MimeError("Content-Type引号不配对")
    parts.append("".join(current))
    media_type = parts[0].strip().lower()
    if "/" not in media_type:
        raise MimeError(f"Content-Type无效：{value!r}")
    params: Dict[str, str] = {}
    for piece in parts[1:]:
        name, sep, val = piece.partition("=")
        if not sep:
            raise MimeError(f"Content-Type参数无效：{piece!r}")
        params[name.strip().lower()] = val.strip()
    return media_type, params


def quote_uid(uid: str) -> str:
    return quote(uid, safe="")


def metadata_url(base: str, key: InstanceKey) -> str:
    return (
        f"{base}/studies/{quote_uid(key.study_uid)}"
        f"/series/{quote_uid(key.series_uid)}"
        f"/instances/{quote_uid(key.sop_instance_uid)}/metadata"
    )


def frames_url(base: str, key: InstanceKey, frame_numbers) -> str:
    frames = ",".join(str(n) for n in frame_numbers)
    return (
        f"{base}/studies/{quote_uid(key.study_uid)}"
        f"/series/{quote_uid(key.series_uid)}"
        f"/instances/{quote_uid(key.sop_instance_uid)}/frames/{frames}"
    )


def parse_frame_location(location: str, base_url: str) -> Optional[FrameKey]:
    """把Content-Location解析为规范帧地址；相对地址按响应地址解析。

    匹配不上规范帧地址形式时返回None，由调用方决定拒绝。
    """
    absolute = urljoin(base_url, location.strip())
    match = _FRAME_PATH.search(urlsplit(absolute).path)
    if not match:
        return None
    study, series, sop, frame = (unquote(g) for g in match.groups())
    number = int(frame)
    if number < 1:
        return None
    return FrameKey(study, series, sop, number)


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ScopeViolationError(f"地址缺少scheme或host：{url!r}")
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    if port and port != default:
        return f"{parts.scheme.lower()}://{host}:{port}"
    return f"{parts.scheme.lower()}://{host}"


@dataclass(frozen=True)
class SourceScope:
    """配置的研究源范围：允许的前缀 + 会话凭据所属的源。

    BulkDataURI逐跳解析后，每一跳都必须落在allowed_prefixes内；
    跨源（origin不同）时不转发会话凭据。
    """

    allowed_prefixes: Tuple[str, ...]
    session_origin: Optional[str] = None

    def check(self, url: str) -> None:
        if not any(url.startswith(prefix) for prefix in self.allowed_prefixes):
            raise ScopeViolationError(f"地址越出研究源范围：{url}")

    def check_hops(self, urls) -> None:
        for url in urls:
            self.check(url)

    def is_session_origin(self, url: str) -> bool:
        if self.session_origin is None:
            return False
        return origin_of(url) == self.session_origin

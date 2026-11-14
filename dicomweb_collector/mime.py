"""流式multipart/related解析（RFC 2046边界语义）。

只认真正的边界行：CRLF + "--" + boundary + （"--" 或 传输填充 + CRLF）。
边界字符串出现在二进制中间、或行首但后续字节不构成合法定界符时，
一律视为正文，不得截断。解析器按块喂入，内存只保留当前块加边界进位，
部件正文由调用方写入临时对象。
"""
from __future__ import annotations

from typing import Callable, List, Optional, Protocol, Tuple

from .errors import MimeError

_MAX_BOUNDARY = 70
_MAX_HEADERS = 64 * 1024
_NEED_MORE = object()

_PREAMBLE = "preamble"
_HEADERS = "headers"
_BODY = "body"
_EPILOGUE = "epilogue"

_TOKEN_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789"
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class PartSink(Protocol):
    """部件正文的接收端，由sink_factory按头部创建。"""

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...

    def abort(self) -> None: ...


class RawPartHeaders:
    """一个部件的原始头部（保序、名字小写）。"""

    def __init__(self, items: List[Tuple[str, str]]):
        self._items = list(items)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        lname = name.lower()
        for key, value in self._items:
            if key.lower() == lname:
                return value
        return default

    def items(self) -> List[Tuple[str, str]]:
        return list(self._items)

    def __repr__(self):  # pragma: no cover
        return f"RawPartHeaders({self._items!r})"


SinkFactory = Callable[[RawPartHeaders], PartSink]


def _is_padding(byte: int) -> bool:
    return byte in (0x20, 0x09)  # SP / HTAB


class MultipartParser:
    """按块喂入的multipart解析器；每个部件头部就绪后回调sink_factory。"""

    def __init__(self, boundary: bytes, sink_factory: SinkFactory):
        if isinstance(boundary, str):
            boundary = boundary.encode("ascii", "strict")
        if not boundary or len(boundary) > _MAX_BOUNDARY:
            raise MimeError("boundary缺失或超过70字节")
        if b"\r" in boundary or b"\n" in boundary:
            raise MimeError("boundary含非法字符")
        self._marker = b"--" + boundary
        self._sink_factory = sink_factory
        self._state = _PREAMBLE
        self._buf = bytearray()
        self._sink: Optional[PartSink] = None
        self._eof = False
        self.parts_seen = 0
        self.closed = False

    # ------------------------------------------------------------------ feed
    def feed(self, data: bytes) -> None:
        if self._state == _EPILOGUE:
            return  # 尾声忽略
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("feed只接受bytes")
        self._buf += data
        self._pump()

    def finish(self) -> None:
        """流结束：必须已经看到关闭定界符，否则按截断拒绝。"""
        self._eof = True
        if self._state != _EPILOGUE:
            self._pump()
        if self._state == _PREAMBLE and self.parts_seen == 0:
            raise MimeError("multipart中未找到边界行")
        if not self.closed:
            raise MimeError("multipart被截断：缺少关闭定界符")

    # ------------------------------------------------------------------ pump
    def _pump(self) -> None:
        while True:
            if self._state == _PREAMBLE:
                if not self._scan_preamble():
                    return
            elif self._state == _HEADERS:
                if not self._scan_headers():
                    return
            elif self._state == _BODY:
                if not self._scan_body():
                    return
            else:
                return

    def _scan_preamble(self) -> bool:
        """寻找第一个定界行；之前的内容是前言，丢弃。"""
        buf = self._buf
        search_from = 0
        while True:
            idx = buf.find(self._marker, search_from)
            if idx == -1:
                keep = len(self._marker) + 2
                if len(buf) > keep:
                    del buf[: len(buf) - keep]
                return False
            if idx != 0 and bytes(buf[idx - 2 : idx]) != b"\r\n":
                search_from = idx + 1
                continue
            matched = self._match_tail(buf, idx + len(self._marker))
            if matched is _NEED_MORE:
                return False
            if matched is None:
                search_from = idx + 1
                continue
            kind, end = matched
            del self._buf[:end]
            self._enter(kind)
            return True

    def _scan_headers(self) -> bool:
        buf = self._buf
        if len(buf) >= 2 and bytes(buf[:2]) == b"\r\n":
            # 定界行后直接是空行：零头部部件，本采集端拒绝
            raise MimeError("部件缺少头部（本采集端要求每部件至少一个头部）")
        if len(buf) < 2:
            return False
        stop = buf.find(b"\r\n\r\n")
        if stop == -1:
            if len(buf) > _MAX_HEADERS:
                self._fail_sink()
                raise MimeError("部件头部超过限制")
            return False
        raw = bytes(buf[:stop])
        del self._buf[: stop + 4]
        headers = self._decode_headers(raw)
        self._sink = self._sink_factory(headers)
        self._state = _BODY
        return True

    def _scan_body(self) -> bool:
        buf = self._buf
        search_from = 0
        while True:
            idx = buf.find(self._marker, search_from)
            if idx == -1:
                # 保留尾部可能构成 "\r\n--boundary" 前缀的进位，其余交付
                keep = len(self._marker) + 2
                if len(buf) > keep:
                    cut = len(buf) - keep
                    self._sink.write(bytes(buf[:cut]))
                    del buf[:cut]
                return False
            if idx < 2 or bytes(buf[idx - 2 : idx]) != b"\r\n":
                search_from = idx + 1
                continue
            matched = self._match_tail(buf, idx + len(self._marker))
            if matched is _NEED_MORE:
                if idx - 2 > 0:
                    self._sink.write(bytes(buf[: idx - 2]))
                    del buf[: idx - 2]
                return False
            if matched is None:
                # 行首的boundary字符串但后续不合法：是正文，不是定界符
                search_from = idx + 1
                continue
            kind, end = matched
            if idx - 2 > 0:
                self._sink.write(bytes(buf[: idx - 2]))
            sink, self._sink = self._sink, None
            sink.close()
            self.parts_seen += 1
            del self._buf[:end]
            self._enter(kind)
            return True

    def _enter(self, kind: str) -> None:
        if kind == "close":
            self._state = _EPILOGUE
            self.closed = True
        else:
            self._state = _HEADERS

    # -------------------------------------------------------------- delimiter
    def _match_tail(self, buf, pos):
        """boundary之后的字节：返回(kind, end)、None（非定界符）或_NEED_MORE。"""
        n = len(buf)
        if pos >= n:
            return None if self._eof else _NEED_MORE
        b0 = buf[pos]
        if b0 == 0x2D:  # '-'：关闭定界符的第二个'-'
            if pos + 1 >= n:
                return None if self._eof else _NEED_MORE
            if buf[pos + 1] != 0x2D:
                return None
            j = self._skip_padding(buf, pos + 2)
            if j is None:
                # "--boundary--"加传输填充后流结束：接受为关闭
                return ("close", n) if self._eof else _NEED_MORE
            return self._match_line_end(buf, j, "close")
        if _is_padding(b0):
            j = self._skip_padding(buf, pos)
            if j is None:
                return None if self._eof else _NEED_MORE
            return self._match_line_end(buf, j, "part")
        if b0 == 0x0D:  # '\r'：部件定界符以CRLF结束
            return self._match_line_end(buf, pos, "part")
        return None

    @staticmethod
    def _skip_padding(buf, j):
        n = len(buf)
        while j < n and _is_padding(buf[j]):
            j += 1
        return j if j < n else None

    def _match_line_end(self, buf, j, kind):
        n = len(buf)
        if buf[j] != 0x0D:
            return None
        if j + 1 >= n:
            # 缓冲区末尾孤立的\r：可能正在等\n，不能按非定界符处理
            return None if self._eof else _NEED_MORE
        if buf[j + 1] == 0x0A:
            return (kind, j + 2)
        return None

    # --------------------------------------------------------------- headers
    @staticmethod
    def _decode_headers(raw: bytes) -> RawPartHeaders:
        if not raw:
            raise MimeError("部件缺少头部（本采集端要求每部件至少一个头部）")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise MimeError("部件头部不是ASCII") from exc
        items: List[Tuple[str, str]] = []
        for line in text.split("\r\n"):
            if line[:1] in (" ", "\t"):
                if not items:
                    raise MimeError("头部折行没有前导头部")
                name, value = items[-1]
                items[-1] = (name, value + " " + line.strip())
                continue
            name, sep, value = line.partition(":")
            if not sep or not name or any(c not in _TOKEN_CHARS for c in name):
                raise MimeError(f"非法部件头部行：{line!r}")
            items.append((name, value.strip()))
        return RawPartHeaders(items)

    def _fail_sink(self) -> None:
        if self._sink is not None:
            self._sink.abort()
            self._sink = None

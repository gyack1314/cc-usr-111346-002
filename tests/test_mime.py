"""流式multipart解析器的边界语义。"""
import unittest

from dicomweb_collector.errors import MimeError
from dicomweb_collector.mime import MultipartParser


def collect(parts_sink, boundary=b"bnd", chunk=7, data=b""):
    parser = MultipartParser(boundary, parts_sink)
    for start in range(0, len(data), chunk):
        parser.feed(data[start:start + chunk])
    parser.finish()
    return parser


class Sink:
    def __init__(self, headers, store):
        self.headers = headers
        self.store = store
        self.body = bytearray()

    def write(self, data):
        self.body += data

    def close(self):
        self.store.append((self.headers, bytes(self.body)))

    def abort(self):
        pass


def parse(data, boundary=b"bnd", chunk=7):
    parts = []

    def factory(headers):
        return Sink(headers, parts)

    parser = MultipartParser(boundary, factory)
    for start in range(0, len(data), chunk):
        parser.feed(data[start:start + chunk])
    parser.finish()
    return parts


def assemble(boundary, bodies, headers=(("Content-Type", "application/octet-stream"),)):
    out = bytearray()
    for body in bodies:
        out += b"--" + boundary + b"\r\n"
        for name, value in headers:
            out += name.encode() + b": " + value.encode() + b"\r\n"
        out += b"\r\n" + body + b"\r\n"
    out += b"--" + boundary + b"--\r\n"
    return bytes(out)


class MimeTests(unittest.TestCase):
    def test_basic_parts(self):
        parts = parse(assemble(b"bnd", [b"abc", b"defg"]))
        self.assertEqual([body for _, body in parts], [b"abc", b"defg"])

    def test_every_chunk_size(self):
        data = assemble(b"frame-boundary", [b"\x01\x02", b"\x03\x04"])
        for chunk in range(1, 17):
            parts = parse(data, boundary=b"frame-boundary", chunk=chunk)
            self.assertEqual([body for _, body in parts], [b"\x01\x02", b"\x03\x04"],
                             f"chunk={chunk}")

    def test_boundary_string_inside_binary_not_truncated(self):
        # 二进制正文中间出现边界字符串：不是行首定界符，不得截断
        body = b"xx--bndyy\r\nnot-a-boundary\r\n--bndX\r\nzz"
        parts = parse(assemble(b"bnd", [body, b"tail"]))
        self.assertEqual([body for _, body in parts], [body, b"tail"])

    def test_line_start_boundary_with_invalid_tail_not_truncated(self):
        # 行首是边界字符串但后续字节不构成合法定界符：仍是正文
        body = b"aa\r\n--bnd--extra\r\nbb\r\n--bnd \tX\r\ncc"
        parts = parse(assemble(b"bnd", [body]))
        self.assertEqual([body for _, body in parts], [body])

    def test_boundary_split_across_chunks(self):
        data = assemble(b"long-boundary-marker", [b"\x00" * 100 + b"\r\n--long-bound"])
        for chunk in (1, 2, 3, 5, 33):
            parts = parse(data, boundary=b"long-boundary-marker", chunk=chunk)
            self.assertEqual(parts[0][1], b"\x00" * 100 + b"\r\n--long-bound")

    def test_transport_padding_accepted(self):
        data = (b"--bnd \t \r\nContent-Type: application/octet-stream\r\n\r\n"
                b"ab\r\n--bnd-- \t\r\n")
        parts = parse(data)
        self.assertEqual([body for _, body in parts], [b"ab"])

    def test_close_delimiter_at_eof_without_crlf(self):
        data = (b"--bnd\r\nContent-Type: application/octet-stream\r\n\r\n"
                b"ab\r\n--bnd--")
        parts = parse(data)
        self.assertEqual([body for _, body in parts], [b"ab"])

    def test_truncated_stream_rejected(self):
        data = (b"--bnd\r\nContent-Type: application/octet-stream\r\n\r\n"
                b"ab\r\n--bnd\r\nContent-Type: application/octet-stream\r\n\r\ncd")
        with self.assertRaisesRegex(MimeError, "截断"):
            parse(data)

    def test_missing_close_delimiter_rejected(self):
        data = (b"--bnd\r\nContent-Type: application/octet-stream\r\n\r\nab\r\n")
        with self.assertRaisesRegex(MimeError, "截断"):
            parse(data)

    def test_no_boundary_rejected(self):
        with self.assertRaisesRegex(MimeError, "边界"):
            parse(b"nothing here at all")

    def test_part_without_headers_rejected(self):
        data = b"--bnd\r\n\r\nab\r\n--bnd--\r\n"
        with self.assertRaisesRegex(MimeError, "头部"):
            parse(data)

    def test_preamble_and_epilogue_ignored(self):
        data = (b"junk preamble\r\n--bnd\r\nContent-Type: application/octet-stream\r\n\r\n"
                b"ab\r\n--bnd--\r\nepilogue ignored")
        parts = parse(data)
        self.assertEqual([body for _, body in parts], [b"ab"])

    def test_header_folding(self):
        data = (b"--bnd\r\nContent-Type: application/octet-stream\r\n"
                b"X-Long: one\r\n\ttwo\r\n\r\nab\r\n--bnd--\r\n")
        parts = parse(data)
        self.assertEqual(parts[0][0].get("X-Long"), "one two")

    def test_empty_body_part(self):
        parts = parse(assemble(b"bnd", [b""]))
        self.assertEqual([body for _, body in parts], [b""])

    def test_seeded_fuzz_random_binary_and_chunks(self):
        """确定性随机：含边界碎片的随机二进制、随机分块，解析必须还原。"""
        import random

        rng = random.Random(20261114)
        boundary = b"fz-boundary"
        for _ in range(200):
            bodies = []
            for _ in range(rng.randint(1, 4)):
                length = rng.randint(0, 64)
                body = bytearray(rng.randbytes(length))
                # 随机植入边界碎片：完整串、行首串、带非法尾巴的行首串。
                # 每个碎片自带失格字节，保证在任何上下文里都不构成真定界符
                # （否则流本身在协议上就是歧义的，不属于本测试范围）。
                if rng.random() < 0.7:
                    pos = rng.randint(0, len(body)) if body else 0
                    fragment = rng.choice([
                        b"--" + boundary + b"X",
                        b"\r\n--" + boundary + b"X",
                        b"\r\n--" + boundary + b"-X",
                        b"\r\n--" + boundary + b"--Z",
                        b"\r\n--" + boundary + b" \tZ",
                        b"\r\n--" + boundary[:3],
                    ])
                    body[pos:pos] = fragment
                bodies.append(bytes(body))
            data = assemble(boundary, bodies)
            chunk = rng.randint(1, 23)
            got = parse(data, boundary=boundary, chunk=chunk)
            self.assertEqual([body for _, body in got], bodies)


if __name__ == "__main__":
    unittest.main()

"""采集端集成测试：乱序部件、共享BulkDataURI、非连续帧、冲突、换代核对。"""
import json
import tempfile
import unittest
from pathlib import Path

from dicomweb_collector import (
    AmbiguousPartError,
    Collector,
    CollectorConfig,
    FrameConflictError,
    FrameLayoutError,
    FrameStore,
    IncompleteInstanceError,
    InstanceRequest,
    MetadataError,
    MimeError,
    PartIdentityError,
    ScopeViolationError,
    SourceScope,
    TransferSyntaxError,
    UnexpectedPartError,
    VersionMismatchError,
)
from dicomweb_collector.check import check_store
from dicomweb_collector.identity import EXPLICIT_VR_LE

from tests.acq_helpers import (
    BASE,
    KEY,
    KEY2,
    ORIGIN,
    FakeAdapter,
    build_multipart,
    frame_bytes,
    frames_response_body,
    frames_url,
    make_metadata,
    metadata_headers,
    metadata_url,
    multipart_content_type,
    part_headers,
    pixel_blob,
)

REPO = Path(__file__).resolve().parents[1]
CREDENTIALS = {"Authorization": "Bearer session-token"}


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        for tmp in self._tmpdirs:
            tmp.cleanup()

    def make_config(self, scope_prefixes=None, max_frames=128,
                    credentials=CREDENTIALS):
        scope = SourceScope(tuple(scope_prefixes or [ORIGIN + "/"]),
                            session_origin=ORIGIN)
        return CollectorConfig(base_url=BASE, scope=scope,
                               credentials=credentials,
                               max_frames_per_request=max_frames)

    def make_env(self, ordered=False, generation="g1", scope_prefixes=None,
                 max_frames=128, credentials=CREDENTIALS):
        tmp = tempfile.TemporaryDirectory()
        self._tmpdirs.append(tmp)
        adapter = FakeAdapter(generation=generation, ordered=ordered)
        store = FrameStore(Path(tmp.name) / "store")
        config = self.make_config(scope_prefixes, max_frames, credentials)
        return adapter, store, Collector(adapter, store, config)

    def reopen_env(self, store, generation="g1", max_frames=128):
        """同一落盘目录新开一个采集器（模拟中断后重启）。"""
        adapter = FakeAdapter(generation=generation)
        store2 = FrameStore(store.root)
        collector = Collector(adapter, store2, self.make_config(
            max_frames=max_frames))
        return adapter, store2, collector

    def route_metadata(self, adapter, key, frames=4, rows=2, cols=3, bits=8,
                       etag='"meta-1"', generation="same", pixel="bulk",
                       bulk_uri="px", headers=None):
        adapter.route(
            metadata_url(key),
            body=make_metadata(key, frames=frames, rows=rows, cols=cols,
                               bits=bits, pixel=pixel, bulk_uri=bulk_uri),
            headers=headers if headers is not None else metadata_headers(),
            etag=etag, generation=generation)

    def route_frames(self, adapter, key, numbers, payload_len=6, order=None,
                     etag='"frames-1"', generation="same", boundary="bnd",
                     extra_headers=None, parts=None):
        if parts is None:
            parts = [(part_headers(key, n), frame_bytes(key, n, payload_len))
                     for n in (order if order is not None else numbers)]
        body = build_multipart(boundary.encode(), parts)
        headers = {"Content-Type": multipart_content_type(boundary)}
        if extra_headers:
            headers.update(extra_headers)
        adapter.route(frames_url(key, numbers), body=body, headers=headers,
                      etag=etag, generation=generation)
        return body

    # ---------------------------------------------------------- 仓库装置
    def test_repo_fixture_out_of_order_parts(self):
        """仓库自带装置：服务端调换了部件顺序，身份必须按Content-Location钉住。"""
        adapter, store, collector = self.make_env()
        fixture_meta = (REPO / "sources" / "metadata.json").read_bytes()
        fixture_multi = (REPO / "sources" / "frames.multipart").read_bytes()
        fixture_resp = json.loads((REPO / "sources" / "response.json").read_text())
        expected = json.loads((REPO / "sources" / "expected-frames.json").read_text())
        adapter.route(metadata_url(KEY), body=fixture_meta,
                      headers=metadata_headers(),
                      etag=fixture_resp["metadata_etag"])
        adapter.route(frames_url(KEY, [1, 2]), body=fixture_multi,
                      headers={"Content-Type": fixture_resp["content_type"]},
                      etag=fixture_resp["etag"])

        report = collector.acquire([InstanceRequest(KEY)])

        self.assertEqual(report.consistency, "bound")
        self.assertEqual(
            store.frame_path(KEY.frame(1)).read_bytes(), bytes(expected["1"]))
        self.assertEqual(
            store.frame_path(KEY.frame(2)).read_bytes(), bytes(expected["2"]))
        # 部件顺序是frame2在前：frame1来自第2个部件，frame2来自第1个部件
        provenance = report.provenance_map()
        self.assertEqual(provenance[KEY.frame(1).label()], "rq-0001#part1")
        self.assertEqual(provenance[KEY.frame(2).label()], "rq-0001#part0")
        # 字节统计：实收=元数据+multipart正文
        self.assertEqual(report.bytes_received,
                         len(fixture_meta) + len(fixture_multi))
        self.assertEqual(len(report.requests), 2)
        check = check_store(store.root)
        self.assertTrue(check["ok"], check["problems"])
        self.assertEqual(check["frames_checked"], 2)

    # ---------------------------------------------------------- 帧身份
    def test_noncontiguous_frames_across_two_runs(self):
        """分两次取得非连续帧：先1、3，后2；恢复只复用已核验同版帧。"""
        adapter, store, collector = self.make_env(max_frames=1)
        self.route_metadata(adapter, KEY, frames=4)
        self.route_frames(adapter, KEY, [1], payload_len=6)
        self.route_frames(adapter, KEY, [3], payload_len=6)
        report1 = collector.acquire([InstanceRequest(KEY, frames=(1, 3))])
        self.assertEqual(report1.instances[0].fetched_frames, (1, 3))

        # 第二次运行：新采集器同一落盘目录，只应补帧2
        adapter2, store2, collector2 = self.reopen_env(store)
        self.route_metadata(adapter2, KEY, frames=4)
        self.route_frames(adapter2, KEY, [2], payload_len=6)
        report2 = collector2.acquire([InstanceRequest(KEY, frames=(1, 2, 3))])
        self.assertEqual(report2.instances[0].reused_frames, (1, 3))
        self.assertEqual(report2.instances[0].fetched_frames, (2,))
        self.assertNotIn(frames_url(KEY, [1]), adapter2.requested_urls())
        self.assertNotIn(frames_url(KEY, [3]), adapter2.requested_urls())
        for n in (1, 2, 3):
            self.assertEqual(store2.frame_path(KEY.frame(n)).read_bytes(),
                             frame_bytes(KEY, n, 6))
        self.assertTrue(check_store(store2.root)["ok"])

    def test_merge_split_requests_same_identity(self):
        """合并/拆分请求不改变最终帧身份与字节。"""
        results = {}
        for max_frames in (1, 2, 128):
            adapter, store, collector = self.make_env(max_frames=max_frames)
            self.route_metadata(adapter, KEY, frames=4)
            step = min(max_frames, 4)
            frames = [1, 2, 3, 4]
            for start in range(0, 4, step):
                self.route_frames(adapter, KEY,
                                  frames[start:start + step], payload_len=6)
            report = collector.acquire([InstanceRequest(KEY)])
            results[max_frames] = (
                {n: store.frame_path(KEY.frame(n)).read_bytes() for n in frames},
                len([r for r in report.requests if r.purpose == "frames"]),
            )
        self.assertEqual(results[1][0], results[2][0])
        self.assertEqual(results[1][0], results[128][0])
        self.assertEqual(results[1][1], 4)
        self.assertEqual(results[128][1], 1)

    def test_conflicting_duplicate_frame_rejected(self):
        """同帧重复部件字节不同：冲突，拒绝。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            (part_headers(KEY, 1), b"\x01" * 6),
            (part_headers(KEY, 1), b"\x02" * 6),
        ]
        self.route_frames(adapter, KEY, [1], parts=parts)
        with self.assertRaises(FrameConflictError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])

    def test_identical_duplicate_frame_deduped(self):
        """同帧重复部件字节相同：去重并记录。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            (part_headers(KEY, 1), frame_bytes(KEY, 1, 6)),
            (part_headers(KEY, 1), frame_bytes(KEY, 1, 6)),
        ]
        self.route_frames(adapter, KEY, [1], parts=parts)
        report = collector.acquire([InstanceRequest(KEY, frames=(1,))])
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(len(report.frames), 1)

    def test_ambiguous_parts_rejected(self):
        """无Content-Location且来源无顺序契约：拒绝歧义，不猜。"""
        adapter, store, collector = self.make_env(ordered=False)
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            ([("Content-Type", "application/octet-stream")], frame_bytes(KEY, 1, 6)),
            ([("Content-Type", "application/octet-stream")], frame_bytes(KEY, 2, 6)),
        ]
        self.route_frames(adapter, KEY, [1, 2], parts=parts)
        with self.assertRaises(AmbiguousPartError):
            collector.acquire([InstanceRequest(KEY, frames=(1, 2))])

    def test_order_mode_only_when_contracted(self):
        """来源声明按序返回时，请求顺序模式可用。"""
        adapter, store, collector = self.make_env(ordered=True)
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            ([("Content-Type", "application/octet-stream")], b"\xaa" * 6),
            ([("Content-Type", "application/octet-stream")], b"\xbb" * 6),
        ]
        self.route_frames(adapter, KEY, [1, 2], parts=parts)
        report = collector.acquire([InstanceRequest(KEY, frames=(1, 2))])
        self.assertEqual(store.frame_path(KEY.frame(1)).read_bytes(), b"\xaa" * 6)
        self.assertEqual(store.frame_path(KEY.frame(2)).read_bytes(), b"\xbb" * 6)

    def test_location_order_disagreement_rejected(self):
        """Content-Location与请求顺序矛盾：拒绝。"""
        adapter, store, collector = self.make_env(ordered=True)
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            (part_headers(KEY, 2), frame_bytes(KEY, 2, 6)),
            (part_headers(KEY, 1), frame_bytes(KEY, 1, 6)),
        ]
        self.route_frames(adapter, KEY, [1, 2], parts=parts)
        with self.assertRaises(PartIdentityError):
            collector.acquire([InstanceRequest(KEY, frames=(1, 2))])

    def test_unrequested_frame_rejected(self):
        """范围外帧不得混入。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            (part_headers(KEY, 1), frame_bytes(KEY, 1, 6)),
            (part_headers(KEY, 3), frame_bytes(KEY, 3, 6)),
        ]
        self.route_frames(adapter, KEY, [1, 2], parts=parts)
        with self.assertRaises(UnexpectedPartError):
            collector.acquire([InstanceRequest(KEY, frames=(1, 2))])

    def test_foreign_instance_part_rejected(self):
        """非请求实例的部件不得混入。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        parts = [
            (part_headers(KEY, 1), frame_bytes(KEY, 1, 6)),
            (part_headers(KEY2, 1), frame_bytes(KEY2, 1, 6)),
        ]
        self.route_frames(adapter, KEY, [1, 2], parts=parts)
        with self.assertRaises(UnexpectedPartError):
            collector.acquire([InstanceRequest(KEY, frames=(1, 2))])

    def test_missing_frame_not_registered(self):
        """少帧：实例不得登记完整，也不得以空文件顶替。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        self.route_frames(adapter, KEY, [1, 2],
                          parts=[(part_headers(KEY, 1), frame_bytes(KEY, 1, 6))])
        with self.assertRaises(IncompleteInstanceError):
            collector.acquire([InstanceRequest(KEY, frames=(1, 2))])
        self.assertFalse(store.frame_path(KEY.frame(2)).exists())
        self.assertIsNone(store.instance_record(KEY))

    # ---------------------------------------------------------- 布局核对
    def test_padding_not_counted_as_pixels(self):
        """奇数像素允许偶数填充，填充不算像素。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=3)  # 3字节像素
        payload = frame_bytes(KEY, 1, 3)
        parts = [(part_headers(KEY, 1), payload + b"\x00")]
        self.route_frames(adapter, KEY, [1], parts=parts)
        report = collector.acquire([InstanceRequest(KEY, frames=(1,))])
        record = report.frames[KEY.frame(1)]
        self.assertEqual(record.length, 3)
        self.assertEqual(record.padding, 1)
        self.assertEqual(store.frame_path(KEY.frame(1)).read_bytes(), payload)

    def test_wrong_frame_length_rejected(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)  # 6字节像素
        parts = [(part_headers(KEY, 1), b"\x00" * 5)]
        self.route_frames(adapter, KEY, [1], parts=parts)
        with self.assertRaises(FrameLayoutError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])

    def test_part_content_length_mismatch_rejected(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        parts = [(part_headers(KEY, 1, content_length=99), frame_bytes(KEY, 1, 6))]
        self.route_frames(adapter, KEY, [1], parts=parts)
        with self.assertRaises(FrameLayoutError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])

    def test_response_content_length_mismatch_rejected(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        body = self.route_frames(adapter, KEY, [1], payload_len=6)
        adapter.routes[frames_url(KEY, [1])]["headers"]["Content-Length"] = str(len(body) + 5)
        with self.assertRaisesRegex(MimeError, "长度不符"):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])

    def test_transfer_syntax_checked_everywhere(self):
        # multipart级
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        body = build_multipart(b"bnd", [(part_headers(KEY, 1), frame_bytes(KEY, 1, 6))])
        adapter.route(frames_url(KEY, [1]), body=body,
                      headers={"Content-Type": multipart_content_type(
                          "bnd", transfer_syntax="1.2.840.10008.1.2.2")})
        with self.assertRaises(TransferSyntaxError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])
        # 部件级
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4)
        parts = [(part_headers(KEY, 1, transfer_syntax="1.2.840.10008.1.2.2"),
                  frame_bytes(KEY, 1, 6))]
        self.route_frames(adapter, KEY, [1], parts=parts)
        with self.assertRaises(TransferSyntaxError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])
        # 元数据级
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=4,
                            headers={"Content-Type": "application/dicom+json",
                                     "X-Transfer-Syntax": "1.2.840.10008.1.2.2"})
        with self.assertRaises(TransferSyntaxError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])

    # ---------------------------------------------------------- 版本绑定
    def test_frames_generation_mismatch_rejected(self):
        """帧响应与元数据目录不同代：拒绝（防止新元数据配旧像素）。"""
        adapter, store, collector = self.make_env(generation="g1")
        self.route_metadata(adapter, KEY, frames=4)
        self.route_frames(adapter, KEY, [1], payload_len=6, generation="g2")
        with self.assertRaises(VersionMismatchError):
            collector.acquire([InstanceRequest(KEY, frames=(1,))])

    def test_metadata_generation_mismatch_rejected(self):
        """两个实例的元数据不同代：目录拒绝冻结。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, generation="g1", etag='"m1"')
        self.route_metadata(adapter, KEY2, frames=2, generation="g2", etag='"m2"')
        with self.assertRaises(VersionMismatchError):
            collector.acquire([InstanceRequest(KEY), InstanceRequest(KEY2)])

    def test_unverifiable_version_marked_unknown(self):
        """没有可验证版本绑定的来源：标一致性未知，不宣称同版完整。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, etag=None)
        self.route_frames(adapter, KEY, [1, 2], payload_len=6, etag=None)
        report = collector.acquire([InstanceRequest(KEY)])
        self.assertEqual(report.consistency, "unknown")
        self.assertEqual(report.instances[0].record.consistency, "unknown")
        check = check_store(store.root)
        self.assertTrue(check["ok"], check["problems"])
        self.assertEqual(check["consistency"]["/".join(KEY.as_tuple())], "unknown")

    def test_generation_change_invalidates_cache(self):
        """换代后恢复：旧代缓存不得复用，必须重新取帧。"""
        adapter, store, collector = self.make_env(generation="g1")
        self.route_metadata(adapter, KEY, frames=2, etag='"meta-g1"')
        self.route_frames(adapter, KEY, [1, 2], payload_len=6)
        collector.acquire([InstanceRequest(KEY)])

        adapter2 = FakeAdapter(generation="g2")
        store2 = FrameStore(store.root)
        collector2 = Collector(adapter2, store2, self.make_config())
        changed = [(part_headers(KEY, n), b"\x99" * 6) for n in (1, 2)]
        self.route_metadata(adapter2, KEY, frames=2, etag='"meta-g2"')
        adapter2.route(frames_url(KEY, [1, 2]),
                       body=build_multipart(b"bnd", changed),
                       headers={"Content-Type": multipart_content_type("bnd")},
                       etag='"frames-g2"')
        report2 = collector2.acquire([InstanceRequest(KEY)])
        self.assertEqual(report2.instances[0].reused_frames, ())
        self.assertEqual(report2.instances[0].fetched_frames, (1, 2))
        self.assertEqual(store2.frame_path(KEY.frame(1)).read_bytes(), b"\x99" * 6)
        self.assertTrue(check_store(store2.root)["ok"])

    def test_resume_after_interruption(self):
        """中断恢复：只复用已核验且版本相符的帧，不重新请求。"""
        adapter, store, collector = self.make_env(max_frames=2)
        self.route_metadata(adapter, KEY, frames=4)
        self.route_frames(adapter, KEY, [1, 2], payload_len=6)
        # frames/3,4故意不登记路由：第一次运行在此中断
        with self.assertRaises(AssertionError):
            collector.acquire([InstanceRequest(KEY)])

        adapter2 = FakeAdapter(generation="g1")
        store2 = FrameStore(store.root)
        collector2 = Collector(adapter2, store2, self.make_config(max_frames=2))
        self.route_metadata(adapter2, KEY, frames=4)
        self.route_frames(adapter2, KEY, [3, 4], payload_len=6)
        report2 = collector2.acquire([InstanceRequest(KEY)])
        self.assertEqual(report2.instances[0].reused_frames, (1, 2))
        self.assertEqual(report2.instances[0].fetched_frames, (3, 4))
        self.assertNotIn(frames_url(KEY, [1, 2]), adapter2.requested_urls())
        self.assertTrue(check_store(store2.root)["ok"])

    # ---------------------------------------------------------- 批量正文
    def test_shared_bulk_data_uri(self):
        """两个实例共享一个BulkDataURI：各自按自身布局切片，身份不混。"""
        adapter, store, collector = self.make_env()
        shared_uri = "/dicomweb/bulk/shared"
        shared_url = ORIGIN + shared_uri
        blob = b"\x0a\x0b" + b"\x0c\x0d"  # 两帧，每帧2字节
        for key, etag in ((KEY, '"m1"'), (KEY2, '"m2"')):
            self.route_metadata(adapter, key, frames=2, rows=1, cols=2,
                                etag=etag, pixel="bulk", bulk_uri=shared_uri)
        adapter.route(shared_url, body=blob, etag='"bulk-1"',
                      headers={"Content-Type": "application/octet-stream"})
        report = collector.acquire([
            InstanceRequest(KEY, via="bulk"),
            InstanceRequest(KEY2, via="bulk"),
        ])
        for key in (KEY, KEY2):
            self.assertEqual(store.frame_path(key.frame(1)).read_bytes(), b"\x0a\x0b")
            self.assertEqual(store.frame_path(key.frame(2)).read_bytes(), b"\x0c\x0d")
            prov = report.frames[key.frame(1)].provenance
            self.assertEqual(prov.kind, "bulk-slice")
            self.assertEqual(prov.source_url, shared_url)
            self.assertEqual(prov.offset, 0)
            self.assertEqual(report.frames[key.frame(2)].provenance.offset, 2)
        # 共享正文只取一次
        self.assertEqual(len(adapter.requested_urls("bulk/shared")), 1)
        self.assertTrue(check_store(store.root)["ok"])

    def test_bulk_relative_uri_resolved_per_hop(self):
        """相对BulkDataURI按元数据响应地址解析。"""
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=2,
                            pixel="bulk", bulk_uri="px")
        resolved = metadata_url(KEY).rsplit("/", 1)[0] + "/px"
        blob = pixel_blob(KEY, 2, 2)
        adapter.route(resolved, body=blob, etag='"bulk-1"',
                      headers={"Content-Type": "application/octet-stream"})
        report = collector.acquire([InstanceRequest(KEY, via="bulk")])
        self.assertEqual(store.frame_path(KEY.frame(2)).read_bytes(),
                         frame_bytes(KEY, 2, 2))
        self.assertIn(resolved, adapter.requested_urls())

    def test_bulk_out_of_scope_rejected(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=2,
                            pixel="bulk", bulk_uri="https://other.invalid/px")
        with self.assertRaises(ScopeViolationError):
            collector.acquire([InstanceRequest(KEY, via="bulk")])
        self.assertEqual(adapter.requested_urls("other.invalid"), [])

    def test_cross_origin_credentials_not_forwarded(self):
        """跨源（范围内）取批量正文：不转发会话凭据。"""
        adapter, store, collector = self.make_env(
            scope_prefixes=[ORIGIN + "/", "https://cdn.invalid/"])
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=2,
                            pixel="bulk", bulk_uri="https://cdn.invalid/bulk/px")
        adapter.route("https://cdn.invalid/bulk/px", body=pixel_blob(KEY, 2, 2),
                      etag='"bulk-1"',
                      headers={"Content-Type": "application/octet-stream"})
        collector.acquire([InstanceRequest(KEY, via="bulk")])
        by_url = {r.url: r for r in adapter.requests}
        self.assertEqual(by_url[metadata_url(KEY)].headers.get("Authorization"),
                         CREDENTIALS["Authorization"])
        self.assertNotIn("Authorization",
                         by_url["https://cdn.invalid/bulk/px"].headers)

    def test_redirect_with_credentials_cross_origin_rejected(self):
        """重定向把同源请求带出源外而凭据已发送：拒绝。"""
        adapter, store, collector = self.make_env(
            scope_prefixes=[ORIGIN + "/", "https://cdn.invalid/"])
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=2,
                            pixel="bulk", bulk_uri="px")
        resolved = metadata_url(KEY).rsplit("/", 1)[0] + "/px"
        adapter.route(resolved, body=pixel_blob(KEY, 2, 2), etag='"bulk-1"',
                      headers={"Content-Type": "application/octet-stream"},
                      history=["https://cdn.invalid/hop"],
                      final_url="https://cdn.invalid/bulk/px")
        with self.assertRaises(ScopeViolationError):
            collector.acquire([InstanceRequest(KEY, via="bulk")])

    def test_inline_binary_pixel_data(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=2, pixel="inline")
        report = collector.acquire([InstanceRequest(KEY, via="bulk")])
        self.assertEqual(store.frame_path(KEY.frame(1)).read_bytes(),
                         frame_bytes(KEY, 1, 2))
        self.assertEqual(report.frames[KEY.frame(1)].provenance.kind,
                         "inline-binary")

    def test_bulk_length_must_match_frozen_layout(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2, rows=1, cols=2,
                            pixel="bulk", bulk_uri="px")
        resolved = metadata_url(KEY).rsplit("/", 1)[0] + "/px"
        adapter.route(resolved, body=b"\x00" * 5, etag='"bulk-1"',
                      headers={"Content-Type": "application/octet-stream"})
        with self.assertRaises(FrameLayoutError):
            collector.acquire([InstanceRequest(KEY, via="bulk")])

    # ---------------------------------------------------------- 清单检查
    def test_manifest_checker_detects_tamper(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2)
        self.route_frames(adapter, KEY, [1, 2], payload_len=6)
        collector.acquire([InstanceRequest(KEY)])
        self.assertTrue(check_store(store.root)["ok"])

        path = store.frame_path(KEY.frame(1))
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        check = check_store(store.root)
        self.assertFalse(check["ok"])
        self.assertTrue(any("摘要" in p for p in check["problems"]))

    def test_run_report_persisted_with_provenance(self):
        adapter, store, collector = self.make_env()
        self.route_metadata(adapter, KEY, frames=2)
        self.route_frames(adapter, KEY, [1, 2], payload_len=6)
        collector.acquire([InstanceRequest(KEY)])
        document = json.loads((store.root / "run_report.json").read_text())
        frames = document["frames"]
        self.assertEqual(frames[KEY.frame(1).label()]["provenance"], "rq-0001#part0")
        self.assertEqual(frames[KEY.frame(2).label()]["provenance"], "rq-0001#part1")
        self.assertEqual(document["consistency"], "bound")
        self.assertGreater(document["bytes_received"], 0)


if __name__ == "__main__":
    unittest.main()

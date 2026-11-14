"""DICOM JSON元数据解析：标签状态分别留存，布局约束与填充计算。"""
import unittest

from dicomweb_collector.dicomjson import (
    BULK_DATA,
    EMPTY,
    INLINE_BINARY,
    MISSING,
    VALUE,
    ZERO_LENGTH,
    build_descriptor,
    parse_dataset,
    parse_tag,
)
from dicomweb_collector.errors import MetadataError, TransferSyntaxError
from dicomweb_collector.identity import EXPLICIT_VR_LE, InstanceKey

KEY = InstanceKey("1.2.3", "1.2.3.4", "1.2.3.4.5")


def base_tags():
    return parse_dataset({
        "00280010": {"vr": "US", "Value": [2]},
        "00280011": {"vr": "US", "Value": [3]},
        "00280100": {"vr": "US", "Value": [8]},
        "00280002": {"vr": "US", "Value": [1]},
        "00280004": {"vr": "CS", "Value": ["MONOCHROME2"]},
        "00280008": {"vr": "IS", "Value": ["4"]},
        "7FE00010": {"vr": "OW", "BulkDataURI": "px"},
    })


class TagStateTests(unittest.TestCase):
    def test_missing_empty_zero_length_distinct(self):
        tags = parse_dataset({
            "00100010": {"vr": "PN", "Value": []},
            "00100020": {"vr": "LO", "Value": [""]},
            "00100030": {},
        })
        self.assertEqual(tags["00100010"].kind, EMPTY)
        self.assertEqual(tags["00100020"].kind, ZERO_LENGTH)
        self.assertEqual(tags["00100030"].kind, EMPTY)
        self.assertNotIn("00100040", tags)  # 标签缺失：不在映射中

    def test_inline_binary_decoded(self):
        tv = parse_tag({"vr": "OB", "InlineBinary": "AAEC"})
        self.assertEqual(tv.kind, INLINE_BINARY)
        self.assertEqual(tv.inline_binary, b"\x00\x01\x02")

    def test_bulk_data_uri(self):
        tv = parse_tag({"vr": "OW", "BulkDataURI": "frames"})
        self.assertEqual(tv.kind, BULK_DATA)
        self.assertEqual(tv.bulk_data_uri, "frames")

    def test_multiple_carriers_rejected(self):
        with self.assertRaises(MetadataError):
            parse_tag({"vr": "OW", "Value": [1], "BulkDataURI": "x"})

    def test_invalid_base64_rejected(self):
        with self.assertRaises(MetadataError):
            parse_tag({"vr": "OB", "InlineBinary": "!!!"})


class DescriptorTests(unittest.TestCase):
    def test_layout_and_padding(self):
        desc = build_descriptor(KEY, base_tags(), EXPLICIT_VR_LE)
        self.assertEqual(desc.frame_payload_bytes, 2 * 3)
        self.assertEqual(desc.padded_frame_bytes, 6)
        self.assertEqual(desc.number_of_frames, 4)

    def test_odd_payload_padded_to_even(self):
        tags = base_tags()
        desc = build_descriptor(KEY, _with(tags, {"00280011": {"vr": "US", "Value": [3]},
                                                  "00280010": {"vr": "US", "Value": [1]}}),
                                EXPLICIT_VR_LE)
        self.assertEqual(desc.frame_payload_bytes, 3)
        self.assertEqual(desc.padded_frame_bytes, 4)

    def test_transfer_syntax_restricted(self):
        with self.assertRaises(TransferSyntaxError):
            build_descriptor(KEY, base_tags(), "1.2.840.10008.1.2.2")

    def test_bits_allocated_restricted(self):
        tags = _with(base_tags(), {"00280100": {"vr": "US", "Value": [12]}})
        with self.assertRaisesRegex(MetadataError, "8或16"):
            build_descriptor(KEY, tags, EXPLICIT_VR_LE)

    def test_samples_per_pixel_restricted(self):
        tags = _with(base_tags(), {"00280002": {"vr": "US", "Value": [3]}})
        with self.assertRaisesRegex(MetadataError, "单样本"):
            build_descriptor(KEY, tags, EXPLICIT_VR_LE)

    def test_photometric_restricted_when_present(self):
        tags = _with(base_tags(), {"00280004": {"vr": "CS", "Value": ["RGB"]}})
        with self.assertRaisesRegex(MetadataError, "单样本灰度"):
            build_descriptor(KEY, tags, EXPLICIT_VR_LE)

    def test_missing_samples_assumed_and_recorded(self):
        tags = dict(base_tags())
        del tags["00280002"]
        desc = build_descriptor(KEY, tags, EXPLICIT_VR_LE)
        self.assertEqual(desc.samples_per_pixel, 1)
        self.assertIn("SamplesPerPixel", desc.assumed)

    def test_empty_samples_assumed_and_recorded(self):
        tags = _with(base_tags(), {"00280002": {}})
        desc = build_descriptor(KEY, tags, EXPLICIT_VR_LE)
        self.assertEqual(desc.samples_per_pixel, 1)
        self.assertIn("SamplesPerPixel", desc.assumed)

    def test_missing_rows_rejected(self):
        tags = dict(base_tags())
        del tags["00280010"]
        with self.assertRaisesRegex(MetadataError, "缺失"):
            build_descriptor(KEY, tags, EXPLICIT_VR_LE)

    def test_empty_rows_rejected(self):
        tags = _with(base_tags(), {"00280010": {"vr": "US", "Value": []}})
        with self.assertRaisesRegex(MetadataError, "为空或零长度"):
            build_descriptor(KEY, tags, EXPLICIT_VR_LE)

    def test_number_of_frames_defaults_to_one(self):
        tags = dict(base_tags())
        del tags["00280008"]
        desc = build_descriptor(KEY, tags, EXPLICIT_VR_LE)
        self.assertEqual(desc.number_of_frames, 1)

    def test_uid_mismatch_rejected(self):
        tags = _with(base_tags(), {"00080018": {"vr": "UI", "Value": ["9.9.9"]}})
        with self.assertRaisesRegex(MetadataError, "身份不符"):
            build_descriptor(KEY, tags, EXPLICIT_VR_LE)


def _with(tags, extra):
    merged = dict(tags)
    merged.update(parse_dataset(extra))
    return merged


if __name__ == "__main__":
    unittest.main()

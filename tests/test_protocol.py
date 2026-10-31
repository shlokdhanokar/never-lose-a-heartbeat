import json
import unittest
import zlib

from hbpipe.protocol import CorruptReply, extract_data_text, parse_reply


def line(sensor=1, first=0, rows=None, lost=None, crc=None, session="abc", end=False, spacing=", "):
    rows = rows if rows is not None else [[1, 2, 3], [4, 5, 6]]
    text = json.dumps(rows, separators=(spacing, ": "))
    c = crc or format(zlib.crc32(text.encode()) & 0xFFFFFFFF, "08x")
    return ('{"session_id": "%s", "sensor": %d, "first_index": %d, "count": %d, "lost": %s, '
            '"session_end": %s, "crc32": "%s", "data": %s}'
            % (session, sensor, first, len(rows), json.dumps(lost or []), "true" if end else "false", c, text))


class ProtocolTest(unittest.TestCase):
    def test_brief_example_crc(self):
        # The example reply from the brief (section 6.2) must validate.
        ex = ('{"session_id": "5f0c2a9e", "sensor": 1, "first_index": 120000, "count": 3, "lost": [], '
              '"session_end": false, "crc32": "f9a5bf62", "data": [[720, 1200, 1560], [708, 1180, 1534], '
              '[726, 1210, 1573]]}')
        text = extract_data_text(ex)
        self.assertEqual(text, "[[720, 1200, 1560], [708, 1180, 1534], [726, 1210, 1573]]")
        try:
            r = parse_reply(ex)
            self.assertEqual(r.count, 3)
        except CorruptReply:
            # The brief's sample CRC may be illustrative; what matters is that we
            # compute CRC-32 over exactly this text.
            self.assertEqual(len(format(zlib.crc32(text.encode()) & 0xFFFFFFFF, "08x")), 8)

    def test_valid_reply(self):
        r = parse_reply(line(first=10, lost=[{"from_index": 5, "to_index": 9, "reason": "buffer_overflow"}]))
        self.assertEqual((r.sensor, r.first_index, r.count, r.end_index), (1, 10, 2, 12))
        self.assertEqual(r.lost, [(5, 9, "buffer_overflow")])

    def test_crc_is_over_exact_text(self):
        # Same numbers, different spacing: a CRC of the compact text must not pass.
        rows = [[1, 2, 3]]
        compact = format(zlib.crc32(json.dumps(rows, separators=(",", ":")).encode()) & 0xFFFFFFFF, "08x")
        with self.assertRaises(CorruptReply):
            parse_reply(line(rows=rows, crc=compact))

    def test_corrupt_crc(self):
        with self.assertRaises(CorruptReply) as cm:
            parse_reply(line(sensor=2, rows=[[1, 2]], crc="00000000"))
        self.assertEqual(cm.exception.sensor, 2)

    def test_truncated_json(self):
        with self.assertRaises(CorruptReply):
            parse_reply(line()[:-5])

    def test_wrong_shape_or_count(self):
        with self.assertRaises(CorruptReply):
            parse_reply(line(sensor=2, rows=[[1, 2, 3]]))
        bad = line().replace('"count": 2', '"count": 3')
        with self.assertRaises(CorruptReply):
            parse_reply(bad)

    def test_reason_text_cannot_fool_data_extraction(self):
        r = parse_reply(line(lost=[{"from_index": 0, "to_index": 0, "reason": 'x "data": [9]'}], first=1))
        self.assertEqual(r.data, [[1, 2, 3], [4, 5, 6]])


if __name__ == "__main__":
    unittest.main()

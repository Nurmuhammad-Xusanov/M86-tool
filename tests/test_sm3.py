"""Offline tests for the SM3 protocol layer.

The fixtures are frames the device actually sent or accepted, quoted where the
protocol recovery recorded them; the code under test derives its answers rather
than replaying them.
"""

from __future__ import annotations

import unittest

from m86tool.sm3 import frame, h4, isotp, selftest, write
from m86tool.sm3.device import CanFrame, Sm3Client
from m86tool.sm3.recorded import CLOSING, OPENING, READ, WRITE_TEMPLATE


def hx(s: str) -> bytes:
    return bytes.fromhex(s)


class SelfTest(unittest.TestCase):
    def test_all_offline_checks_pass(self):
        failures = [name for name, ok, detail in selftest.run() if not ok]
        self.assertEqual(failures, [])


class Framing(unittest.TestCase):
    def test_stride_rounds_up_to_four(self):
        self.assertEqual(frame.stride(0), 16)
        self.assertEqual(frame.stride(172), 188)
        self.assertEqual(frame.stride(175), 192)
        self.assertEqual(frame.stride(2), 20)
        self.assertEqual(frame.stride(261), 280)
        self.assertEqual(frame.stride(305), 324)

    def test_checksum_matches_the_wire(self):
        self.assertEqual(frame.checksum(0x83, 0), 0xD8)
        self.assertEqual(frame.checksum(0x88, 0), 0xDD)
        self.assertEqual(frame.checksum(0x30, 261), 0x8B)
        self.assertEqual(frame.checksum(0x1C, 19), 0x84)
        self.assertEqual(frame.checksum(0x1C, 3), 0x74)

    def test_greeting_round_trips(self):
        wire = hx("ffff0000ffff000000000000830000d8")
        result = frame.decode(wire)
        self.assertIsNotNone(result)
        msg, used = result
        self.assertEqual(used, 16)
        self.assertEqual(msg.op, 0x83)
        self.assertEqual(msg.seq, 0xFFFF)
        self.assertEqual(msg.h4, 0xFFFF)  # len 0: h4 equals seq
        self.assertEqual(msg.data, b"")
        self.assertEqual(msg.encode(), wire)

    def test_a_write_round_trips_with_its_padding(self):
        wire = hx("ffff0000c69b6d40688002e91c130084"
                  "60800208000000df070000020100000000000000")
        msg, used = frame.decode(wire)
        self.assertEqual(msg.op, 0x1C)
        self.assertEqual(len(msg.data), 19)
        self.assertEqual(used, 36)  # 16 + 19 = 35, padded to 36
        self.assertEqual(msg.data[0:3], bytes([0x60, 0x80, 0x02]))
        self.assertEqual(int.from_bytes(msg.data[7:11], "little"), 0x7DF)


class H4(unittest.TestCase):
    def test_the_table_is_a_crc_chain(self):
        poly = h4.POLY
        for n in range(h4.BITS - 1):
            nxt = h4.H4_BITS[n + 1]
            fold = poly if nxt & 1 else 0
            self.assertEqual(h4.H4_BITS[n], (nxt >> 1) ^ fold,
                             f"chain breaks at bit {n}")

    def test_h8_is_the_sum_of_the_data_words(self):
        # A 0100 request to 0x7DF as the official tool put it on the wire.
        data = hx("60800208000000df0700000201000000000000")
        five_words = sum(
            int.from_bytes(data[i:i + 4], "little") for i in range(0, 20, 4)
        )
        self.assertEqual(h4.h8(data), five_words & 0xFFFFFFFF)

    def test_h4_predicts_a_real_write(self):
        # The request that reads the VIN from the engine module; the frame
        # appears five times in the captures, so there is a right answer.
        recorded = hx("ffff0000d2f1e4e2811003ea1c130084"
                      "60800208000000e0070000021a90000000000000")
        template = WRITE_TEMPLATE
        data = bytearray(template[frame.HEADER:])
        data[7] = 0xE0
        data[8] = 0x07
        data[11] = 2
        data[12] = 0x1A
        data[13] = 0x90
        built = frame.reframe(template, bytes(data))
        self.assertEqual(built, recorded)


class Writes(unittest.TestCase):
    def test_write_reproduces_the_recorded_vin_request(self):
        recorded = hx("ffff0000d2f1e4e2811003ea1c130084"
                      "60800208000000e0070000021a90000000000000")
        self.assertEqual(write.write(0x7E0, bytes([0x1A, 0x90])), recorded)

    def test_write_rebuilds_the_reference_for_its_own_payload(self):
        self.assertEqual(write.write(0x7DF, bytes([0x01, 0x00])), WRITE_TEMPLATE)

    def test_single_frame_puts_the_length_in_the_pci_byte(self):
        self.assertEqual(write.single_frame(bytes([0x1A, 0x90])),
                         bytes([0x02, 0x1A, 0x90] + [0] * 5))

    def test_flow_control_reaches_the_module_as_a_flow_control_frame(self):
        built = write.write_frame(0x7E0, isotp.FLOW_CONTROL)
        msg, _ = frame.decode(built)
        self.assertEqual(msg.data[11:19], bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0]))
        self.assertEqual(int.from_bytes(msg.data[3:7], "little"), 8)


class IsoTp(unittest.TestCase):
    def test_single_frame_comes_straight_back(self):
        r = isotp.Reassembler()
        self.assertEqual(r.push(0x7E8, hx("064100983b201300")), hx("4100983b2013"))

    def test_segmented_response_is_joined(self):
        r = isotp.Reassembler()
        self.assertIsNone(r.push(0x7E8, hx("1011010203040506")))
        self.assertIsNone(r.push(0x7E8, hx("2107080910111213")))
        out = r.push(0x7E8, hx("2214151617000000"))
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 17)
        self.assertEqual(out[0:6], hx("010203040506"))
        self.assertEqual(out[16], 0x17)

    def test_segmenting_is_the_inverse_of_reassembly(self):
        payload = bytes(range(1, 31))
        r = isotp.Reassembler()
        done = None
        for f in isotp.segment(payload):
            p = r.push(0x7E8, f)
            if p is not None:
                done = p
        self.assertEqual(done, payload)


class RecordedData(unittest.TestCase):
    def test_every_recorded_message_decodes_and_re_encodes(self):
        # The padding the device sends is stale buffer content, so a re-encode
        # (which zero-pads) matches header and data but not the padding bytes.
        for message in OPENING + CLOSING + [READ, WRITE_TEMPLATE]:
            result = frame.decode(message)
            self.assertIsNotNone(result, message.hex())
            msg, used = result
            self.assertEqual(used, len(message))
            meaningful = frame.HEADER + len(msg.data)
            self.assertEqual(msg.encode()[:meaningful], message[:meaningful])

    def test_expected_sequence_lengths(self):
        self.assertEqual(len(OPENING), 18)
        self.assertEqual(len(CLOSING), 4)


class FrameDelivery(unittest.TestCase):
    def test_frames_in_parses_a_delivery_block(self):
        client = Sm3Client.__new__(Sm3Client)
        client.keep_all = True
        block = bytearray(36 + 16)
        block[34] = 0x10  # one frame, high nibble
        block[36:40] = (4).to_bytes(4, "little")
        block[40:44] = (0x7E8).to_bytes(4, "little")
        block[44:48] = bytes([0x06, 0x41, 0x00, 0x98])
        frames = client._frames_in(bytes(block))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].arbitration_id, 0x7E8)
        self.assertEqual(frames[0].data, bytes([0x06, 0x41, 0x00, 0x98]))

    def test_filter_drops_uninteresting_ids(self):
        self.assertTrue(Sm3Client._worth_keeping(0x7E8))
        self.assertTrue(Sm3Client._worth_keeping(0x640))
        self.assertTrue(Sm3Client._worth_keeping(0x5E8))
        self.assertTrue(Sm3Client._worth_keeping(0x540))
        self.assertFalse(Sm3Client._worth_keeping(0x123))


class Sm3AdapterIntegration(unittest.TestCase):
    def test_can_frame_duck_typing(self):
        f = CanFrame(0x7E8, b"\x02\x50\x03", is_extended_id=False, timestamp=1.23)
        self.assertEqual(f.arbitration_id, 0x7E8)
        self.assertEqual(f.data, b"\x02\x50\x03")
        self.assertFalse(f.is_extended_id)
        self.assertEqual(f.timestamp, 1.23)

    def test_sm3_client_recv_from_buffer(self):
        client = Sm3Client.__new__(Sm3Client)
        client._frames = [CanFrame(0x7E8, b"\x01\x02")]
        f = client.recv(timeout=0.01)
        self.assertIsNotNone(f)
        self.assertEqual(f.arbitration_id, 0x7E8)
        self.assertEqual(f.data, b"\x01\x02")
        self.assertIsNone(client.recv(timeout=0.001))

    def test_cli_parsing_sm3_flags(self):
        from m86tool.cli import build_parser
        p = build_parser()
        args = p.parse_args(["identify", "--sm3"])
        self.assertTrue(args.sm3)
        self.assertEqual(args.adapter, "auto")

        args2 = p.parse_args(["read", "flash.bin", "--adapter", "sm3", "--host", "192.168.1.100", "--port", "999"])
        self.assertEqual(args2.adapter, "sm3")
        self.assertEqual(args2.host, "192.168.1.100")
        self.assertEqual(args2.port, 999)


if __name__ == "__main__":
    unittest.main()

"""sau.peinfo 的单元测试：用系统自带的 kernel32.dll 当真实 PE 样本。"""

import os
import unittest
from pathlib import Path

from sau import peinfo

KERNEL32 = Path(os.environ.get("WINDIR", r"C:\Windows"), "System32", "kernel32.dll")


@unittest.skipUnless(KERNEL32.exists(), "需要 Windows 系统 DLL 作样本")
class PeInfoRealDllTests(unittest.TestCase):
    def test_machine_is_x64(self):
        data = KERNEL32.read_bytes()
        self.assertEqual(peinfo.machine_name_of(data), "x64")
        self.assertEqual(peinfo.machine_bits("x64"), 64)

    def test_export_names_parsed(self):
        names = peinfo.export_names_of(KERNEL32.read_bytes())
        self.assertIsNotNone(names)
        self.assertGreater(len(names), 300)
        self.assertIn("CreateFileW", names)
        self.assertNotIn("SteamAPI_Init", names)

    def test_inspect_summary(self):
        info = peinfo.inspect(KERNEL32)
        self.assertEqual(info["machine_name"], "x64")
        self.assertEqual(info["bits"], 64)


class PeInfoEdgeCaseTests(unittest.TestCase):
    def test_garbage_bytes_return_none(self):
        self.assertIsNone(peinfo.machine_name_of(b"not a pe file at all"))
        self.assertIsNone(peinfo.export_names_of(b"MZ" + b"\x00" * 100))

    def test_truncated_pe_header(self):
        data = b"MZ" + b"\x00" * 0x3C + (0x200).to_bytes(4, "little") + b"PE\x00\x00"[:2]
        self.assertIsNone(peinfo.machine_name_of(data))

    def test_missing_file(self):
        self.assertIsNone(peinfo.inspect("Z:/definitely/not/here.dll"))


if __name__ == "__main__":
    unittest.main()

import json
import sys
import tempfile
import unittest
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT))

from production_tool import FirmwareBundle, evaluate_status, save_record  # noqa: E402


def ready_status(family="ML307Y"):
    return {
        "firmware": "sukiyra-test",
        "chip": {"model": "ESP32-C3", "id": "AABBCCDDEEFF"},
        "modem": {
            "supported": True,
            "family": family,
            "model": family + "-TEST",
            "firmware": "TEST",
            "smsMode": "direct",
            "registered": True,
        },
        "sim": {"ready": True, "smsReady": True, "type": "physical", "iccidTail": "1234"},
        "network": {"plmn": "46001"},
    }


class ProductionToolTests(unittest.TestCase):
    def test_supported_modem_families_pass(self):
        for family in ("ML307A", "ML307C", "ML307R", "ML307Y"):
            with self.subTest(family=family):
                self.assertEqual(evaluate_status(ready_status(family), True, True), [])

    def test_requirements_are_enforced(self):
        status = ready_status()
        status["sim"]["ready"] = False
        status["modem"]["registered"] = False
        failures = evaluate_status(status, True, True)
        self.assertIn("SIM 未就绪", failures)
        self.assertIn("未完成蜂窝网络注册", failures)

    def test_manifest_hash_is_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "app.bin"
            image.write_bytes(b"firmware")
            import hashlib
            manifest = {
                "chip": "esp32c3",
                "version": "test",
                "images": [{"offset": "0x10000", "file": "app.bin", "sha256": hashlib.sha256(b"firmware").hexdigest()}],
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(FirmwareBundle.load(root).version, "test")
            image.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                FirmwareBundle.load(root)

    def test_csv_and_json_records_are_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_record(root, "COM9", ready_status(), "PASS", "")
            self.assertTrue((root / "production.csv").is_file())
            details = list(root.glob("*.json"))
            self.assertEqual(len(details), 1)
            self.assertEqual(json.loads(details[0].read_text(encoding="utf-8"))["result"], "PASS")


if __name__ == "__main__":
    unittest.main()

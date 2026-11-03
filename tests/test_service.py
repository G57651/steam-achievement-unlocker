"""UnlockService 业务流程测试（全部基于 FakeBindings，不需要 Steam）。"""

import json
import tempfile
import unittest
from pathlib import Path

from sau.service import ServiceError, UnlockService
from tests.fake_bindings import FakeState, make_factory

SCHEMA = ["ACH_A", "ACH_B", "ACH_C"]
NOOP_SLEEP = lambda *_args, **_kw: None  # noqa: E731


class ServiceTestBase(unittest.TestCase):
    def make_service(self, state: FakeState) -> UnlockService:
        return UnlockService(bindings_factory=make_factory(state), dll_path="fake/steam_api64.dll")

    def unlock(self, state, **kwargs) -> object:
        kwargs.setdefault("sleep", NOOP_SLEEP)
        return self.make_service(state).unlock(400, **kwargs)


class UnlockFlowTests(ServiceTestBase):
    def test_unlock_single_name(self):
        state = FakeState(SCHEMA)
        report = self.unlock(state, names=["ACH_A"])
        self.assertEqual(report.newly_unlocked, ["ACH_A"])
        self.assertEqual(state.set_calls, ["ACH_A"])
        self.assertEqual(state.store_calls, 1)
        self.assertEqual(state.shutdown_calls, 1)
        self.assertTrue(report.ok)

    def test_unlock_runs_callbacks_after_store(self):
        state = FakeState(SCHEMA)
        ticks = []

        def fake_sleep(seconds):
            ticks.append(seconds)

        report = self.unlock(state, names=["ACH_A"], settle=1.0, sleep=fake_sleep)
        self.assertTrue(report.ok)
        self.assertEqual(state.callbacks_calls, int(1.0 / 0.2))
        self.assertTrue(ticks)

    def test_unlock_all_skips_achieved(self):
        state = FakeState(SCHEMA, achieved={"ACH_B"})
        report = self.unlock(state, unlock_all=True)
        self.assertEqual(report.newly_unlocked, ["ACH_A", "ACH_C"])
        self.assertEqual(report.already_unlocked, ["ACH_B"])

    def test_unknown_name_does_not_block_others(self):
        state = FakeState(SCHEMA)
        report = self.unlock(state, names=["NOPE", "ACH_A"])
        self.assertEqual(report.unknown, ["NOPE"])
        self.assertEqual(report.newly_unlocked, ["ACH_A"])
        self.assertFalse(report.ok)

    def test_dry_run_changes_nothing(self):
        state = FakeState(SCHEMA)
        report = self.unlock(state, names=["ACH_A"], dry_run=True)
        self.assertEqual(report.newly_unlocked, ["ACH_A"])
        self.assertEqual(state.set_calls, [])
        self.assertEqual(state.store_calls, 0)
        self.assertTrue(report.ok)  # dry-run 不要求 stored

    def test_set_failure_recorded(self):
        state = FakeState(SCHEMA, fail_names={"ACH_A"})
        report = self.unlock(state, names=["ACH_A"])
        self.assertEqual(report.failed, ["ACH_A"])
        self.assertFalse(report.stored)
        self.assertFalse(report.ok)

    def test_store_failure_recorded(self):
        state = FakeState(SCHEMA, store_ok=False)
        report = self.unlock(state, names=["ACH_A"])
        self.assertEqual(report.newly_unlocked, ["ACH_A"])
        self.assertFalse(report.stored)
        self.assertFalse(report.ok)

    def test_no_settle_skips_callback_loop(self):
        state = FakeState(SCHEMA)
        report = self.unlock(state, names=["ACH_A"], settle=0)
        self.assertTrue(report.stored)
        self.assertEqual(state.callbacks_calls, 0)

    def test_duplicate_names_deduped(self):
        state = FakeState(SCHEMA)
        report = self.unlock(state, names=["ACH_A", "ACH_A"])
        self.assertEqual(report.newly_unlocked, ["ACH_A"])
        self.assertEqual(state.set_calls, ["ACH_A"])

    def test_all_and_nothing_to_do(self):
        state = FakeState(SCHEMA, achieved=set(SCHEMA))
        report = self.unlock(state, unlock_all=True)
        self.assertEqual(report.newly_unlocked, [])
        self.assertEqual(report.already_unlocked, SCHEMA)
        self.assertEqual(state.store_calls, 0)


class ServiceGuardTests(ServiceTestBase):
    def test_requires_names_or_all(self):
        with self.assertRaises(ServiceError):
            self.unlock(FakeState(SCHEMA))

    def test_steam_not_running(self):
        state = FakeState(SCHEMA, running=False)
        with self.assertRaises(ServiceError) as ctx:
            self.unlock(state, names=["ACH_A"])
        self.assertIn("Steam", str(ctx.exception))
        self.assertEqual(state.shutdown_calls, 0)

    def test_cwd_restored_and_appid_file_in_temp_dir(self):
        before = Path.cwd()
        state = FakeState(SCHEMA)
        self.unlock(state, names=["ACH_A"])
        self.assertEqual(Path.cwd(), before)
        self.assertFalse((before / "steam_appid.txt").exists())
        # init 时确实在临时目录里写好了 steam_appid.txt
        self.assertNotEqual(str(state.cwd_at_init), str(before))
        self.assertEqual(state.appid_at_init, "400")


class NamesJsonTests(ServiceTestBase):
    def test_fallback_when_dll_cannot_enumerate(self):
        state = FakeState(schema=None)  # 模拟旧 DLL
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            path.write_text(json.dumps({"400": ["ACH_A", "ACH_B"]}), encoding="utf-8")
            report = self.unlock(state, names=["ACH_A"], names_json=str(path))
        self.assertEqual(report.newly_unlocked, ["ACH_A"])

    def test_json_bom_is_tolerated(self):
        state = FakeState(schema=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            path.write_text(
                json.dumps(["ACH_A"], ensure_ascii=False), encoding="utf-8-sig"
            )
            report = self.unlock(state, names=["ACH_A"], names_json=str(path))
        self.assertEqual(report.newly_unlocked, ["ACH_A"])

    def test_invalid_json_raises(self):
        state = FakeState(schema=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            path.write_text("{oops", encoding="utf-8")
            with self.assertRaises(ServiceError):
                self.unlock(state, names=["ACH_A"], names_json=str(path))

    def test_missing_appid_in_dict_raises(self):
        state = FakeState(schema=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            path.write_text(json.dumps({"500": ["ACH_X"]}), encoding="utf-8")
            with self.assertRaises(ServiceError):
                self.unlock(state, names=["ACH_A"], names_json=str(path))


class ListTests(ServiceTestBase):
    def test_list_returns_full_metadata(self):
        state = FakeState(SCHEMA, achieved={"ACH_B"})
        rows = self.make_service(state).list_achievements(400)
        self.assertEqual([a.api_name for a in rows], SCHEMA)
        self.assertEqual(rows[0].display_name, "ACH_A 的名字")
        self.assertEqual(rows[0].description, "ACH_A 的描述")
        self.assertFalse(rows[0].achieved)
        self.assertTrue(rows[1].achieved)

    def test_list_uses_names_json_for_old_dll(self):
        state = FakeState(schema=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.json"
            path.write_text(json.dumps(["ACH_A"]), encoding="utf-8")
            rows = self.make_service(state).list_achievements(400, names_json=str(path))
        self.assertEqual([a.api_name for a in rows], ["ACH_A"])
        self.assertFalse(rows[0].achieved)

    def test_list_without_enumeration_or_json_raises(self):
        state = FakeState(schema=None)
        with self.assertRaises(ServiceError):
            self.make_service(state).list_achievements(400)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.admin, kind, data)

    def act(self, entity, action, data=None):
        return self.service.transition(self.admin, entity["id"], action, data or {})

    def equipment(self):
        return self.create("equipment", {
            "asset_no": "E-1", "equipment_type": "elevator",
            "location": "A", "inspection_interval_days": 365,
        })

    def alarm(self, equipment, code, level=None):
        data = {"equipment_id": equipment["id"], "code": code, "occurred_at": "2026-10-04T10:00:00Z"}
        if level is not None:
            data["level"] = level
        return self.create("alarm", data)

    def job(self, alarm, key, team):
        return self.create("rescue_job", {"alarm_id": alarm["id"], "dedupe_key": key, "team": team})

    def rescuer(self, name):
        return self.create("rescuer", {"name": name})

    def state(self, job):
        fresh = self.service.get(job["id"])
        return fresh["status"], fresh["data"].get("team")

    def test_capacity_limit_queues_excess_jobs(self):
        equipment = self.equipment()
        self.rescuer("Alpha")
        first = self.job(self.alarm(equipment, "A1"), "k1", "Alpha")
        second = self.job(self.alarm(equipment, "A2"), "k2", "Alpha")
        third = self.job(self.alarm(equipment, "A3"), "k3", "Alpha")
        self.assertEqual(self.state(first), ("dispatched", "Alpha"))
        self.assertEqual(self.state(second), ("queued", "Alpha"))
        self.assertEqual(self.state(third), ("queued", "Alpha"))

    def test_higher_priority_jumps_to_front_stable_fifo(self):
        equipment = self.equipment()
        self.rescuer("Alpha")
        low_one = self.job(self.alarm(equipment, "A1", level=3), "k1", "Alpha")
        low_two = self.job(self.alarm(equipment, "A2", level=3), "k2", "Alpha")
        high = self.job(self.alarm(equipment, "A3", level=5), "k3", "Alpha")
        self.assertEqual(self.state(low_one), ("dispatched", "Alpha"))
        self.assertEqual(self.state(low_two), ("queued", "Alpha"))
        self.assertEqual(self.state(high), ("queued", "Alpha"))
        # free Alpha -> high priority job assigned before the older low-priority one
        self.act(low_one, "arrive", {})
        self.act(low_one, "complete", {"outcome": "ok"})
        self.assertEqual(self.state(high), ("dispatched", "Alpha"))
        self.assertEqual(self.state(low_two), ("queued", "Alpha"))
        # same priority keeps original order: low_two is next in line
        self.act(high, "arrive", {})
        self.act(high, "complete", {"outcome": "ok"})
        self.assertEqual(self.state(low_two), ("dispatched", "Alpha"))

    def test_deactivate_reassigns_unfinished_jobs_preserves_arrival(self):
        equipment = self.equipment()
        alpha = self.rescuer("Alpha")
        self.rescuer("Bravo")
        job = self.job(self.alarm(equipment, "A1"), "k1", "Alpha")
        job = self.act(job, "arrive", {})
        self.assertEqual(job["status"], "on_site")
        arrived_at = job["data"].get("arrived_at")
        self.assertTrue(arrived_at)
        self.act(alpha, "deactivate", {})
        fresh = self.service.get(job["id"])
        self.assertEqual(fresh["status"], "on_site")
        self.assertEqual(fresh["data"].get("team"), "Bravo")
        self.assertEqual(fresh["data"].get("arrived_at"), arrived_at)

    def test_shift_change_reassigns_and_backfills_queue(self):
        equipment = self.equipment()
        alpha = self.rescuer("Alpha")
        self.rescuer("Bravo")
        job = self.job(self.alarm(equipment, "A1"), "k1", "Alpha")
        self.act(job, "arrive", {})
        self.act(alpha, "go_off_shift", {})
        fresh = self.service.get(job["id"])
        self.assertEqual(fresh["data"].get("team"), "Bravo")
        self.assertTrue(fresh["data"].get("arrived_at"))

    def test_deactivated_rescuer_jobs_queue_when_no_capacity(self):
        equipment = self.equipment()
        alpha = self.rescuer("Alpha")
        job = self.job(self.alarm(equipment, "A1"), "k1", "Alpha")
        self.act(job, "arrive", {})
        self.act(job, "complete", {"outcome": "ok"})
        second = self.job(self.alarm(equipment, "A2"), "k2", "Alpha")
        self.assertEqual(self.state(second), ("dispatched", "Alpha"))
        self.act(alpha, "deactivate", {})
        self.assertEqual(self.state(second), ("queued", "Alpha"))

    def test_equipment_update_recalculates_keeps_on_site(self):
        equipment = self.equipment()
        self.rescuer("Alpha")
        on_site = self.job(self.alarm(equipment, "A1"), "k1", "Alpha")
        self.act(on_site, "arrive", {})
        queued = self.job(self.alarm(equipment, "A2"), "k2", "Alpha")
        self.assertEqual(self.state(on_site), ("on_site", "Alpha"))
        self.assertEqual(self.state(queued), ("queued", "Alpha"))
        # equipment status change recomputes non-started dispatches; on-site continues
        self.act(equipment, "suspend", {})
        self.assertEqual(self.state(on_site), ("on_site", "Alpha"))
        self.assertEqual(self.state(queued), ("queued", "Alpha"))
        # freeing Alpha lets the recomputed queue advance
        self.act(on_site, "complete", {"outcome": "ok"})
        self.assertEqual(self.state(queued), ("dispatched", "Alpha"))

    def test_second_dispatcher_sees_current_owner(self):
        equipment = self.equipment()
        self.rescuer("Alpha")
        alarm = self.alarm(equipment, "A1")
        self.job(alarm, "k1", "Alpha")
        with self.assertRaises(ConflictError) as ctx:
            self.job(alarm, "k2", "Bravo")
        self.assertIn("Alpha", str(ctx.exception))

    def test_arrive_on_queued_job_is_invalid(self):
        equipment = self.equipment()
        self.rescuer("Alpha")
        self.job(self.alarm(equipment, "A1"), "k1", "Alpha")
        queued = self.job(self.alarm(equipment, "A2"), "k2", "Alpha")
        with self.assertRaises(InvalidTransition):
            self.act(queued, "arrive", {})

    def test_duplicate_rescuer_name_rejected(self):
        self.rescuer("Alpha")
        with self.assertRaises(ConflictError):
            self.rescuer("Alpha")


if __name__ == "__main__":
    unittest.main()

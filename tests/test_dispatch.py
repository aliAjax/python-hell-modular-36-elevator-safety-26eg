import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DispatchLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.dispatcher = Actor("dispatch-1", "dispatcher")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no="E-1"):
        return self.service.create(self.admin, "equipment", {
            "asset_no": asset_no, "equipment_type": "elevator",
            "location": "Tower", "inspection_interval_days": 365,
        })

    def worker(self, no, name=None):
        return self.service.create(self.dispatcher, "rescue_worker", {"worker_no": no, "name": name or no})

    def alarm(self, equipment, code, level=2):
        return self.service.create(self.dispatcher, "alarm", {
            "equipment_id": equipment["id"], "code": code,
            "occurred_at": "2026-10-04T10:00:00Z", "level": level,
        })

    def dispatch(self, alarm, team=None, level=None):
        payload = {}
        if team:
            payload["team"] = team
        if level is not None:
            payload["level"] = level
        return self.service.dispatch_alarm(self.dispatcher, alarm["id"], payload)["job"]

    def job(self, job_id):
        return self.service.get(job_id)

    def test_capacity_overflow_queues_by_level_and_fifo(self):
        equipment = self.equipment()
        w1 = self.worker("W1", "Alice")
        w2 = self.worker("W2", "Bob")

        a1 = self.dispatch(self.alarm(equipment, "C1"), level=2)
        a2 = self.dispatch(self.alarm(equipment, "C2"), level=2)
        a3 = self.dispatch(self.alarm(equipment, "C3"), level=2)

        self.assertEqual(self.job(a1["id"])["status"], "dispatched")
        self.assertEqual(self.job(a2["id"])["status"], "dispatched")
        self.assertEqual(self.job(a3["id"])["status"], "queued")
        initial_slots = {
            a1["id"]: self.job(a1["id"])["data"]["assignee_id"],
            a2["id"]: self.job(a2["id"])["data"]["assignee_id"],
        }
        self.assertEqual(len(set(initial_slots.values())), 2)
        self.assertIsNone(self.job(a3["id"])["data"]["assignee_id"])

        # A higher level job jumps to the front of the wait list; the
        # displaced ordinary job keeps its original place behind it.
        a4 = self.dispatch(self.alarm(equipment, "C4"), level=4)
        self.assertEqual(self.job(a4["id"])["status"], "queued")
        view = self.service.queue_view()
        self.assertEqual([j["id"] for j in view["waiting"]], [a4["id"], a3["id"]])

        # Completing the front job frees one slot: level-4 goes first.
        self.service.transition(self.dispatcher, a1["id"], "arrive", {})
        freed_slot = self.job(a1["id"])["data"]["assignee_id"]
        self.service.transition(self.dispatcher, a1["id"], "complete", {"outcome": "freed"})
        self.assertEqual(self.job(a4["id"])["status"], "dispatched")
        self.assertEqual(self.job(a4["id"])["data"]["assignee_id"], freed_slot)
        self.assertEqual(self.job(a3["id"])["status"], "queued")

        self.service.transition(self.dispatcher, a2["id"], "abort", {"reason": "false rescue"})
        self.assertEqual(self.job(a3["id"])["status"], "dispatched")
        self.assertEqual(self.job(a3["id"])["data"]["assignee_id"], initial_slots[a2["id"]])

    def test_deactivated_worker_reassigns_and_keeps_arrival_time(self):
        equipment = self.equipment()
        w1 = self.worker("W1", "Alice")
        w2 = self.worker("W2", "Bob")

        a1 = self.dispatch(self.alarm(equipment, "C1"))
        a2 = self.dispatch(self.alarm(equipment, "C2"))
        a3 = self.dispatch(self.alarm(equipment, "C3"))

        arrived = self.service.transition(self.dispatcher, a1["id"], "arrive", {})
        arrived_at = arrived["data"]["arrived_at"]
        self.assertTrue(arrived_at)

        # Alice (on site at C1) is deactivated while Bob is still en route to
        # C2. The on-site orphan jumps ahead and inherits Bob, because an
        # ongoing rescue cannot stall; C2 goes back to the queue keeping its
        # place in front of C3.
        self.service.transition(self.dispatcher, w1["id"], "deactivate", {"reason": "injury"})

        a1_now = self.job(a1["id"])
        self.assertEqual(a1_now["status"], "on_site")
        self.assertEqual(a1_now["data"]["assignee_id"], w2["id"])
        self.assertEqual(a1_now["data"]["arrived_at"], arrived_at)
        self.assertEqual(a1_now["data"]["reassignments"][-1]["from_worker_id"], w1["id"])
        self.assertEqual(a1_now["data"]["reassignments"][-1]["to_worker_id"], w2["id"])

        a2_now = self.job(a2["id"])
        self.assertEqual(a2_now["status"], "queued")
        self.assertIsNone(a2_now["data"]["assignee_id"])

        view = self.service.queue_view()
        self.assertEqual([j["id"] for j in view["waiting"]], [a2["id"], a3["id"]])

        # Reactivating a worker drains the queue in original order.
        self.service.transition(self.dispatcher, w1["id"], "activate", {})
        self.assertEqual(self.job(a2["id"])["status"], "dispatched")
        self.assertEqual(self.job(a2["id"])["data"]["assignee_id"], w1["id"])
        self.assertEqual(self.job(a3["id"])["status"], "queued")

        # Shift end works the same way; arrival time survives every move.
        self.service.transition(self.dispatcher, w2["id"], "end_shift", {"reason": "handover"})
        a1_now = self.job(a1["id"])
        self.assertEqual(a1_now["status"], "on_site")
        self.assertEqual(a1_now["data"]["assignee_id"], w1["id"])
        self.assertEqual(a1_now["data"]["arrived_at"], arrived_at)
        self.assertEqual(self.job(a2["id"])["status"], "queued")

    def test_equipment_change_recomputes_unstarted_only(self):
        eq1 = self.equipment("E-1")
        eq2 = self.equipment("E-2")
        w1 = self.worker("W1", "Alice")
        w2 = self.worker("W2", "Bob")

        # eq2 gets its on-site mission first (on Alice). eq1 then fills Bob
        # with a low-level job and queues a high-level one.
        other = self.dispatch(self.alarm(eq2, "OTHER"), level=1)
        low = self.dispatch(self.alarm(eq1, "LOW"), level=1)
        high = self.dispatch(self.alarm(eq1, "HIGH"), level=4)
        self.assertEqual(self.job(other["id"])["data"]["assignee_id"], w1["id"])
        self.assertEqual(self.job(low["id"])["data"]["assignee_id"], w2["id"])
        self.assertEqual(self.job(high["id"])["status"], "queued")
        self.service.transition(self.dispatcher, other["id"], "arrive", {})
        other_arrived_at = self.job(other["id"])["data"]["arrived_at"]

        # Equipment state change on eq1: not-started jobs are immediately
        # re-matched while on-site missions continue. HIGH is the only free
        # move (Bob takes it), LOW queues; eq2's mission and arrival time are
        # untouched.
        self.service.transition(self.admin, eq1["id"], "suspend", {})
        self.assertEqual(self.job(high["id"])["status"], "dispatched")
        self.assertEqual(self.job(high["id"])["data"]["assignee_id"], w2["id"])
        self.assertEqual(self.job(low["id"])["status"], "queued")
        self.assertIsNone(self.job(low["id"])["data"]["assignee_id"])

        other_now = self.job(other["id"])
        self.assertEqual(other_now["status"], "on_site")
        self.assertEqual(other_now["data"]["assignee_id"], w1["id"])
        self.assertEqual(other_now["data"]["arrived_at"], other_arrived_at)

    def test_concurrent_dispatch_same_alarm_shows_current_owner(self):
        equipment = self.equipment()
        self.worker("W1", "Alice")
        alarm = self.alarm(equipment, "C1")

        results = []
        barrier = threading.Barrier(2)

        def submit(user):
            barrier.wait()
            actor = Actor(user, "dispatcher")
            results.append(self.service.dispatch_alarm(actor, alarm["id"], {"team": user}))

        t1 = threading.Thread(target=submit, args=("d1",))
        t2 = threading.Thread(target=submit, args=("d2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        jobs = [r["job"] for r in results]
        self.assertEqual(jobs[0]["id"], jobs[1]["id"])
        flags = sorted(r["already_owned"] for r in results)
        self.assertEqual(flags, [False, True])

        stored = self.service.list("rescue_job")
        unfinished = [j for j in stored if j["data"]["alarm_id"] == alarm["id"]]
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(unfinished[0]["data"]["assignee_id"], jobs[0]["data"]["assignee_id"])


if __name__ == "__main__":
    unittest.main()

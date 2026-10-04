import hashlib
import threading
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine, UNFINISHED_JOB_STATUSES


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # The dispatch ledger is a single serialized account: every create,
        # dispatch and transition takes this lock so two dispatchers racing on
        # the same alarm observe the current ownership, never a double assign.
        self.ledger_lock = threading.RLock()

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _all(self, kind):
        return self.repository.list_entities(kind=self.rules.normalize_kind(kind))

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ queue

    def queue_view(self):
        """Snapshot of the dispatch ledger."""
        workers = self._all("rescue_worker")
        jobs = self._all("rescue_job")
        active = {w["id"]: w for w in workers if w["status"] == "active"}
        busy = {}
        for job in jobs:
            if job["status"] in UNFINISHED_JOB_STATUSES:
                assignee = job["data"].get("assignee_id")
                if assignee:
                    busy[assignee] = job["id"]
        queue = [
            j for j in jobs
            if j["status"] == "queued"
        ]
        queue.sort(key=lambda j: (-int(j["data"].get("level", 2)), int(j["data"].get("seq", 0))))
        return {
            "workers": [
                {
                    "id": w["id"],
                    "worker_no": w["data"].get("worker_no"),
                    "name": w["data"].get("name"),
                    "status": w["status"],
                    "current_job_id": busy.get(w["id"]),
                }
                for w in workers
            ],
            "free_worker_ids": [w["id"] for w in workers if w["status"] == "active" and w["id"] not in busy],
            "waiting": [
                {
                    "id": j["id"],
                    "alarm_id": j["data"].get("alarm_id"),
                    "level": int(j["data"].get("level", 2)),
                    "seq": int(j["data"].get("seq", 0)),
                    "status": j["status"],
                }
                for j in queue
            ],
        }

    def dispatch_alarm(self, actor, alarm_id, data=None, idempotency_key=None):
        """Dispatcher-facing dispatch: one active job per alarm, current
        ownership wins when two dispatchers submit concurrently."""
        with self.ledger_lock:
            alarm = self.repository.get_entity(alarm_id)
            if not alarm or alarm["kind"] != "alarm":
                raise NotFoundError("alarm not found: " + alarm_id)
            if actor.role not in ("admin", "dispatcher"):
                raise PermissionDenied("role %s is not allowed here" % actor.role)
            if alarm["status"] in ("closed", "false_alarm"):
                raise InvalidTransition("cannot dispatch alarm from status %s" % alarm["status"])

            existing = [
                j for j in self._all("rescue_job")
                if j["data"].get("alarm_id") == alarm_id and j["status"] in UNFINISHED_JOB_STATUSES
            ]
            if existing:
                # Late submitter sees the current ownership.
                return {"job": existing[0], "already_owned": True}

            payload = dict(data or {})
            payload["alarm_id"] = alarm_id
            payload.setdefault("dedupe_key", "alarm-" + alarm_id)
            payload.setdefault("team", payload.get("team") or actor.user_id)
            job = self._create_rescue_job(actor, payload)

            if alarm["status"] == "received":
                self._apply_transition(
                    actor, alarm, "dispatch",
                    {"team": job["data"].get("team"), "job_id": job["id"]},
                )
                alarm = self.repository.get_entity(alarm_id)
            return {"job": job, "already_owned": False}

    # ----------------------------------------------------------------- create

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        with self.ledger_lock:
            payload = dict(data or {})
            if idempotency_key:
                existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
                if existing:
                    entity = self.repository.get_entity(existing)
                    if entity:
                        return entity
            self.rules.validate_create(actor, kind, payload, self._lookup)
            entity_id = str(payload.pop("id", "") or uuid4())
            if self.repository.get_entity(entity_id):
                raise ConflictError("entity already exists: " + entity_id)

            if kind == "rescue_job":
                return self._create_rescue_job(actor, payload, entity_id=entity_id, idempotency_key=idempotency_key)

            status = self.rules.initial_status(kind, payload)
            if kind == "rescue_worker":
                workers = self._all("rescue_worker")
                payload["seq"] = (max((int(w["data"].get("seq", 0)) for w in workers), default=0) + 1)
            entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
            self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)

            if kind == "rescue_worker":
                # A fresh/extra crew can immediately take the highest queued job.
                self._reconcile(actor, full_recompute=False)
            return entity

    def _create_rescue_job(self, actor, payload, entity_id=None, idempotency_key=None):
        self.rules.validate_create(actor, "rescue_job", payload, self._lookup)
        entity_id = entity_id or str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        jobs = self._all("rescue_job")
        payload["seq"] = max((int(j["data"].get("seq", 0)) for j in jobs), default=0) + 1
        payload.setdefault("enqueued_at", _utcnow())
        payload["reassignments"] = []
        workers = self._all("rescue_worker")
        if not workers:
            # Legacy team-based mode: a free-floating job with a team name and
            # no capacity ledger.
            payload.setdefault("assignee_id", None)
            status = "dispatched"
        else:
            payload["assignee_id"] = None
            status = "queued"
        job = self.repository.create_entity(entity_id, "rescue_job", status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": "rescue_job"})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        if not workers:
            return job
        # Try to place the newcomer; existing dispatches are never reshuffled
        # by a new job (only higher level moves to the front of the wait list).
        return self._reconcile(actor, full_recompute=False, focus_job_id=entity_id) or self.repository.get_entity(entity_id)

    # ------------------------------------------------------------- transition

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self.ledger_lock:
            entity = self.repository.get_entity(entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            updated = self._apply_transition(actor, entity, action, dict(data or {}), expected_version)
            self._after_transition(actor, updated, action)
            return self.repository.get_entity(entity_id)

    def _apply_transition(self, actor, entity, action, data, expected_version=None):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        if entity["kind"] == "rescue_job" and action == "arrive":
            # Registered arrival time is immutable: once recorded it survives
            # queueing and any reassignment.
            patch.setdefault("arrived_at", _utcnow())
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"],
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _after_transition(self, actor, entity, action):
        kind = entity["kind"]
        if kind == "rescue_worker" and action in ("deactivate", "end_shift"):
            # Crew stops: free the unfinished job (on_site included) and hand
            # it to whoever is free; recorded arrived_at is preserved.
            self._reconcile(actor, full_recompute=False, released_worker_ids=[entity["id"]])
        elif kind == "rescue_worker" and action == "activate":
            self._reconcile(actor, full_recompute=False)
        elif kind == "rescue_job" and action in ("complete", "abort"):
            # Capacity frees up: pull the next job off the queue.
            self._reconcile(actor, full_recompute=False)
        elif kind == "rescue_job" and action == "arrive":
            self._reconcile(actor, full_recompute=False)
        elif kind == "equipment" and action in ("suspend", "out_of_service", "return_to_service"):
            # Equipment state changed: recompute every not-started dispatch
            # immediately; on_site missions continue untouched.
            self._reconcile(actor, full_recompute=True, equipment_id=entity["id"])

    # -------------------------------------------------------------- reconcile

    def _reconcile(self, actor, full_recompute, released_worker_ids=None, focus_job_id=None, equipment_id=None):
        workers = self._all("rescue_worker")
        if not workers:
            # Legacy mode (team names only, no registered crew): ledger rules
            # do not apply, nothing to move.
            return None
        jobs = self._all("rescue_job")
        if equipment_id and full_recompute:
            alarm_ids = {
                a["id"] for a in self._all("alarm") if a["data"].get("equipment_id") == equipment_id
            }
            scoped_ids = {j["id"] for j in jobs if j["data"].get("alarm_id") in alarm_ids}
            # Jobs of other equipment keep their workers; only the changed
            # equipment's not-started jobs are re-matched. On-site jobs on the
            # changed equipment are pinned inside plan_assignments anyway.
            movements = self.rules.plan_assignments(
                workers, jobs,
                released_worker_ids=released_worker_ids,
                recompute_ids=scoped_ids,
            )
        else:
            movements = self.rules.plan_assignments(
                workers, jobs,
                released_worker_ids=released_worker_ids,
                full_recompute=full_recompute,
            )

        if focus_job_id and not full_recompute:
            # Incremental events (new job / arrival) never steal a worker from
            # an already dispatched job; the focused job may still be matched
            # if a slot is genuinely free.
            focused = [m for m in movements if m["job_id"] == focus_job_id]
            movements = focused

        jobs_by_id = {j["id"]: j for j in jobs}
        workers_by_id = {w["id"]: w for w in workers}
        focus_updated = None
        for movement in movements:
            job = jobs_by_id[movement["job_id"]]
            patch = dict(job["data"])
            previous = patch.get("assignee_id")
            worker_id = movement["worker_id"]
            if worker_id != previous:
                history = list(patch.get("reassignments") or [])
                history.append({
                    "from_worker_id": previous,
                    "to_worker_id": worker_id,
                    "reason": "release" if previous in (released_worker_ids or []) else (
                        "recompute" if full_recompute else "schedule"),
                    "at": _utcnow(),
                    "by": actor.user_id,
                })
                patch["reassignments"] = history
                patch["assignee_id"] = worker_id
                if worker_id:
                    worker = workers_by_id[worker_id]
                    patch["team"] = worker["data"].get("name") or worker["data"].get("worker_no")
            # arrived_at is deliberately untouched through every move.
            internal_action = "assign" if worker_id else "queue"
            expected = job["status"]
            updated = self.repository.update_entity(job["id"], job["version"], movement["status"], patch)
            self.audit.record(
                job["id"], actor, internal_action,
                job["status"], movement["status"],
                {"assignee_id": worker_id, "previous_assignee_id": previous},
            )
            if movement["job_id"] == focus_job_id:
                focus_updated = updated
        return focus_updated

    # ------------------------------------------------------------- utilities

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        with self.ledger_lock:
            for raw in records:
                if not isinstance(raw, dict):
                    raise ValidationError("each offline record must be an object")
                source_id = str(raw.get("source_id", "")).strip()
                record_id = str(raw.get("record_id", "")).strip()
                if not source_id or not record_id:
                    raise ValidationError("source_id and record_id are required")
                digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
                entity_id = "offline-" + digest
                existing = self.repository.get_entity(entity_id)
                if existing:
                    created.append(existing)
                    continue
                payload = dict(raw)
                self.rules.validate_create(actor, "offline_record", payload, self._lookup)
                entity = self.repository.create_entity(
                    entity_id,
                    "offline_record",
                    self.rules.initial_status("offline_record", payload),
                    payload,
                    actor.user_id,
                )
                self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
                created.append(entity)
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

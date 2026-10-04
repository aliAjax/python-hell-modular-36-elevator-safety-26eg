import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _system_actor(self):
        return Actor("system", "admin")

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        if kind == "rescue_job":
            return self._create_rescue_job(actor, payload, idempotency_key)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_rescue_job(self, actor, payload, idempotency_key=None):
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        payload["priority"] = self._alarm_priority(payload.get("alarm_id"), payload.get("priority"))
        entity = self.repository.create_rescue_job(entity_id, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, entity["status"], {"kind": "rescue_job"})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        self._process_queue(actor)
        return entity

    def _alarm_priority(self, alarm_id, fallback=None):
        alarm = self.repository.find_entities("alarm", "id", alarm_id) if alarm_id else []
        if alarm:
            level = alarm[0]["data"].get("level")
            if level is not None and level != "":
                try:
                    return int(level)
                except (TypeError, ValueError):
                    pass
        try:
            return int(fallback)
        except (TypeError, ValueError):
            return 3

    def _all_jobs(self):
        return self.repository.list_entities("rescue_job")

    def _unfinished_jobs(self):
        return [job for job in self._all_jobs() if job["status"] in ("dispatched", "on_site")]

    def _find_rescuer_by_name(self, name):
        if not name:
            return None
        for rescuer in self.repository.list_entities("rescuer"):
            if rescuer["data"].get("name") == name:
                return rescuer
        return None

    def _free_rescuers(self):
        busy = {job["data"].get("team") for job in self._unfinished_jobs()}
        return [
            rescuer
            for rescuer in self.repository.list_entities("rescuer")
            if rescuer["status"] == "on_duty" and rescuer["data"].get("name") not in busy
        ]

    def _assign_job(self, job, preferred=None, actor=None):
        free = self._free_rescuers()
        if not free:
            return None
        chosen = None
        if preferred:
            for rescuer in free:
                if rescuer["data"].get("name") == preferred:
                    chosen = rescuer
                    break
        if not chosen:
            chosen = free[0]
        new_status = "on_site" if job["status"] == "on_site" else "dispatched"
        updated = self.repository.update_entity(
            job["id"],
            job["version"],
            new_status,
            {**job["data"], "team": chosen["data"].get("name")},
        )
        if updated["data"].get("team") != job["data"].get("team") or updated["status"] != job["status"]:
            self.audit.record(
                job["id"],
                actor or self._system_actor(),
                "assign",
                job["status"],
                updated["status"],
                {"to": updated["data"].get("team"), "priority": updated["data"].get("priority")},
            )
        return updated

    def _back_to_queue(self, job, actor=None):
        updated = self.repository.update_entity(job["id"], job["version"], "queued", dict(job["data"]))
        self.audit.record(
            job["id"],
            actor or self._system_actor(),
            "queue",
            job["status"],
            updated["status"],
            {"from": job["data"].get("team")},
        )
        return updated

    def _process_queue(self, actor=None):
        """Assign queued jobs to free rescuers.

        Higher-priority jobs go first; within the same priority the original
        arrival order is preserved (stable FIFO).
        """
        queued = sorted(
            [job for job in self._all_jobs() if job["status"] == "queued"],
            key=lambda job: (-int(job["data"].get("priority", 3) or 3), job["created_at"], job["id"]),
        )
        for job in queued:
            if not self._free_rescuers():
                break
            self._assign_job(job, actor=actor)

    def _reassign_rescuer_jobs(self, rescuer, actor=None):
        """Reassign an off-duty rescuer's unfinished jobs to free rescuers."""
        name = rescuer["data"].get("name")
        jobs = [job for job in self._unfinished_jobs() if job["data"].get("team") == name]
        for job in jobs:
            if not self._assign_job(job, actor=actor):
                self._back_to_queue(job, actor=actor)
        self._process_queue(actor)

    def _recalculate_for_equipment(self, equipment_id, actor=None):
        """Recompute non-started dispatches for an equipment.

        Queued and dispatched (not yet on-site) jobs are re-evaluated: their
        priority is refreshed and jobs whose rescuer is unavailable are
        reassigned. On-site jobs continue untouched.
        """
        alarms = self.repository.find_entities("alarm", "equipment_id", equipment_id)
        alarm_ids = {alarm["id"] for alarm in alarms}
        for job in self._all_jobs():
            if job["data"].get("alarm_id") not in alarm_ids or job["status"] == "on_site":
                continue
            if job["status"] not in ("queued", "dispatched"):
                continue
            priority = self._alarm_priority(job["data"].get("alarm_id"), job["data"].get("priority"))
            if job["status"] == "dispatched":
                rescuer = self._find_rescuer_by_name(job["data"].get("team"))
                if not rescuer or rescuer["status"] != "on_duty":
                    if not self._assign_job(job, actor=actor):
                        self._back_to_queue(job, actor=actor)
                        continue
            if int(job["data"].get("priority", 3) or 3) != priority:
                fresh = self.repository.get_entity(job["id"])
                if fresh:
                    self.repository.update_entity(
                        fresh["id"], fresh["version"], fresh["status"],
                        {**fresh["data"], "priority": priority},
                    )
        self._process_queue(actor)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "rescuer":
            if updated["status"] == "off_duty":
                self._reassign_rescuer_jobs(updated, actor)
            elif updated["status"] == "on_duty":
                self._process_queue(actor)
        elif kind == "rescue_job" and updated["status"] in ("completed", "aborted"):
            self._process_queue(actor)
        elif kind == "equipment":
            self._recalculate_for_equipment(entity_id, actor)
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
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

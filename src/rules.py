from datetime import datetime, timedelta

from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _require(data, fields):
    for field in fields:
        value = data.get(field)
        if value is None or value == "" or value == [] or value == {}:
            raise ValidationError("missing required field: " + field)


def _ensure_role(actor, allowed):
    if "*" not in allowed and actor.role not in allowed:
        raise PermissionDenied("role %s is not allowed here" % actor.role)


def _all(lookup, kind):
    return lookup(kind, "*", None) or [] if lookup else []


def _find_one(lookup, kind, field, value):
    rows = lookup(kind, field, value) or [] if lookup else []
    return rows[0] if rows else None


def _positive(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be numeric")
    if number <= 0:
        raise ValidationError(field + " must be positive")
    return number


def _validate_equipment(data, lookup):
    asset_no = str(data.get("asset_no", "")).strip()
    if not asset_no:
        raise ValidationError("asset_no is required")
    if _find_one(lookup, "equipment", "asset_no", asset_no):
        raise ConflictError("equipment asset_no already exists: " + asset_no)
    _positive(data.get("inspection_interval_days"), "inspection_interval_days")


def _validate_inspection(data, lookup):
    equipment = _find_one(lookup, "equipment", "id", data.get("equipment_id"))
    if not equipment:
        raise ValidationError("inspection requires equipment")
    try:
        datetime.fromisoformat(str(data.get("scheduled_at")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("scheduled_at must be ISO-8601")
    _positive(data.get("cycle_days"), "cycle_days")


def _validate_maintenance(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("maintenance requires equipment")
    if data.get("work_type") not in ("routine", "repair", "component_replacement", "modernization"):
        raise ValidationError("invalid work_type")
    if data.get("work_type") == "component_replacement" and not data.get("part_serial"):
        raise ValidationError("part_serial is required for component replacement")


DEFAULT_JOB_LEVEL = 2
MAX_JOB_LEVEL = 4
UNFINISHED_JOB_STATUSES = ("queued", "dispatched", "on_site")


def _job_level(data):
    level = data.get("level", DEFAULT_JOB_LEVEL)
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise ValidationError("level must be an integer between 0 and %s" % MAX_JOB_LEVEL)
    if level < 0 or level > MAX_JOB_LEVEL:
        raise ValidationError("level must be an integer between 0 and %s" % MAX_JOB_LEVEL)
    return level


def _validate_alarm(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("alarm requires equipment")
    if data.get("level") is not None:
        _job_level(data)
    for alarm in _all(lookup, "alarm"):
        if (
            alarm["data"].get("equipment_id") == data.get("equipment_id")
            and alarm["data"].get("code") == data.get("code")
            and alarm["status"] not in ("closed", "false_alarm")
        ):
            raise ConflictError("active alarm already exists for equipment and code")


def _validate_rescue(data, lookup):
    alarm = _find_one(lookup, "alarm", "id", data.get("alarm_id"))
    if not alarm or alarm["status"] in ("closed", "false_alarm"):
        raise ValidationError("rescue_job requires an active alarm")
    data["level"] = _job_level(data) if data.get("level") is not None else int(alarm["data"].get("level", DEFAULT_JOB_LEVEL))
    key = data.get("dedupe_key")
    if key:
        for job in _all(lookup, "rescue_job"):
            if job["data"].get("dedupe_key") == key and job["status"] not in ("completed", "aborted"):
                raise ConflictError("active rescue job already exists for dedupe_key")


def _validate_worker(data, lookup):
    worker_no = str(data.get("worker_no", "")).strip()
    if not worker_no:
        raise ValidationError("worker_no is required")
    if _find_one(lookup, "rescue_worker", "worker_no", worker_no):
        raise ConflictError("worker_no already exists: " + worker_no)


def _validate_remediation(data, lookup):
    if not data.get("equipment_id") and not data.get("alarm_id"):
        raise ValidationError("remediation requires equipment_id or alarm_id")
    issue = str(data.get("issue", "")).strip()
    for item in _all(lookup, "remediation"):
        if item["data"].get("equipment_id") == data.get("equipment_id") and item["data"].get("issue") == issue and item["status"] not in ("closed",):
            raise ConflictError("open remediation already exists for issue")


def _validate_permit(data, lookup):
    if not _find_one(lookup, "equipment", "id", data.get("equipment_id")):
        raise ValidationError("permit requires equipment")
    if data.get("purpose") not in ("return_to_service", "special_inspection", "temporary_operation"):
        raise ValidationError("invalid permit purpose")


def _grant_permit(actor, entity, data, lookup):
    equipment = _find_one(lookup, "equipment", "id", entity["data"].get("equipment_id"))
    if not equipment or equipment["status"] not in ("in_service", "suspended"):
        raise ConflictError("permit can only be granted for a serviceable equipment")
    inspections = [i for i in _all(lookup, "inspection") if i["data"].get("equipment_id") == equipment["id"] and i["status"] == "passed"]
    if not inspections:
        raise ConflictError("permit requires a passed inspection")
    if [r for r in _all(lookup, "remediation") if r["data"].get("equipment_id") == equipment["id"] and r["status"] != "closed"]:
        raise ConflictError("permit blocked by open remediation")
    return {"granted_by": actor.user_id, "granted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def _verify_remediation(actor, entity, data, lookup):
    if not entity["data"].get("evidence"):
        raise ValidationError("remediation evidence is required before verification")
    return {"verified_by": actor.user_id}


def _complete_rescue(actor, entity, data, lookup):
    jobs = [j for j in _all(lookup, "rescue_job") if j["data"].get("alarm_id") == entity["id"]]
    if not jobs or any(job["status"] not in ("completed", "aborted") for job in jobs):
        raise ConflictError("alarm cannot close before rescue jobs are complete")
    return {"resolved_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "equipments": "equipment", "inspections": "inspection", "maintenances": "maintenance",
        "alarms": "alarm", "rescue_jobs": "rescue_job", "remediations": "remediation",
        "permits": "permit", "rescue_workers": "rescue_worker",
    }
    INITIAL_STATUS = {
        "equipment": "in_service", "inspection": "scheduled", "maintenance": "planned",
        "alarm": "received", "rescue_job": "queued", "remediation": "open",
        "permit": "blocked", "rescue_worker": "active",
    }
    TRANSITIONS = {
        "equipment": {
            "suspend": (("in_service",), "suspended"),
            "out_of_service": (("in_service", "suspended"), "out_of_service"),
            "return_to_service": (("suspended", "out_of_service"), "in_service"),
        },
        "inspection": {
            "pass": (("scheduled",), "passed"),
            "fail": (("scheduled",), "failed"),
            "reschedule": (("failed",), "scheduled"),
        },
        "maintenance": {
            "start": (("planned",), "in_progress"),
            "complete": (("in_progress",), "completed"),
        },
        "alarm": {
            "dispatch": (("received",), "dispatched"),
            "mark_false": (("received", "dispatched"), "false_alarm"),
            "resolve": (("dispatched",), "resolved"),
            "close": (("resolved",), "closed"),
        },
        "rescue_job": {
            "assign": (("queued", "dispatched"), "dispatched"),
            "queue": (("queued", "dispatched"), "queued"),
            "arrive": (("dispatched",), "on_site"),
            "complete": (("on_site",), "completed"),
            "abort": (("queued", "dispatched", "on_site"), "aborted"),
        },
        "rescue_worker": {
            "deactivate": (("active",), "inactive"),
            "end_shift": (("active",), "off_shift"),
            "activate": (("inactive", "off_shift"), "active"),
        },
        "remediation": {
            "submit_evidence": (("open",), "evidence_submitted"),
            "verify": (("evidence_submitted",), "verified"),
            "reject": (("evidence_submitted",), "open"),
            "close": (("verified",), "closed"),
        },
        "permit": {
            "request_review": (("blocked",), "pending_review"),
            "grant": (("pending_review",), "granted"),
            "revoke": (("granted", "pending_review"), "revoked"),
            "expire": (("granted",), "expired"),
        },
    }
    CREATE_REQUIRED = {
        "equipment": ("asset_no", "equipment_type", "location", "inspection_interval_days"),
        "inspection": ("equipment_id", "scheduled_at", "cycle_days"),
        "maintenance": ("equipment_id", "work_type", "planned_at"),
        "alarm": ("equipment_id", "code", "occurred_at"),
        "rescue_job": ("alarm_id",),
        "remediation": ("issue", "owner", "due_at"),
        "permit": ("equipment_id", "purpose", "requested_by"),
        "rescue_worker": ("worker_no", "name"),
    }
    ACTION_REQUIRED = {
        ("inspection", "pass"): ("findings",),
        ("inspection", "fail"): ("findings",),
        ("maintenance", "complete"): ("completed_at",),
        ("rescue_job", "complete"): ("outcome",),
        ("remediation", "submit_evidence"): ("evidence",),
        ("alarm", "resolve"): ("resolution",),
        ("permit", "revoke"): ("reason",),
        ("rescue_worker", "deactivate"): ("reason",),
        ("rescue_worker", "end_shift"): ("reason",),
    }
    CREATE_ROLES = {
        "equipment": ("admin", "inspector"),
        "inspection": ("admin", "inspector"),
        "maintenance": ("admin", "maintenance"),
        "alarm": ("admin", "dispatcher", "inspector"),
        "rescue_job": ("admin", "dispatcher"),
        "remediation": ("admin", "inspector", "maintenance"),
        "permit": ("admin", "inspector"),
        "rescue_worker": ("admin", "dispatcher"),
    }
    ROLE_ACTIONS = {
        "suspend": ("admin", "inspector"),
        "out_of_service": ("admin", "inspector"),
        "return_to_service": ("admin", "inspector"),
        "pass": ("admin", "inspector"),
        "fail": ("admin", "inspector"),
        "reschedule": ("admin", "inspector"),
        "start": ("admin", "maintenance"),
        "complete": ("admin", "maintenance", "dispatcher"),
        "dispatch": ("admin", "dispatcher"),
        "mark_false": ("admin", "dispatcher", "inspector"),
        "resolve": ("admin", "dispatcher"),
        "close": ("admin", "dispatcher", "inspector"),
        "assign": ("admin", "dispatcher"),
        "queue": ("admin", "dispatcher"),
        "arrive": ("admin", "dispatcher", "maintenance"),
        "abort": ("admin", "dispatcher"),
        "deactivate": ("admin", "dispatcher"),
        "end_shift": ("admin", "dispatcher"),
        "activate": ("admin", "dispatcher"),
        "submit_evidence": ("admin", "maintenance", "inspector"),
        "verify": ("admin", "inspector"),
        "reject": ("admin", "inspector"),
        "request_review": ("admin", "inspector"),
        "grant": ("admin", "inspector"),
        "revoke": ("admin", "inspector"),
        "expire": ("admin", "inspector"),
    }
    CUSTOM_CREATE = {
        "equipment": lambda a, d, l: _validate_equipment(d, l),
        "inspection": lambda a, d, l: _validate_inspection(d, l),
        "maintenance": lambda a, d, l: _validate_maintenance(d, l),
        "alarm": lambda a, d, l: _validate_alarm(d, l),
        "rescue_job": lambda a, d, l: _validate_rescue(d, l),
        "remediation": lambda a, d, l: _validate_remediation(d, l),
        "permit": lambda a, d, l: _validate_permit(d, l),
        "rescue_worker": lambda a, d, l: _validate_worker(d, l),
    }
    CUSTOM_TRANSITIONS = {
        ("permit", "grant"): _grant_permit,
        ("remediation", "verify"): _verify_remediation,
        ("alarm", "close"): _complete_rescue,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind, data=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        _ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        _require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        _ensure_role(actor, allowed)
        _require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def plan_assignments(self, workers, jobs, released_worker_ids=None, full_recompute=False, recompute_ids=None):
        """Compute the dispatch ledger for unfinished rescue jobs.

        Rules:
        - every active worker carries at most one unfinished job;
        - jobs wait by (level desc, seq asc), i.e. higher level jumps to the
          front while displaced jobs keep their original order;
        - on_site jobs always keep their worker (on-site missions continue);
        - released_worker_ids lose their jobs (worker disabled / shift end),
          on_site orphans still get first dibs at a free worker;
        - full_recompute / recompute_ids re-matches not-started jobs (e.g.
          equipment state change); jobs outside recompute_ids stay pinned.

        Returns a list of movements: {job_id, worker_id or None, status}.
        """
        released = set(released_worker_ids or ())
        # When a crew is stopped, unstarted dispatches are reshuffled together
        # with the released jobs so an on-site orphan can inherit a crew;
        # ordinary incremental scheduling never steals occupied workers.
        release_mode = bool(released)
        # No registered workers at all: keep the legacy team-based mode untouched.
        if not workers:
            return []
        recompute = set(recompute_ids or ())
        if full_recompute:
            recompute = {j["id"] for j in jobs}
        active_workers = [w for w in workers if w["status"] == "active"]
        active_ids = {w["id"] for w in active_workers}
        pending = [j for j in jobs if j["status"] in UNFINISHED_JOB_STATUSES]

        # 1. Current bindings that stay pinned. On-site tasks on a working crew
        # always continue; dispatched tasks stay unless their slot is released
        # (release reshuffles so the on-site orphan can inherit a crew) or
        # they are inside a recompute scope.
        occupied = set()
        loose = []
        for job in pending:
            worker_id = job["data"].get("assignee_id")
            keep = False
            if job["status"] == "on_site" and worker_id in active_ids and worker_id not in released:
                keep = True
            elif (
                not release_mode
                and job["id"] not in recompute
                and worker_id
                and worker_id in active_ids
            ):
                keep = True
            if keep:
                occupied.add(worker_id)
            else:
                loose.append(job)

        # 2. Free workers, in stable worker order.
        free_workers = [w["id"] for w in active_workers if w["id"] not in occupied and w["id"] not in released]

        # 3. Waiting order. On-site orphans (crew just stopped) jump ahead so
        # the rescue continues; otherwise higher level first, original queue
        # order otherwise.
        def job_key(job):
            level = int(job["data"].get("level", DEFAULT_JOB_LEVEL))
            seq = int(job["data"].get("seq", 0))
            on_site_bonus = 1 if job["status"] == "on_site" else 0
            return (-on_site_bonus, -level, seq)

        loose.sort(key=job_key)

        movements = []
        free_index = 0
        for job in loose:
            if free_index < len(free_workers):
                worker_id = free_workers[free_index]
                free_index += 1
                target_status = "on_site" if job["status"] == "on_site" else "dispatched"
                if job["data"].get("assignee_id") != worker_id or job["status"] != target_status:
                    movements.append({"job_id": job["id"], "worker_id": worker_id, "status": target_status})
            else:
                if job["status"] == "on_site":
                    # No one free, but an on-site mission cannot regress to
                    # queued: it stays on_site without an assignee until a crew
                    # comes back, keeping the recorded arrival time.
                    if job["data"].get("assignee_id") is not None:
                        movements.append({"job_id": job["id"], "worker_id": None, "status": "on_site"})
                elif job["data"].get("assignee_id") is not None or job["status"] != "queued":
                    movements.append({"job_id": job["id"], "worker_id": None, "status": "queued"})
        return movements

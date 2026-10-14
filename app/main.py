"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from datetime import date as date_type
from datetime import datetime, time as time_type, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Optional

from pymongo import MongoClient
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError
from pymongo.collection import ReturnDocument

# load_dotenv()

# MONGO_URI = os.getenv("MONGO_URI")
# MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# if not MONGO_URI:
#     raise RuntimeError("MONGO_URI not found. Check your .env file.")

# client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
# db = client[MONGO_DB]

import os
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

# Find candidate_kit/.env
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path, override=True)

MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI.startswith(("mongodb://", "mongodb+srv://")):
    raise RuntimeError(
        "Invalid MONGO_URI. Check the candidate_kit/.env file."
    )

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client[MONGO_DB]


IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def round_half_up(value: float, decimals: int) -> float:
    d = Decimal(str(value))
    q = Decimal("0." + "0" * decimals)
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def to_ms(dt: datetime) -> int:
    return int(_as_utc(dt).timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def trunc_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC tzinfo to a naive datetime (PyMongo returns naive UTC by default)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_attendance_date(ts: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Return IST attendance date string, handling overnight shifts."""
    ts_ist = _as_utc(ts).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if is_overnight and ts_ist.time() < time_type(eh, em):
        return (ts_ist.date() - timedelta(days=1)).isoformat()
    return ts_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    """R2: Grace period is strictly > 10 min. Returns floor minutes since shift_start."""
    ts = trunc_seconds(_as_utc(punch_in).astimezone(IST))
    h, m = map(int, shift_start.split(":"))
    start = ts.replace(hour=h, minute=m, second=0)
    elapsed = int((ts - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: Half-up rounded to 2 decimals."""
    seconds = (trunc_seconds(punch_out) - trunc_seconds(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, shift_start: str, att_date: str) -> int:
    """R3: Overtime only when >= 30 min after shift_end. Handles overnight shifts."""
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    is_overnight = (eh * 60 + em) <= (sh * 60 + sm)
    base = date_type.fromisoformat(att_date)
    end_date = base + timedelta(days=1) if is_overnight else base
    end = datetime(end_date.year, end_date.month, end_date.day, eh, em, tzinfo=IST)
    po = trunc_seconds(_as_utc(punch_out).astimezone(IST))
    elapsed = int((po - end).total_seconds())
    return elapsed // 60 if elapsed >= 1800 else 0


def serialize_record(doc: dict) -> dict:
    doc.pop("_id", None)
    for field in ("punch_in", "punch_out"):
        if doc.get(field) is not None:
            doc[field] = to_ms(doc[field])
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    for entry in doc.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_ms(entry["at"])
        for key, change in entry.get("changes", {}).items():
            if key in ("punch_in", "punch_out"):
                if isinstance(change.get("from"), datetime):
                    change["from"] = to_ms(change["from"])
                if isinstance(change.get("to"), datetime):
                    change["to"] = to_ms(change["to"])
    return doc


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = to_ms(doc["created_at"])
    return doc


def working_days_in_range(from_date: date_type, to_date: date_type) -> int:
    """Count Mon-Fri days inclusive."""
    count = 0
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def month_bounds(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM month."""
    year, mo = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mo)[1]
    return f"{month}-01", f"{month}-{last_day:02d}"


# --------------------------------------------------------------------------- #
# Startup: create indexes
# --------------------------------------------------------------------------- #

@app.on_event("startup")
def startup():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)]
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("emp_code", ASCENDING), ("late_minutes", ASCENDING)]
    )


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

# Epoch-millisecond range as defined in openapi.yaml EpochMillis schema
_EMS_GE = 100_000_000_000
_EMS_LE = 4_102_444_800_000
OptionalEpochMs = Annotated[Optional[int], Field(default=None, ge=_EMS_GE, le=_EMS_LE)]

_HH_MM = r"^([01]\d|2[0-3]):[0-5]\d$"
_EMP_CODE = r"^EMP\d{4,6}$"
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_DATE = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
_PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
_ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=_EMP_CODE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=_EMAIL)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=_HH_MM)
    shift_end: str = Field(default="18:30", pattern=_HH_MM)
    joined_on: str = Field(pattern=_DATE)


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: OptionalEpochMs = None
    status: str = Field(default="PRESENT")

    @field_validator("status")
    @classmethod
    def status_must_be_presence(cls, v: str) -> str:
        if v not in _PRESENCE:
            raise ValueError(f"status must be one of {sorted(_PRESENCE)}")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: OptionalEpochMs = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: OptionalEpochMs = None
    punch_out: OptionalEpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def status_must_be_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _ALL_STATUSES:
            raise ValueError(f"status must be one of {sorted(_ALL_STATUSES)}")
        return v


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize_employee(d) for d in items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = trunc_seconds(
        from_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc)
    )
    d = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = trunc_seconds(
        from_ms(body.punched_at) if body.punched_at else datetime.now(timezone.utc)
    )

    # Find most recent record whose punch_in <= ts (regardless of punch_out state)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found at or before the given time")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out for that record")

    pi = _as_utc(record["punch_in"])
    if ts <= pi:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pi).total_seconds() > 86400:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    wh = compute_work_hours(pi, ts)
    ot = compute_overtime(ts, emp["shift_end"], emp["shift_start"], record["date"])
    hd = wh < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")

    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [serialize_record(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status if body.status is not None else record["status"]

    # ABSENT/LEAVE cannot have punch times supplied
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "cannot set punch times for ABSENT or LEAVE status")
        new_pi = None
        new_po = None
    else:
        new_pi = trunc_seconds(from_ms(body.punch_in)) if body.punch_in is not None else (
            _as_utc(record["punch_in"]) if record.get("punch_in") else None
        )
        new_po = trunc_seconds(from_ms(body.punch_out)) if body.punch_out is not None else (
            _as_utc(record["punch_out"]) if record.get("punch_out") else None
        )

        if new_pi is None:
            raise HTTPException(422, "presence status requires punch_in")

        # punch_in IST date must match attendance date (R1)
        pi_att_date = get_attendance_date(new_pi, emp["shift_start"], emp["shift_end"])
        if pi_att_date != date:
            raise HTTPException(422, "punch_in does not fall on the record's attendance date")

        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(422, "punch_out more than 24 hours after punch_in")

    # Recompute derived fields
    if new_status in ("PRESENT", "WFH", "ON_DUTY"):
        new_late = compute_late_minutes(new_pi, emp["shift_start"])
        if new_po is not None:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(new_po, emp["shift_end"], emp["shift_start"], date)
            new_hd = new_wh < 4.50
        else:
            new_wh = None
            new_ot = 0
            new_hd = False
    else:
        new_late = 0
        new_wh = None
        new_ot = 0
        new_hd = False

    new_vals = {
        "status": new_status,
        "punch_in": new_pi,
        "punch_out": new_po,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }

    # Build changes dict — only fields that actually changed
    changes = {}
    for field, new_val in new_vals.items():
        old_val = record.get(field)
        # normalize missing to defaults
        if field == "half_day":
            old_val = old_val if old_val is not None else False
        elif field in ("late_minutes", "overtime_minutes"):
            old_val = old_val if old_val is not None else 0
        elif field == "history":
            continue
        if field in ("punch_in", "punch_out") and isinstance(old_val, datetime):
            old_val = _as_utc(old_val)
        if old_val != new_val:
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": to_ms(old_val) if isinstance(old_val, datetime) else old_val,
                    "to": to_ms(new_val) if isinstance(new_val, datetime) else new_val,
                }
            else:
                changes[field] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # OCC: use history length as version token to detect concurrent edits
    history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, history_len]},
        },
        {"$set": new_vals, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "concurrent edit detected, please retry")

    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    month_start, month_end = month_bounds(month)
    joined = emp["joined_on"]
    from_date = max(date_type.fromisoformat(joined), date_type.fromisoformat(month_start))
    wd = working_days_in_range(from_date, date_type.fromisoformat(month_end))

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {"$addFields": {
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$addFields": {
            "is_weekday": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_present", "$is_weekday"]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    row = rows[0] if rows else {}

    present_days = row.get("present_days", 0)
    att_pct = round_half_up(present_days / wd * 100, 2) if wd > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present_days,
        "leave_days": row.get("leave_days", 0),
        "late_count": row.get("late_count", 0),
        "total_late_minutes": row.get("total_late_minutes", 0),
        "total_overtime_minutes": row.get("total_overtime_minutes", 0),
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)

    # Step 1: headcount per department (R9: joined_on <= month_end)
    emp_match = {"joined_on": {"$lte": month_end}}
    if department:
        emp_match["department"] = department

    headcount_pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
    ]
    headcount_map = {r["_id"]: r["headcount"] for r in db.employees.aggregate(headcount_pipeline)}

    if not headcount_map:
        return {"month": month, "items": []}

    # Step 2: attendance aggregation per department for the month
    log_pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.joined_on": {"$lte": month_end}}},
        *([{"$match": {"emp.department": department}}] if department else []),
        {"$addFields": {
            "dept": "$emp.department",
            "dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$addFields": {
            "is_weekday": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
            "is_present": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
        }},
        {"$group": {
            "_id": "$dept",
            "present_days": {"$sum": {"$cond": [
                {"$and": ["$is_present", "$is_weekday"]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0,
            ]}},
            "wh_values": {"$push": {"$cond": [
                {"$and": ["$is_present", {"$gt": [{"$ifNull": ["$work_hours", None]}, None]}]},
                "$work_hours", "$$REMOVE",
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]

    log_map = {r["_id"]: r for r in db.attendance_logs.aggregate(log_pipeline)}

    items = []
    for dept in sorted(headcount_map.keys()):
        hc = headcount_map[dept]
        if hc == 0:
            continue
        r = log_map.get(dept, {})
        wh_vals = r.get("wh_values", [])
        avg_wh = round_half_up(sum(wh_vals) / len(wh_vals), 2) if wh_vals else None
        items.append({
            "department": dept,
            "headcount": hc,
            "present_days": r.get("present_days", 0),
            "avg_work_hours": avg_wh,
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end = month_bounds(month)

    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})

    rows = list(db.attendance_logs.aggregate(pipeline))

    # Competition ranking
    result = []
    rank = 1
    prev = None
    for i, row in enumerate(rows):
        tlm = row["total_late_minutes"]
        if tlm != prev:
            rank = i + 1
        if rank > limit:
            break
        result.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["emp"]["name"],
            "department": row["emp"]["department"],
            "total_late_minutes": tlm,
            "late_count": row["late_count"],
        })
        prev = tlm

    return {"month": month, "items": result}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    if to < from_:
        raise HTTPException(422, "to must be >= from")
    from_date = date_type.fromisoformat(from_)
    to_date = date_type.fromisoformat(to)
    if (to_date - from_date).days > 91:
        raise HTTPException(422, "date range must not exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    # densify bounds: inclusive from, exclusive to+1 so to_date is included
    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=timezone.utc)
    to_dt_excl = datetime(to_date.year, to_date.month, to_date.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # Add a BSON date field so $densify can fill date gaps
        {"$addFields": {
            "dt": {"$dateFromString": {"dateString": "$_id", "timezone": "UTC"}},
        }},
        {"$densify": {
            "field": "dt",
            "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_excl]},
        }},
        # Filled-in rows have null _id/counts; default them and rebuild date string
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt", "timezone": "UTC"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$sort": {"dt": ASCENDING}},
    ]

    agg_rows = list(db.attendance_logs.aggregate(pipeline))

    # Headcount per day (R9: joined_on <= day) — fetch employees once, compute in Python
    dept_emps = list(db.employees.find(
        {"department": department},
        {"joined_on": 1, "_id": 0},
    ))

    items = []
    for i, row in enumerate(agg_rows):
        d_str = row["date_str"]
        is_working = date_type.fromisoformat(d_str).weekday() < 5
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= d_str)
        present_count = row["present_count"]

        att_rate = round_half_up(present_count / headcount, 4) if (is_working and headcount > 0) else None

        items.append({
            "date": d_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": row["late_count"],
            "attendance_rate": att_rate,
            "moving_avg_7d": None,
        })

    # 7-day moving average over non-null attendance_rate within the range
    for i, row in enumerate(items):
        window = [
            items[j]["attendance_rate"]
            for j in range(max(0, i - 6), i + 1)
            if items[j]["attendance_rate"] is not None
        ]
        row["moving_avg_7d"] = round_half_up(sum(window) / len(window), 4) if window else None

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #

def _build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = _build_attendance_query(emp_code, date_from, date_to, status)
        explain = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip((page - 1) * page_size)
            .limit(page_size)
            .explain("executionStats")
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not month:
            raise HTTPException(422, "month is required for employee_monthly")
        if not emp_code:
            raise HTTPException(422, "emp_code is required for employee_monthly")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        month_start, month_end = month_bounds(month)
        emp_match: dict = {"joined_on": {"$lte": month_end}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        ]
        explain = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        month_start, month_end = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            }},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not from_ or not to:
            raise HTTPException(422, "from and to are required for department_trend")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    else:
        raise HTTPException(422, f"unknown endpoint '{endpoint}'")

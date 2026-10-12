"""Employee Attendance & Analytics API.

Run with:
    uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import os
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = AsyncIOMotorClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_TYPES = {"PRESENT", "WFH", "ON_DUTY"}
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def truncate_to_seconds(dt: datetime) -> datetime:
    return ensure_utc(dt).replace(microsecond=0)


def dt_to_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(ensure_utc(value).timestamp() * 1000)


def ms_to_dt(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def round_half_up(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    quant = Decimal("0.01") if digits == 2 else Decimal("0.0001")
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def parse_time(value: str) -> time:
    h, m = map(int, value.split(":"))
    return time(hour=h, minute=m)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if len(value) != 7 or value[4] != "-":
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    try:
        year, month = int(value[:4]), int(value[5:])
        first_day = date(year, month, 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    if first_day.strftime("%Y-%m") != value:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    last_day = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return first_day, last_day


def format_date(value: date) -> str:
    return value.isoformat()


def attendance_date_for_punch(punched_dt: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_to_seconds(punched_dt).astimezone(IST)
    date_value = local.date()
    if shift_end <= shift_start and local.time() < parse_time(shift_end):
        date_value -= timedelta(days=1)
    return format_date(date_value)


def shift_start_dt_for_date(day: str, shift_start: str) -> datetime:
    return datetime.combine(parse_date(day), parse_time(shift_start), tzinfo=IST)


def shift_end_dt_for_date(day: str, shift_start: str, shift_end: str) -> datetime:
    d = parse_date(day)
    if shift_end <= shift_start:
        d += timedelta(days=1)
    return datetime.combine(d, parse_time(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    local = truncate_to_seconds(punch_in).astimezone(IST)
    start_dt = datetime.combine(local.date(), parse_time(shift_start), tzinfo=IST)
    if shift_end is not None and shift_end <= shift_start and local.time() < parse_time(shift_end):
        start_dt -= timedelta(days=1)
    elapsed_seconds = (local - start_dt).total_seconds()
    if elapsed_seconds <= 10 * 60:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    total_seconds = (truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds()
    hours = Decimal(str(total_seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    end_dt = shift_end_dt_for_date(attendance_day, shift_start, shift_end)
    diff_minutes = int((truncate_to_seconds(punch_out).astimezone(IST) - end_dt).total_seconds() // 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_half_day(work_hours: float | None) -> bool:
    if work_hours is None:
        return False
    return round_half_up(work_hours, 2) < 4.5


def serialize_history_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if value is None:
        return None
    return value


def serialize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in history or []:
        entry = {
            "at": dt_to_ms(item.get("at")),
            "by": item.get("by"),
            "reason": item.get("reason"),
            "changes": {},
        }
        for k, v in (item.get("changes") or {}).items():
            entry["changes"][k] = {
                "from": serialize_history_value(v.get("from")),
                "to": serialize_history_value(v.get("to")),
            }
        out.append(entry)
    return out


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_attendance_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history") or []),
    }


def employee_exists(emp_code: str) -> bool:
    return db.employees.count_documents({"emp_code": emp_code}, limit=1) > 0


class EpochMillisField:
    @staticmethod
    def validate(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer epoch millisecond value")
        if value < 100_000_000_000 or value > 4_102_444_800_000:
            raise ValueError("epoch milliseconds out of range")
        return value


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("joined_on must be YYYY-MM-DD")
        except ValueError as exc:
            raise ValueError("joined_on must be a real YYYY-MM-DD date") from exc
        return value

class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        return EpochMillisField.validate(value)

class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        return EpochMillisField.validate(value)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[AttendanceStatus] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        return EpochMillisField.validate(value)


async def ensure_indexes() -> None:
    employee_indexes = [
        ("emp_code", {"unique": True}, "uniq_emp_code"),
        ([ ("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], {}, "department_joined_emp"),
        ("joined_on", {}, "joined_on"),
    ]
    for spec, extra, name in employee_indexes:
        await db.employees.create_index(spec, name=name, **extra)

    attendance_indexes = [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}, "uniq_emp_date"),
        ([("emp_code", ASCENDING), ("date", DESCENDING)], {}, "emp_date_desc"),
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}, "emp_punch_in_desc"),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}, "date_desc_emp"),
        ([("status", ASCENDING), ("date", ASCENDING)], {}, "status_date"),
    ]
    for spec, extra, name in attendance_indexes:
        await db.attendance_logs.create_index(spec, name=name, **extra)


@app.on_event("startup")
async def startup_event() -> None:
    await ensure_indexes()


@app.get("/health")
async def health() -> dict[str, str]:
    try:
        await db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
async def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    existing = await db.employees.find_one({"emp_code": body.emp_code})
    if existing:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    payload = body.model_dump()
    payload["created_at"] = datetime.now(tz=UTC)
    try:
        await db.employees.insert_one(payload)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(payload)


@app.get("/employees")
async def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = await db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_employee(doc) async for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
async def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = await db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    now_ms = body.punched_at if body.punched_at is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
    punched_dt = ms_to_dt(now_ms)
    attendance_day = attendance_date_for_punch(punched_dt, employee["shift_start"], employee["shift_end"])

    existing = await db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_day})
    if existing:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    record = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punched_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_dt, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        await db.attendance_logs.insert_one(record)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance_doc(record)


@app.get("/attendance")
async def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if date_from is not None:
        parse_date(date_from)
    if date_to is not None:
        parse_date(date_to)
    if date_from and date_to and parse_date(date_from) > parse_date(date_to):
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if status is not None and status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
        raise HTTPException(status_code=422, detail="invalid status")

    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        date_filter: dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["date"] = date_filter

    total = await db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_attendance_doc(doc) async for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out", status_code=200)
async def punch_out(payload: PunchOutRequest) -> dict[str, Any]:
    employee = await db.employees.find_one({"emp_code": payload.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = ms_to_dt(payload.punched_at) if payload.punched_at is not None else ms_to_dt(int(datetime.now(tz=UTC).timestamp() * 1000))
    record = await db.attendance_logs.find_one(
        {"emp_code": payload.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no open punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_dt = record["punch_in"]
    if punched_at <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if punched_at - punch_in_dt > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punch_out is more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punched_at)
    overtime_minutes = compute_overtime_minutes(punched_at, employee["shift_start"], employee["shift_end"], record["date"])
    new_record = dict(record)
    new_record["punch_out"] = punched_at
    new_record["work_hours"] = work_hours
    new_record["overtime_minutes"] = overtime_minutes
    new_record["half_day"] = compute_half_day(work_hours)

    update_result = await db.attendance_logs.update_one({"_id": record["_id"], "punch_out": None}, {"$set": {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": new_record["half_day"],
    }})
    if update_result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record was already punched out")

    new_record["punch_out"] = punched_at
    new_record["work_hours"] = work_hours
    new_record["overtime_minutes"] = overtime_minutes
    new_record["half_day"] = new_record["half_day"]
    return serialize_attendance_doc(new_record)


@app.patch("/attendance/{emp_code}/{date}")
async def regularize_attendance(emp_code: str, date: str, payload: RegularizeRequest) -> dict[str, Any]:
    parse_date(date)
    record = await db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        employee = await db.employees.find_one({"emp_code": emp_code})
        if not employee:
            raise HTTPException(status_code=404, detail="employee not found")
        raise HTTPException(status_code=404, detail="attendance record not found")

    employee = await db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    current = dict(record)
    updated = dict(record)
    changes: dict[str, dict[str, Any]] = {}

    if payload.status is not None:
        if payload.status in {"ABSENT", "LEAVE"}:
            if payload.punch_in is not None or payload.punch_out is not None:
                raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot have punch times")
            new_status = payload.status
            if new_status != current["status"]:
                changes["status"] = {"from": current.get("status"), "to": new_status}
                updated["status"] = new_status
            updated["punch_in"] = None
            updated["punch_out"] = None
            if current.get("punch_in") is not None:
                changes["punch_in"] = {"from": current.get("punch_in"), "to": None}
            if current.get("punch_out") is not None:
                changes["punch_out"] = {"from": current.get("punch_out"), "to": None}
        else:
            if payload.status not in PRESENT_TYPES:
                raise HTTPException(status_code=422, detail="invalid status for presence record")
            if payload.punch_in is None and current.get("punch_in") is None:
                raise HTTPException(status_code=422, detail="presence status requires punch_in")
            if payload.status != current["status"]:
                changes["status"] = {"from": current.get("status"), "to": payload.status}
                updated["status"] = payload.status

    if payload.punch_in is not None:
        new_punch_in = ms_to_dt(payload.punch_in)
        if attendance_date_for_punch(new_punch_in, employee["shift_start"], employee["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in must be on the attendance date")
        if payload.punch_out is not None and payload.punch_out <= payload.punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if current.get("punch_out") is not None and new_punch_in >= current["punch_out"]:
            raise HTTPException(status_code=422, detail="punch_in must be before punch_out")
        if current.get("punch_in") is None or new_punch_in != current["punch_in"]:
            changes["punch_in"] = {"from": current.get("punch_in"), "to": new_punch_in}
            updated["punch_in"] = new_punch_in

    if payload.punch_out is not None:
        new_punch_out = ms_to_dt(payload.punch_out)
        punch_in_value = updated.get("punch_in") or current.get("punch_in")
        if punch_in_value is None:
            raise HTTPException(status_code=422, detail="punch_out requires an existing punch_in")
        if new_punch_out <= punch_in_value:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if new_punch_out - punch_in_value > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        if current.get("punch_out") is None or new_punch_out != current["punch_out"]:
            changes["punch_out"] = {"from": current.get("punch_out"), "to": new_punch_out}
            updated["punch_out"] = new_punch_out

    final_status = updated.get("status", current.get("status"))
    if final_status in {"ABSENT", "LEAVE"} and (payload.punch_in is not None or payload.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT/LEAVE records cannot have punch times")

    if payload.status is not None and payload.status in {"ABSENT", "LEAVE"}:
        updated["late_minutes"] = 0
        updated["overtime_minutes"] = 0
        updated["work_hours"] = None
        updated["half_day"] = False
        if current.get("late_minutes", 0) != 0:
            changes["late_minutes"] = {"from": current.get("late_minutes"), "to": 0}
        if current.get("overtime_minutes", 0) != 0:
            changes["overtime_minutes"] = {"from": current.get("overtime_minutes"), "to": 0}
        if current.get("work_hours") is not None:
            changes["work_hours"] = {"from": current.get("work_hours"), "to": None}
        if current.get("half_day", False) is not False:
            changes["half_day"] = {"from": current.get("half_day"), "to": False}
    else:
        final_punch_in = updated.get("punch_in") or current.get("punch_in")
        final_punch_out = updated.get("punch_out") or current.get("punch_out")
        final_status = updated.get("status") or current.get("status")
        if final_punch_in is None and final_status not in {"ABSENT", "LEAVE"}:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if final_punch_in is not None and final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if final_punch_out - final_punch_in > timedelta(hours=24):
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
        if final_punch_in is not None:
            updated["late_minutes"] = compute_late_minutes(final_punch_in, employee["shift_start"], employee["shift_end"])
            if current.get("late_minutes", 0) != updated["late_minutes"]:
                changes["late_minutes"] = {"from": current.get("late_minutes"), "to": updated["late_minutes"]}
        else:
            updated["late_minutes"] = 0
        if final_punch_in is not None and final_punch_out is not None:
            work_hours = compute_work_hours(final_punch_in, final_punch_out)
            updated["work_hours"] = work_hours
            updated["overtime_minutes"] = compute_overtime_minutes(final_punch_out, employee["shift_start"], employee["shift_end"], date)
            updated["half_day"] = compute_half_day(work_hours)
            if current.get("work_hours") != work_hours:
                changes["work_hours"] = {"from": current.get("work_hours"), "to": work_hours}
            if current.get("overtime_minutes", 0) != updated["overtime_minutes"]:
                changes["overtime_minutes"] = {"from": current.get("overtime_minutes"), "to": updated["overtime_minutes"]}
            if current.get("half_day", False) != updated["half_day"]:
                changes["half_day"] = {"from": current.get("half_day"), "to": updated["half_day"]}
        else:
            updated["work_hours"] = None
            updated["overtime_minutes"] = 0
            updated["half_day"] = False
            if current.get("work_hours") is not None:
                changes["work_hours"] = {"from": current.get("work_hours"), "to": None}
            if current.get("overtime_minutes", 0) != 0:
                changes["overtime_minutes"] = {"from": current.get("overtime_minutes"), "to": 0}
            if current.get("half_day", False) is not False:
                changes["half_day"] = {"from": current.get("half_day"), "to": False}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_entry = {
        "at": datetime.now(tz=UTC),
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": changes,
    }
    updated["history"] = (current.get("history") or []) + [history_entry]
    if "_id" in updated:
        del updated["_id"]

    guard: dict[str, Any] = {"_id": record["_id"]}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"):
        guard[key] = current[key] if key in current else {"$exists": False}
    update_result = await db.attendance_logs.update_one(guard, {"$set": {
        "status": updated["status"],
        "punch_in": updated.get("punch_in"),
        "punch_out": updated.get("punch_out"),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
    }, "$push": {"history": history_entry}})
    if update_result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")

    result = dict(updated)
    result["history"] = [{
        "at": entry["at"],
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {key: {"from": serialize_history_value(val["from"]), "to": serialize_history_value(val["to"])} for key, val in entry["changes"].items()},
    } for entry in result["history"]]
    return serialize_attendance_doc(result)


def monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict[str, Any]]:
    present_weekday = {
        "$and": [
            {"$in": ["$status", sorted(PRESENT_TYPES)]},
            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
        ]
    }
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [present_weekday, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start: date, end: date, department: str | None = None) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    present_weekday = {
        "$and": [
            {"$in": ["$status", sorted(PRESENT_TYPES)]},
            {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
        ]
    }
    return [
        {"$match": employee_match},
        {"$group": {"_id": "$department", "emp_codes": {"$push": "$emp_code"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"codes": "$emp_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$gte": ["$date", start.isoformat()]},
                    {"$lte": ["$date", end.isoformat()]},
                    {"$in": ["$emp_code", "$$codes"]},
                ]}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": {"$cond": [present_weekday, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "avg_work_hours": {"$avg": {"$cond": [{"$and": [
                        {"$in": ["$status", sorted(PRESENT_TYPES)]},
                        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                    ]}, "$work_hours", None]}},
                }},
            ],
            "as": "metrics",
        }},
        {"$unwind": {"path": "$metrics", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$emp_codes"},
            "present_days": {"$ifNull": ["$metrics.present_days", 0]},
            "avg_work_hours": "$metrics.avg_work_hours",
            "late_count": {"$ifNull": ["$metrics.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$metrics.total_late_minutes", 0]},
            "leave_count": {"$ifNull": ["$metrics.leave_count", 0]},
            "on_duty_count": {"$ifNull": ["$metrics.on_duty_count", 0]},
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: date, end: date, department: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "emp_code": {"$first": "$emp_code"},
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_day: str, day_count: int) -> list[dict[str, Any]]:
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department", "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"days": {"$map": {
            "input": {"$range": [0, day_count]},
            "as": "offset",
            "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {
                "startDate": {"$dateFromString": {"dateString": from_day}},
                "unit": "day",
                "amount": "$$offset",
            }}}},
        }}}},
        {"$unwind": "$days"},
        {"$set": {
            "day_date": {"$dateFromString": {"dateString": "$days"}},
            "eligible": {"$filter": {"input": "$employees", "as": "employee", "cond": {"$lte": ["$$employee.joined_on", "$days"]}}},
        }},
        {"$set": {
            "headcount": {"$size": "$eligible"},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day_date"}, 5]},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$days", "codes": {"$map": {"input": "$eligible", "as": "employee", "in": "$$employee.emp_code"}}},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", sorted(PRESENT_TYPES)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }},
            ],
            "as": "daily",
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$let": {
                "vars": {"rate": {"$divide": ["$present_count", "$headcount"]}},
                "in": {"$divide": [{"$floor": {"$add": [{"$multiply": ["$$rate", 10000]}, 0.5]}}, 10000]},
            }},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"day_date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": "$days",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
async def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = await db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")

    start, end = parse_month(month)
    joined = parse_date(employee["joined_on"])
    working_days = 0
    current = start
    while current <= end:
        if current >= joined and current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)

    pipeline = monthly_pipeline(emp_code, start, end)
    agg = await db.attendance_logs.aggregate(pipeline).to_list(length=1)
    result = agg[0] if agg else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = float(result.get("present_days", 0) or 0)
    attendance_pct = None if working_days == 0 else round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(result.get("leave_days", 0) or 0),
        "late_count": int(result.get("late_count", 0) or 0),
        "total_late_minutes": int(result.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(result.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
async def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None) -> dict[str, Any]:
    start, end = parse_month(month)
    pipeline = department_summary_pipeline(start, end, department)
    rows = await db.employees.aggregate(pipeline).to_list(length=None)
    items = []
    for row in rows:
        average = row.get("avg_work_hours")
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(float(row.get("present_days", 0) or 0), 2),
            "avg_work_hours": round_half_up(float(average), 2) if average is not None else None,
            "late_count": int(row.get("late_count", 0) or 0),
            "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
            "leave_count": int(row.get("leave_count", 0) or 0),
            "on_duty_count": int(row.get("on_duty_count", 0) or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
async def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None) -> dict[str, Any]:
    start, end = parse_month(month)
    pipeline = leaderboard_pipeline(start, end, department, limit)
    rows = await db.attendance_logs.aggregate(pipeline).to_list(length=None)
    return {"month": month, "items": [{
        "rank": int(row["rank"]),
        "emp_code": row["emp_code"],
        "name": row["name"],
        "department": row["department"],
        "total_late_minutes": int(row["total_late_minutes"]),
        "late_count": int(row["late_count"]),
    } for row in rows]}


@app.get("/analytics/departments/{department}/trend")
async def department_trend(department: str, from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"), to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")) -> dict[str, Any]:
    from_day = parse_date(from_date)
    to_day = parse_date(to_date)
    if from_day > to_day:
        raise HTTPException(status_code=422, detail="from must be <= to")
    day_count = (to_day - from_day).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="date range must be at most 92 days")
    pipeline = department_trend_pipeline(department, from_day.isoformat(), day_count)
    rows = await db.employees.aggregate(pipeline).to_list(length=None)
    if not rows:
        raise HTTPException(status_code=404, detail="department not found")
    for row in rows:
        row["present_count"] = round_half_up(float(row["present_count"]), 2)
        row["attendance_rate"] = round_half_up(float(row["attendance_rate"]), 4) if row["attendance_rate"] is not None else None
        row["moving_avg_7d"] = round_half_up(float(row["moving_avg_7d"]), 4) if row["moving_avg_7d"] is not None else None
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
async def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[AttendanceStatus] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    valid_endpoints = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail="invalid endpoint")

    if endpoint == "attendance_list":
        if date_from is not None:
            parse_date(date_from)
        if date_to is not None:
            parse_date(date_to)
        if date_from and date_to and parse_date(date_from) > parse_date(date_to):
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        if status is not None and status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
            raise HTTPException(status_code=422, detail="invalid status")
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            query["status"] = status
        if date_from or date_to:
            d: dict[str, str] = {}
            if date_from:
                d["$gte"] = date_from
            if date_to:
                d["$lte"] = date_to
            query["date"] = d
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        plan = await db.command("explain", command, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start, end = parse_month(month)
        pipeline = monthly_pipeline(emp_code, start, end)
        plan = await db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end = parse_month(month)
        pipeline = department_summary_pipeline(start, end, department)
        plan = await db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(plan))}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start, end = parse_month(month)
        pipeline = leaderboard_pipeline(start, end, department, limit)
        plan = await db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json.loads(json_util.dumps(plan))}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        from_day, to_day = parse_date(from_date), parse_date(to_date)
        day_count = (to_day - from_day).days + 1
        if day_count < 1 or day_count > 92:
            raise HTTPException(status_code=422, detail="date range must be between 1 and 92 days")
        pipeline = department_trend_pipeline(department, from_day.isoformat(), day_count)
        plan = await db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": json.loads(json_util.dumps(plan))}

    raise HTTPException(status_code=422, detail="invalid endpoint")


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}

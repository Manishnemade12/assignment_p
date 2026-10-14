"""Employee attendance and analytics API.

The API keeps calendar dates as strings and instants as BSON datetimes.  The
conversion at the HTTP boundary is deliberately centralised so every endpoint
uses the same IST and epoch-millisecond rules.
"""
import math
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from bson import json_util
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
attendance = db.attendance_logs
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("must be a valid YYYY-MM-DD date") from exc


def parse_month(value: str) -> tuple[date, date]:
    if len(value) != 7 or value[4] != "-":
        raise ValueError("month must be YYYY-MM")
    try:
        first = date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    next_month = date(first.year + (first.month == 12), 1 if first.month == 12 else first.month + 1, 1)
    return first, next_month - timedelta(days=1)


def require_date(value: str) -> date:
    try:
        return parse_date(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def require_month(value: str) -> tuple[date, date]:
    try:
        return parse_month(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def validate_epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be epoch milliseconds in the supported range")
    return value


def ms_to_dt(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp(validate_epoch(value) / 1000, UTC).replace(microsecond=0)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def shift_instant(day: date, shift: str) -> datetime:
    hour, minute = map(int, shift.split(":"))
    return datetime.combine(day, time(hour, minute), IST)


def attendance_date(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if shift_minutes(employee["shift_end"]) <= shift_minutes(employee["shift_start"]):
        if local.hour * 60 + local.minute < shift_minutes(employee["shift_end"]):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_end_for_record(record_date: str, employee: dict[str, Any]) -> datetime:
    day = parse_date(record_date)
    if shift_minutes(employee["shift_end"]) <= shift_minutes(employee["shift_start"]):
        day += timedelta(days=1)
    return shift_instant(day, employee["shift_end"])


def compute_late_minutes(punch_in: datetime, employee: dict[str, Any], record_date: str) -> int:
    start = shift_instant(parse_date(record_date), employee["shift_start"])
    elapsed = math.floor((punch_in - start).total_seconds() / 60)
    return elapsed if elapsed > 10 else 0


def compute_derived(punch_in: Optional[datetime], punch_out: Optional[datetime],
                    status: str, employee: dict[str, Any], record_date: str) -> dict[str, Any]:
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee, record_date)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = round_half_up((punch_out - punch_in).total_seconds() / 3600)
    overtime_elapsed = math.floor((punch_out - shift_end_for_record(record_date, employee)).total_seconds() / 60)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime_elapsed if overtime_elapsed >= 30 else 0,
        "half_day": hours < 4.50,
    }


def serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [serialize(v) for v in value]
    return value


def serialize_explain(value: Any) -> Any:
    return json.loads(json_util.dumps(value))


def output_record(doc: dict[str, Any]) -> dict[str, Any]:
    result = serialize(doc)
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    result.setdefault("history", [])
    return result


def page_args(page: int, page_size: int) -> tuple[int, int]:
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be 1..100")
    return (page - 1) * page_size, page_size


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shifts(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class EpochModel(StrictModel):
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else validate_epoch(value)


class PunchInIn(EpochModel):
    emp_code: str
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(EpochModel):
    emp_code: str


class RegularizeIn(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid attendance status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else validate_epoch(value)


@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="attendance_filter")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1), page_size: int = Query(20)) -> dict[str, Any]:
    skip, size = page_args(page, page_size)
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(size)
    return {"items": [serialize(d) for d in docs], "total": total, "page": page, "page_size": size}


def get_employee(emp_code: str) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    return employee


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = get_employee(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    record_date = attendance_date(punched_at, employee)
    doc = {
        "emp_code": body.emp_code, "date": record_date, "status": body.status,
        "punch_in": punched_at, "punch_out": None, "history": [],
        **compute_derived(punched_at, None, body.status, employee, record_date),
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return output_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = get_employee(body.emp_code)
    punched_at = ms_to_dt(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    current = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
                                  sort=[("punch_in", DESCENDING)])
    if current is None:
        if attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}}):
            raise HTTPException(409, "attendance record is already punched out")
        raise HTTPException(404, "no punch-in found")
    if punched_at <= current["punch_in"] or punched_at - current["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    derived = compute_derived(current["punch_in"], punched_at, current["status"], employee, current["date"])
    updated = attendance.find_one_and_update(
        {"_id": current["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return output_record(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, page: int = Query(1), page_size: int = Query(20)) -> dict[str, Any]:
    skip, size = page_args(page, page_size)
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        require_date(date_from)
        query.setdefault("date", {})["$gte"] = date_from
    if date_to is not None:
        require_date(date_to)
        query.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    total = attendance.count_documents(query)
    docs = attendance.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(size)
    return {"items": [output_record(d) for d in docs], "total": total, "page": page, "page_size": size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn) -> dict[str, Any]:
    require_date(date)
    employee = get_employee(emp_code)
    current = attendance.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    old_status = current.get("status")
    new_status = body.status if body.status is not None else old_status
    if new_status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else current.get("punch_in")
    new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else current.get("punch_out")
    if new_status in PRESENCE and new_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if new_in is not None and attendance_date(new_in, employee) != date:
        raise HTTPException(422, "punch_in must belong to the record attendance date")
    if new_out is not None:
        if new_in is None or new_out <= new_in or new_out - new_in > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = compute_derived(new_in, new_out, new_status, employee, date)
    final = {"status": new_status, "punch_in": new_in if new_status in PRESENCE else None,
             "punch_out": new_out if new_status in PRESENCE else None, **derived}
    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in tracked:
        before = current.get(field)
        after = final.get(field)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "correction does not change anything")
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    updated = attendance.find_one_and_update(
        {"_id": current["_id"], "history": current.get("history", [])},
        {"$set": final, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was changed concurrently")
    return output_record(updated)


def month_match(month: str) -> dict[str, Any]:
    first, last = require_month(month)
    return {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}


def working_days(start: date, end: date) -> int:
    return sum((start + timedelta(days=i)).weekday() < 5 for i in range((end - start).days + 1))


def aggregate_logs(match: dict[str, Any]) -> list[dict[str, Any]]:
    return list(attendance.aggregate([{"$match": match}]))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    employee = get_employee(emp_code)
    first, last = require_month(month)
    start = max(first, parse_date(employee["joined_on"]))
    days = working_days(start, last) if start <= last else 0
    logs = aggregate_logs({"emp_code": emp_code, **month_match(month)})
    present = sum((0.5 if d.get("half_day") else 1) for d in logs
                  if d.get("status") in PRESENCE and parse_date(d["date"]).weekday() < 5)
    late_count = sum(d.get("late_minutes", 0) > 0 for d in logs)
    result = {
        "emp_code": emp_code, "month": month, "working_days": days, "present_days": present,
        "leave_days": sum(d.get("status") == "LEAVE" for d in logs),
        "late_count": late_count, "total_late_minutes": sum(d.get("late_minutes", 0) for d in logs),
        "total_overtime_minutes": sum(d.get("overtime_minutes", 0) for d in logs),
        "attendance_pct": round_half_up(present / days * 100) if days else None,
    }
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict[str, Any]:
    first, last = parse_month(month)
    employee_query: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_query["department"] = department
    people = list(employees.find(employee_query, {"_id": 0, "emp_code": 1, "department": 1}))
    by_dept: dict[str, list[str]] = {}
    for person in people:
        by_dept.setdefault(person["department"], []).append(person["emp_code"])
    logs = aggregate_logs({**month_match(month), "emp_code": {"$in": [p["emp_code"] for p in people]}})
    items = []
    for dept, codes in sorted(by_dept.items()):
        rows = [d for d in logs if d["emp_code"] in codes]
        present_rows = [d for d in rows if d.get("status") in PRESENCE]
        hours = [d["work_hours"] for d in present_rows if d.get("work_hours") is not None]
        items.append({"department": dept, "headcount": len(codes),
                      "present_days": sum((0.5 if d.get("half_day") else 1)
                                          for d in present_rows if parse_date(d["date"]).weekday() < 5),
                      "avg_work_hours": round_half_up(sum(hours) / len(hours)) if hours else None,
                      "late_count": sum(d.get("late_minutes", 0) > 0 for d in rows),
                      "total_late_minutes": sum(d.get("late_minutes", 0) for d in rows),
                      "leave_count": sum(d.get("status") == "LEAVE" for d in rows),
                      "on_duty_count": sum(d.get("status") == "ON_DUTY" for d in rows)})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None) -> dict[str, Any]:
    require_month(month)
    employee_query = {"department": department} if department is not None else {}
    people = {p["emp_code"]: p for p in employees.find(employee_query, {"_id": 0})}
    logs = aggregate_logs({"date": month_match(month)["date"], "emp_code": {"$in": list(people)}})
    totals: dict[str, dict[str, int]] = {}
    for log in logs:
        if log["emp_code"] in people:
            row = totals.setdefault(log["emp_code"], {"total": 0, "count": 0})
            row["total"] += log.get("late_minutes", 0)
            row["count"] += log.get("late_minutes", 0) > 0
    ordered = sorted(((code, v) for code, v in totals.items() if v["total"] > 0),
                     key=lambda x: (-x[1]["total"], x[0]))
    items = []
    previous = None
    rank = 0
    for index, (code, values) in enumerate(ordered, 1):
        if values["total"] != previous:
            rank = index
            previous = values["total"]
        if rank > limit:
            break
        person = people[code]
        items.append({"rank": rank, "emp_code": code, "name": person["name"], "department": person["department"],
                      "total_late_minutes": values["total"], "late_count": values["count"]})
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")) -> dict[str, Any]:
    start, end = require_date(from_date), require_date(to_date)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "invalid trend range")
    people = list(employees.find({"department": department, "joined_on": {"$lte": end.isoformat()}},
                                 {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not people:
        raise HTTPException(404, "department not found")
    codes = {p["emp_code"] for p in people}
    logs = aggregate_logs({"emp_code": {"$in": list(codes)}, "date": {"$gte": from_date, "$lte": to_date}})
    grouped: dict[str, list[dict[str, Any]]] = {}
    for log in logs:
        grouped.setdefault(log["date"], []).append(log)
    items = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        rows = grouped.get(key, [])
        headcount = sum(parse_date(p["joined_on"]) <= day for p in people)
        present = sum(0.5 if d.get("half_day") else 1 for d in rows if d.get("status") in PRESENCE)
        rate = round_half_up(present / headcount, 4) if day.weekday() < 5 and headcount else None
        items.append({"date": key, "is_working_day": day.weekday() < 5, "headcount": headcount,
                      "present_count": present, "late_count": sum(d.get("late_minutes", 0) > 0 for d in rows),
                      "attendance_rate": rate, "moving_avg_7d": None})
    for i, item in enumerate(items):
        values = [x["attendance_rate"] for x in items[max(0, i - 6):i + 1] if x["attendance_rate"] is not None]
        item["moving_avg_7d"] = round_half_up(sum(values) / len(values), 4) if values else None
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, from_date: Optional[str] = Query(None, alias="from"),
                     to_date: Optional[str] = Query(None, alias="to"), page: int = Query(1),
                     page_size: int = Query(20)) -> dict[str, Any]:
    if endpoint == "attendance_list":
        skip, size = page_args(page, page_size)
        query: dict[str, Any] = {}
        if emp_code: query["emp_code"] = emp_code
        if status: query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from: query["date"]["$gte"] = date_from
            if date_to: query["date"]["$lte"] = date_to
        explanation = db.command(
            "explain",
            {"find": "attendance_logs", "filter": query,
             "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": size},
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
        require_month(month)
        explanation = db.command("aggregate", "attendance_logs",
                                 pipeline=[{"$match": {"emp_code": emp_code, **month_match(month)}}],
                                 explain=True)
        collection = "attendance_logs"
    elif endpoint in {"department_summary", "late_leaderboard"}:
        if not month: raise HTTPException(422, "month is required")
        require_month(month)
        explanation = db.command("aggregate", "attendance_logs",
                                 pipeline=[{"$match": month_match(month)}], explain=True)
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date: raise HTTPException(422, "department, from and to are required")
        require_date(from_date); require_date(to_date)
        explanation = attendance.find({"date": {"$gte": from_date, "$lte": to_date}}).explain("executionStats")
        collection = "attendance_logs"
    else:
        raise HTTPException(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": collection, "explain": serialize_explain(explanation)}

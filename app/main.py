"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient
from fastapi import Query
from decimal import Decimal, ROUND_HALF_UP


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))

db = client[os.getenv("MONGO_DB", "attendance_db")]

# Query-performance indexes.
db.employees.create_index("emp_code")
db.employees.create_index("department")
db.attendance_logs.create_index([
    ("emp_code", 1),
    ("date", -1),
])
db.attendance_logs.create_index([
    ("date", -1),
    ("emp_code", 1),
])
db.attendance_logs.create_index([
    ("emp_code", 1),
    ("punch_in", -1),
])

IST = timezone(timedelta(hours=5, minutes=30))


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    elapsed_seconds = (punch_in - start).total_seconds()

    # Late tabhi jab shift start ke 10 minutes se zyada ho.
    if elapsed_seconds <= 600:
        return 0

    # Actual delay in whole minutes, measured from shift start.
    return int(elapsed_seconds // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime
) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )
    return float(hours)



def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: str
) -> int:
    h, m = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))

    shift_end_dt = datetime.fromisoformat(date_str).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    # Overnight shift ends on the following calendar day.
    if (h, m) <= (sh, sm):
        shift_end_dt += timedelta(days=1)

    # Do not award overtime if the employee punched out
    # before the scheduled shift end.
    elapsed_seconds = (punch_out - shift_end_dt).total_seconds()

    if elapsed_seconds < 1800:
        return 0

    return int(elapsed_seconds // 60)

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"



def epoch_ms_to_utc_naive(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in milliseconds")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "Timestamp is outside the allowed range")
    return datetime.fromtimestamp(
        value // 1000, tz=timezone.utc
    ).replace(tzinfo=None)


def to_ist(value: datetime) -> datetime:
    # MongoDB datetimes are stored as naive UTC.
    return value.replace(tzinfo=timezone.utc).astimezone(IST)


def to_epoch_ms(value: Optional[datetime]):
    if value is None:
        return None
    return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(422, "Invalid punch-in status")

    if body.punched_at is None:
        ts_ist = datetime.now(IST).replace(microsecond=0)
    else:
        ts_ist = to_ist(epoch_ms_to_utc_naive(body.punched_at))

    ts_ist = ts_ist.replace(microsecond=0)
    attendance_date = ts_ist.date().isoformat()

    # Store timestamps in MongoDB as naive UTC.
    ts_db = ts_ist.astimezone(timezone.utc).replace(tzinfo=None)

    h, m = map(int, emp["shift_start"].split(":"))
    start_today = ts_ist.replace(
        hour=h, minute=m, second=0, microsecond=0
    )

    # For an overnight shift, early-morning punches belong to
    # the previous day's shift.
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    if (eh, em) <= (sh, sm) and (ts_ist.hour, ts_ist.minute) < (eh, em):
        attendance_date = (ts_ist.date() - timedelta(days=1)).isoformat()

    late_minutes = compute_late_minutes(ts_ist, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts_db,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

   
    # Reject a second attendance record for the same employee/date.
    existing = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": attendance_date,
    })
    if existing:
        raise HTTPException(409, "Already punched in for this date")

    try:
        result = db.attendance_logs.insert_one(doc)
    except Exception as exc:
        # Handles duplicate-key errors once the unique index exists.
        if db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "date": attendance_date,
        }):
            raise HTTPException(409, "Already punched in for this date")
        raise HTTPException(500, "Could not save attendance") from exc

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is None:
        out_ist = datetime.now(IST).replace(microsecond=0)
    else:
        out_ist = to_ist(epoch_ms_to_utc_naive(body.punched_at))

    out_ist = out_ist.replace(microsecond=0)
    out_db = out_ist.astimezone(timezone.utc).replace(tzinfo=None)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": out_db},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    in_db = record["punch_in"]
    in_ist = to_ist(in_db)

    if out_db <= in_db:
        raise HTTPException(422, "Punch-out must be after punch-in")

    if out_db > in_db + timedelta(hours=24):
        raise HTTPException(422, "Punch-out cannot be more than 24 hours later")

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    work_hours = compute_work_hours(in_db, out_db)

    # Determine shift-end date for overnight shifts.
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    shift_end_date = datetime.fromisoformat(record["date"])
    if (eh, em) <= (sh, sm):
        shift_end_date += timedelta(days=1)

    overtime_minutes = compute_overtime(
        out_ist,
        shift_end,
        shift_end_date.date().isoformat(),
        shift_start,
    )

    half_day = work_hours < 4.50

    db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": out_db,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }},
    )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated.get("punch_out") is None:
        raise HTTPException(409, "Attendance was updated concurrently")

    return {
        "id": str(updated["_id"]),
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": to_epoch_ms(updated["punch_in"]),
        "punch_out": to_epoch_ms(updated["punch_out"]),
        "work_hours": updated["work_hours"],
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated["overtime_minutes"],
        "half_day": updated["half_day"],
        "history": updated.get("history", []),
    }

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc





@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    for item in items:
        created_at = item.get("created_at")
        if created_at is not None:
            item["created_at"] = int(
                created_at.replace(tzinfo=timezone.utc).timestamp() * 1000
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



# @app.post("/attendance/punch-in", status_code=201)
# def punch_in(body: PunchInIn):
#     emp = db.employees.find_one({"emp_code": body.emp_code})
#     ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
#     d = ts.date().isoformat()
#     if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
#         raise HTTPException(409, "already punched in for this date")
#     doc = {
#         "emp_code": body.emp_code,
#         "date": d,
#         "status": body.status,
#         "punch_in": ts,
#         "punch_out": None,
#         "work_hours": None,
#         "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
#         "overtime_minutes": 0,
#         "half_day": False,
#         "history": [],
#     }
#     res = db.attendance_logs.insert_one(doc)
#     doc["id"] = str(res.inserted_id)
#     doc.pop("_id", None)
#     return doc


# @app.get("/attendance")
# def list_attendance(
#     emp_code: Optional[str] = None,
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     page: int = 1,
#     page_size: int = 20,
# ):
#     q = {}
#     if emp_code:
#         q["emp_code"] = emp_code
#     if date_from or date_to:
#         q["date"] = {}
#         if date_from:
#             q["date"]["$gte"] = date_from
#         if date_to:
#             q["date"]["$lte"] = date_to
#     if status:
#         q["status"] = status
#     docs = list(db.attendance_logs.find(q))
#     docs.sort(key=lambda d: d["date"], reverse=True)
#     total = len(docs)
#     page_docs = docs[(page - 1) * page_size : page * page_size]
#     for d in page_docs:
#         d["id"] = str(d.pop("_id"))
#     return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Validate date format: YYYY-MM-DD
    for field_name, value in (
        ("date_from", date_from),
        ("date_to", date_to),
    ):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} must use YYYY-MM-DD format",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    # Build MongoDB filters
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        query["status"] = status

    if date_from or date_to:
        date_filter = {}

        if date_from:
            date_filter["$gte"] = date_from

        if date_to:
            date_filter["$lte"] = date_to

        query["date"] = date_filter

    # Count and paginate inside MongoDB, not in Python
    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = []

    for doc in cursor:
        item = {
            "id": str(doc["_id"]),
            "emp_code": doc["emp_code"],
            "date": doc["date"],
            "status": doc["status"],
            "punch_in": to_epoch_ms(doc.get("punch_in")),
            "punch_out": to_epoch_ms(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes", 0),
            "overtime_minutes": doc.get("overtime_minutes", 0),
            "half_day": doc.get("half_day", False),
            "history": doc.get("history", []),
        }
        items.append(item)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

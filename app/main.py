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



def to_epoch_ms(value):
    if value is None:
        return None

    # Already an epoch-milliseconds integer
    if isinstance(value, int):
        return value

    # Handle BSON/Pandas integer types such as Int64
    if not isinstance(value, datetime):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise TypeError(f"Unsupported timestamp type: {type(value).__name__}")

    # MongoDB stores UTC-naive datetimes
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)

    return int(value.timestamp() * 1000)



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

class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # Validate date and correction metadata.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(422, "reason must contain 5-200 characters")
    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(422, "regularized_by must contain 1-50 characters")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if not record:
        raise HTTPException(404, "Attendance record not found")

    old_status = record.get("status", "PRESENT")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_status = body.status if body.status is not None else old_status

    allowed = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if new_status not in allowed:
        raise HTTPException(422, "Invalid status")

    # ABSENT/LEAVE cannot include supplied punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        in_value = body.punch_in if body.punch_in is not None else (
            to_epoch_ms(old_in) if old_in else None
        )
        out_value = body.punch_out if body.punch_out is not None else (
            to_epoch_ms(old_out) if old_out else None
        )
        if in_value is None:
            raise HTTPException(422, "Presence status requires punch_in")

        new_in = epoch_ms_to_utc_naive(in_value)
        new_out = epoch_ms_to_utc_naive(out_value) if out_value is not None else None

        # Punch-in must belong to this attendance day, including overnight shifts.
        in_ist = to_ist(new_in).replace(microsecond=0)
        sh, sm = map(int, emp["shift_start"].split(":"))
        eh, em = map(int, emp["shift_end"].split(":"))
        expected_date = in_ist.date()
        if (eh, em) <= (sh, sm) and (in_ist.hour, in_ist.minute) < (eh, em):
            expected_date -= timedelta(days=1)
        if expected_date.isoformat() != date:
            raise HTTPException(422, "punch_in must belong to attendance date")

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out > new_in + timedelta(hours=24):
                raise HTTPException(422, "punch_out must be within 24 hours")

    # Recompute every derived field from the final values.
    if new_status in {"ABSENT", "LEAVE"}:
        late = overtime = 0
        hours = None
        half_day = False
    else:
        late = compute_late_minutes(to_ist(new_in), emp["shift_start"])
        if new_out is None:
            hours = None
            overtime = 0
            half_day = False
        else:
            hours = compute_work_hours(new_in, new_out)
            sh, sm = map(int, emp["shift_start"].split(":"))
            eh, em = map(int, emp["shift_end"].split(":"))
            end_date = datetime.fromisoformat(date)
            if (eh, em) <= (sh, sm):
                end_date += timedelta(days=1)
            overtime = compute_overtime(
                to_ist(new_out), emp["shift_end"],
                end_date.date().isoformat(), emp["shift_start"]
            )
            half_day = hours < 4.50

    changes = {}

    def track(field, old, new, timestamps=False):
        if old != new:
            changes[field] = {
                "from": to_epoch_ms(old) if timestamps and old else old,
                "to": to_epoch_ms(new) if timestamps and new else new,
            }

    track("status", old_status, new_status)
    track("punch_in", old_in, new_in, True)
    track("punch_out", old_out, new_out, True)
    track("late_minutes", record.get("late_minutes", 0), late)
    track("work_hours", record.get("work_hours"), hours)
    track("overtime_minutes", record.get("overtime_minutes", 0), overtime)
    track("half_day", record.get("half_day", False), half_day)

    if not changes:
        raise HTTPException(422, "Correction makes no changes")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare current values so a concurrent correction cannot silently win.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "late_minutes": late,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Record changed concurrently; retry correction")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})

    # Convert all stored timestamps, including nested audit changes, to epoch ms.
    history = []
    for entry in updated.get("history", []):
        item = dict(entry)
        item["at"] = to_epoch_ms(item["at"])
        item["changes"] = {
            field: {
                side: (
                    to_epoch_ms(value)
                    if field in {"punch_in", "punch_out"} and value
                    else value
                )
                for side, value in pair.items()
            }
            for field, pair in item["changes"].items()
        }
        history.append(item)

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": to_epoch_ms(updated.get("punch_in")),
        "punch_out": to_epoch_ms(updated.get("punch_out")),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": history,
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


from decimal import Decimal, ROUND_HALF_UP
import calendar


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate YYYY-MM.
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        year, mon = map(int, month.split("-"))
        if not 1 <= mon <= 12:
            raise ValueError
        first_day = f"{year:04d}-{mon:02d}-01"
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be YYYY-MM")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")

    last_day_number = calendar.monthrange(year, mon)[1]
    last_day = f"{year:04d}-{mon:02d}-{last_day_number:02d}"

    # Working days begin on joined_on, and exclude weekends.
    start_date = max(first_day, employee["joined_on"])
    working_days = 0
    if start_date <= last_day:
        current = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(last_day, "%Y-%m-%d").date()
        while current <= end:
            if current.weekday() < 5:
                working_days += 1
            from datetime import timedelta
            current += timedelta(days=1)

    # Aggregate stored values; do not recalculate derived attendance fields.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    stats = result[0] if result else {}

    present_days = float(stats.get("present_days", 0))
    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (Decimal(str(present_days * 100 / working_days))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        year, mon = map(int, month.split("-"))
        if not 1 <= mon <= 12:
            raise ValueError
        first_day = f"{year:04d}-{mon:02d}-01"
    except (ValueError, TypeError):
        raise HTTPException(422, "month must be YYYY-MM")

    last_day = (
        f"{year:04d}-{mon:02d}-"
        f"{calendar.monthrange(year, mon)[1]:02d}"
    )

    employee_match = {"joined_on": {"$lte": last_day}}
    if department:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$emp_code", "$$code"]},
                            "date": {"$gte": first_day, "$lte": last_day},
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    output = []
    for row in results:
        if not row.get("headcount"):
            continue

        hours = [
            value for value in row.get("work_hours_values", [])
            if value is not None
        ]
        avg_hours = (
            float(Decimal(str(sum(hours) / len(hours))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ))
            if hours else None
        )

        output.append({
            "department": row["_id"],
            "headcount": len(row["headcount"]),
            "present_days": row.get("present_days", 0),
            "avg_work_hours": avg_hours,
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })

    return output




@app.get("/analytics/leaderboard/late")
def late_leaderboard(limit: int = 10):
    if limit < 1 or limit > 100:
        raise HTTPException(422, "limit must be between 1 and 100")

    pipeline = [
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "rank": 1,
            }
        },
    ]

    return list(db.attendance_logs.aggregate(pipeline))




@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str,
    date_to: str,
):
    from datetime import datetime, timedelta, timezone

    # Validate dates
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
        if start.strftime("%Y-%m-%d") != date_from:
            raise ValueError
        if end.strftime("%Y-%m-%d") != date_to:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date_from and date_to must use YYYY-MM-DD",
        )

    if end < start:
        raise HTTPException(422, "date_to must be on or after date_from")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    # MongoDB date bounds: upper bound is exclusive for $densify.
    start_utc = start.replace(tzinfo=timezone.utc)
    end_exclusive = (end + timedelta(days=1)).replace(
        tzinfo=timezone.utc
    )

    pipeline = [
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {
            "$project": {
                "_id": 0,
                "day": {
                    "$dateFromString": {"dateString": "$date"}
                },
                "present_weight": {
                    "$cond": [
                        {
                            "$in": [
                                "$status",
                                ["PRESENT", "WFH", "ON_DUTY"],
                            ]
                        },
                        {
                            "$cond": [
                                {"$eq": ["$half_day", True]},
                                0.5,
                                1,
                            ]
                        },
                        0,
                    ]
                },
                "late_count": {
                    "$cond": [
                        {"$gt": ["$late_minutes", 0]},
                        1,
                        0,
                    ]
                },
            }
        },
        # Seed the requested range so dates without attendance
        # records can also be included by $densify.
       
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "day": {"$literal": start_utc},
                            "present_weight": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },

        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_utc, end_exclusive],
                },
            }
        },
        {
            "$group": {
                "_id": "$day",
                "present_count": {"$sum": "$present_weight"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$day_string"]
                            },
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                },
                "weekday": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$gte": ["$weekday", 2]},
                                {"$lte": ["$weekday", 6]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "working_day": {
                    "$and": [
                        {"$gte": ["$weekday", 2]},
                        {"$lte": ["$weekday", 6]},
                    ]
                },
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$eq": ["$attendance_rate", None]},
                        None,
                        {
                            "$round": ["$attendance_rate", 4]
                        },
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        {"$round": ["$moving_avg_7d", 4]},
                    ]
                },
            }
        },
    ]

    return list(db.attendance_logs.aggregate(pipeline))


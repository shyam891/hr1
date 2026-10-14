import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymongo import MongoClient

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# Define FastAPI app BEFORE using decorators
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))

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

# --------------------------------------------------------------------------- #
# Basic Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/employees")
def add_employee(emp: EmployeeIn):
    db.employees.insert_one(emp.dict())
    return {"message": "Employee added"}

@app.post("/attendance/punch-in")
def punch_in(data: PunchInIn):
    now = datetime.now(IST)
    today = now.date().isoformat()
    emp = db.employees.find_one({"emp_code": data.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    late_minutes = compute_late_minutes(now, emp["shift_start"])
    db.attendance_logs.insert_one({
        "emp_code": data.emp_code,
        "date": today,
        "punch_in": now,
        "status": data.status,
        "late_minutes": late_minutes
    })
    return {"message": "Punch-in recorded", "late_minutes": late_minutes}

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None):
    query = {"emp_code": emp_code} if emp_code else {}
    return list(db.attendance_logs.find(query, {"_id": 0}))

# --------------------------------------------------------------------------- #
# New Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-out", status_code=200)
def punch_out(emp_code: str):
    today = datetime.now(IST).date().isoformat()
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": today})
    if not record:
        raise HTTPException(404, "No punch-in record found")
    if record.get("punch_out"):
        raise HTTPException(409, "Already punched out")

    now = datetime.now(IST)
    work_hours = compute_work_hours(record["punch_in"], now)
    overtime = compute_overtime(now, db.employees.find_one({"emp_code": emp_code})["shift_end"], today)
    half_day = work_hours < 4.5

    db.attendance_logs.update_one(
        {"emp_code": emp_code, "date": today},
        {"$set": {
            "punch_out": now,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": half_day
        }}
    )
    return {"message": "Punch-out recorded", "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}

@app.patch("/attendance/{emp_code}/{date}")
def correct(emp_code: str, date: str, changes: Dict, by: str, reason: str):
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Record not found")

    history_entry = {
        "at": datetime.utcnow(),
        "by": by,
        "reason": reason,
        "changes": changes
    }
    db.attendance_logs.update_one(
        {"emp_code": emp_code, "date": date},
        {"$push": {"history": history_entry}, "$set": changes}
    )
    return {"message": "Correction applied", "changes": changes}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {"$group": {
            "_id": "$emp_code",
            "days_present": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]}, 1, 0]}},
            "late_minutes": {"$sum": "$late_minutes"},
            "overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    return result[0] if result else {"emp_code": emp_code, "days_present": 0, "late_minutes": 0, "overtime_minutes": 0}

@app.get("/analytics/departments/summary")
def department_summary():
    employees = list(db.employees.find({}, {"_id": 0}))
    summary = {}
    for emp in employees:
        dept = emp["department"]
        summary.setdefault(dept, {"employees": 0, "present_days": 0})
        summary[dept]["employees"] += 1
        logs = db.attendance_logs.count_documents({"emp_code": emp["emp_code"], "status": {"$in": ["PRESENT","WFH","ON_DUTY"]}})
        summary[dept]["present_days"] += logs
    return summary

@app.get("/analytics/leaderboard/late")
def leaderboard_late(limit: int = 5):
    pipeline = [
        {"$group": {"_id": "$emp_code", "late_minutes": {"$sum": "$late_minutes"}}},
        {"$sort": {"late_minutes": -1}}
    ]
    results = list(db.attendance_logs.aggregate(pipeline))
    cutoff = results[limit-1]["late_minutes"] if len(results) >= limit else 0
    return [r for r in results if r["late_minutes"] >= cutoff]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str):
    pipeline = [
        {"$match": {"status": {"$in": ["PRESENT","WFH","ON_DUTY"]}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {"_id": {"month": {"$substr": ["$date", 0, 7]}}, "present_days": {"$sum": 1}}},
        {"$sort": {"_id.month": 1}}
    ]
    return list(db.attendance_logs.aggregate(pipeline))

@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str):
    explanations = {
        "punch-in": "Records punch-in time, computes late minutes.",
        "punch-out": "Records punch-out time, computes work hours, overtime, half-day.",
        "correct": "Applies manual corrections with audit trail.",
        "employee_monthly": "Summarizes attendance for one employee in a month.",
        "department_summary": "Summarizes attendance across departments.",
        "leaderboard_late": "Ranks employees by late minutes.",
        "department_trend": "Shows monthly presence trend for a department."
    }
    return {"endpoint": endpoint, "explanation": explanations.get(endpoint, "Unknown endpoint")}

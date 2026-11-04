import os
import io
from datetime import datetime, date, time, timedelta
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
import pandas as pd

# ----------------- 1. 資料庫連線設定 (SQLite) -----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "shuttle.db")
DB_URL = f"sqlite:///{DB_FILE}"

engine = create_engine(
    DB_URL, 
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ----------------- 2. 初始化資料表與預設帳號 -----------------
def init_db():
    with engine.connect() as conn:
        # 使用者帳號表 (支援多個管理員與駕駛)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                real_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'driver', -- 'admin' 或 'driver'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))

        # 若無管理員，建立預設起始管理員
        admin_count = conn.execute(text("SELECT COUNT(*) FROM admin_users WHERE role = 'admin'")).scalar()
        if admin_count == 0:
            conn.execute(text("""
                INSERT INTO admin_users (username, password, real_name, role)
                VALUES ('admin', 'admin888', '系統管理員', 'admin')
            """))

        # 其他業務表
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS employees (
                emp_no TEXT PRIMARY KEY,
                emp_name TEXT NOT NULL,
                dept_name TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS default_time_slots (
                slot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_slot TEXT UNIQUE NOT NULL,
                default_capacity INTEGER DEFAULT 8,
                cutoff_day TEXT DEFAULT 'PREV',
                cutoff_time TEXT DEFAULT '21:00',
                is_active INTEGER DEFAULT 1
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shuttle_schedules (
                schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ride_date TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                max_capacity INTEGER DEFAULT 8,
                current_booked INTEGER DEFAULT 0,
                cutoff_day TEXT DEFAULT 'PREV',
                cutoff_time TEXT DEFAULT '21:00',
                status TEXT DEFAULT 'OPEN',
                UNIQUE(ride_date, time_slot)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shuttle_bookings (
                booking_id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                emp_no TEXT NOT NULL,
                emp_name TEXT NOT NULL,
                dept_name TEXT NOT NULL,
                ride_date TEXT NOT NULL,
                time_slot TEXT NOT NULL,
                status TEXT DEFAULT 'BOOKED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                canceled_at TIMESTAMP
            )
        """))

        # 相容檢查補齊 cutoff 欄位
        try:
            conn.execute(text("ALTER TABLE default_time_slots ADD COLUMN cutoff_day TEXT DEFAULT 'PREV'"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE default_time_slots ADD COLUMN cutoff_time TEXT DEFAULT '21:00'"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE shuttle_schedules ADD COLUMN cutoff_day TEXT DEFAULT 'PREV'"))
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE shuttle_schedules ADD COLUMN cutoff_time TEXT DEFAULT '21:00'"))
        except Exception:
            pass

        # 初始化標準時段 (若尚未建立)
        count = conn.execute(text("SELECT COUNT(*) FROM default_time_slots")).scalar()
        if count == 0:
            default_slots = [
                ("06:50", 8, "PREV", "21:00"),
                ("07:20", 8, "PREV", "21:00"),
                ("07:40", 8, "PREV", "21:00"),
                ("07:56", 8, "PREV", "21:00"),
                ("17:10", 8, "SAME", "15:00"),
                ("17:40", 8, "SAME", "15:00"),
                ("18:10", 8, "SAME", "15:00"),
                ("18:45", 8, "SAME", "15:00")
            ]
            for slot, cap, c_day, c_time in default_slots:
                conn.execute(
                    text("""
                        INSERT INTO default_time_slots (time_slot, default_capacity, cutoff_day, cutoff_time, is_active) 
                        VALUES (:slot, :cap, :c_day, :c_time, 1)
                    """),
                    {"slot": slot, "cap": cap, "c_day": c_day, "c_time": c_time}
                )
        conn.commit()

init_db()

# ----------------- 3. 初始化 FastAPI 應用 -----------------
app = FastAPI(
    title="生達公司車接送預約系統 API",
    description="提供員工班次預約、取消、多帳號管理與個別截止時間控制",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----------------- 4. Pydantic 模型定義 -----------------
class BookingRequest(BaseModel):
    emp_no: str
    emp_name: str
    dept_name: str
    ride_date: str
    time_slot: str

class CancelRequest(BaseModel):
    booking_id: int
    emp_no: str

class SlotManageRequest(BaseModel):
    time_slot: str
    default_capacity: int = 8
    cutoff_day: str = "PREV"
    cutoff_time: str = "21:00"
    is_active: bool = True

class DailySlotAddRequest(BaseModel):
    ride_date: str
    time_slot: str
    max_capacity: int = 8
    cutoff_day: str = "PREV"
    cutoff_time: str = "21:00"

class SlotStatusUpdateRequest(BaseModel):
    schedule_id: int
    status: str

# 帳號驗證與管理模型
class LoginRequest(BaseModel):
    target: str   # 'admin' 或 'driver'
    username: str
    password: str

class CreateUserRequest(BaseModel):
    username: str
    password: str
    real_name: str
    role: str     # 'admin' 或 'driver'

class UpdateUserRequest(BaseModel):
    id: int
    password: Optional[str] = None
    real_name: Optional[str] = None
    role: Optional[str] = None

# ----------------- 5. 多帳號驗證與維護 API -----------------

@app.post("/api/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.execute(
        text("SELECT id, username, password, real_name, role FROM admin_users WHERE username = :u"),
        {"u": req.username.strip()}
    ).mappings().first()

    if not user or user["password"] != req.password.strip():
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤，請重新確認！")

    # 權限檢查：時段維護只能管理員；名冊則管理員與駕駛均可
    if req.target == "admin" and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="此帳號為駕駛權限，無權進入管理員時段維護功能！")

    return {
        "status": "ok",
        "user_id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "role": user["role"]
    }

@app.get("/api/admin/users")
def get_admin_users(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT id, username, real_name, role, created_at FROM admin_users ORDER BY role ASC, id ASC")).mappings().all()
    return {"users": [dict(r) for r in rows]}

@app.post("/api/admin/users")
def create_admin_user(req: CreateUserRequest, db: Session = Depends(get_db)):
    u = req.username.strip()
    p = req.password.strip()
    r_name = req.real_name.strip()
    if not u or not p or not r_name:
        raise HTTPException(status_code=400, detail="帳號、密碼與姓名皆不可為空！")

    exists = db.execute(text("SELECT id FROM admin_users WHERE username = :u"), {"u": u}).first()
    if exists:
        raise HTTPException(status_code=400, detail="該帳號已存在，請使用其他帳號！")

    db.execute(
        text("INSERT INTO admin_users (username, password, real_name, role) VALUES (:u, :p, :name, :role)"),
        {"u": u, "p": p, "name": r_name, "role": req.role}
    )
    db.commit()
    return {"message": f"成功建立 {req.role} 帳號：{u} ({r_name})！"}

@app.put("/api/admin/users")
def update_admin_user(req: UpdateUserRequest, db: Session = Depends(get_db)):
    user = db.execute(text("SELECT id FROM admin_users WHERE id = :id"), {"id": req.id}).first()
    if not user:
        raise HTTPException(status_code=404, detail="查無此帳號！")

    updates = []
    params = {"id": req.id}
    if req.password and req.password.strip():
        updates.append("password = :p")
        params["p"] = req.password.strip()
    if req.real_name and req.real_name.strip():
        updates.append("real_name = :name")
        params["name"] = req.real_name.strip()
    if req.role:
        updates.append("role = :role")
        params["role"] = req.role

    if updates:
        sql = f"UPDATE admin_users SET {', '.join(updates)} WHERE id = :id"
        db.execute(text(sql), params)
        db.commit()
    return {"message": "帳號資訊更新成功！"}

@app.delete("/api/admin/users/{user_id}")
def delete_admin_user(user_id: int, current_username: Optional[str] = None, db: Session = Depends(get_db)):
    user = db.execute(text("SELECT id, username, role FROM admin_users WHERE id = :id"), {"id": user_id}).mappings().first()
    if not user:
        raise HTTPException(status_code=404, detail="查無此帳號")

    if current_username and user["username"] == current_username:
        raise HTTPException(status_code=400, detail="不可刪除目前正在登入使用的自己帳號！")

    # 確保系統至少保留一名管理員
    if user["role"] == "admin":
        admin_count = db.execute(text("SELECT COUNT(*) FROM admin_users WHERE role = 'admin'")).scalar()
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail="系統必須至少保留一位管理員，無法刪除最後一名管理員！")

    db.execute(text("DELETE FROM admin_users WHERE id = :id"), {"id": user_id})
    db.commit()
    return {"message": f"已刪除帳號：{user['username']}"}

# ----------------- 6. 業務規則與排程函式 -----------------
def parse_flexible_time(t_str: str) -> time:
    if not t_str:
        return time(21, 0)
    clean_t = str(t_str).strip()
    if ":" in clean_t:
        parts = clean_t.split(":")
        return time(int(parts[0]), int(parts[1]))
    if len(clean_t) == 4:
        return time(int(clean_t[:2]), int(clean_t[2:]))
    if len(clean_t) == 3:
        return time(int(clean_t[:1]), int(clean_t[1:]))
    return datetime.strptime(clean_t, "%H:%M").time()

def validate_slot_deadline(ride_date_str: str, cutoff_day: str, cutoff_time_str: str):
    try:
        clean_date_str = str(ride_date_str).strip().replace("/", "-")
        r_date = datetime.strptime(clean_date_str, "%Y-%m-%d").date()
        c_time = parse_flexible_time(cutoff_time_str)
    except Exception:
        raise HTTPException(status_code=400, detail="日期或時間格式錯誤")

    target_date = r_date - timedelta(days=1) if cutoff_day == "PREV" else r_date
    deadline = datetime.combine(target_date, c_time)

    if datetime.now() > deadline:
        day_text = "前一日" if cutoff_day == "PREV" else "搭乘當日"
        c_time_display = c_time.strftime("%H:%M")
        raise HTTPException(
            status_code=400, 
            detail=f"已超過截止時間！該班次截止時間為：{day_text} {c_time_display}（{deadline.strftime('%m/%d %H:%M')}）"
        )

def ensure_daily_schedules(target_date: str, db: Session):
    check_date = str(target_date).strip().replace("/", "-")
    check_sql = text("SELECT COUNT(*) FROM shuttle_schedules WHERE ride_date = :ride_date")
    count = db.execute(check_sql, {"ride_date": check_date}).scalar()
    
    if count == 0:
        query_default = text("""
            SELECT time_slot, default_capacity, 
                   COALESCE(cutoff_day, 'PREV') AS cutoff_day, 
                   COALESCE(cutoff_time, '21:00') AS cutoff_time
            FROM default_time_slots 
            WHERE is_active = 1 
            ORDER BY time_slot ASC
        """)
        default_slots = db.execute(query_default).mappings().all()

        insert_sql = text("""
            INSERT INTO shuttle_schedules (ride_date, time_slot, max_capacity, current_booked, cutoff_day, cutoff_time, status)
            VALUES (:ride_date, :time_slot, :max_capacity, 0, :cutoff_day, :cutoff_time, 'OPEN')
        """)
        for s in default_slots:
            db.execute(insert_sql, {
                "ride_date": check_date,
                "time_slot": s["time_slot"],
                "max_capacity": s["default_capacity"],
                "cutoff_day": s["cutoff_day"],
                "cutoff_time": s["cutoff_time"]
            })
        db.commit()

# ----------------- 7. 時段維護管理 API -----------------

@app.get("/api/admin/default-slots")
def get_default_slots(db: Session = Depends(get_db)):
    sql = text("""
        SELECT slot_id, time_slot, default_capacity, 
               COALESCE(cutoff_day, 'PREV') AS cutoff_day, 
               COALESCE(cutoff_time, '21:00') AS cutoff_time, 
               is_active 
        FROM default_time_slots 
        ORDER BY time_slot ASC
    """)
    rows = db.execute(sql).mappings().all()
    return {"slots": [dict(r) for r in rows]}

@app.post("/api/admin/default-slots")
def save_default_slot(req: SlotManageRequest, db: Session = Depends(get_db)):
    formatted_cutoff_time = parse_flexible_time(req.cutoff_time).strftime("%H:%M")

    sql = text("""
        INSERT INTO default_time_slots (time_slot, default_capacity, cutoff_day, cutoff_time, is_active)
        VALUES (:time_slot, :default_capacity, :cutoff_day, :cutoff_time, :is_active)
        ON CONFLICT(time_slot) DO UPDATE SET
            default_capacity = excluded.default_capacity,
            cutoff_day = excluded.cutoff_day,
            cutoff_time = excluded.cutoff_time,
            is_active = excluded.is_active
    """)
    db.execute(sql, {
        "time_slot": req.time_slot,
        "default_capacity": req.default_capacity,
        "cutoff_day": req.cutoff_day,
        "cutoff_time": formatted_cutoff_time,
        "is_active": 1 if req.is_active else 0
    })

    today_str = date.today().strftime("%Y-%m-%d")
    sync_sql = text("""
        UPDATE shuttle_schedules
        SET cutoff_day = :cutoff_day,
            cutoff_time = :cutoff_time,
            max_capacity = :default_capacity
        WHERE time_slot = :time_slot AND ride_date >= :today
    """)
    db.execute(sync_sql, {
        "time_slot": req.time_slot,
        "cutoff_day": req.cutoff_day,
        "cutoff_time": formatted_cutoff_time,
        "default_capacity": req.default_capacity,
        "today": today_str
    })

    db.commit()
    return {"message": f"時段 {req.time_slot} 設定已儲存，並已同步更新未來所有預約班次！"}

@app.delete("/api/admin/default-slots/{slot_id}")
def delete_default_slot(slot_id: int, db: Session = Depends(get_db)):
    sql = text("DELETE FROM default_time_slots WHERE slot_id = :slot_id")
    db.execute(sql, {"slot_id": slot_id})
    db.commit()
    return {"message": "已刪除該標準時段"}

@app.post("/api/admin/daily-slots/add")
def add_daily_slot(req: DailySlotAddRequest, db: Session = Depends(get_db)):
    try:
        clean_date = str(req.ride_date).strip().replace("/", "-")
        formatted_cutoff_time = parse_flexible_time(req.cutoff_time).strftime("%H:%M")
        sql = text("""
            INSERT INTO shuttle_schedules (ride_date, time_slot, max_capacity, current_booked, cutoff_day, cutoff_time, status)
            VALUES (:ride_date, :time_slot, :max_capacity, 0, :cutoff_day, :cutoff_time, 'OPEN')
            ON CONFLICT(ride_date, time_slot) DO UPDATE SET
                max_capacity = excluded.max_capacity,
                cutoff_day = excluded.cutoff_day,
                cutoff_time = excluded.cutoff_time,
                status = 'OPEN'
        """)
        db.execute(sql, {
            "ride_date": clean_date,
            "time_slot": req.time_slot,
            "max_capacity": req.max_capacity,
            "cutoff_day": req.cutoff_day,
            "cutoff_time": formatted_cutoff_time
        })
        db.commit()
        return {"message": f"成功更新 {clean_date} 的 {req.time_slot} 班次！"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/admin/daily-slots/status")
def update_daily_slot_status(req: SlotStatusUpdateRequest, db: Session = Depends(get_db)):
    sql = text("UPDATE shuttle_schedules SET status = :status WHERE schedule_id = :schedule_id")
    db.execute(sql, {"status": req.status, "schedule_id": req.schedule_id})
    db.commit()
    return {"message": f"班次狀態已更新為 {req.status}"}

@app.delete("/api/admin/daily-slots/{schedule_id}")
def delete_daily_slot(schedule_id: int, db: Session = Depends(get_db)):
    sched = db.execute(
        text("SELECT schedule_id, current_booked, time_slot, ride_date FROM shuttle_schedules WHERE schedule_id = :id"),
        {"id": schedule_id}
    ).mappings().first()
    
    if not sched:
        raise HTTPException(status_code=404, detail="查無此班次紀錄")
    
    if sched["current_booked"] > 0:
        db.execute(text("""
            UPDATE shuttle_bookings 
            SET status = 'CANCELED', canceled_at = CURRENT_TIMESTAMP 
            WHERE schedule_id = :id AND status = 'BOOKED'
        """), {"id": schedule_id})

    db.execute(text("DELETE FROM shuttle_schedules WHERE schedule_id = :id"), {"id": schedule_id})
    db.commit()
    return {"message": f"已成功刪除 {sched['ride_date']} 的 {sched['time_slot']} 班次！"}

# ----------------- 8. 預約與班次排程 API -----------------

@app.get("/api/schedules")
def get_schedules(ride_date: str, db: Session = Depends(get_db)):
    clean_date = str(ride_date).strip().replace("/", "-")
    ensure_daily_schedules(clean_date, db)
    sql = text("""
        SELECT schedule_id, ride_date, time_slot, max_capacity, current_booked, 
               COALESCE(cutoff_day, 'PREV') AS cutoff_day, 
               COALESCE(cutoff_time, '21:00') AS cutoff_time, 
               status
        FROM shuttle_schedules
        WHERE ride_date = :ride_date
        ORDER BY time_slot ASC
    """)
    rows = db.execute(sql, {"ride_date": clean_date}).mappings().all()
    
    result = []
    for r in rows:
        result.append({
            "schedule_id": r["schedule_id"],
            "ride_date": str(r["ride_date"]),
            "time_slot": r["time_slot"],
            "max_capacity": r["max_capacity"],
            "current_booked": r["current_booked"],
            "cutoff_day": r["cutoff_day"],
            "cutoff_time": r["cutoff_time"],
            "remaining_seats": max(0, r["max_capacity"] - r["current_booked"]),
            "status": r["status"]
        })
    return {"data": result}

@app.post("/api/bookings")
def create_booking(req: BookingRequest, db: Session = Depends(get_db)):
    try:
        clean_date = str(req.ride_date).strip().replace("/", "-")
        query_schedule = text("""
            SELECT schedule_id, current_booked, max_capacity, 
                   COALESCE(cutoff_day, 'PREV') AS cutoff_day, 
                   COALESCE(cutoff_time, '21:00') AS cutoff_time, 
                   status
            FROM shuttle_schedules
            WHERE ride_date = :ride_date AND time_slot = :time_slot
        """)
        schedule = db.execute(query_schedule, {
            "ride_date": clean_date,
            "time_slot": req.time_slot
        }).mappings().first()

        if not schedule:
            raise HTTPException(status_code=404, detail="該日期時段未開放預約")

        validate_slot_deadline(clean_date, schedule["cutoff_day"], schedule["cutoff_time"])

        if schedule["status"] != "OPEN" or schedule["current_booked"] >= schedule["max_capacity"]:
            raise HTTPException(status_code=400, detail="該班次已額滿或已停止預約")

        check_dup = text("""
            SELECT booking_id FROM shuttle_bookings
            WHERE emp_no = :emp_no 
              AND ride_date = :ride_date 
              AND time_slot = :time_slot 
              AND status = 'BOOKED'
        """)
        dup = db.execute(check_dup, {
            "emp_no": req.emp_no,
            "ride_date": clean_date,
            "time_slot": req.time_slot
        }).first()

        if dup:
            raise HTTPException(status_code=400, detail="您已預約過此班次，請勿重複登記")

        insert_booking = text("""
            INSERT INTO shuttle_bookings (schedule_id, emp_no, emp_name, dept_name, ride_date, time_slot, status)
            VALUES (:schedule_id, :emp_no, :emp_name, :dept_name, :ride_date, :time_slot, 'BOOKED')
        """)
        db.execute(insert_booking, {
            "schedule_id": schedule["schedule_id"],
            "emp_no": req.emp_no,
            "emp_name": req.emp_name,
            "dept_name": req.dept_name,
            "ride_date": clean_date,
            "time_slot": req.time_slot
        })

        new_count = schedule["current_booked"] + 1
        new_status = "FULL" if new_count >= schedule["max_capacity"] else "OPEN"
        update_schedule = text("""
            UPDATE shuttle_schedules
            SET current_booked = :current_booked, status = :status
            WHERE schedule_id = :schedule_id
        """)
        db.execute(update_schedule, {
            "current_booked": new_count,
            "status": new_status,
            "schedule_id": schedule["schedule_id"]
        })

        db.commit()
        return {"message": "預約成功", "emp_name": req.emp_name, "time_slot": req.time_slot}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"系統處理失敗: {str(e)}")

@app.post("/api/bookings/cancel")
def cancel_booking(req: CancelRequest, db: Session = Depends(get_db)):
    try:
        query_booking = text("""
            SELECT b.booking_id, b.schedule_id, b.ride_date, b.time_slot, b.status,
                   COALESCE(s.cutoff_day, 'PREV') AS cutoff_day,
                   COALESCE(s.cutoff_time, '21:00') AS cutoff_time
            FROM shuttle_bookings b
            LEFT JOIN shuttle_schedules s ON b.schedule_id = s.schedule_id
            WHERE b.booking_id = :booking_id AND b.emp_no = :emp_no
        """)
        booking = db.execute(query_booking, {
            "booking_id": req.booking_id,
            "emp_no": req.emp_no
        }).mappings().first()

        if not booking:
            raise HTTPException(status_code=404, detail="查無此預約紀錄或身分不符")
        if booking["status"] == "CANCELED":
            raise HTTPException(status_code=400, detail="該預約先前已取消")

        validate_slot_deadline(str(booking["ride_date"]), booking["cutoff_day"], booking["cutoff_time"])

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(text("""
            UPDATE shuttle_bookings
            SET status = 'CANCELED', canceled_at = :now
            WHERE booking_id = :booking_id
        """), {"booking_id": req.booking_id, "now": now_str})

        db.execute(text("""
            UPDATE shuttle_schedules
            SET current_booked = MAX(current_booked - 1, 0),
                status = 'OPEN'
            WHERE schedule_id = :schedule_id
        """), {"schedule_id": booking["schedule_id"]})

        db.commit()
        return {"message": "已成功取消預約"}

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"取消失敗: {str(e)}")

# [司機與管理名冊查詢]
@app.get("/api/admin/passengers")
def get_passengers(ride_date: str, time_slot: Optional[str] = None, db: Session = Depends(get_db)):
    clean_date = str(ride_date).strip().replace("/", "-")
    params = {"ride_date": clean_date}
    where_clause = "WHERE ride_date = :ride_date AND status = 'BOOKED'"

    if time_slot:
        where_clause += " AND time_slot = :time_slot"
        params["time_slot"] = time_slot

    sql = text(f"""
        SELECT booking_id, dept_name, emp_no, emp_name, ride_date, time_slot, created_at
        FROM shuttle_bookings
        {where_clause}
        ORDER BY time_slot ASC, created_at ASC
    """)
    rows = db.execute(sql, params).mappings().all()

    return {
        "ride_date": clean_date,
        "total_passengers": len(rows),
        "passengers": [
            {
                "booking_id": r["booking_id"],
                "dept_name": r["dept_name"],
                "emp_no": r["emp_no"],
                "emp_name": r["emp_name"],
                "ride_date": str(r["ride_date"]),
                "time_slot": r["time_slot"],
                "booked_time": str(r["created_at"])
            }
            for r in rows
        ]
    }

# [個人預約查詢]
@app.get("/api/my-bookings")
def get_my_bookings(emp_no: str, db: Session = Depends(get_db)):
    today_str = date.today().strftime("%Y-%m-%d")
    sql = text("""
        SELECT booking_id, dept_name, emp_no, emp_name, ride_date, time_slot, created_at
        FROM shuttle_bookings
        WHERE emp_no = :emp_no 
          AND ride_date >= :today 
          AND status = 'BOOKED'
        ORDER BY ride_date ASC, time_slot ASC
    """)
    rows = db.execute(sql, {"emp_no": emp_no, "today": today_str}).mappings().all()

    return {
        "bookings": [
            {
                "booking_id": r["booking_id"],
                "dept_name": r["dept_name"],
                "emp_no": r["emp_no"],
                "emp_name": r["emp_name"],
                "ride_date": str(r["ride_date"]),
                "time_slot": r["time_slot"],
                "booked_time": str(r["created_at"])
            }
            for r in rows
        ]
    }

# ----------------- 9. PWA 與靜態頁面託管 -----------------

@app.get("/manifest.json", include_in_schema=False)
def serve_manifest():
    manifest_data = {
        "name": "生達公司車預約平台",
        "short_name": "生達派車",
        "description": "生達化學製藥上下班接駁專車預約系統",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#065f46",
        "theme_color": "#065f46",
        "orientation": "portrait"
    }
    return Response(content=pd.Series(manifest_data).to_json(), media_type="application/manifest+json")

@app.get("/icon.png", include_in_schema=False)
def serve_icon():
    icon_path = os.path.join(BASE_DIR, "icon.png")
    if os.path.exists(icon_path):
        return FileResponse(icon_path, media_type="image/png")
    svg_content = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
      <rect width="512" height="512" rx="100" fill="#065f46"/>
      <text x="50%" y="54%" font-family="Arial, Helvetica, sans-serif" font-size="220" font-weight="900" fill="#ffffff" dominant-baseline="middle" text-anchor="middle">ST</text>
    </svg>"""
    return Response(content=svg_content, media_type="image/svg+xml")

@app.get("/", include_in_schema=False)
def serve_frontend():
    html_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"message": "後端服務運作中，請確保 index.html 放置於同目錄下。"}
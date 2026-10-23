from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("LABLINK_DB", str(BASE_DIR / "lablink.db")))
STATIC_DIR = BASE_DIR / "static"


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS resources (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            capacity INTEGER NOT NULL DEFAULT 1 CHECK(capacity > 0),
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            resource_id TEXT NOT NULL REFERENCES resources(id),
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('CONFIRMED','CANCELLED')),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_res_resource_time
            ON reservations(resource_id, start_time, end_time, status);
        CREATE TABLE IF NOT EXISTS wait_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            resource_id TEXT NOT NULL REFERENCES resources(id),
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            request_id TEXT,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        if con.execute("SELECT COUNT(*) FROM resources").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO resources(id,name,location,capacity,active) VALUES(?,?,?,?,1)",
                [
                    ("GPU-01", "GPU Workstation", "Computer Lab A", 1),
                    ("PR-01", "3D Printer", "Innovation Lab", 1),
                    ("DEV-01", "Embedded Development Kit", "Computer Lab B", 1),
                ],
            )


def log_event(con, event_type, request_id, detail):
    con.execute(
        "INSERT INTO events(event_type,request_id,detail,created_at) VALUES(?,?,?,?)",
        (event_type, request_id, detail, now_utc()),
    )


def parse_datetime(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Invalid ISO-8601 datetime: {value}")
    if dt.tzinfo is None:
        raise HTTPException(400, "Datetime must include a timezone.")
    return dt.astimezone(timezone.utc)


class ReservationRequest(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=80)
    user_id: str = Field(min_length=1, max_length=80)
    resource_id: str = Field(min_length=1, max_length=30)
    start_time: str
    end_time: str


class ResourceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=100)
    capacity: int = Field(default=1, ge=1, le=100)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="LabLink Distributed Campus Resource Coordinator",
    description="Mini-project API for shared resource reservations, retries, queueing and event logs.",
    version="1.0.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "LabLink", "timestamp": now_utc()}


@app.get("/api/resources")
def list_resources():
    with connect() as con:
        rows = con.execute("SELECT * FROM resources ORDER BY id").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/resources", status_code=201)
def create_resource(resource: ResourceCreate):
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO resources(id,name,location,capacity,active) VALUES(?,?,?,?,1)",
                (resource.id, resource.name, resource.location, resource.capacity),
            )
            log_event(con, "RESOURCE_CREATED", None, json.dumps(resource.model_dump()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Resource ID already exists")
    return {"status": "created", "resource_id": resource.id}


@app.get("/api/reservations")
def list_reservations():
    with connect() as con:
        rows = con.execute("""
            SELECT b.*, r.name AS resource_name
            FROM reservations b JOIN resources r ON r.id=b.resource_id
            ORDER BY b.created_at DESC
        """).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/reservations")
def reserve(req: ReservationRequest):
    start, end = parse_datetime(req.start_time), parse_datetime(req.end_time)
    if end <= start:
        raise HTTPException(400, "end_time must be later than start_time")

    con = connect()
    try:
        # BEGIN IMMEDIATE serializes writers for this single-host SQLite demo.
        con.execute("BEGIN IMMEDIATE")

        previous_reservation = con.execute(
            "SELECT id,status FROM reservations WHERE request_id=?",
            (req.request_id,),
        ).fetchone()
        if previous_reservation:
            con.commit()
            return {
                "status": previous_reservation["status"],
                "request_id": req.request_id,
                "reservation_id": previous_reservation["id"],
                "idempotent_replay": True,
            }

        previous_queue = con.execute(
            "SELECT id,status FROM wait_queue WHERE request_id=?",
            (req.request_id,),
        ).fetchone()
        if previous_queue:
            con.commit()
            return {
                "status": previous_queue["status"],
                "request_id": req.request_id,
                "reservation_id": None,
                "idempotent_replay": True,
            }

        resource = con.execute(
            "SELECT * FROM resources WHERE id=? AND active=1",
            (req.resource_id,),
        ).fetchone()
        if not resource:
            raise HTTPException(404, "Active resource not found")

        # This starter implementation treats each resource as exclusive.
        conflicts = con.execute("""
            SELECT COUNT(*) AS n FROM reservations
            WHERE resource_id=? AND status='CONFIRMED'
              AND start_time < ? AND end_time > ?
        """, (req.resource_id, end.isoformat(), start.isoformat())).fetchone()["n"]

        if conflicts == 0:
            reservation_id = str(uuid.uuid4())
            con.execute("""
                INSERT INTO reservations
                (id,request_id,user_id,resource_id,start_time,end_time,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                reservation_id, req.request_id, req.user_id, req.resource_id,
                start.isoformat(), end.isoformat(), "CONFIRMED", now_utc()
            ))
            log_event(con, "RESERVATION_CONFIRMED", req.request_id,
                      f"{req.user_id} booked {req.resource_id} from {start.isoformat()} to {end.isoformat()}")
            con.commit()
            return {
                "status": "CONFIRMED",
                "request_id": req.request_id,
                "reservation_id": reservation_id,
                "idempotent_replay": False,
            }

        con.execute("""
            INSERT INTO wait_queue(request_id,user_id,resource_id,start_time,end_time,status,created_at)
            VALUES(?,?,?,?,?,'QUEUED',?)
        """, (
            req.request_id, req.user_id, req.resource_id,
            start.isoformat(), end.isoformat(), now_utc()
        ))
        log_event(con, "REQUEST_QUEUED", req.request_id,
                  f"{req.user_id} queued for {req.resource_id}")
        con.commit()
        return {
            "status": "QUEUED",
            "request_id": req.request_id,
            "reservation_id": None,
            "idempotent_replay": False,
        }
    except HTTPException:
        con.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        con.rollback()
        raise HTTPException(409, f"Request conflict: {exc}")
    finally:
        con.close()


@app.get("/api/queue")
def get_queue():
    with connect() as con:
        rows = con.execute("""
            SELECT q.*, r.name AS resource_name
            FROM wait_queue q JOIN resources r ON r.id=q.resource_id
            WHERE q.status='QUEUED' ORDER BY q.id
        """).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/reservations/{reservation_id}/cancel")
def cancel_reservation(reservation_id: str):
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM reservations WHERE id=?", (reservation_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Reservation not found")
        if row["status"] == "CANCELLED":
            con.commit()
            return {"status": "CANCELLED", "reservation_id": reservation_id, "already_cancelled": True}

        con.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (reservation_id,))
        log_event(con, "RESERVATION_CANCELLED", row["request_id"], f"Reservation {reservation_id} cancelled")

        # Promote the oldest queued request that fits the released interval.
        queued = con.execute("""
            SELECT * FROM wait_queue
            WHERE resource_id=? AND status='QUEUED' AND start_time < ? AND end_time > ?
            ORDER BY id
        """, (row["resource_id"], row["end_time"], row["start_time"])).fetchall()

        for q in queued:
            conflict = con.execute("""
                SELECT COUNT(*) AS n FROM reservations
                WHERE resource_id=? AND status='CONFIRMED'
                  AND start_time < ? AND end_time > ?
            """, (q["resource_id"], q["end_time"], q["start_time"])).fetchone()["n"]
            if conflict == 0:
                new_id = str(uuid.uuid4())
                con.execute("""
                    INSERT INTO reservations
                    (id,request_id,user_id,resource_id,start_time,end_time,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (
                    new_id, q["request_id"], q["user_id"], q["resource_id"],
                    q["start_time"], q["end_time"], "CONFIRMED", now_utc()
                ))
                con.execute("UPDATE wait_queue SET status='PROMOTED' WHERE id=?", (q["id"],))
                log_event(con, "QUEUE_PROMOTED", q["request_id"],
                          f"Queued request promoted to reservation {new_id}")
                break
        con.commit()
        return {"status": "CANCELLED", "reservation_id": reservation_id}
    except HTTPException:
        con.rollback()
        raise
    finally:
        con.close()


@app.get("/api/events")
def list_events(limit: int = 100):
    limit = max(1, min(limit, 500))
    with connect() as con:
        rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]

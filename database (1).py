"""Data access helpers + relational schema (mirrors the ERD in the specification)."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from flask import g, current_app

FMT = "%Y-%m-%d %H:%M:%S"

# Đồng hồ có thể thay thế khi test: monkeypatch.setattr(database, "_clock", lambda: ...)
_clock = datetime.now


def now():
    return _clock().strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT)


class AppError(Exception):
    """Business-rule violation; message is shown to the user."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
 user_id INTEGER PRIMARY KEY AUTOINCREMENT,
 username TEXT UNIQUE NOT NULL COLLATE NOCASE,
 password_hash TEXT NOT NULL,
 full_name TEXT NOT NULL,
 email TEXT UNIQUE NOT NULL COLLATE NOCASE,
 phone TEXT DEFAULT '',
 role TEXT NOT NULL CHECK(role IN ('ADMIN','LECTURER','STUDENT')),
 is_locked INTEGER NOT NULL DEFAULT 0 CHECK(is_locked IN (0,1)),
 created_at TEXT, last_login_at TEXT);

CREATE TABLE IF NOT EXISTS password_reset_tokens(
 token_id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users,
 token_hash TEXT UNIQUE NOT NULL,
 expires_at TEXT NOT NULL, used_at TEXT);

CREATE TABLE IF NOT EXISTS notifications(
 notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users,
 message TEXT NOT NULL,
 is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN (0,1)),
 created_at TEXT);

CREATE TABLE IF NOT EXISTS reports(
 report_id INTEGER PRIMARY KEY AUTOINCREMENT,
 generated_by INTEGER REFERENCES users,
 report_type TEXT, file_format TEXT, generated_at TEXT);

CREATE TABLE IF NOT EXISTS faculties(
 faculty_id INTEGER PRIMARY KEY AUTOINCREMENT,
 faculty_name TEXT UNIQUE NOT NULL);

CREATE TABLE IF NOT EXISTS courses(
 course_id INTEGER PRIMARY KEY AUTOINCREMENT,
 course_code TEXT UNIQUE NOT NULL,
 course_name TEXT NOT NULL,
 credits INTEGER NOT NULL CHECK(credits > 0),
 total_sessions INTEGER NOT NULL CHECK(total_sessions > 0),
 faculty_id INTEGER NOT NULL REFERENCES faculties);

CREATE TABLE IF NOT EXISTS students(
 user_id INTEGER PRIMARY KEY REFERENCES users,
 student_code TEXT UNIQUE NOT NULL,
 date_of_birth TEXT, major TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS lecturers(
 user_id INTEGER PRIMARY KEY REFERENCES users,
 lecturer_code TEXT UNIQUE NOT NULL);

CREATE TABLE IF NOT EXISTS class_sections(
 class_id INTEGER PRIMARY KEY AUTOINCREMENT,
 class_code TEXT UNIQUE NOT NULL,
 class_name TEXT NOT NULL,
 semester TEXT NOT NULL,
 course_id INTEGER NOT NULL REFERENCES courses,
 lecturer_id INTEGER NOT NULL REFERENCES lecturers);

CREATE TABLE IF NOT EXISTS class_sessions(
 class_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
 class_id INTEGER NOT NULL REFERENCES class_sections,
 session_date TEXT NOT NULL,
 start_time TEXT NOT NULL,
 end_time TEXT NOT NULL,
 room TEXT DEFAULT '',
 CHECK(end_time > start_time));

CREATE TABLE IF NOT EXISTS enrollments(
 enrollment_id INTEGER PRIMARY KEY AUTOINCREMENT,
 class_id INTEGER NOT NULL REFERENCES class_sections,
 student_id INTEGER NOT NULL REFERENCES students,
 enrolled_date TEXT,
 UNIQUE(class_id, student_id));

CREATE TABLE IF NOT EXISTS attendance_rules(
 rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
 qr_valid_minutes INTEGER NOT NULL CHECK(qr_valid_minutes > 0),
 late_threshold_minutes INTEGER NOT NULL CHECK(late_threshold_minutes >= 0),
 max_absent_rate REAL NOT NULL CHECK(max_absent_rate BETWEEN 0 AND 1),
 is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
 updated_by INTEGER REFERENCES users,
 updated_at TEXT);

CREATE TABLE IF NOT EXISTS attendance_sessions(
 att_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
 class_session_id INTEGER UNIQUE NOT NULL REFERENCES class_sessions,
 opened_by INTEGER NOT NULL REFERENCES lecturers,
 qr_token TEXT UNIQUE NOT NULL,
 opened_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 closed_at TEXT,
 is_open INTEGER NOT NULL DEFAULT 0 CHECK(is_open IN (0,1)));

CREATE TABLE IF NOT EXISTS attendance_records(
 record_id INTEGER PRIMARY KEY AUTOINCREMENT,
 att_session_id INTEGER NOT NULL REFERENCES attendance_sessions,
 student_id INTEGER NOT NULL REFERENCES students,
 status TEXT NOT NULL CHECK(status IN ('PRESENT','ABSENT','LATE','EXCUSED')),
 check_in_time TEXT,
 method TEXT NOT NULL CHECK(method IN ('QR','MANUAL')),
 note TEXT DEFAULT '',
 UNIQUE(att_session_id, student_id));

CREATE TABLE IF NOT EXISTS attendance_edit_logs(
 log_id INTEGER PRIMARY KEY AUTOINCREMENT,
 record_id INTEGER NOT NULL REFERENCES attendance_records,
 edited_by INTEGER NOT NULL REFERENCES users,
 old_status TEXT, new_status TEXT NOT NULL,
 reason TEXT NOT NULL, edited_at TEXT);

CREATE TABLE IF NOT EXISTS absence_requests(
 request_id INTEGER PRIMARY KEY AUTOINCREMENT,
 student_id INTEGER NOT NULL REFERENCES students,
 class_session_id INTEGER NOT NULL REFERENCES class_sessions,
 reviewed_by INTEGER REFERENCES users,
 reason TEXT NOT NULL,
 attachment_url TEXT DEFAULT '',
 status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
 submitted_at TEXT, processed_at TEXT,
 UNIQUE(student_id, class_session_id));

CREATE TABLE IF NOT EXISTS attendance_alerts(
 notification_id INTEGER PRIMARY KEY REFERENCES notifications,
 enrollment_id INTEGER NOT NULL REFERENCES enrollments,
 absent_rate REAL NOT NULL CHECK(absent_rate BETWEEN 0 AND 1));

-- Chỉ cho phép đúng 1 luật đang hoạt động.
-- Khi đổi luật: UPDATE luật cũ is_active=0 trước, rồi INSERT luật mới (trong cùng transaction).
CREATE UNIQUE INDEX IF NOT EXISTS one_active_rule ON attendance_rules(is_active) WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS idx_enroll_student ON enrollments(student_id);
CREATE INDEX IF NOT EXISTS idx_session_class  ON class_sessions(class_id);
CREATE INDEX IF NOT EXISTS idx_section_lect   ON class_sections(lecturer_id);
CREATE INDEX IF NOT EXISTS idx_record_student ON attendance_records(student_id);
CREATE INDEX IF NOT EXISTS idx_notif_user     ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_alert_enroll   ON attendance_alerts(enrollment_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.tx_depth = 0
    return g.db


def query(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    """Chạy 1 câu lệnh ghi, trả về lastrowid.
    Tự commit, trừ khi đang nằm trong `with transaction():`."""
    db = get_db()
    cur = db.execute(sql, args)
    if g.tx_depth == 0:
        db.commit()
    return cur.lastrowid


def execute_rc(sql, args=()):
    """Giống execute() nhưng trả về số dòng bị ảnh hưởng (dùng cho UPDATE/DELETE)."""
    db = get_db()
    cur = db.execute(sql, args)
    if g.tx_depth == 0:
        db.commit()
    return cur.rowcount


@contextmanager
def transaction():
    """Gom nhiều thao tác ghi thành 1 transaction: lỗi ở đâu thì rollback tất cả.

        with transaction():
            uid = execute("INSERT INTO users ...", (...))
            execute("INSERT INTO students ...", (uid, ...))
    """
    db = get_db()
    g.tx_depth += 1
    try:
        yield db
    except Exception:
        g.tx_depth -= 1
        if g.tx_depth == 0:
            db.rollback()
        raise
    else:
        g.tx_depth -= 1
        if g.tx_depth == 0:
            db.commit()


def _seed_defaults():
    """Luật điểm danh mặc định, để hệ thống chạy được ngay khi DB còn trống."""
    execute(
        "INSERT INTO attendance_rules(qr_valid_minutes, late_threshold_minutes, max_absent_rate, "
        "is_active, updated_at) "
        "SELECT 10, 15, 0.2, 1, ? WHERE NOT EXISTS (SELECT 1 FROM attendance_rules)",
        (now(),),
    )


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        get_db().executescript(SCHEMA)
        _seed_defaults()


def close_db(e=None):
    db = g.pop("db", None)
    g.pop("tx_depth", None)
    if db is not None:
        db.close()

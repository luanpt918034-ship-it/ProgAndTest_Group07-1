"""Attendance classes: AttendanceRule, AttendanceSession, AttendanceRecord, AttendanceEditLog, AbsenceRequest,
AttendanceAlert, Notification."""
import os, secrets, uuid
from datetime import datetime, timedelta
from flask import current_app
from database import query, execute, now, parse, AppError, FMT
from models.academic import ClassSection

STATUSES = ("PRESENT", "ABSENT", "LATE", "EXCUSED")
ALLOWED_EXT = {"pdf", "png", "jpg", "jpeg"}

class AttendanceRule:
    @staticmethod
    def get(): return query("SELECT * FROM attendance_rules WHERE is_active=1 ORDER BY rule_id DESC LIMIT 1", one=True)
    @staticmethod
    def update(admin_id, qr_minutes, late_minutes, max_rate):
        try: qr, late, mx = int(qr_minutes), int(late_minutes), float(max_rate)
        except (TypeError, ValueError): raise AppError("All rule values must be numbers.")
        if qr <= 0 or late <= 0: raise AppError("QR validity and late threshold must be greater than 0.")
        if late > qr: raise AppError("Late threshold cannot exceed QR validity.")
        if not 0 < mx <= 100: raise AppError("Maximum absence rate must be between 0 and 100.")
        execute("UPDATE attendance_rules SET is_active=0")
        execute("INSERT INTO attendance_rules(qr_valid_minutes,late_threshold_minutes,max_absent_rate,is_active,updated_by,updated_at)"
                " VALUES(?,?,?,1,?,?)", (qr, late, mx, admin_id, now()))

class Notification:
    @staticmethod
    def create(user_id, message):
        return execute("INSERT INTO notifications(user_id,message,is_read,created_at) VALUES(?,?,0,?)", (user_id, message, now()))
    @staticmethod
    def for_user(uid, unread_only=False):
        return query("SELECT * FROM notifications WHERE user_id=?" + (" AND is_read=0" if unread_only else "") +
                     " ORDER BY notification_id DESC LIMIT 50", (uid,))
    @staticmethod
    def mark_all_read(uid): execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))

class AttendanceAlert:
    @staticmethod
    def check(class_id, student_id):
        """Create an alert + notification when the absence rate exceeds the configured maximum."""
        rate, mx = ClassSection.calculate_rate(class_id, student_id), AttendanceRule.get()["max_absent_rate"]
        if rate <= mx: return None
        en = query("SELECT enrollment_id FROM enrollments WHERE class_id=? AND student_id=?", (class_id, student_id), one=True)
        last = query("SELECT MAX(absent_rate) m FROM attendance_alerts WHERE enrollment_id=?", (en["enrollment_id"],), one=True)["m"]
        if last is not None and last >= rate: return None
        cs = ClassSection.get(class_id)
        nid = Notification.create(student_id, f"Warning: your absence rate in {cs['class_name']} is {rate:.1f}%, "
                                              f"above the maximum of {mx:g}%.")
        execute("INSERT INTO attendance_alerts(notification_id,enrollment_id,absent_rate) VALUES(?,?,?)",
                (nid, en["enrollment_id"], rate))
        return nid

def _own_class_session(lecturer_id, class_session_id):
    row = query("""SELECT cs.*, c.lecturer_id FROM class_sessions cs JOIN class_sections c USING(class_id)
                   WHERE cs.class_session_id=?""", (class_session_id,), one=True)
    if not row: raise AppError("Class session not found.")
    if row["lecturer_id"] != lecturer_id: raise AppError("You can only manage your own classes.")
    return row

class AttendanceSession:
    @staticmethod
    def generate_qr(): return secrets.token_hex(32)   # 64 hex chars, unique (UQ in DB)

    @staticmethod
    def get(att_id): return query("SELECT * FROM attendance_sessions WHERE att_session_id=?", (att_id,), one=True)

    @staticmethod
    def open(lecturer_id, class_session_id):
        _own_class_session(lecturer_id, class_session_id)
        rule = AttendanceRule.get(); t = datetime.now()
        exp = (t + timedelta(minutes=rule["qr_valid_minutes"])).strftime(FMT)
        cur = query("SELECT * FROM attendance_sessions WHERE class_session_id=?", (class_session_id,), one=True)
        if cur is None:
            return execute("INSERT INTO attendance_sessions(class_session_id,opened_by,qr_token,opened_at,expires_at,is_open)"
                           " VALUES(?,?,?,?,?,1)", (class_session_id, lecturer_id, AttendanceSession.generate_qr(), t.strftime(FMT), exp))
        if cur["closed_at"]: raise AppError("Attendance for this session has already been closed.")
        if cur["is_open"] and parse(cur["expires_at"]) > t: return cur["att_session_id"]
        execute("UPDATE attendance_sessions SET qr_token=?, opened_at=?, expires_at=?, is_open=1 WHERE att_session_id=?",
                (AttendanceSession.generate_qr(), t.strftime(FMT), exp, cur["att_session_id"]))
        return cur["att_session_id"]

    @staticmethod
    def ensure(lecturer_id, class_session_id):
        """Session row used by manual marking / approved requests (created closed-less and not open)."""
        cur = query("SELECT att_session_id FROM attendance_sessions WHERE class_session_id=?", (class_session_id,), one=True)
        if cur: return cur["att_session_id"]
        return execute("INSERT INTO attendance_sessions(class_session_id,opened_by,qr_token,opened_at,expires_at,is_open)"
                       " VALUES(?,?,?,?,?,0)", (class_session_id, lecturer_id, AttendanceSession.generate_qr(), now(), now()))

    @staticmethod
    def validate_qr(token):
        row = query("SELECT * FROM attendance_sessions WHERE qr_token=?", ((token or "").strip(),), one=True)
        if not row: raise AppError("Invalid QR code.")
        if not row["is_open"]: raise AppError("This attendance session is closed.")
        if parse(row["expires_at"]) < datetime.now(): raise AppError("This QR code has expired.")
        return row

    @staticmethod
    def close(lecturer_id, att_id):
        a = AttendanceSession.get(att_id)
        if not a: raise AppError("Attendance session not found.")
        cs = _own_class_session(lecturer_id, a["class_session_id"])
        execute("UPDATE attendance_sessions SET is_open=0, closed_at=? WHERE att_session_id=?", (now(), att_id))
        n = 0
        for s in ClassSection.students(cs["class_id"]):      # students who never checked in are Absent
            if not query("SELECT 1 FROM attendance_records WHERE att_session_id=? AND student_id=?", (att_id, s["user_id"]), one=True):
                execute("INSERT INTO attendance_records(att_session_id,student_id,status,check_in_time,method,note)"
                        " VALUES(?,?,'ABSENT',NULL,'MANUAL','Auto-marked absent when session closed')", (att_id, s["user_id"]))
                AttendanceAlert.check(cs["class_id"], s["user_id"]); n += 1
        return n

class AttendanceRecord:
    @staticmethod
    def check_in(student_id, token):
        a = AttendanceSession.validate_qr(token)
        cs = query("SELECT class_id FROM class_sessions WHERE class_session_id=?", (a["class_session_id"],), one=True)
        if not query("SELECT 1 FROM enrollments WHERE class_id=? AND student_id=?", (cs["class_id"], student_id), one=True):
            raise AppError("You are not enrolled in this class.")
        if query("SELECT 1 FROM attendance_records WHERE att_session_id=? AND student_id=?", (a["att_session_id"], student_id), one=True):
            raise AppError("You have already checked in for this session.")
        late = AttendanceRule.get()["late_threshold_minutes"]
        status = "LATE" if datetime.now() > parse(a["opened_at"]) + timedelta(minutes=late) else "PRESENT"
        execute("INSERT INTO attendance_records(att_session_id,student_id,status,check_in_time,method) VALUES(?,?,?,?,'QR')",
                (a["att_session_id"], student_id, status, now()))
        return status

    @staticmethod
    def update_status(editor_id, class_session_id, student_id, new_status, reason):
        """Manual mark/edit. Every change is written to attendance_edit_logs."""
        cs = _own_class_session(editor_id, class_session_id)
        if new_status not in STATUSES: raise AppError("Invalid attendance status.")
        if not query("SELECT 1 FROM enrollments WHERE class_id=? AND student_id=?", (cs["class_id"], student_id), one=True):
            raise AppError("Student is not enrolled in this class.")
        return AttendanceRecord._upsert(editor_id, cs, student_id, new_status, (reason or "").strip())

    @staticmethod
    def _upsert(editor_id, cs, student_id, new_status, reason):
        att = AttendanceSession.ensure(cs["lecturer_id"], cs["class_session_id"])
        rec = query("SELECT * FROM attendance_records WHERE att_session_id=? AND student_id=?", (att, student_id), one=True)
        if rec and rec["status"] == new_status: raise AppError("Status is unchanged.")
        if rec:
            rid, old = rec["record_id"], rec["status"]
            execute("UPDATE attendance_records SET status=?, method='MANUAL', note=? WHERE record_id=?", (new_status, reason, rid))
        else:
            rid, old = execute("INSERT INTO attendance_records(att_session_id,student_id,status,check_in_time,method,note)"
                               " VALUES(?,?,?,?,'MANUAL',?)", (att, student_id, new_status, now(), reason)), "NONE"
        execute("INSERT INTO attendance_edit_logs(record_id,edited_by,old_status,new_status,reason,edited_at) VALUES(?,?,?,?,?,?)",
                (rid, editor_id, old, new_status, reason, now()))
        AttendanceAlert.check(cs["class_id"], student_id)
        return rid

    @staticmethod
    def logs(class_session_id):
        return query("""SELECT l.*, u.full_name AS editor, s.student_code FROM attendance_edit_logs l
            JOIN attendance_records r USING(record_id) JOIN attendance_sessions a USING(att_session_id)
            JOIN users u ON u.user_id=l.edited_by JOIN students s ON s.user_id=r.student_id
            WHERE a.class_session_id=? ORDER BY l.log_id DESC""", (class_session_id,))

    @staticmethod
    def for_session(class_session_id):
        """Every enrolled student with the current status for a class session (None if not recorded)."""
        return query("""SELECT s.user_id, s.student_code, u.full_name, r.status, r.method, r.check_in_time
            FROM class_sessions cs JOIN enrollments e USING(class_id) JOIN students s ON s.user_id=e.student_id
            JOIN users u ON u.user_id=s.user_id
            LEFT JOIN attendance_sessions a ON a.class_session_id=cs.class_session_id
            LEFT JOIN attendance_records r ON r.att_session_id=a.att_session_id AND r.student_id=s.user_id
            WHERE cs.class_session_id=? ORDER BY s.student_code""", (class_session_id,))

class AbsenceRequest:
    @staticmethod
    def submit(student_id, class_session_id, reason, file=None):
        if not (reason or "").strip(): raise AppError("A reason is required.")
        row = query("""SELECT cs.class_id FROM class_sessions cs JOIN enrollments e USING(class_id)
                       WHERE cs.class_session_id=? AND e.student_id=?""", (class_session_id, student_id), one=True)
        if not row: raise AppError("You are not enrolled in the class of this session.")
        if query("SELECT 1 FROM absence_requests WHERE student_id=? AND class_session_id=? AND status IN ('PENDING','APPROVED')",
                 (student_id, class_session_id), one=True):
            raise AppError("You already have an active request for this session.")
        url = ""
        if file is not None and file.filename:
            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in ALLOWED_EXT: raise AppError("Attachment must be a PDF, PNG or JPG file.")
            url = f"{uuid.uuid4().hex}.{ext}"
            file.save(os.path.join(current_app.config["UPLOAD_DIR"], url))
        return execute("INSERT INTO absence_requests(student_id,class_session_id,reason,attachment_url,status,submitted_at)"
                       " VALUES(?,?,?,?,'PENDING',?)", (student_id, class_session_id, reason.strip(), url, now()))

    @staticmethod
    def for_student(sid):
        return query("""SELECT ar.*, cs.session_date, c.class_name FROM absence_requests ar JOIN class_sessions cs USING(class_session_id)
            JOIN class_sections c USING(class_id) WHERE ar.student_id=? ORDER BY request_id DESC""", (sid,))

    @staticmethod
    def for_lecturer(lid, status=None):
        return query("""SELECT ar.*, cs.session_date, c.class_name, s.student_code, u.full_name FROM absence_requests ar
            JOIN class_sessions cs USING(class_session_id) JOIN class_sections c USING(class_id)
            JOIN students s ON s.user_id=ar.student_id JOIN users u ON u.user_id=ar.student_id
            WHERE c.lecturer_id=?""" + (" AND ar.status=?" if status else "") + " ORDER BY ar.request_id DESC",
                     (lid, status) if status else (lid,))

    @staticmethod
    def review(lecturer_id, request_id, approve):
        r = query("SELECT * FROM absence_requests WHERE request_id=?", (request_id,), one=True)
        if not r: raise AppError("Request not found.")
        cs = _own_class_session(lecturer_id, r["class_session_id"])
        if r["status"] != "PENDING": raise AppError("This request has already been processed.")
        status = "APPROVED" if approve else "REJECTED"
        execute("UPDATE absence_requests SET status=?, reviewed_by=?, processed_at=? WHERE request_id=?",
                (status, lecturer_id, now(), request_id))
        if approve:
            try: AttendanceRecord._upsert(lecturer_id, cs, r["student_id"], "EXCUSED", "Absence request approved")
            except AppError: pass     # already Excused
        Notification.create(r["student_id"], f"Your absence request for {cs['session_date']} was {status.lower()}.")
        return status

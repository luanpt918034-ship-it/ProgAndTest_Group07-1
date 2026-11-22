"""Academic structure: Faculty, Course, ClassSection, Session, Enrollment."""
import re
from datetime import datetime
from database import query, execute, now, AppError

def _need(**fields):
    for k, v in fields.items():
        if v is None or str(v).strip() == "": raise AppError(f"{k} is required.")

def _pos_int(v, name):
    try: n = int(v)
    except (TypeError, ValueError): raise AppError(f"{name} must be a whole number.")
    if n <= 0: raise AppError(f"{name} must be greater than 0.")
    return n

class Faculty:
    @staticmethod
    def create(name):
        _need(**{"Faculty name": name})
        if query("SELECT 1 FROM faculties WHERE faculty_name=?", (name.strip(),), one=True):
            raise AppError("Faculty already exists.")
        return execute("INSERT INTO faculties(faculty_name) VALUES(?)", (name.strip(),))
    @staticmethod
    def all(): return query("SELECT * FROM faculties ORDER BY faculty_name")

class Course:
    @staticmethod
    def create(code, name, credits, total_sessions, faculty_id):
        _need(**{"Course code": code, "Course name": name})
        credits, total = _pos_int(credits, "Credits"), _pos_int(total_sessions, "Total sessions")
        if query("SELECT 1 FROM courses WHERE course_code=?", (code.strip(),), one=True):
            raise AppError("Course code already exists.")
        if not query("SELECT 1 FROM faculties WHERE faculty_id=?", (faculty_id,), one=True):
            raise AppError("Faculty not found.")
        return execute("INSERT INTO courses(course_code,course_name,credits,total_sessions,faculty_id) VALUES(?,?,?,?,?)",
                       (code.strip(), name.strip(), credits, total, faculty_id))
    @staticmethod
    def all():
        return query("SELECT c.*, f.faculty_name FROM courses c JOIN faculties f USING(faculty_id) ORDER BY course_code")

class ClassSection:
    @staticmethod
    def create(class_code, class_name, semester, course_id, lecturer_id):
        _need(**{"Class code": class_code, "Class name": class_name, "Semester": semester})
        if query("SELECT 1 FROM class_sections WHERE class_code=?", (class_code.strip(),), one=True):
            raise AppError("Class code already exists.")
        if not query("SELECT 1 FROM courses WHERE course_id=?", (course_id,), one=True): raise AppError("Course not found.")
        if not query("SELECT 1 FROM lecturers WHERE user_id=?", (lecturer_id,), one=True): raise AppError("Lecturer not found.")
        return execute("INSERT INTO class_sections(class_code,class_name,semester,course_id,lecturer_id) VALUES(?,?,?,?,?)",
                       (class_code.strip(), class_name.strip(), semester.strip(), course_id, lecturer_id))
    _BASE = """SELECT cs.*, c.course_code, c.course_name, c.total_sessions, c.faculty_id, u.full_name AS lecturer_name,
        (SELECT COUNT(*) FROM enrollments e WHERE e.class_id=cs.class_id) AS n_students
        FROM class_sections cs JOIN courses c USING(course_id) JOIN users u ON u.user_id=cs.lecturer_id """
    @staticmethod
    def all(): return query(ClassSection._BASE + "ORDER BY cs.class_code")
    @staticmethod
    def get(class_id): return query(ClassSection._BASE + "WHERE cs.class_id=?", (class_id,), one=True)
    @staticmethod
    def for_lecturer(lid): return query(ClassSection._BASE + "WHERE cs.lecturer_id=? ORDER BY cs.class_code", (lid,))
    @staticmethod
    def for_student(sid):
        return query(ClassSection._BASE + "JOIN enrollments en ON en.class_id=cs.class_id WHERE en.student_id=? ORDER BY cs.class_code", (sid,))
    @staticmethod
    def students(class_id):
        return query("""SELECT s.user_id, s.student_code, u.full_name, e.enrollment_id FROM enrollments e
            JOIN students s ON s.user_id=e.student_id JOIN users u ON u.user_id=s.user_id
            WHERE e.class_id=? ORDER BY s.student_code""", (class_id,))
    @staticmethod
    def add_student(class_id, student_code):
        st = query("SELECT s.user_id FROM students s JOIN users u ON u.user_id=s.user_id WHERE s.student_code=?",
                   ((student_code or "").strip(),), one=True)
        if not st: raise AppError("Student not found.")
        if query("SELECT 1 FROM enrollments WHERE class_id=? AND student_id=?", (class_id, st["user_id"]), one=True):
            raise AppError("Student is already enrolled in this class.")
        return execute("INSERT INTO enrollments(class_id,student_id,enrolled_date) VALUES(?,?,?)",
                       (class_id, st["user_id"], now()[:10]))
    @staticmethod
    def remove_student(class_id, student_id):
        if query("""SELECT 1 FROM attendance_records r JOIN attendance_sessions a USING(att_session_id)
                    JOIN class_sessions cs USING(class_session_id) WHERE cs.class_id=? AND r.student_id=?""",
                 (class_id, student_id), one=True):
            raise AppError("Cannot remove a student who already has attendance records.")
        execute("DELETE FROM enrollments WHERE class_id=? AND student_id=?", (class_id, student_id))
    @staticmethod
    def calculate_rate(class_id, student_id):
        """Absence rate (%) = ABSENT records / total sessions of the course."""
        cs = ClassSection.get(class_id)
        n = query("""SELECT COUNT(*) n FROM attendance_records r JOIN attendance_sessions a USING(att_session_id)
            JOIN class_sessions s USING(class_session_id) WHERE s.class_id=? AND r.student_id=? AND r.status='ABSENT'""",
                  (class_id, student_id), one=True)["n"]
        return round(100.0 * n / cs["total_sessions"], 2)

class Session:
    @staticmethod
    def create(class_id, date, start, end, room=""):
        try:
            datetime.strptime(date, "%Y-%m-%d"); s = datetime.strptime(start, "%H:%M"); e = datetime.strptime(end, "%H:%M")
        except (TypeError, ValueError): raise AppError("Use date YYYY-MM-DD and time HH:MM.")
        if e <= s: raise AppError("End time must be after start time.")
        return execute("INSERT INTO class_sessions(class_id,session_date,start_time,end_time,room) VALUES(?,?,?,?,?)",
                       (class_id, date, start, end, room.strip()))
    @staticmethod
    def for_class(class_id):
        return query("""SELECT cs.*, a.att_session_id, a.is_open, a.closed_at, a.expires_at FROM class_sessions cs
            LEFT JOIN attendance_sessions a USING(class_session_id) WHERE cs.class_id=?
            ORDER BY session_date, start_time""", (class_id,))
    @staticmethod
    def today_for_student(sid):
        return query("""SELECT cs.*, c.class_name FROM class_sessions cs JOIN class_sections c USING(class_id)
            JOIN enrollments e ON e.class_id=c.class_id WHERE e.student_id=? AND cs.session_date=? ORDER BY start_time""",
                     (sid, now()[:10]))
    @staticmethod
    def today_for_lecturer(lid):
        return query("""SELECT cs.*, c.class_name FROM class_sessions cs JOIN class_sections c USING(class_id)
            WHERE c.lecturer_id=? AND cs.session_date=? ORDER BY start_time""", (lid, now()[:10]))

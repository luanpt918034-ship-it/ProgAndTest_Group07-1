"""Academic structure: Faculty, Course, ClassSection, Session, Enrollment."""
from datetime import datetime
from database import query, execute, now, AppError


def _need(**fields):
    """Validate that required fields are not None or empty."""
    for field_name, value in fields.items():
        if value is None or str(value).strip() == "":
            raise AppError(f"{field_name} is required.")


def _pos_int(value, name):
    """Validate and return a positive integer."""
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise AppError(f"{name} must be a whole number.")
    if num <= 0:
        raise AppError(f"{name} must be greater than 0.")
    return num


class Faculty:
    @staticmethod
    def create(name):
        _need(**{"Faculty name": name})
        clean_name = name.strip()
        if query("SELECT 1 FROM faculties WHERE faculty_name=?", (clean_name,), one=True):
            raise AppError("Faculty already exists.")
        return execute("INSERT INTO faculties(faculty_name) VALUES(?)", (clean_name,))

    @staticmethod
    def all():
        return query("SELECT * FROM faculties ORDER BY faculty_name")


class Course:
    @staticmethod
    def create(code, name, credits, total_sessions, faculty_id):
        _need(**{"Course code": code, "Course name": name})
        clean_code = code.strip()
        clean_name = name.strip()
        credits_val = _pos_int(credits, "Credits")
        total_val = _pos_int(total_sessions, "Total sessions")

        if query("SELECT 1 FROM courses WHERE course_code=?", (clean_code,), one=True):
            raise AppError("Course code already exists.")
        if not query("SELECT 1 FROM faculties WHERE faculty_id=?", (faculty_id,), one=True):
            raise AppError("Faculty not found.")

        return execute(
            "INSERT INTO courses(course_code, course_name, credits, total_sessions, faculty_id) VALUES(?, ?, ?, ?, ?)",
            (clean_code, clean_name, credits_val, total_val, faculty_id),
        )

    @staticmethod
    def all():
        return query(
            "SELECT c.*, f.faculty_name FROM courses c "
            "JOIN faculties f USING(faculty_id) ORDER BY course_code"
        )


class ClassSection:
    _BASE = """
        SELECT cs.*, c.course_code, c.course_name, c.total_sessions, c.faculty_id,
               u.full_name AS lecturer_name,
               (SELECT COUNT(*) FROM enrollments e WHERE e.class_id=cs.class_id) AS n_students
        FROM class_sections cs
        JOIN courses c USING(course_id)
        JOIN users u ON u.user_id=cs.lecturer_id
    """

    @staticmethod
    def create(class_code, class_name, semester, course_id, lecturer_id):
        _need(**{"Class code": class_code, "Class name": class_name, "Semester": semester})
        clean_code = class_code.strip()
        clean_name = class_name.strip()
        clean_semester = semester.strip()

        if query("SELECT 1 FROM class_sections WHERE class_code=?", (clean_code,), one=True):
            raise AppError("Class code already exists.")
        if not query("SELECT 1 FROM courses WHERE course_id=?", (course_id,), one=True):
            raise AppError("Course not found.")
        if not query("SELECT 1 FROM lecturers WHERE user_id=?", (lecturer_id,), one=True):
            raise AppError("Lecturer not found.")

        return execute(
            "INSERT INTO class_sections(class_code, class_name, semester, course_id, lecturer_id) "
            "VALUES(?, ?, ?, ?, ?)",
            (clean_code, clean_name, clean_semester, course_id, lecturer_id),
        )

    @staticmethod
    def all():
        return query(ClassSection._BASE + " ORDER BY cs.class_code")

    @staticmethod
    def get(class_id):
        return query(ClassSection._BASE + " WHERE cs.class_id=?", (class_id,), one=True)

    @staticmethod
    def for_lecturer(lecturer_id):
        return query(
            ClassSection._BASE + " WHERE cs.lecturer_id=? ORDER BY cs.class_code",
            (lecturer_id,),
        )

    @staticmethod
    def for_student(student_id):
        return query(
            ClassSection._BASE + " JOIN enrollments en ON en.class_id=cs.class_id "
            "WHERE en.student_id=? ORDER BY cs.class_code",
            (student_id,),
        )

    @staticmethod
    def students(class_id):
        return query(
            """
            SELECT s.user_id, s.student_code, u.full_name, e.enrollment_id
            FROM enrollments e
            JOIN students s ON s.user_id=e.student_id
            JOIN users u ON u.user_id=s.user_id
            WHERE e.class_id=?
            ORDER BY s.student_code
            """,
            (class_id,),
        )

    @staticmethod
    def add_student(class_id, student_code):
        if not ClassSection.get(class_id):
            raise AppError("Class section not found.")

        clean_code = (student_code or "").strip()
        if not clean_code:
            raise AppError("Student code is required.")

        st = query(
            "SELECT s.user_id FROM students s JOIN users u ON u.user_id=s.user_id WHERE s.student_code=?",
            (clean_code,),
            one=True,
        )
        if not st:
            raise AppError("Student not found.")

        if query(
            "SELECT 1 FROM enrollments WHERE class_id=? AND student_id=?",
            (class_id, st["user_id"]),
            one=True,
        ):
            raise AppError("Student is already enrolled in this class.")

        return execute(
            "INSERT INTO enrollments(class_id, student_id, enrolled_date) VALUES(?, ?, ?)",
            (class_id, st["user_id"], now()[:10]),
        )

    @staticmethod
    def remove_student(class_id, student_id):
        if not query(
            "SELECT 1 FROM enrollments WHERE class_id=? AND student_id=?",
            (class_id, student_id),
            one=True,
        ):
            raise AppError("Student is not enrolled in this class.")

        if query(
            """
            SELECT 1 FROM attendance_records r
            JOIN attendance_sessions a USING(att_session_id)
            JOIN class_sessions cs USING(class_session_id)
            WHERE cs.class_id=? AND r.student_id=?
            """,
            (class_id, student_id),
            one=True,
        ):
            raise AppError("Cannot remove a student who already has attendance records.")

        execute(
            "DELETE FROM enrollments WHERE class_id=? AND student_id=?",
            (class_id, student_id),
        )

    @staticmethod
    def calculate_rate(class_id, student_id):
        """Absence rate (%) = ABSENT records / total sessions of the course."""
        cs = ClassSection.get(class_id)
        if not cs:
            raise AppError("Class section not found.")

        total_sessions = cs.get("total_sessions") or 0
        if total_sessions <= 0:
            return 0.0

        absent_row = query(
            """
            SELECT COUNT(*) AS n
            FROM attendance_records r
            JOIN attendance_sessions a USING(att_session_id)
            JOIN class_sessions s USING(class_session_id)
            WHERE s.class_id=? AND r.student_id=? AND r.status='ABSENT'
            """,
            (class_id, student_id),
            one=True,
        )
        absent_count = absent_row["n"] if absent_row else 0
        return round(100.0 * absent_count / total_sessions, 2)


class Session:
    @staticmethod
    def create(class_id, date, start, end, room=""):
        if not ClassSection.get(class_id):
            raise AppError("Class section not found.")

        try:
            datetime.strptime(date, "%Y-%m-%d")
            start_dt = datetime.strptime(start, "%H:%M")
            end_dt = datetime.strptime(end, "%H:%M")
        except (TypeError, ValueError):
            raise AppError("Use date YYYY-MM-DD and time HH:MM.")

        if end_dt <= start_dt:
            raise AppError("End time must be after start time.")

        clean_room = (room or "").strip()
        return execute(
            "INSERT INTO class_sessions(class_id, session_date, start_time, end_time, room) "
            "VALUES(?, ?, ?, ?, ?)",
            (class_id, date, start, end, clean_room),
        )

    @staticmethod
    def for_class(class_id):
        return query(
            """
            SELECT cs.*, a.att_session_id, a.is_open, a.closed_at, a.expires_at
            FROM class_sessions cs
            LEFT JOIN attendance_sessions a USING(class_session_id)
            WHERE cs.class_id=?
            ORDER BY session_date, start_time
            """,
            (class_id,),
        )

    @staticmethod
    def today_for_student(student_id):
        return query(
            """
            SELECT cs.*, c.class_name
            FROM class_sessions cs
            JOIN class_sections c USING(class_id)
            JOIN enrollments e ON e.class_id=c.class_id
            WHERE e.student_id=? AND cs.session_date=?
            ORDER BY start_time
            """,
            (student_id, now()[:10]),
        )

    @staticmethod
    def today_for_lecturer(lecturer_id):
        return query(
            """
            SELECT cs.*, c.class_name
            FROM class_sessions cs
            JOIN class_sections c USING(class_id)
            WHERE c.lecturer_id=? AND cs.session_date=?
            ORDER BY start_time
            """,
            (lecturer_id, now()[:10]),
        )

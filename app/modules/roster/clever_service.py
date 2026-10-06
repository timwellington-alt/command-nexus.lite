"""
Clever CSV parsing, roster diff, email compliance, and NutriKids export.

Ported from v1 clever.py with these v2 improvements:
- Student email format is configurable via Settings (no hardcoded domain)
- School code mapping comes from Settings, not hardcoded dicts
- All pure functions — no DB access, no side effects

Settings required (roster.* namespace):
- roster.student_email_domain     e.g. "yourdomain.org"
- roster.student_email_format     e.g. "{last}{first1}{year2}" or "{last}{first2}{year2}"
- roster.school_code_map          JSON: {"clever_id": "CODE", ...}
"""

import csv
import io
import json
import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV type detection and parsing
# ---------------------------------------------------------------------------

def detect_csv_type(headers: list[str]) -> str:
    """Return 'sections', 'staff', 'admins', 'enrollments', or 'students' based on CSV headers."""
    stripped = {h.strip() for h in headers}
    # Sections have Section_id + Teacher_id + Course_name (distinguish from staff)
    if "Section_id" in stripped and "Teacher_id" in stripped and "Course_name" in stripped:
        return "sections"
    # Admin staff (Clever-Admins) — has Admin_email or Staff_email
    if "Admin_email" in stripped or "Staff_email" in stripped:
        return "admins"
    if "Teacher_id" in stripped and "Teacher_email" in stripped:
        return "staff"
    if stripped >= {"School_id", "Section_id", "Student_id"} and "Teacher_id" not in stripped:
        return "enrollments"
    # Fallback: if it has Teacher_id but no Teacher_email, it's likely sections
    if "Teacher_id" in stripped and "Teacher_email" not in stripped:
        return "sections"
    return "students"


def parse_students_csv(content: str) -> list[dict]:
    """
    Parse a Clever student CSV. Supports two shapes:

      * SIS→Clever MetaSolutions email format — Title_Case with
        underscores (`Student_id`, `First_name`, `Student_email`,
        `PrimaryContactFirstName`, etc.). This is the daily automated
        feed we process in `poll_clever_imports`.

      * Clever dashboard/platform export — lowercase, dot-notation
        (`sis_id`, `name.first`, `email`, `location.city`, etc.).
        This is what an operator downloads from Clever's admin UI
        for the manual `/roster/clever-verify` upload.

    Normalized output keys are the lowercase snake_case names the rest
    of the codebase already uses (sis_id, first_name, last_name, email,
    grade, school). Original row keys are preserved under the same
    dict so downstream code that reaches for e.g. `Student_email` still
    works on the MetaSolutions path.

    Deduplicates by SIS ID keeping the first occurrence. Contact rows
    (MetaSolutions format only) are collected into a `_contacts` sub-list.
    """
    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    if not rows:
        return []

    def _first(row: dict, *keys: str) -> str:
        """Return the first non-empty value across candidate column names."""
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    seen: dict[str, dict] = {}
    for row in rows:
        # SIS ID resolution — prefer explicit `sis_id`, then MetaSolutions
        # `Student_id`, then Clever's `student_number` (rarely the primary
        # but present in some exports). NEVER use Clever's `_id` — that's
        # Clever's internal platform identifier and not portable across
        # systems.
        sid = _first(row, "sis_id", "Student_id", "student_number")
        if not sid:
            continue

        if sid not in seen:
            normalized = {
                # Preserve every original column so downstream code that
                # already knows a specific header still finds it
                **row,
                # Normalized snake_case aliases downstream code uses
                "sis_id":       sid,
                "student_id":   sid,
                "first_name":   _first(row, "First_name", "name.first", "first_name"),
                "last_name":    _first(row, "Last_name",  "name.last",  "last_name"),
                "middle_name":  _first(row, "Middle_name", "name.middle", "middle_name"),
                "email":        _first(row, "Student_email", "email"),
                "grade":        _first(row, "Grade", "grade"),
                "school":       _first(row, "School", "School_id", "school"),
                "dob":          _first(row, "DOB", "Dob", "dob"),
                "_contacts":    [],
            }
            seen[sid] = normalized

        # MetaSolutions-format contact rows (one row per contact).
        # Clever dashboard exports don't repeat like this, so this
        # block silently no-ops on that path.
        contact = {
            "title":         row.get("PrimaryContactTitle", ""),
            "last_name":     row.get("PrimaryContactLastName", ""),
            "suffix":        row.get("PrimaryContactNameSuffix", ""),
            "street":        row.get("PrimaryContactStreet", ""),
            "street2":       row.get("PrimaryContactStreet2", ""),
            "city":          row.get("PrimaryContactCity", ""),
            "state":         row.get("PrimaryContactState", ""),
            "zip":           row.get("PrimaryContactZip", ""),
            "home_phone":    row.get("PrimaryContactHomePhone", ""),
            "mobile_phone":  row.get("PrimaryContactMobilePhone", ""),
            "email":         row.get("PrimaryContactEmail", ""),
        }
        if any(contact.values()):
            seen[sid]["_contacts"].append(contact)

    return list(seen.values())


def parse_enrollments_csv(content: str) -> list[dict]:
    """Parse a Clever-Enrollments CSV. Returns list of {school_id, section_id, student_id}."""
    reader = csv.DictReader(io.StringIO(content))
    return [
        {
            "school_id": row.get("School_id", "").strip(),
            "section_id": row.get("Section_id", "").strip(),
            "student_id": row.get("Student_id", "").strip(),
        }
        for row in reader
        if row.get("Student_id", "").strip()
    ]


def parse_staff_csv(content: str) -> list[dict]:
    """Parse a Clever staff/admin CSV. Works with Teachers, Admins, and Staff reports."""
    reader = csv.DictReader(io.StringIO(content))
    results = []
    for row in reader:
        # Accept any row with an ID field (Teacher_id, Admin_id, Staff_id)
        has_id = (
            row.get("Teacher_id", "").strip()
            or row.get("Admin_id", "").strip()
            or row.get("Staff_id", "").strip()
        )
        if has_id:
            results.append(row)
    return results


def extract_teacher_id_from_section(section_id: str, teacher_ids: set[str]) -> str | None:
    """
    Find which Teacher_id is a suffix of the section_id (case-insensitive).
    Returns the longest match to handle IDs that are prefixes of each other.
    """
    sid_lower = section_id.lower()
    candidates = sorted(
        [tid for tid in teacher_ids if sid_lower.endswith(tid.lower())],
        key=len, reverse=True,
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Roster diff
# ---------------------------------------------------------------------------

def _get_sid(s: dict) -> str:
    """Extract student ID from either Clever CSV dict or DB snapshot dict."""
    return s.get("Student_id") or s.get("sis_id") or ""


def _get_school(s: dict) -> str:
    """Extract school from either Clever CSV dict or DB snapshot dict."""
    return s.get("School_id") or s.get("school") or ""


def _get_grade(s: dict) -> str:
    """Extract grade from either Clever CSV dict or DB snapshot dict."""
    return s.get("Grade") or s.get("grade") or ""


def _get_first(s: dict) -> str:
    """Extract first name from either Clever CSV dict or DB snapshot dict."""
    return s.get("First_name") or s.get("first_name") or ""


def _get_last(s: dict) -> str:
    """Extract last name from either Clever CSV dict or DB snapshot dict."""
    return s.get("Last_name") or s.get("last_name") or ""


def _get_email(s: dict) -> str:
    """Extract email from either dict format."""
    return s.get("Student_email") or s.get("email") or ""


def _student_full_name(s: dict) -> str:
    """Full name from either dict format."""
    return f"{_get_first(s)} {_get_last(s)}".strip()


def diff_roster(
    old_students: list[dict],
    new_students: list[dict],
    school_code_map: dict[str, str] | None = None,
) -> list[dict]:
    """
    Compare two student lists (each a list of dicts keyed by Student_id or sis_id).
    Supports both Clever CSV dicts and DB snapshot dicts.
    Returns list of change dicts: {student_id, change_type, student_name, school_code, details}.
    """
    old_map = {_get_sid(s): s for s in old_students if _get_sid(s)}
    new_map = {_get_sid(s): s for s in new_students if _get_sid(s)}

    changes = []

    # New enrollments
    for sid, student in new_map.items():
        if sid not in old_map:
            school = _get_school(student)
            grade = _get_grade(student)
            email = _get_email(student)
            changes.append({
                "student_id": sid,
                "change_type": "added",
                "student_name": _student_full_name(student),
                "school_code": _school_code(student, school_code_map),
                "details": json.dumps({
                    "school": school,
                    "grade": grade,
                    "email": email,
                    "first_name": _get_first(student),
                    "last_name": _get_last(student),
                    "student_data": {
                        "sis_id": sid,
                        "first_name": _get_first(student),
                        "last_name": _get_last(student),
                        "school": school,
                        "grade": grade,
                        "email": email,
                    },
                }),
            })

    # Withdrawals (handled by mark_students_inactive, but included for completeness)
    for sid, student in old_map.items():
        if sid not in new_map:
            changes.append({
                "student_id": sid,
                "change_type": "removed",
                "student_name": _student_full_name(student),
                "school_code": _school_code(student, school_code_map),
                "details": json.dumps({
                    "last_school": _get_school(student),
                    "email": _get_email(student),
                }),
            })

    # Modifications — check for transfers, grade changes, name changes
    for sid, new_s in new_map.items():
        if sid not in old_map:
            continue
        old_s = old_map[sid]
        diffs = {}

        old_school = _get_school(old_s)
        new_school = _get_school(new_s)
        if old_school and new_school and old_school != new_school:
            diffs["school"] = {"from": old_school, "to": new_school}

        old_grade = _get_grade(old_s)
        new_grade = _get_grade(new_s)
        if old_grade != new_grade:
            diffs["grade"] = {"from": old_grade, "to": new_grade}

        old_name = _student_full_name(old_s)
        new_name = _student_full_name(new_s)
        if old_name != new_name:
            diffs["name"] = {"from": old_name, "to": new_name}

        if not diffs:
            continue

        # A student can have multiple change types — emit one per type
        # Priority: transferred > grade_change > name_change
        if "school" in diffs:
            changes.append({
                "student_id": sid,
                "change_type": "transferred",
                "student_name": new_name,
                "school_code": _school_code(new_s, school_code_map),
                "details": json.dumps({
                    "school": diffs["school"],
                    "email": _get_email(new_s),
                    **{k: v for k, v in diffs.items() if k != "school"},
                }),
            })

        if "grade" in diffs and "school" not in diffs:
            changes.append({
                "student_id": sid,
                "change_type": "grade_change",
                "student_name": new_name,
                "school_code": _school_code(new_s, school_code_map),
                "details": json.dumps({"grade": diffs["grade"]}),
            })

        if "name" in diffs and "school" not in diffs:
            changes.append({
                "student_id": sid,
                "change_type": "name_change",
                "student_name": new_name,
                "school_code": _school_code(new_s, school_code_map),
                "details": json.dumps({"name": diffs["name"]}),
            })

    return changes


# ---------------------------------------------------------------------------
# Email compliance
# ---------------------------------------------------------------------------

# Clever grade -> years until graduation from current school year start
_GRADE_TO_YEARS = {
    "PS": 14, "PK": 14, "KG": 13, "K": 13,
    "1": 12, "2": 11, "3": 10, "4": 9, "5": 8,
    "6": 7, "7": 6, "8": 5, "9": 4, "10": 3, "11": 2, "12": 1,
}


def expected_grad_year(grade: str) -> int | None:
    """
    Compute expected graduation year from grade level.

    Standard K-12 grades map through _GRADE_TO_YEARS. Guidance also
    uses two-digit "grade" codes as graduation years for extended-
    enrollment students (super-seniors, extended-IEP, adult-ed) —
    e.g. grade '23' means grad year 2023. Anything that looks like a
    two-digit year (20-99) gets interpreted that way so emails still
    generate (`kigarz23@` was created under this convention pre-Nexus).

    School-year boundary: SIS rolls grades over at end-of-year (usually
    late June). Anytime from July onward, a student labeled "3" in SIS
    means "3rd grader in the upcoming fall term" — not "3rd grader
    during the year that just ended." Using September as the boundary
    would mis-compute grad_year by 1 across the whole district for the
    entire summer.
    """
    today = date.today()
    school_year_start = today.year if today.month >= 7 else today.year - 1
    g = grade.strip().upper()
    # Zero-padded single-digit grades from MetaSolutions ("05" for 5th,
    # "01" for 1st, etc.) would otherwise fall through to the two-digit
    # grad-year shorthand below and compute a grad year of 2005 for a
    # current 5th grader — which then produces bogus expected emails
    # and mass-tags legit students as year_mismatch. Strip a leading
    # zero from ANY two-char numeric code that starts with '0' before
    # hitting the map. Preserves "10", "11", "12" untouched.
    if len(g) == 2 and g[0] == "0" and g[1:].isdigit():
        g = g[1:]
    years = _GRADE_TO_YEARS.get(g)
    if years is not None:
        return school_year_start + years
    # Two-digit grad-year shorthand: '23' → 2023, '95' → 1995 (unlikely
    # but harmless). Filter to numeric-only + length 2 to avoid
    # accidentally matching K-12 codes like '10' or '12'.
    if g.isdigit() and len(g) == 2:
        century = 2000 if int(g) < 50 else 1900
        return century + int(g)
    return None


def grad_year_to_grade_label(grad_year: int) -> str:
    """Convert a 4-digit graduation year back to a human-readable grade label."""
    today = date.today()
    school_year_start = today.year if today.month >= 7 else today.year - 1
    years = grad_year - school_year_start
    mapping = {
        1: "12", 2: "11", 3: "10", 4: "9", 5: "8", 6: "7", 7: "6",
        8: "5", 9: "4", 10: "3", 11: "2", 12: "1", 13: "K", 14: "PK",
    }
    return mapping.get(years, f"Grad {str(grad_year)[-2:]}")


def build_expected_email(
    last_name: str,
    first_name: str,
    grad_year: int,
    *,
    domain: str,
    disambiguate: bool = False,
) -> str:
    """
    Build the expected district email address.

    Default format (configurable via Settings):
      Primary:        lastnamefirstinitial{2-digit-year}@domain
      Disambiguated:  lastnamefirst2letters{2-digit-year}@domain

    The domain parameter should come from Settings: roster.student_email_domain.
    """
    last_clean = re.sub(r"[^a-z]", "", last_name.strip().lower())
    first_clean = re.sub(r"[^a-z]", "", first_name.strip().lower())
    grad_str = str(grad_year)[-2:]
    if not first_clean:
        first_clean = "x"
    if disambiguate and len(first_clean) >= 2:
        local = f"{last_clean}{first_clean[:2]}{grad_str}"
    else:
        local = f"{last_clean}{first_clean[0]}{grad_str}"
    return f"{local}@{domain}"


def check_email_compliance(
    student: dict,
    ignored_emails: set[str],
    *,
    domain: str,
) -> dict:
    """
    Check whether a student's SIS email matches the district naming convention.

    Valid patterns:
      - Primary:       lastnamefirstinitial{2-digit-year}[@domain]
      - Disambiguated: lastnamefirst2letters{2-digit-year}[@domain]
      - Either may have a numeric suffix after the year (e.g. smithj262)
      - +/-2 year drift tolerated for retained students

    Returns {compliant: bool, expected: str, actual: str, reason: str | None}.
    """
    actual_email = student.get("Student_email", "").strip().lower()
    last_name = re.sub(r"[^a-z]", "", student.get("Last_name", "").strip().lower())
    first_name = re.sub(r"[^a-z]", "", student.get("First_name", "").strip().lower())
    grade = student.get("Grade", "")
    grad_year = expected_grad_year(grade)

    if not last_name or not first_name or grad_year is None:
        return {"compliant": None, "expected": None, "actual": actual_email, "reason": "insufficient_data"}

    primary_expected = build_expected_email(last_name, first_name, grad_year, domain=domain)
    grad_str = str(grad_year)[-2:]

    if actual_email in ignored_emails:
        return {"compliant": True, "expected": None, "actual": actual_email, "reason": "ignored"}

    domain_suffix = f"@{domain}"
    if not actual_email.endswith(domain_suffix):
        return {"compliant": False, "expected": primary_expected,
                "actual": actual_email, "reason": "wrong_domain"}

    local = actual_email.split("@")[0]
    local_normalized = re.sub(r"[^a-z0-9]", "", local)

    primary_prefix = f"{last_name}{first_name[0]}"
    disambig_prefix = f"{last_name}{first_name[:2]}" if len(first_name) >= 2 else primary_prefix

    locals_to_check = [local] if local == local_normalized else [local, local_normalized]
    for local_part in locals_to_check:
        for prefix in [primary_prefix, disambig_prefix]:
            pattern = rf"^{re.escape(prefix)}(\d{{2}})(\d*)$"
            match = re.match(pattern, local_part)
            if match:
                email_year = int(match.group(1))
                expected_year_int = int(grad_str)
                if abs(email_year - expected_year_int) <= 2:
                    return {"compliant": True, "expected": primary_expected,
                            "actual": actual_email, "reason": None}
                else:
                    return {"compliant": False, "expected": primary_expected,
                            "actual": actual_email, "reason": "year_mismatch"}

    if last_name and last_name not in local_normalized:
        return {"compliant": False, "expected": primary_expected,
                "actual": actual_email, "reason": "name_mismatch"}

    return {"compliant": False, "expected": primary_expected,
            "actual": actual_email, "reason": "format_mismatch"}


# ---------------------------------------------------------------------------
# NutriKids export
# ---------------------------------------------------------------------------

NUTRIKIDS_HEADERS = [
    "FirstName", "LastName", "MI", "StudentNumber", "AltID", "BirthDate",
    "Homeroom", "HomeroomCode", "HomeroomTeacher", "BuildingGrade",
    "PrimaryBuilding", "PrimaryBuildingCode",
    "PrimaryContactTitle", "PrimaryContactLastName", "PrimaryContactNameSuffix", "",
    "PrimaryContactStreet", "PrimaryContactStreet2", "PrimaryContactCity",
    "PrimaryContactState", "PrimaryContactZip", "PrimaryContactHomePhone",
    "PrimaryContactMobilePhone", "PrimaryContactEmail", "Eligibility",
]


def build_nutrikids_rows(student: dict) -> list[list[str]]:
    """
    Build one or more NutriKids CSV rows for a student (one per contact).
    AltID = last 4 digits of StudentNumber. Eligibility = F.
    """
    student_number = student.get("Student_id", "")
    alt_id = student_number[-4:] if len(student_number) >= 4 else student_number

    base = [
        student.get("First_name", ""),
        student.get("Last_name", ""),
        student.get("Middle_name", "")[:1] if student.get("Middle_name") else "",
        student_number,
        alt_id,
        student.get("DOB", ""),
        student.get("Homeroom", ""),
        student.get("Homeroom_id", ""),
        student.get("Teacher_display_name", ""),
        student.get("Grade", ""),
        student.get("School_name", ""),
        student.get("School_id", ""),
    ]

    contacts = student.get("_contacts") or [{}]
    rows = []
    for c in contacts:
        row = base + [
            c.get("title", ""),
            c.get("last_name", ""),
            c.get("suffix", ""),
            "",  # blank column in spec
            c.get("street", ""),
            c.get("street2", ""),
            c.get("city", ""),
            c.get("state", ""),
            c.get("zip", ""),
            c.get("home_phone", ""),
            c.get("mobile_phone", ""),
            c.get("email", ""),
            "F",
        ]
        rows.append(row)
    return rows


def generate_nutrikids_csv(students: list[dict]) -> str:
    """Generate a complete NutriKids CSV from a list of student dicts."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(NUTRIKIDS_HEADERS)
    for student in students:
        for row in build_nutrikids_rows(student):
            writer.writerow(row)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _full_name(student: dict) -> str:
    first = student.get("First_name", "")
    last = student.get("Last_name", "")
    return f"{first} {last}".strip()


def _school_code(student: dict, code_map: dict[str, str] | None = None) -> str:
    """
    Map a Clever School_id to a building code.

    Uses code_map from Settings if provided, otherwise falls back to
    uppercased first-4-chars of the Clever ID.
    """
    school_id = student.get("School_id", "")
    if code_map:
        mapped = code_map.get(school_id) or code_map.get(school_id.lower())
        if mapped:
            return mapped
    return school_id.upper()[:4]

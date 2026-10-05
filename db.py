import sqlite3
import os
import json
import pandas as pd

# 클라우드 배포 시(예: Railway) 코드가 재배포될 때마다 로컬 파일시스템이 초기화될 수 있으므로,
# DATA_DIR 환경변수가 설정되어 있으면 그 경로(영구 볼륨을 마운트한 위치)에 DB를 저장하고,
# 없으면 예전처럼 코드 옆에 저장한다(로컬 개발 시 동작은 그대로 유지).
_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "collect.db")

# 최초 설치 시(question_presets가 비어있을 때) 시드로 넣어줄 기본 문항.
# 기존 questions.py에 하드코딩돼 있던 값과 동일 — 마이그레이션해도 동작이 바뀌지 않도록.
DEFAULT_PEER_QUESTIONS = [
    "맡은 일을 잘한다",
    "함께 일하기 좋다",
    "지속적으로 성장하고 있다",
    "우리 조직에 충분히 기여한다",
    "앞으로도 계속해서 일하고 싶다",
]
DEFAULT_LEADER_QUESTIONS = [
    "업무 전문성이 높고 판단력이 명확하다",
    "목표와 우선순위를 분명하게 제시한다",
    "필요한 지원과 성장을 돕는 피드백을 준다",
    "팀원의 의견을 경청하고 소통이 원활하다",
    "리더로서 신뢰하며 계속 함께 일하고 싶다",
]
DEFAULT_SCALE_MAX = 6

DEFAULT_BRAND_SUBTITLE = "2026년 상반기 · Genians 인사총무팀"
DEFAULT_CREDIT_LINE = "🔧 In-house Build · Genians 인사총무팀"

# 문항 관리 화면에서 항목당 등록 가능한 최대 문항 수 (화면 과다 길어짐/대량 입력 방지용 안전장치)
MAX_QUESTIONS_PER_SET = 20


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_exists(conn, table, column):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return column in cols


def init_db():
    conn = get_conn()
    # 아래 마이그레이션 블록에서 employees 테이블을 통째로 재생성할 수 있으므로,
    # 그 사이 FK 제약으로 인한 오류를 피하기 위해 잠시 꺼둔다(작업 끝나면 다시 켠다).
    conn.execute("PRAGMA foreign_keys = OFF")

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        closed_at TEXT,
        is_current INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES rounds(id),
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        org_group TEXT,
        team TEXT,
        job_family TEXT,
        grade TEXT,
        role TEXT CHECK(role IN ('팀원','팀장')),
        is_admin INTEGER DEFAULT 0,
        UNIQUE(email, round_id)
    );

    CREATE TABLE IF NOT EXISTS mappings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL REFERENCES rounds(id),
        target_id INTEGER NOT NULL REFERENCES employees(id),
        writer_id INTEGER NOT NULL REFERENCES employees(id),
        relation_type TEXT NOT NULL,
        overall_comment TEXT,
        UNIQUE(target_id, writer_id, relation_type)
    );

    CREATE TABLE IF NOT EXISTS responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mapping_id INTEGER NOT NULL REFERENCES mappings(id),
        question_index INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        score INTEGER,           -- NULL이면 N/A
        comment TEXT,
        submitted_at TEXT DEFAULT (datetime('now')),
        UNIQUE(mapping_id, question_index)
    );

    CREATE TABLE IF NOT EXISTS question_presets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        peer_questions TEXT NOT NULL,    -- JSON 배열
        leader_questions TEXT NOT NULL,  -- JSON 배열
        scale_max INTEGER NOT NULL DEFAULT 6,
        is_active INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS access_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_email TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.commit()

    # 회차가 하나도 없으면(최초 설치) 1차 회차를 만들어 활성화한다.
    row = conn.execute("SELECT COUNT(*) as c FROM rounds").fetchone()
    if row["c"] == 0:
        conn.execute("INSERT INTO rounds (name, is_current) VALUES (?, 1)", ("1차 다면평가",))
        conn.commit()
    current_round_id = conn.execute("SELECT id FROM rounds WHERE is_current=1").fetchone()["id"]

    # --- 마이그레이션: '회차' 개념 도입 이전 DB 호환 ---
    # employees.email이 단독 UNIQUE였던 옛 스키마는 회차별 이력을 담을 수 없으므로,
    # (email, round_id) 복합 UNIQUE로 테이블을 재생성하고 기존 데이터는 모두 1차 회차로 이관한다.
    # (id는 그대로 보존하므로 mappings.target_id/writer_id 참조는 깨지지 않는다.)
    if not _column_exists(conn, "employees", "round_id"):
        conn.executescript("ALTER TABLE employees RENAME TO employees_old;")
        conn.executescript("""
        CREATE TABLE employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL REFERENCES rounds(id),
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            org_group TEXT,
            team TEXT,
            job_family TEXT,
            grade TEXT,
            role TEXT CHECK(role IN ('팀원','팀장')),
            is_admin INTEGER DEFAULT 0,
            UNIQUE(email, round_id)
        );
        """)
        conn.execute("""
            INSERT INTO employees (id, round_id, name, email, org_group, team, job_family, grade, role, is_admin)
            SELECT id, ?, name, email, org_group, team, job_family, grade, role, is_admin FROM employees_old
        """, (current_round_id,))
        conn.execute("DROP TABLE employees_old;")
        conn.commit()

    if not _column_exists(conn, "mappings", "round_id"):
        conn.execute("ALTER TABLE mappings ADD COLUMN round_id INTEGER")
        conn.execute("UPDATE mappings SET round_id=? WHERE round_id IS NULL", (current_round_id,))
        conn.commit()

    # 마이그레이션 가드: overall_comment 컬럼 추가 전에 만들어진 기존 collect.db도
    # 삭제/재생성 없이 그대로 이어서 쓸 수 있도록 조용히 한 번만 컬럼을 보강한다.
    if not _column_exists(conn, "mappings", "overall_comment"):
        conn.execute("ALTER TABLE mappings ADD COLUMN overall_comment TEXT")
        conn.commit()

    conn.execute("PRAGMA foreign_keys = ON")

    # 문항 프리셋이 하나도 없으면(최초 설치 or 마이그레이션 직후) 기존 하드코딩 값으로 시드
    row = conn.execute("SELECT COUNT(*) as c FROM question_presets").fetchone()
    if row["c"] == 0:
        conn.execute(
            "INSERT INTO question_presets (name, peer_questions, leader_questions, scale_max, is_active) VALUES (?,?,?,?,1)",
            ("기본 문항", json.dumps(DEFAULT_PEER_QUESTIONS, ensure_ascii=False),
             json.dumps(DEFAULT_LEADER_QUESTIONS, ensure_ascii=False), DEFAULT_SCALE_MAX),
        )
        conn.commit()

    conn.close()


# ---------------------------------------------------------------
# 회차 (survey round)
# ---------------------------------------------------------------
def get_current_round():
    conn = get_conn()
    row = conn.execute("SELECT * FROM rounds WHERE is_current=1").fetchone()
    conn.close()
    return dict(row) if row else None


def get_current_round_id():
    r = get_current_round()
    return r["id"] if r else None


def list_rounds():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM rounds ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def start_new_round(name):
    """현재 회차를 '마감' 처리하고 새 회차를 시작한다.
    기존 인원/매핑/응답/총평은 절대로 삭제하지 않고 그대로 이력으로 남겨두며,
    새 회차는 로스터 업로드 전의 완전히 빈 상태로 시작한다.
    (문항 프리셋 / 관리자 접근 로그는 회차와 무관한 전역 데이터라 그대로 유지된다.)
    평가 마감일은 회차마다 새로 정하는 값이므로 새 회차 시작과 함께 초기화한다."""
    conn = get_conn()
    conn.execute("UPDATE rounds SET is_current=0, closed_at=datetime('now') WHERE is_current=1")
    cur = conn.execute("INSERT INTO rounds (name, is_current) VALUES (?, 1)", (name,))
    new_round_id = cur.lastrowid
    conn.commit()
    conn.close()
    set_setting("deadline", "")
    return new_round_id


# ---------------------------------------------------------------
# 직원 / 매핑 (모두 '현재 회차' 기준으로 동작 — round_id 생략 시 자동으로 현재 회차)
# ---------------------------------------------------------------
def upsert_employee(name, email, org_group, team, job_family, grade, role, is_admin=0, round_id=None):
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    row = conn.execute("SELECT id FROM employees WHERE email=? AND round_id=?", (email, round_id)).fetchone()
    if row:
        conn.execute(
            "UPDATE employees SET name=?, org_group=?, team=?, job_family=?, grade=?, role=? WHERE id=?",
            (name, org_group, team, job_family, grade, role, row["id"]),
        )
        emp_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO employees (round_id, name, email, org_group, team, job_family, grade, role, is_admin) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (round_id, name, email, org_group, team, job_family, grade, role, is_admin),
        )
        emp_id = cur.lastrowid
    conn.commit()
    conn.close()
    return emp_id


def get_employee_by_email(email, round_id=None):
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    row = conn.execute("SELECT * FROM employees WHERE email=? AND round_id=?", (email, round_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_employee(emp_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_employees(round_id=None):
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM employees WHERE round_id=? ORDER BY org_group, team, role DESC, name", (round_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_known_people():
    """전 회차 통틀어 알려진 모든 사람(이름/이메일)을 이력 조회 검색용으로 제공."""
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT name, email FROM employees ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_admin(email, round_id=None):
    """지정한 이메일 계정에 관리자 권한을 부여한다 (해당 회차에 계정이 없으면 아무 일도 하지 않음)."""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    conn.execute("UPDATE employees SET is_admin=1 WHERE email=? AND round_id=?", (email, round_id))
    conn.commit()
    conn.close()


def add_mapping(target_id, writer_id, relation_type, round_id=None):
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO mappings (round_id, target_id, writer_id, relation_type) VALUES (?,?,?,?)",
            (round_id, target_id, writer_id, relation_type),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_mapping_keys(round_id=None):
    """(대상 이메일, 작성자 이메일, 관계유형) 기준으로 현재 회차의 모든 매핑과 응답 수를 함께 조회.
    로스터 재업로드 시 '더 이상 유효하지 않은 매핑'을 찾아내는 데 사용."""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    rows = conn.execute("""
        SELECT m.id as mapping_id, m.relation_type, e.name as target_name, e.email as target_email,
               w.name as writer_name, w.email as writer_email,
               (SELECT COUNT(*) FROM responses r WHERE r.mapping_id = m.id) as response_count
        FROM mappings m
        JOIN employees e ON e.id = m.target_id
        JOIN employees w ON w.id = m.writer_id
        WHERE m.round_id = ?
    """, (round_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_mapping(mapping_id):
    """응답이 하나도 없는 매핑만 안전하게 삭제한다. 응답이 있으면 아무 것도 하지 않고 False를 반환."""
    conn = get_conn()
    cnt = conn.execute("SELECT COUNT(*) as c FROM responses WHERE mapping_id=?", (mapping_id,)).fetchone()["c"]
    if cnt > 0:
        conn.close()
        return False
    conn.execute("DELETE FROM mappings WHERE id=?", (mapping_id,))
    conn.commit()
    conn.close()
    return True


# ---------------------------------------------------------------
# 문항 관리 (프리셋) — 회차와 무관한 전역 데이터
# ---------------------------------------------------------------
def get_active_questions():
    conn = get_conn()
    row = conn.execute("SELECT * FROM question_presets WHERE is_active=1 LIMIT 1").fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"], "name": row["name"],
        "peer": json.loads(row["peer_questions"]),
        "leader": json.loads(row["leader_questions"]),
        "scale_max": row["scale_max"],
    }


def list_question_presets():
    conn = get_conn()
    rows = conn.execute("SELECT id, name, scale_max, is_active, created_at FROM question_presets ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_question_preset(preset_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM question_presets WHERE id=?", (preset_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"], "name": row["name"],
        "peer": json.loads(row["peer_questions"]),
        "leader": json.loads(row["leader_questions"]),
        "scale_max": row["scale_max"],
    }


def save_question_preset(name, peer_questions, leader_questions, scale_max):
    """이름이 같은 프리셋이 있으면 내용을 덮어쓰고, 없으면 새로 만든다.
    두 경우 모두 저장 직후 이 프리셋을 '활성' 상태로 전환한다.
    문항 개수는 항목당 MAX_QUESTIONS_PER_SET개로 안전하게 자른다(호출 측에서도 검증하지만 이중 방어)."""
    peer_questions = list(peer_questions)[:MAX_QUESTIONS_PER_SET]
    leader_questions = list(leader_questions)[:MAX_QUESTIONS_PER_SET]
    conn = get_conn()
    peer_json = json.dumps(peer_questions, ensure_ascii=False)
    leader_json = json.dumps(leader_questions, ensure_ascii=False)
    row = conn.execute("SELECT id FROM question_presets WHERE name=?", (name,)).fetchone()
    conn.execute("UPDATE question_presets SET is_active=0")
    if row:
        conn.execute(
            "UPDATE question_presets SET peer_questions=?, leader_questions=?, scale_max=?, is_active=1 WHERE id=?",
            (peer_json, leader_json, scale_max, row["id"]),
        )
        preset_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO question_presets (name, peer_questions, leader_questions, scale_max, is_active) VALUES (?,?,?,?,1)",
            (name, peer_json, leader_json, scale_max),
        )
        preset_id = cur.lastrowid
    conn.commit()
    conn.close()
    return preset_id


def activate_question_preset(preset_id):
    conn = get_conn()
    conn.execute("UPDATE question_presets SET is_active=0")
    conn.execute("UPDATE question_presets SET is_active=1 WHERE id=?", (preset_id,))
    conn.commit()
    conn.close()


def relation_questionnaire():
    """관계유형 -> 사용할 문항 리스트. 현재 '활성' 문항 세트를 기준으로 매번 새로 계산한다
    (questions.py에 하드코딩하던 것을 DB 기반으로 전환 — 관리자가 언제든 바꿀 수 있게)."""
    active = get_active_questions()
    peer = active["peer"]
    leader = active["leader"]
    return {
        "본인평가": peer,
        "동료평가": peer,
        "팀장평가(팀원이줌)": leader,
        "본인평가(팀장)": leader,
        "팀장간평가": peer,
    }


def has_any_responses(round_id=None):
    """문항을 바꾸기 전에 '현재 회차에 이미 제출된 응답이 있는지' 경고하기 위한 헬퍼."""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    cnt = conn.execute("""
        SELECT COUNT(*) as c FROM responses r JOIN mappings m ON m.id = r.mapping_id WHERE m.round_id=?
    """, (round_id,)).fetchone()["c"]
    conn.close()
    return cnt > 0


# ---------------------------------------------------------------
# 설정 (평가 마감일, 브랜드 문구 등 단순 key-value) — 회차와 무관한 전역 데이터
# (deadline은 start_new_round()가 새 회차 시작 시 자동으로 비워준다)
# ---------------------------------------------------------------
def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------
# 관리자 접근 로그
# ---------------------------------------------------------------
def log_access(admin_email, action, detail=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO access_logs (admin_email, action, detail) VALUES (?,?,?)",
        (admin_email, action, detail),
    )
    conn.commit()
    conn.close()


def recent_access_logs(limit=30):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM access_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------
# 평가 진행
# ---------------------------------------------------------------
def get_my_mappings(writer_id):
    """내가 작성해야 할 평가 목록 (완료 여부 포함) — writer_id가 이미 특정 회차의
    직원 레코드를 가리키므로 자연히 그 회차의 매핑만 조회된다."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT m.id as mapping_id, m.relation_type, e.name as target_name, e.id as target_id,
               (SELECT COUNT(*) FROM responses r WHERE r.mapping_id = m.id) as answered
        FROM mappings m
        JOIN employees e ON e.id = m.target_id
        WHERE m.writer_id = ?
        ORDER BY m.relation_type, e.name
    """, (writer_id,)).fetchall()
    conn.close()
    questionnaire = relation_questionnaire()
    result = []
    for r in rows:
        d = dict(r)
        total_q = len(questionnaire.get(d["relation_type"], []))
        d["total_questions"] = total_q
        d["done"] = d["answered"] >= total_q
        result.append(d)
    return result


def get_mapping(mapping_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT m.*, e.name as target_name, w.name as writer_name
        FROM mappings m
        JOIN employees e ON e.id = m.target_id
        JOIN employees w ON w.id = m.writer_id
        WHERE m.id = ?
    """, (mapping_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_existing_responses(mapping_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT question_index, score, comment FROM responses WHERE mapping_id = ?", (mapping_id,)
    ).fetchall()
    conn.close()
    return {r["question_index"]: {"score": r["score"], "comment": r["comment"]} for r in rows}


def save_response(mapping_id, question_index, question_text, score, comment):
    conn = get_conn()
    conn.execute("""
        INSERT INTO responses (mapping_id, question_index, question_text, score, comment)
        VALUES (?,?,?,?,?)
        ON CONFLICT(mapping_id, question_index) DO UPDATE SET
            score=excluded.score, comment=excluded.comment, submitted_at=datetime('now')
    """, (mapping_id, question_index, question_text, score, comment))
    conn.commit()
    conn.close()


def save_overall_comment(mapping_id, comment):
    conn = get_conn()
    conn.execute("UPDATE mappings SET overall_comment=? WHERE id=?", (comment, mapping_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------
# 관리자 — 완료 현황 / 추출 (모두 '현재 회차' 기준)
# ---------------------------------------------------------------
def completion_stats(round_id=None):
    """평가자별 완료 현황 (문항 수까지 정확히 반영해 파이썬에서 계산)"""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    mappings = conn.execute("""
        SELECT m.id as mapping_id, m.relation_type, w.id as writer_id, w.name as writer_name, w.org_group
        FROM mappings m JOIN employees w ON w.id = m.writer_id
        WHERE m.round_id = ?
    """, (round_id,)).fetchall()
    conn.close()

    questionnaire = relation_questionnaire()
    by_writer = {}
    for m in mappings:
        key = (m["writer_id"], m["writer_name"], m["org_group"])
        by_writer.setdefault(key, {"total": 0, "done": 0})
        need = len(questionnaire.get(m["relation_type"], []))
        existing = get_existing_responses(m["mapping_id"])
        by_writer[key]["total"] += 1
        if len(existing) >= need:
            by_writer[key]["done"] += 1

    result = []
    for (wid, wname, org), v in by_writer.items():
        result.append({
            "writer_name": wname, "org_group": org,
            "total_targets": v["total"], "done_targets": v["done"],
            "complete": v["total"] > 0 and v["total"] == v["done"],
        })
    return sorted(result, key=lambda x: (x["org_group"] or "", x["writer_name"]))


def overall_completion(round_id=None):
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    mappings = conn.execute("SELECT id, relation_type FROM mappings WHERE round_id=?", (round_id,)).fetchall()
    conn.close()
    questionnaire = relation_questionnaire()
    total, done = 0, 0
    for m in mappings:
        total += 1
        existing = get_existing_responses(m["id"])
        need = len(questionnaire.get(m["relation_type"], []))
        if len(existing) >= need:
            done += 1
    return {"total": total, "done": done, "pct": round(done/total*100, 1) if total else 0}


def completion_by_org(round_id=None):
    """조직(그룹+팀) 단위로 집계한 평가 진행률."""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    mappings = conn.execute("""
        SELECT m.id as mapping_id, m.relation_type, w.org_group, w.team
        FROM mappings m JOIN employees w ON w.id = m.writer_id
        WHERE m.round_id = ?
    """, (round_id,)).fetchall()
    conn.close()

    questionnaire = relation_questionnaire()
    by_org = {}
    for m in mappings:
        key = (m["org_group"] or "-", m["team"] or "-")
        by_org.setdefault(key, {"total": 0, "done": 0})
        need = len(questionnaire.get(m["relation_type"], []))
        existing = get_existing_responses(m["mapping_id"])
        by_org[key]["total"] += 1
        if len(existing) >= need:
            by_org[key]["done"] += 1

    result = []
    for (org, team), v in by_org.items():
        pct = round(v["done"] / v["total"] * 100, 1) if v["total"] else 0
        result.append({"org_group": org, "team": team, "total": v["total"], "done": v["done"], "pct": pct})
    return sorted(result, key=lambda x: (x["org_group"], x["team"]))


def incomplete_writers(round_id=None):
    """아직 하나라도 미완료 평가가 남은 평가자 목록 (리마인드 발송용)."""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    mappings = conn.execute("""
        SELECT m.id as mapping_id, m.relation_type, w.id as writer_id, w.name as writer_name, w.email as writer_email
        FROM mappings m JOIN employees w ON w.id = m.writer_id
        WHERE m.round_id = ?
    """, (round_id,)).fetchall()
    conn.close()

    questionnaire = relation_questionnaire()
    by_writer = {}
    for m in mappings:
        key = (m["writer_id"], m["writer_name"], m["writer_email"])
        by_writer.setdefault(key, {"total": 0, "done": 0})
        need = len(questionnaire.get(m["relation_type"], []))
        existing = get_existing_responses(m["mapping_id"])
        by_writer[key]["total"] += 1
        if len(existing) >= need:
            by_writer[key]["done"] += 1

    result = []
    for (wid, wname, wemail), v in by_writer.items():
        if v["done"] < v["total"]:
            result.append({
                "name": wname, "email": wemail,
                "done": v["done"], "total": v["total"],
                "remaining": v["total"] - v["done"],
            })
    return sorted(result, key=lambda x: (-x["remaining"], x["name"]))


# 결과 엑셀 컬럼. 앞 12개는 기존 리포트 엔진이 검증한 구성이므로 이름·순서를 바꾸지 않고,
# 새 컬럼은 항상 맨 뒤에만 추가한다 (엔진이 모르는 컬럼은 무시하므로 기존 파이프라인과 호환).
EXPORT_COLUMNS_LEGACY = ["리뷰 대상자", "그룹", "팀", "직군", "직급", "역할", "리뷰 작성자", "관계유형", "질문/업적", "등급", "코멘트 내용", "총평"]
EXPORT_COLUMNS_NEW = ["리뷰 대상자 이메일", "리뷰 작성자 이메일", "작성자 그룹", "작성자 팀", "문항 번호", "응답 완료", "회차"]
EXPORT_COLUMNS = EXPORT_COLUMNS_LEGACY + EXPORT_COLUMNS_NEW


def export_dataframe(round_id=None):
    """리포트 엔진(build_final.py)이 바로 읽을 수 있는 스키마로 추출 (지정 회차, 기본은 현재 회차).

    - 응답 1건 = 1행 (등급이 비어 있으면 N/A). '총평'은 매핑당 값이 그 매핑의 모든 행에 반복된다.
    - 이메일 2종: 동명이인을 구분하고 로스터와 정확히 연결하기 위한 식별자.
    - 작성자 그룹/팀: 같은 팀 평가 vs 타 부서 평가를 나눠 볼 수 있게 한다.
    - 문항 번호: 1부터 시작하는 문항 순서 (회차 중 문구가 바뀌어도 같은 문항을 구분).
    - 응답 완료: 그 매핑(평가자→대상자)의 모든 문항에 답했으면 '완료', 일부만이면 '미완료'.
      자동 저장 때문에 '제출' 단계가 따로 없으므로, 최소응답자 수 판정에는 '완료'만 세는 것을 권장.
      (현재 회차: 지금 활성 문항 수 기준 / 지난 회차: 같은 관계유형에서 가장 많이 답한 건수 기준)
    - 회차: 회차명."""
    if round_id is None:
        round_id = get_current_round_id()
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            e.name as "리뷰 대상자",
            e.org_group as "그룹",
            e.team as "팀",
            e.job_family as "직군",
            e.grade as "직급",
            e.role as "역할",
            w.name as "리뷰 작성자",
            m.relation_type as "관계유형",
            r.question_text as "질문/업적",
            r.score as "등급",
            r.comment as "코멘트 내용",
            m.overall_comment as "총평",
            e.email as "리뷰 대상자 이메일",
            w.email as "리뷰 작성자 이메일",
            w.org_group as "작성자 그룹",
            w.team as "작성자 팀",
            r.question_index + 1 as "문항 번호",
            m.id as "_mapping_id"
        FROM responses r
        JOIN mappings m ON m.id = r.mapping_id
        JOIN employees e ON e.id = m.target_id
        JOIN employees w ON w.id = m.writer_id
        WHERE m.round_id = ?
    """, (round_id,)).fetchall()
    round_row = conn.execute("SELECT name FROM rounds WHERE id = ?", (round_id,)).fetchone()
    conn.close()
    if not rows:
        return pd.DataFrame(columns=EXPORT_COLUMNS)

    df = pd.DataFrame([dict(r) for r in rows])
    answered = df.groupby("_mapping_id")["문항 번호"].nunique()
    if round_id == get_current_round_id():
        questionnaire = relation_questionnaire()
        total = df.groupby("_mapping_id")["관계유형"].first().map(lambda rel: len(questionnaire.get(rel, [])))
    else:
        # 지난 회차는 당시 문항 수를 알 수 없어, 같은 관계유형에서 가장 많이 답한 건수를 전체 문항 수로 본다
        rel_of = df.groupby("_mapping_id")["관계유형"].first()
        max_by_rel = answered.groupby(rel_of).max()
        total = rel_of.map(max_by_rel)
    complete = (answered >= total).map({True: "완료", False: "미완료"})
    df["응답 완료"] = df["_mapping_id"].map(complete)
    df["회차"] = round_row["name"] if round_row else ""
    return df[EXPORT_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------
# 관리자 — 직원별 평가 이력 (회차를 넘나드는 조회)
# ---------------------------------------------------------------
def employee_report(email):
    """특정 이메일이 회차별로 '받은' 평가 결과(항목별 평균 점수 + 코멘트 + 총평)를 모두 모아 반환.
    한 사람이 여러 회차에 걸쳐 어떻게 변화했는지 관리자가 한눈에 훑어볼 수 있게 하기 위함.
    응답이 있는 매핑만 집계하며, 해당 이메일로 참여한 적이 없는 회차는 결과에서 제외한다."""
    conn = get_conn()
    rounds = conn.execute("SELECT * FROM rounds ORDER BY id").fetchall()
    result = []
    for rnd in rounds:
        emp = conn.execute(
            "SELECT * FROM employees WHERE email=? AND round_id=?", (email, rnd["id"])
        ).fetchone()
        if not emp:
            continue

        mappings = conn.execute("""
            SELECT m.id as mapping_id, m.relation_type, m.overall_comment, w.name as writer_name
            FROM mappings m JOIN employees w ON w.id = m.writer_id
            WHERE m.target_id = ?
        """, (emp["id"],)).fetchall()

        by_relation = {}
        overall_scores = []
        overall_comments = []
        for m in mappings:
            resp = conn.execute(
                "SELECT question_text, score, comment FROM responses WHERE mapping_id=? ORDER BY question_index",
                (m["mapping_id"],),
            ).fetchall()
            if not resp and not m["overall_comment"]:
                continue
            rel = m["relation_type"]
            bucket = by_relation.setdefault(rel, {"scores": [], "question_comments": []})
            for r in resp:
                if r["score"] is not None:
                    bucket["scores"].append(r["score"])
                    overall_scores.append(r["score"])
                if r["comment"]:
                    bucket["question_comments"].append({"question": r["question_text"], "comment": r["comment"]})
            if m["overall_comment"]:
                overall_comments.append({
                    "writer_name": m["writer_name"], "relation_type": rel, "text": m["overall_comment"],
                })

        relation_summary = []
        for rel, b in by_relation.items():
            avg = round(sum(b["scores"]) / len(b["scores"]), 2) if b["scores"] else None
            relation_summary.append({
                "relation_type": rel, "avg": avg, "count": len(b["scores"]),
                "question_comments": b["question_comments"],
            })

        if not relation_summary and not overall_comments:
            continue  # 이 회차 명단엔 있었지만 아직 아무도 응답을 남기지 않은 경우는 생략

        result.append({
            "round_id": rnd["id"], "round_name": rnd["name"],
            "created_at": rnd["created_at"], "closed_at": rnd["closed_at"],
            "employee_snapshot": {
                "org_group": emp["org_group"], "team": emp["team"],
                "job_family": emp["job_family"], "grade": emp["grade"], "role": emp["role"],
            },
            "overall_avg": round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else None,
            "overall_response_count": len(overall_scores),
            "by_relation": sorted(relation_summary, key=lambda x: x["relation_type"]),
            "overall_comments": overall_comments,
        })
    conn.close()
    return list(reversed(result))  # 최신 회차가 위로 오도록

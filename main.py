"""
다면평가 수집 웹앱 — 1단계 프로토타입 (최종본)
인증: Slack 관련 환경변수(SLACK_CLIENT_ID 등)가 채워져 있으면 실제 Slack 로그인(OIDC), 비어있으면 더미(이름 선택) 로그인.
"""
import os
import io
import time
import secrets
import datetime
import requests
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.sessions import SessionMiddleware
import pandas as pd

import db
import roster
import purge
from questions import RELATION_LABEL, RELATION_DESC

# Slack Client Secret 같은 값은 코드/저장소에 절대 두지 않고, 배포 플랫폼의 환경변수로만 주입한다
# (예: Railway → 프로젝트 → Variables 탭). 값이 하나라도 비어있으면 자동으로 더미 로그인으로 동작한다.
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
SLACK_REDIRECT_URI = os.environ.get("SLACK_REDIRECT_URI", "")
SLACK_TEAM_ID = os.environ.get("SLACK_TEAM_ID", "")

SLACK_ENABLED = bool(SLACK_CLIENT_ID and SLACK_CLIENT_SECRET and SLACK_REDIRECT_URI)

# 이 이메일로 Slack 로그인하면 항상 관리자 권한이 보장된다.
# 이메일은 비밀값이 아니라 "누가 관리자인지"를 가리키는 식별자일 뿐이므로 코드에 두어도 안전하다.
# (실제 접근 권한은 여전히 해당 Slack 계정으로 로그인할 수 있는 사람에게만 있다)
ADMIN_EMAILS = {"jdh@genians.com"}

# 사이드바/배너에서 5가지 관계유형을 3개 카테고리로 묶어 보여주기 위한 매핑
RELATION_CATEGORY = {
    "팀장평가(팀원이줌)": "팀장평가",
    "팀장간평가": "팀장평가",
    "본인평가": "본인평가",
    "본인평가(팀장)": "본인평가",
    "동료평가": "동료평가",
}
CATEGORY_ORDER = ["팀장평가", "본인평가", "동료평가"]
CATEGORY_META = {
    "팀장평가": {"label": "팀장 평가", "color": "#6D28D9", "bg": "#F3EEFC", "border": "#D8C5F5"},
    "본인평가": {"label": "본인 평가", "color": "#1F3864", "bg": "#EDF0F6", "border": "#C9D2E3"},
    "동료평가": {"label": "동료 평가", "color": "#2F5FA8", "bg": "#EFF3FC", "border": "#C7D6F0"},
}

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)  # 로고 파일을 나중에 넣어도 되도록 폴더가 없으면 만들어둠

app = FastAPI(title="다면평가 수집 (프로토타입)")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 세션 서명 키 — 반드시 환경변수로 고정값을 주입해야 한다.
# 코드에 고정 문자열로 박아두면(과거 dev용 값처럼) 그 값을 아는 사람이 세션을 위조할 수 있어
# 시크릿 코딩 관점에서 취약하다. 환경변수가 없을 때는 매 기동마다 새 키를 생성해
# "안전하지만 재시작 시 전원 재로그인"으로 안전한 쪽으로 fail-safe 시킨다.
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    SESSION_SECRET_KEY = secrets.token_hex(32)
    print(
        "[경고] SESSION_SECRET_KEY 환경변수가 설정되지 않아 임시 키를 생성했습니다. "
        "서버를 재시작하면 기존 로그인 세션이 모두 만료됩니다. "
        "운영 배포 시에는 반드시 환경변수로 고정값을 설정하세요."
    )

# https_only: ngrok/실제 배포는 항상 https이므로 기본 활성화.
# ngrok 없이 http://127.0.0.1 로 로컬 단독 테스트할 때만 SESSION_HTTPS_ONLY=false로 끄면 된다.
SESSION_HTTPS_ONLY = os.environ.get("SESSION_HTTPS_ONLY", "true").lower() != "false"

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
    max_age=60 * 60 * 24 * 60,  # 60일 — 평가 마감일과 별개로 세션 자체도 무한정 살아있지 않도록
)
templates = Jinja2Templates(env=Environment(
    loader=FileSystemLoader(os.path.join(APP_DIR, "templates")),
    cache_size=0,        # Python 3.14 + Jinja2 캐시 조합에서 발생하는 TypeError 회피
    autoescape=True,
    auto_reload=True,
))

db.init_db()

# 헤더/푸터의 브랜드 문구는 관리자 화면에서 언제든 바꿀 수 있어야 하므로(회차명이 바뀔 때마다
# 코드를 고칠 필요가 없도록) 템플릿 전역 함수로 노출해, base.html이 매 렌더링마다 DB에서
# 최신 값을 읽어오게 한다.
templates.env.globals["brand_subtitle"] = lambda: db.get_setting("brand_subtitle", db.DEFAULT_BRAND_SUBTITLE)
templates.env.globals["credit_line"] = lambda: db.get_setting("credit_line", db.DEFAULT_CREDIT_LINE)


def current_user(request: Request):
    emp_id = request.session.get("employee_id")
    if not emp_id:
        return None
    emp = db.get_employee(emp_id)
    if not emp:
        return None
    if emp.get("round_id") != db.get_current_round_id():
        # 관리자가 '새 회차 시작'을 누르면 이전 회차 소속 세션은 자동으로 만료 처리한다.
        # (데이터 자체는 지우지 않고 이력으로 남기므로, 다시 로그인하면 이메일 기준으로
        # 새 회차 계정에 자연스럽게 연결된다.)
        return None
    return emp


def deadline_passed():
    """관리자가 설정한 평가 마감일이 지났는지 확인. 마감일 미설정이면 항상 False."""
    deadline_str = db.get_setting("deadline")
    if not deadline_str:
        return False
    try:
        deadline = datetime.date.fromisoformat(deadline_str)
    except ValueError:
        return False
    return datetime.date.today() > deadline


# ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/dashboard")
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if SLACK_ENABLED:
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        slack_auth_url = (
            "https://slack.com/openid/connect/authorize"
            "?response_type=code&scope=openid%20email%20profile"
            f"&client_id={SLACK_CLIENT_ID}&state={state}&redirect_uri={SLACK_REDIRECT_URI}"
            f"&team={SLACK_TEAM_ID}"
        )
        return templates.TemplateResponse(request, "login_slack.html", {"slack_auth_url": slack_auth_url})
    employees = db.list_employees()
    return templates.TemplateResponse(request, "login.html", {"employees": employees})


@app.post("/login")
def login_submit(request: Request, employee_id: int = Form(...)):
    """더미 로그인 (Slack 미설정 시에만 사용됨)"""
    request.session["employee_id"] = employee_id
    request.session["login_at"] = time.time()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/auth/slack/callback", response_class=HTMLResponse)
def slack_callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"Slack 로그인이 취소되었거나 오류가 발생했습니다: {error}", status_code=400)
    if not code or state != request.session.get("oauth_state"):
        return HTMLResponse("잘못된 요청입니다 (state 불일치). 다시 로그인해주세요. <a href='/login'>로그인으로</a>", status_code=400)

    token_res = requests.post("https://slack.com/api/openid.connect.token", data={
        "client_id": SLACK_CLIENT_ID,
        "client_secret": SLACK_CLIENT_SECRET,
        "code": code,
        "redirect_uri": SLACK_REDIRECT_URI,
    }, timeout=10)
    token_data = token_res.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return HTMLResponse(f"Slack 인증에 실패했습니다: {token_data.get('error', '알 수 없는 오류')}", status_code=400)

    userinfo_res = requests.get(
        "https://slack.com/api/openid.connect.userInfo",
        headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
    )
    userinfo = userinfo_res.json()
    email = userinfo.get("email")
    if not email:
        return HTMLResponse("Slack 계정에서 이메일 정보를 가져오지 못했습니다.", status_code=400)

    emp = db.get_employee_by_email(email)

    # 하드코딩된 관리자 이메일이면, 로스터 업로드 여부/초기화 여부와 무관하게
    # 로그인할 때마다 관리자 권한을 보장한다 (없으면 생성, 있으면 승격).
    if email in ADMIN_EMAILS:
        if not emp:
            emp_id = db.upsert_employee("관리자", email, "-", "-", "-", "-", "팀장", is_admin=1)
            emp = db.get_employee(emp_id)
        elif not emp.get("is_admin"):
            db.set_admin(email)
            emp = db.get_employee_by_email(email)

    if not emp:
        return HTMLResponse(
            f"'{email}' 계정은 이번 다면평가 명단에 없습니다. 담당자에게 문의해주세요. <a href='/login'>다시 시도</a>",
            status_code=403,
        )
    request.session["employee_id"] = emp["id"]
    request.session["login_at"] = time.time()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    mine = db.get_my_mappings(user["id"])
    for m in mine:
        m["relation_label"] = RELATION_LABEL.get(m["relation_type"], m["relation_type"])

    # 평가 화면 사이드바와 동일하게 팀장평가 -> 본인평가 -> 동료평가 순으로 카테고리화해서 보여준다.
    grouped = {c: [] for c in CATEGORY_ORDER}
    for m in mine:
        cat = RELATION_CATEGORY.get(m["relation_type"], "동료평가")
        grouped[cat].append(m)
    categories = [
        {
            "label": CATEGORY_META[c]["label"], "color": CATEGORY_META[c]["color"],
            "entries": grouped[c],
        }
        for c in CATEGORY_ORDER if grouped[c]
    ]

    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "categories": categories,
        "pending_count": sum(1 for m in mine if not m["done"]),
        "completed_count": sum(1 for m in mine if m["done"]),
        "is_locked": deadline_passed(),
        "deadline": db.get_setting("deadline"),
    })


@app.get("/evaluate/{mapping_id}", response_class=HTMLResponse)
def evaluate_page(request: Request, mapping_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    mapping = db.get_mapping(mapping_id)
    if not mapping or mapping["writer_id"] != user["id"]:
        return HTMLResponse("이 평가에 접근할 권한이 없습니다.", status_code=403)

    questionnaire = db.relation_questionnaire()
    active_questions = db.get_active_questions()
    scale_max = active_questions["scale_max"] if active_questions else 6

    existing = db.get_existing_responses(mapping_id)
    questions = questionnaire.get(mapping["relation_type"], [])
    q_list = []
    for i, q in enumerate(questions):
        prev = existing.get(i)  # None이면 아직 응답 안 한 문항
        q_list.append({
            "index": i, "text": q,
            "answered": prev is not None,
            "score": prev["score"] if prev else None,
            "comment": (prev["comment"] if prev else "") or "",
        })

    # 좌측 사이드바용: 내가 해야 할 모든 평가를 관계유형 카테고리별로 묶는다
    # (배치 순서: 팀장 평가 -> 본인 평가 -> 동료 평가)
    mine = db.get_my_mappings(user["id"])
    grouped = {c: [] for c in CATEGORY_ORDER}
    for m in mine:
        cat = RELATION_CATEGORY.get(m["relation_type"], "동료평가")
        if m["mapping_id"] == mapping_id:
            status = "current"
        elif m["done"]:
            status = "done"
        else:
            status = "pending"
        grouped[cat].append({"mapping_id": m["mapping_id"], "name": m["target_name"], "status": status})

    sidebar_categories = [
        {
            "label": CATEGORY_META[c]["label"],
            "color": CATEGORY_META[c]["color"],
            "bg": CATEGORY_META[c]["bg"],
            # 주의: 키 이름을 "items"로 두면 Jinja가 dict.items 내장 메서드를
            # 먼저 찾아버려서 템플릿에서 길이/반복이 깨진다 (실제로 겪은 버그).
            "entries": grouped[c],
        }
        for c in CATEGORY_ORDER if grouped[c]
    ]
    done_count = sum(1 for m in mine if m["done"])
    total_count = len(mine)
    progress_pct = round(done_count / total_count * 100) if total_count else 0
    current_index = next((i + 1 for i, m in enumerate(mine) if m["mapping_id"] == mapping_id), 1)

    cat = RELATION_CATEGORY.get(mapping["relation_type"], "동료평가")
    meta = CATEGORY_META[cat]

    # 평가 대상자가 어느 소속/직급인지 화면에 같이 보여주기 위한 정보
    target_info = db.get_employee(mapping["target_id"])

    return templates.TemplateResponse(request, "evaluate.html", {
        "mapping": mapping, "questions": q_list, "target_info": target_info,
        "relation_label": RELATION_LABEL.get(mapping["relation_type"], mapping["relation_type"]),
        "relation_desc": RELATION_DESC.get(mapping["relation_type"], ""),
        "relation_color": meta["color"], "relation_bg": meta["bg"], "relation_border": meta["border"],
        "scale_max": scale_max,
        "sidebar_categories": sidebar_categories,
        "done_count": done_count, "total_count": total_count, "progress_pct": progress_pct,
        "current_index": current_index,
        "overall_comment": mapping.get("overall_comment"),
        "is_locked": deadline_passed(),
    })


@app.post("/evaluate/{mapping_id}")
async def evaluate_submit(request: Request, mapping_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if deadline_passed():
        return HTMLResponse("평가가 마감되었습니다. 수정이 필요하면 담당자에게 문의해주세요.", status_code=403)
    mapping = db.get_mapping(mapping_id)
    if not mapping or mapping["writer_id"] != user["id"]:
        return HTMLResponse("이 평가에 접근할 권한이 없습니다.", status_code=403)

    questionnaire = db.relation_questionnaire()
    active_questions = db.get_active_questions()
    scale_max = active_questions["scale_max"] if active_questions else 6

    form = await request.form()
    questions = questionnaire.get(mapping["relation_type"], [])
    for i, q in enumerate(questions):
        raw = form.get(f"score_{i}")
        if raw is None or raw == "":
            continue  # 선택 안 한 문항은 저장하지 않음 (미응답으로 남김)
        if raw == "NA":
            score = None
        else:
            try:
                score = int(raw)
            except (TypeError, ValueError):
                continue  # 폼 조작 등 비정상 입력은 조용히 무시
            if score < 1 or score > scale_max:
                continue  # 척도 범위를 벗어난 값도 무시
        comment = form.get(f"comment_{i}", "").strip() or None
        db.save_response(mapping_id, i, q, score, comment)

    overall_comment = (form.get("overall_comment") or "").strip() or None
    db.save_overall_comment(mapping_id, overall_comment)

    return RedirectResponse("/dashboard", status_code=303)


# ---------------------------------------------------------------
# 관리자
# ---------------------------------------------------------------
def _admin_context(request: Request):
    """관리자 화면(admin.html)이 GET/업로드 두 경로에서 공통으로 필요로 하는 컨텍스트."""
    current_round = db.get_current_round()
    return {
        "stats": db.completion_stats(),
        "overall": db.overall_completion(),
        "org_stats": db.completion_by_org(),
        "incomplete": db.incomplete_writers(),
        "site_url": str(request.base_url).rstrip("/"),
        "deadline": db.get_setting("deadline"),
        "is_locked": deadline_passed(),
        "access_logs": db.recent_access_logs(20),
        "current_round_name": current_round["name"] if current_round else "-",
        "brand_subtitle_value": db.get_setting("brand_subtitle", db.DEFAULT_BRAND_SUBTITLE),
        "credit_line_value": db.get_setting("credit_line", db.DEFAULT_CREDIT_LINE),
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다. (개발 로그인 화면에서 '관리자로 로그인'을 선택하세요)", status_code=403)
    return templates.TemplateResponse(request, "admin.html", _admin_context(request))


@app.post("/admin/upload_roster")
async def admin_upload_roster(request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)

    try:
        content = await file.read()
        if file.filename.endswith(".csv"):
            roster_df = pd.read_csv(io.BytesIO(content))
        else:
            roster_df = pd.read_excel(io.BytesIO(content))
        result = roster.generate_mappings_from_roster(roster_df)
        db.log_access(user["email"], "로스터 업로드", detail=str(result.get("summary")))
    except Exception as e:
        # 잘못된 형식의 파일이 올라와도 서버 내부 오류(스택트레이스)를 그대로
        # 노출하지 않고, 관리자 화면 안에서 원인만 안내한다.
        result = {"ok": False, "errors": [f"파일을 읽는 중 오류가 발생했습니다: {e}"]}

    return templates.TemplateResponse(request, "admin.html", {
        **_admin_context(request),
        "upload_result": result,
    })


@app.get("/admin/roster_template")
def admin_roster_template(request: Request):
    """로스터 업로드용 엑셀 양식을 내려받는다. 필수/선택 컬럼과 예시 2~3행,
    그리고 컬럼 설명을 담은 별도 시트를 함께 제공한다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)

    columns = ["이름", "이메일", "그룹", "팀", "직군", "직급", "역할", "리더이메일", "동료평가그룹"]
    sample_rows = [
        ["김팀장", "kim.leader@genians.co.kr", "경영지원본부", "인사총무팀", "경영지원", "팀장", "팀장", "", ""],
        ["이사원", "lee.staff@genians.co.kr", "경영지원본부", "인사총무팀", "경영지원", "사원", "팀원", "kim.leader@genians.co.kr", ""],
        ["박대리", "park.staff@genians.co.kr", "경영지원본부", "인사총무팀", "경영지원", "대리", "팀원", "kim.leader@genians.co.kr", ""],
    ]
    sample_df = pd.DataFrame(sample_rows, columns=columns)

    guide_rows = [
        ["이름", "필수", "평가 대상/작성자로 표시될 이름"],
        ["이메일", "필수", "Slack 로그인 이메일과 반드시 일치해야 함 (조직 내 고유값)"],
        ["그룹", "필수", "상위 조직 단위 (예: 본부/실)"],
        ["팀", "필수", "하위 조직 단위 (예: 팀)"],
        ["직군", "필수", "직군 구분 (예: 경영지원, 개발 등)"],
        ["직급", "필수", "직급/호칭 (예: 사원, 대리, 팀장 등)"],
        ["역할", "필수", "'팀원' 또는 '팀장' 중 하나만 입력"],
        ["리더이메일", "선택 (팀원만)", "본인의 팀장 이메일. 복수면 콤마(,)로 구분. 비우면 팀장평가 매핑이 생성되지 않음"],
        ["동료평가그룹", "선택", "태그를 하나라도 공유하는 사람끼리 팀 경계 없이 동료평가로 묶임. 콤마(,)로 여러 개 지정 가능(대소문자·공백 무시). 예) 직속팀 'SOL-N,SOL-E', Network연구실 'SOL-N', Endpoint연구실 'SOL-E' → 직속팀은 양쪽과 평가, Network↔Endpoint는 서로 평가 안 함. 비워두면 같은 '그룹+팀'끼리 자동으로 묶임"],
    ]
    guide_df = pd.DataFrame(guide_rows, columns=["컬럼명", "필수여부", "설명"])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        sample_df.to_excel(writer, index=False, sheet_name="로스터")
        guide_df.to_excel(writer, index=False, sheet_name="컬럼설명")
    buf.seek(0)

    from urllib.parse import quote
    korean_name = "로스터_업로드_양식.xlsx"
    encoded_name = quote(korean_name)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=roster_template.xlsx; filename*=UTF-8''{encoded_name}"},
    )


@app.get("/admin/export")
def admin_export(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    db.log_access(user["email"], "엑셀 내보내기")
    df = db.export_dataframe()
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    # HTTP 헤더는 ASCII만 허용하므로, 한글 파일명은 RFC 5987 방식(filename*)으로 인코딩
    from urllib.parse import quote
    korean_name = "다면평가_수집결과.xlsx"
    encoded_name = quote(korean_name)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=report.xlsx; filename*=UTF-8''{encoded_name}"},
    )


@app.get("/admin/backup")
def admin_backup(request: Request):
    """collect.db 파일 원본을 그대로 다운로드 — 클라우드 정식 배포 전까지의 수동 백업 수단.
    (자동 정기 백업은 호스팅 플랫폼을 정한 뒤 그 플랫폼의 스냅샷 기능으로 대체 예정)"""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    db.log_access(user["email"], "DB 백업 다운로드")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        db.DB_PATH,
        media_type="application/octet-stream",
        filename=f"collect_backup_{ts}.db",
    )


@app.post("/admin/new_round")
def admin_new_round(request: Request, round_name: str = Form(...)):
    """'전체 초기화' 대신 새 회차를 시작한다. 기존 인원/매핑/응답은 삭제하지 않고
    이력으로 그대로 남기며(관리자 > 이력 조회에서 회차별로 조회 가능), 새 회차는
    로스터 업로드 전의 빈 상태로 시작된다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    round_name = round_name.strip()[:80]
    if not round_name:
        return HTMLResponse("회차 이름을 입력해주세요. <a href='/admin'>관리자로</a>", status_code=400)
    db.start_new_round(round_name)
    db.log_access(user["email"], "새 회차 시작", detail=round_name)
    # 현재 세션의 관리자 계정은 방금 마감된 회차 소속이므로, 새 회차 계정으로
    # 다시 연결되도록 로그아웃시킨다 (current_user()의 회차 검증으로도 결국 막히지만
    # 바로 재로그인을 유도하는 편이 더 명확하다).
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/admin/deadline")
def admin_set_deadline(request: Request, deadline: str = Form("")):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    deadline = deadline.strip()
    if deadline:
        try:
            datetime.date.fromisoformat(deadline)
        except ValueError:
            return HTMLResponse("날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).", status_code=400)
        db.set_setting("deadline", deadline)
        db.log_access(user["email"], "평가 마감일 설정", detail=deadline)
    else:
        db.set_setting("deadline", "")
        db.log_access(user["email"], "평가 마감일 해제")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/branding")
def admin_set_branding(request: Request, brand_subtitle: str = Form(""), credit_line: str = Form("")):
    """헤더 부제(회차/팀 문구)와 하단 크레딧 문구를 관리자가 직접 수정할 수 있게 한다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    brand_subtitle = brand_subtitle.strip()[:120]
    credit_line = credit_line.strip()[:160]
    db.set_setting("brand_subtitle", brand_subtitle or db.DEFAULT_BRAND_SUBTITLE)
    db.set_setting("credit_line", credit_line or db.DEFAULT_CREDIT_LINE)
    db.log_access(user["email"], "브랜드 문구 수정")
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/history", response_class=HTMLResponse)
def admin_history(request: Request, q: str = None):
    """직원별 평가 이력 조회 — 이름 또는 이메일로 검색해 회차별 점수 추이/코멘트를 모아 보여준다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)

    query = (q or "").strip()
    report = None
    matches = None
    resolved_email = None
    if query:
        if "@" in query:
            resolved_email = query
        else:
            candidates = [p for p in db.list_known_people() if query in p["name"]]
            if len(candidates) == 1:
                resolved_email = candidates[0]["email"]
            elif len(candidates) > 1:
                matches = candidates
        if resolved_email:
            report = db.employee_report(resolved_email)

    return templates.TemplateResponse(request, "history.html", {
        "people": db.list_known_people(),
        "query": query,
        "matches": matches,
        "report": report,
    })


# ---------------------------------------------------------------
# 관리자 — 문항 관리 (프리셋)
# ---------------------------------------------------------------
@app.get("/admin/questions", response_class=HTMLResponse)
def admin_questions_page(request: Request, load: int = None, saved: int = None):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)

    if load:
        preset = db.get_question_preset(load)
    else:
        preset = db.get_active_questions()

    return templates.TemplateResponse(request, "questions.html", {
        "preset": preset,
        "presets": db.list_question_presets(),
        "has_responses": db.has_any_responses(),
        "saved": bool(saved),
        "max_questions": db.MAX_QUESTIONS_PER_SET,
    })


@app.post("/admin/questions/save")
async def admin_questions_save(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)

    form = await request.form()
    name = (form.get("name") or "").strip() or "기본 문항"
    # 문항 입력칸이 이제 +/- 버튼으로 개수가 자유롭게 늘고 주는 구조라, 인덱스 기반(peer_0..peer_4)
    # 대신 같은 이름(peer_questions/leader_questions)을 반복해서 보내고 getlist로 전부 받는다.
    peer_questions = [q.strip() for q in form.getlist("peer_questions") if q.strip()]
    leader_questions = [q.strip() for q in form.getlist("leader_questions") if q.strip()]
    try:
        scale_max = int(form.get("scale_max") or 6)
    except ValueError:
        scale_max = 6
    scale_max = max(2, min(scale_max, 10))

    if len(peer_questions) > db.MAX_QUESTIONS_PER_SET or len(leader_questions) > db.MAX_QUESTIONS_PER_SET:
        return templates.TemplateResponse(request, "questions.html", {
            "preset": {
                "id": None, "name": name,
                "peer": peer_questions[:db.MAX_QUESTIONS_PER_SET] or [""],
                "leader": leader_questions[:db.MAX_QUESTIONS_PER_SET] or [""],
                "scale_max": scale_max,
            },
            "presets": db.list_question_presets(),
            "has_responses": db.has_any_responses(),
            "error": f"문항은 항목당 최대 {db.MAX_QUESTIONS_PER_SET}개까지 등록할 수 있습니다.",
            "max_questions": db.MAX_QUESTIONS_PER_SET,
        })

    if not peer_questions or not leader_questions:
        return templates.TemplateResponse(request, "questions.html", {
            "preset": {"id": None, "name": name, "peer": peer_questions or [""], "leader": leader_questions or [""], "scale_max": scale_max},
            "presets": db.list_question_presets(),
            "has_responses": db.has_any_responses(),
            "error": "동료용/팀장용 문항이 각각 최소 1개 이상 있어야 합니다.",
            "max_questions": db.MAX_QUESTIONS_PER_SET,
        })

    db.save_question_preset(name, peer_questions, leader_questions, scale_max)
    db.log_access(user["email"], "문항 세트 저장/적용", detail=name)
    return RedirectResponse("/admin/questions?saved=1", status_code=303)


@app.post("/admin/questions/activate/{preset_id}")
def admin_questions_activate(request: Request, preset_id: int):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    db.activate_question_preset(preset_id)
    db.log_access(user["email"], "문항 프리셋 활성화", detail=str(preset_id))
    return RedirectResponse("/admin/questions", status_code=303)



# ---------------------------------------------------------------
# 관리자 — 최종 백업 / 선택 삭제 / 임시 보관 (purge.py)
# ---------------------------------------------------------------
def _fresh_login(request: Request):
    """삭제 같은 위험 작업은 최근(30분 이내) 로그인한 세션에서만 허용한다."""
    login_at = request.session.get("login_at")
    return bool(login_at) and (time.time() - float(login_at)) <= purge.FRESH_LOGIN_SECONDS


def _flash(request: Request, ok: bool, msg: str):
    request.session["flash"] = {"ok": ok, "msg": msg}


@app.get("/admin/purge", response_class=HTMLResponse)
def admin_purge_page(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    purge.cleanup_trash()  # 화면을 열 때도 만료분을 정리
    current_round = db.get_current_round()
    return templates.TemplateResponse(request, "purge.html", {
        "flash": request.session.pop("flash", None),
        "current_round_name": current_round["name"] if current_round else "-",
        "counts": purge.scope_counts(),
        "backup": purge.get_backup_status(),
        "trash": purge.list_trash(),
        "fresh_login": _fresh_login(request),
        "scope_label": purge.SCOPE_LABEL,
    })


@app.post("/admin/purge/backup")
def admin_purge_backup(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    data, sha = purge.build_backup_zip(user["email"])
    from urllib.parse import quote
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = quote(f"다면평가_최종백업_{ts}.zip")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=final_backup_{ts}.zip; filename*=UTF-8''{name}"},
    )


@app.post("/admin/purge/execute")
async def admin_purge_execute(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    if not _fresh_login(request):
        _flash(request, False, "보안을 위해 삭제 직전 30분 이내에 다시 로그인해야 합니다. "
                               "아래 '다시 로그인' 후 다시 시도해주세요.")
        return RedirectResponse("/admin/purge", status_code=303)

    form = await request.form()
    ok, msg, relogin = purge.execute_purge(
        user["email"],
        scopes=form.getlist("scopes"),
        typed_round_name=form.get("round_name") or "",
        typed_count=form.get("response_count") or "",
    )
    if ok and relogin:
        # 인원까지 지웠으면 현재 세션의 계정도 사라지므로 로그아웃시킨다 (관리자 이메일은 재로그인 시 자동 복구됨)
        request.session.clear()
        return HTMLResponse(
            "삭제가 완료되었습니다. 삭제 직전 상태는 24시간 임시 보관됩니다. "
            "<a href='/login'>다시 로그인</a> 후 관리자 화면 > 삭제·임시 보관에서 확인하세요.")
    _flash(request, ok, msg)
    return RedirectResponse("/admin/purge", status_code=303)


@app.post("/admin/purge/trash/{trash_id}/restore")
def admin_trash_restore(request: Request, trash_id: str):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    ok, msg, relogin = purge.restore_trash(trash_id, user["email"])
    if ok and relogin:
        request.session.clear()
        return HTMLResponse("복구가 완료되었습니다. <a href='/login'>다시 로그인</a>해주세요.")
    _flash(request, ok, msg)
    return RedirectResponse("/admin/purge", status_code=303)


@app.post("/admin/purge/trash/{trash_id}/delete")
def admin_trash_delete(request: Request, trash_id: str):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    ok, msg = purge.delete_trash_now(trash_id, user["email"])
    _flash(request, ok, msg)
    return RedirectResponse("/admin/purge", status_code=303)

@app.on_event("startup")
def ensure_admin_account():
    """개발용 더미 로그인 관리자 계정이 없으면 하나 만들어둔다.
    (Slack 모드에서는 ADMIN_EMAILS 쪽 로직이 이 계정 없이도 관리자 접근을 보장하므로,
    이 계정은 SLACK_ENABLED=False인 로컬 개발 상황에서만 실제로 쓰인다.)"""
    existing = db.get_employee_by_email("admin@local")
    if not existing:
        db.upsert_employee("관리자", "admin@local", "-", "-", "-", "-", "팀장", is_admin=1)
    purge.start_cleanup_thread()  # 앱 시작 시 만료된 임시 보관본 정리 + 주기적 정리 스레드

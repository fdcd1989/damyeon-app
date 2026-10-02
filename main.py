"""
다면평가 수집 웹앱 — 1단계 프로토타입 (최종본)
인증: Slack 관련 환경변수(SLACK_CLIENT_ID 등)가 채워져 있으면 실제 Slack 로그인(OIDC), 비어있으면 더미(이름 선택) 로그인.
"""
import os
import re
from collections import defaultdict
import io
import time
import threading
import secrets
import datetime
import requests
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse, HTMLResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader
from starlette.middleware.sessions import SessionMiddleware
import pandas as pd

import db
import roster
import purge
import audit
import slack_notify
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


# --- 마스코트 영상: 구간 요청(Range) 지원 ---------------------------------------
# 기본 정적 파일 서버(StaticFiles)는 Range 요청을 지원하지 않아, 영상의 특정 구간으로 이동(seek)이 안 되고
# iOS/Safari 에서는 재생 자체가 안 될 수 있다. 영상 두 개(수백 KB)만 허용 목록으로 직접 서빙한다.
_MASCOT_MEDIA = {"mascot.mp4": "video/mp4", "mascot.webm": "video/webm"}


@app.api_route("/media/mascot/{name}", methods=["GET", "HEAD"])
def mascot_media(request: Request, name: str):
    ctype = _MASCOT_MEDIA.get(name)
    path = os.path.join(STATIC_DIR, "mascot", name) if ctype else None
    if not ctype or not os.path.isfile(path):
        return Response(status_code=404)
    size = os.path.getsize(path)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"}
    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if rng:
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
        if not m or (not m.group(1) and not m.group(2)):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        if not m.group(1):                       # 'bytes=-N' : 끝에서 N바이트
            start = max(size - int(m.group(2)), 0)
        else:
            start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
        if start > end or start >= size:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start + 1)
    headers["Content-Length"] = str(len(data))
    if request.method == "HEAD":
        return Response(status_code=status, media_type=ctype, headers=headers)
    return Response(content=data, status_code=status, media_type=ctype, headers=headers)


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
_KST = datetime.timezone(datetime.timedelta(hours=9))


def _dash_summary(mine, deadline_str, locked):
    """대시보드 상단에 보여줄 말풍선 문구와 '이어서 평가하기' 대상을 계산한다. 본인 진행 건수만 사용한다."""
    total = len(mine)
    done = sum(1 for m in mine if m["done"])
    partial = sum(1 for m in mine if not m["done"] and m["answered"] > 0)
    remaining = total - done

    days_left = None
    if deadline_str and not locked:
        try:
            days_left = (datetime.date.fromisoformat(deadline_str) - datetime.datetime.now(_KST).date()).days
        except ValueError:
            days_left = None
    sub = ""
    if days_left is not None and remaining:
        if days_left <= 0:
            sub = "⏰ 오늘 마감이에요"
        elif days_left <= 3:
            sub = f"⏰ 마감까지 D-{days_left}"

    if locked:
        text, tone, intro = "평가가 마감되었어요. 참여해 주셔서 감사합니다 🙏", "locked", "greet"
    elif total == 0:
        text, tone, intro = "아직 배정된 평가가 없어요.", "start", "greet"
    elif remaining == 0:
        text, tone, intro = "모두 끝났어요! 고생하셨습니다 🎉", "done", "cheer"
    elif done == 0 and partial == 0:
        text, tone, intro = f"안녕하세요! 평가 {total}건이 기다리고 있어요 👋", "start", "greet"
    else:
        text, tone, intro = f"{done}건 완료! {remaining}건 남았어요, 조금만 더 힘내요 💪", "progress", "greet"

    return {"text": text, "sub": sub, "tone": tone, "intro": intro, "total": total, "done": done,
            "progress_pct": round(done / total * 100) if total else 0}


def _dash_deadline_label(deadline_str):
    """통계 칸에 보여줄 마감 표시: {'date': '10/10', 'tail': 'D-3' | '오늘 마감' | ''} (미설정이면 None)"""
    if not deadline_str:
        return None
    try:
        d = datetime.date.fromisoformat(deadline_str)
    except ValueError:
        return None
    left = (d - datetime.datetime.now(_KST).date()).days
    return {"date": f"{d.month}/{d.day}", "tail": "오늘 마감" if left == 0 else f"D-{left}" if left > 0 else ""}


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

    locked = deadline_passed()
    deadline = db.get_setting("deadline")
    for m in mine:
        m["status"] = "done" if m["done"] else ("partial" if m["answered"] > 0 else "wait")
    ordered = [m for c in categories for m in c["entries"]]
    nxt = None
    if not locked:   # 이어서 평가하기: 쓰다 만 것을 먼저, 없으면 화면 순서상 첫 미완료
        nxt = next((m for m in ordered if m["status"] == "partial"), None) or next((m for m in ordered if m["status"] == "wait"), None)

    summary = _dash_summary(mine, deadline, locked)
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "categories": categories,
        "pending_count": sum(1 for m in mine if not m["done"]),
        "completed_count": sum(1 for m in mine if m["done"]),
        "total_count": len(mine),
        "is_locked": locked,
        "deadline": deadline,
        "deadline_label": _dash_deadline_label(deadline),
        "summary": summary,
        "next_item": nxt,
        "mascot_intro": summary["intro"],
    })


@app.get("/evaluate/{mapping_id}", response_class=HTMLResponse)
def evaluate_page(request: Request, mapping_id: int, saved: int = 0):
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
        elif m["answered"] > 0:
            status = "partial"  # 일부 문항만 작성한 상태 ('작성 중')
        else:
            status = "pending"
        grouped[cat].append({"mapping_id": m["mapping_id"], "name": m["target_name"],
                             "status": status, "done": m["done"]})

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
        "saved_flash": bool(saved),
    })


async def _save_evaluation(request: Request, user, mapping_id: int):
    """평가 저장 공통 로직. (ok, http_status, message, info) 반환.
    선택하지 않은 문항은 저장하지 않고(미응답으로 유지), 점수 없이 입력된 코멘트는 개수만 세어 알려준다."""
    if deadline_passed():
        return False, 403, "평가가 마감되어 저장할 수 없습니다. 수정이 필요하면 담당자에게 문의해주세요.", None
    mapping = db.get_mapping(mapping_id)
    if not mapping or mapping["writer_id"] != user["id"]:
        return False, 403, "이 평가에 접근할 권한이 없습니다.", None

    questionnaire = db.relation_questionnaire()
    active_questions = db.get_active_questions()
    scale_max = active_questions["scale_max"] if active_questions else 6

    form = await request.form()
    questions = questionnaire.get(mapping["relation_type"], [])
    skipped_comments = 0
    for i, q in enumerate(questions):
        raw = form.get(f"score_{i}")
        comment = (form.get(f"comment_{i}") or "").strip() or None
        if raw is None or raw == "":
            if comment:
                skipped_comments += 1  # 점수를 고르지 않으면 코멘트도 저장되지 않음 -> 사용자에게 알림
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
        db.save_response(mapping_id, i, q, score, comment)

    overall_comment = (form.get("overall_comment") or "").strip() or None
    db.save_overall_comment(mapping_id, overall_comment)

    answered = len(db.get_existing_responses(mapping_id))
    total = len(questions)
    mine = db.get_my_mappings(user["id"])
    done_count = sum(1 for m in mine if m["done"])
    total_count = len(mine)
    kst = datetime.timezone(datetime.timedelta(hours=9))
    info = {
        "name": mapping["target_name"],
        "answered": answered, "total": total, "complete": answered >= total,
        "skipped_comments": skipped_comments,
        "done_count": done_count, "total_count": total_count,
        "progress_pct": round(done_count / total_count * 100) if total_count else 0,
        "saved_at": datetime.datetime.now(kst).strftime("%H:%M:%S"),
    }
    return True, 200, "저장되었습니다.", info


@app.post("/evaluate/{mapping_id}")
async def evaluate_submit(request: Request, mapping_id: int):
    """자바스크립트가 꺼진 환경 등을 위한 일반 폼 제출. 저장 후 같은 사람의 평가 화면에 머문다."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    ok, status, msg, _ = await _save_evaluation(request, user, mapping_id)
    if not ok:
        return HTMLResponse(msg, status_code=status)
    return RedirectResponse(f"/evaluate/{mapping_id}?saved=1", status_code=303)


@app.post("/evaluate/{mapping_id}/autosave")
async def evaluate_autosave(request: Request, mapping_id: int):
    """화면 이동 없이 저장하는 JSON 엔드포인트 ('저장' 버튼, 다른 사람/목록으로 이동 시 자동 저장에서 사용)."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 만료되었습니다. 이 화면을 닫지 말고 새 탭에서 다시 로그인한 뒤 저장해주세요."},
                            status_code=401)
    ok, status, msg, info = await _save_evaluation(request, user, mapping_id)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=status)
    return JSONResponse({"ok": True, "message": msg, **info})


# ---------------------------------------------------------------
# 관리자
# ---------------------------------------------------------------
def _public_base_url(request: Request):
    """사용자에게 보여줄 접속 주소. 프록시 뒤에서 http 로 잡혀도 운영 도메인은 https 로 안내한다.
    환경변수 PUBLIC_BASE_URL 이 있으면 그 값을 우선 사용."""
    env = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if env:
        return env
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and not re.match(r"http://(localhost|127\.|0\.0\.0\.0)", base):
        base = "https://" + base[len("http://"):]
    return base


def _admin_context(request: Request):
    """관리자 화면(admin.html)이 GET/업로드 두 경로에서 공통으로 필요로 하는 컨텍스트."""
    current_round = db.get_current_round()
    return {
        "stats": db.completion_stats(),
        "overall": db.overall_completion(),
        "org_stats": db.completion_by_org(),
        "incomplete": db.incomplete_writers(),
        "site_url": _public_base_url(request),
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


# --- 로스터 업로드: 미리보기 -> 확인 후 반영 -------------------------------
# 업로드 파일(개인정보 포함)은 디스크에 저장하지 않고 메모리에만 30분간 보관한다.
_PENDING_ROSTERS = {}
_PENDING_LOCK = threading.Lock()
_PENDING_TTL = 30 * 60


def _pending_put(admin_email, filename, content, signature):
    now = time.time()
    token = secrets.token_urlsafe(24)
    with _PENDING_LOCK:
        for t in [t for t, v in _PENDING_ROSTERS.items() if now - v["ts"] > _PENDING_TTL or v["admin"] == admin_email]:
            _PENDING_ROSTERS.pop(t, None)  # 만료분 + 같은 관리자의 이전 미리보기는 폐기
        _PENDING_ROSTERS[token] = {"admin": admin_email, "filename": filename, "content": content,
                                   "signature": signature, "ts": now}
    return token


def _pending_get(token, admin_email):
    with _PENDING_LOCK:
        v = _PENDING_ROSTERS.get(token)
        if not v or v["admin"] != admin_email or time.time() - v["ts"] > _PENDING_TTL:
            return None
        return v


def _pending_drop(token):
    with _PENDING_LOCK:
        _PENDING_ROSTERS.pop(token, None)


def _read_roster_file(filename, content):
    """엑셀/CSV를 문자열로 읽는다(숫자처럼 보이는 값이 변형되지 않도록). CSV는 UTF-8, 안 되면 CP949(엑셀 한글 CSV)로 시도."""
    if (filename or "").lower().endswith(".csv"):
        for enc in ("utf-8-sig", "cp949"):
            try:
                return pd.read_csv(io.BytesIO(content), dtype=str, encoding=enc)
            except UnicodeDecodeError:
                continue
        raise ValueError("CSV 인코딩을 인식하지 못했습니다. 엑셀(.xlsx)로 저장해서 올려주세요.")
    return pd.read_excel(io.BytesIO(content), dtype=str)


def _preview_context(request, plan, filename, token=None, error_msgs=None):
    ctx = {"filename": filename, "token": token, "errors": error_msgs or [], "plan": None}
    if not plan or not plan.get("ok"):
        if plan:
            ctx["errors"] = plan["errors"]
            ctx["notes"] = plan.get("notes", [])
        return ctx
    diff = audit.diff_plan_with_db(plan)
    result = audit.analyze(plan["employees"], plan["intended"])
    notes = plan.get("notes", [])
    n_warn = result["n_warn"] + sum(1 for n in notes if n["level"] == "warn")
    ctx.update({
        "plan": plan, "diff": diff, "audit": result, "notes": notes, "n_warn": n_warn,
        "needs_ack": bool(n_warn or diff["large_change"] or diff["n_keep"]),
        "people": {e: audit._nm(plan["employees"], e) for e in plan["employees"]},
    })
    return ctx


@app.post("/admin/upload_roster")
async def admin_upload_roster(request: Request, file: UploadFile = File(...)):
    """1단계: 파일을 검증하고 '반영하면 무엇이 바뀌는지'만 보여준다. 이 단계에서는 DB를 바꾸지 않는다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    try:
        content = await file.read()
        roster_df = _read_roster_file(file.filename, content)
        plan = roster.build_plan(roster_df)
        ctx = _preview_context(request, plan, file.filename)
        if plan["ok"]:
            ctx["token"] = _pending_put(user["email"], file.filename, content, ctx["diff"]["signature"])
            db.log_access(user["email"], "로스터 미리보기", detail=f"인원 {len(plan['employees'])}명 / 반영 전 확인")
    except Exception as e:
        # 잘못된 형식의 파일이 올라와도 스택트레이스를 노출하지 않고 원인만 안내한다.
        ctx = {"filename": getattr(file, "filename", ""), "token": None, "plan": None,
               "errors": [f"파일을 읽는 중 오류가 발생했습니다: {e}"], "notes": []}
    return templates.TemplateResponse(request, "roster_preview.html", ctx)


@app.post("/admin/upload_roster/apply")
async def admin_upload_roster_apply(request: Request):
    """2단계: 미리보기에서 확인한 내용을 실제로 반영한다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    form = await request.form()
    token = form.get("token") or ""
    pend = _pending_get(token, user["email"])
    if not pend:
        result = {"ok": False, "errors": ["미리보기가 만료되었거나 찾을 수 없습니다(30분 경과·서버 재시작 등). 파일을 다시 업로드해주세요."]}
    else:
        try:
            plan = roster.build_plan(_read_roster_file(pend["filename"], pend["content"]))
            ctx = _preview_context(request, plan, pend["filename"], token=token)
            if not plan["ok"]:
                result = {"ok": False, "errors": plan["errors"]}
            elif ctx["diff"]["signature"] != pend["signature"]:
                result = {"ok": False, "errors": ["미리보기 이후 데이터가 바뀌었습니다(다른 응답 제출 등). 변경 내용이 달라졌을 수 있어 "
                                                   "반영하지 않았습니다. 파일을 다시 업로드해 미리보기를 확인해주세요."]}
                _pending_drop(token)
            elif ctx["needs_ack"] and form.get("ack") != "1":
                return templates.TemplateResponse(request, "roster_preview.html",
                                                  {**ctx, "errors": ["경고 확인 체크박스를 선택해야 반영할 수 있습니다."]})
            else:
                applied = roster.apply_plan(plan)
                result = {"ok": True, "errors": [], "summary": plan["summary"], "peer_group_stats": plan["peer_group_stats"],
                          **applied}
                db.log_access(user["email"], "로스터 반영", detail=str(plan["summary"]) +
                              f" / 추가 {ctx['diff']['n_add']}건·삭제 {ctx['diff']['n_remove']}건·응답으로 유지 {ctx['diff']['n_keep']}건")
                _pending_drop(token)
        except Exception as e:
            result = {"ok": False, "errors": [f"반영 중 오류가 발생했습니다. 데이터는 변경되지 않았습니다: {e}"]}
    return templates.TemplateResponse(request, "admin.html", {**_admin_context(request), "upload_result": result})


@app.post("/admin/upload_roster/cancel")
async def admin_upload_roster_cancel(request: Request):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    form = await request.form()
    pend = _pending_get(form.get("token") or "", user["email"])
    if pend:
        _pending_drop(form.get("token"))
    return RedirectResponse("/admin", status_code=303)


# --- 미완료자 슬랙 리마인드 -------------------------------------------------
def _admin_or_none(request: Request):
    user = current_user(request)
    return user if user and user.get("is_admin") else None


@app.get("/admin/reminders", response_class=HTMLResponse)
def admin_reminders(request: Request, job: str = ""):
    user = _admin_or_none(request)
    if not user:
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    emps = {e["email"].lower(): e for e in db.list_employees()}
    last = slack_notify.last_reminders()
    recent = slack_notify.recently_sent_emails()
    rows = []
    for w in db.incomplete_writers():
        key = w["email"].lower()
        e = emps.get(key, {})
        lr = last.get(key)
        rows.append({**w, "org_group": e.get("org_group", ""), "team": e.get("team", ""),
                     "recent": key in recent,
                     "last_status": lr["status"] if lr else None,
                     "last_error": slack_notify.describe_error(lr["error"]) if lr and lr["error"] else "",
                     "last_at": purge.to_kst(lr["sent_at"]) if lr else ""})
    return templates.TemplateResponse(request, "reminders.html", {
        "flash": request.session.pop("flash", None),
        "status": slack_notify.connection_status(),
        "token_format_ok": (not slack_notify.is_configured()) or slack_notify.token_looks_valid(),
        "rows": rows, "deadline": db.get_setting("deadline"), "is_locked": deadline_passed(),
        "default_template": slack_notify.DEFAULT_TEMPLATE, "placeholders": slack_notify.PLACEHOLDERS,
        "link_label": slack_notify.LINK_LABEL,
        "job_id": job if job and slack_notify.job_snapshot(job) else "",
        "running": bool(slack_notify.running_job()),
    })


@app.post("/admin/reminders/send")
async def admin_reminders_send(request: Request):
    user = _admin_or_none(request)
    if not user:
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    form = await request.form()

    def back(msg, ok=False, job=None):
        request.session["flash"] = {"ok": ok, "msg": msg}
        return RedirectResponse("/admin/reminders" + (f"?job={job}" if job else ""), status_code=303)

    if deadline_passed():
        return back("평가 마감일이 지나 발송할 수 없습니다. 마감일을 연장하면 다시 발송할 수 있습니다.")
    if not slack_notify.is_configured():
        return back("Slack 봇 토큰(SLACK_BOT_TOKEN)이 설정되지 않아 발송할 수 없습니다.")
    template = form.get("message") or ""
    err = slack_notify.validate_template(template)
    if err:
        return back(err)
    if slack_notify.running_job():
        return back("이미 발송 중인 작업이 있습니다. 끝난 뒤 다시 시도해주세요.")

    wanted = {e.strip().lower() for e in form.getlist("emails") if e.strip()}
    incomplete = {w["email"].lower(): w for w in db.incomplete_writers()}   # 서버에서 다시 계산: 지금도 미완료인 사람만
    targets = [incomplete[e] for e in wanted if e in incomplete]
    skipped = len(wanted) - len(targets)
    if not targets:
        return back("발송 대상이 없습니다. (선택한 사람이 없거나 이미 모두 완료했습니다)")
    if len(targets) > 300:
        return back("한 번에 300명까지만 발송할 수 있습니다.")
    recent = slack_notify.recently_sent_emails()
    dup = [t for t in targets if t["email"].lower() in recent]
    if dup and form.get("dup_ok") != "1":
        return back(f"24시간 안에 이미 발송한 {len(dup)}명이 포함되어 있어 발송하지 않았습니다. 확인 후 다시 발송해주세요.")

    targets.sort(key=lambda t: (-t["remaining"], t["name"]))
    job_id = slack_notify.start_job(user["email"], targets, template, db.get_setting("deadline"),
                                    _public_base_url(request) + "/login")
    db.log_access(user["email"], "슬랙 리마인드 발송 시작", detail=f"대상 {len(targets)}명" + (f" (이미 완료한 {skipped}명 제외)" if skipped else ""))
    return back(f"{len(targets)}명에게 발송을 시작했습니다." + (f" (이미 완료해 제외된 {skipped}명)" if skipped else ""), ok=True, job=job_id)


@app.post("/admin/reminders/test")
async def admin_reminders_test(request: Request):
    """실제 대량 발송 전에 내 슬랙 DM으로 미리 받아본다."""
    user = _admin_or_none(request)
    if not user:
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    form = await request.form()

    def back(msg, ok):
        request.session["flash"] = {"ok": ok, "msg": msg}
        return RedirectResponse("/admin/reminders", status_code=303)

    if not slack_notify.is_configured():
        return back("Slack 봇 토큰(SLACK_BOT_TOKEN)이 설정되지 않았습니다.", False)
    template = form.get("message") or ""
    err = slack_notify.validate_template(template)
    if err:
        return back(err, False)
    text = "🧪 [테스트 발송 — 실제 대상자에게는 가지 않았습니다]\n" + slack_notify.render_message(
        template, user["name"], 0, 3, db.get_setting("deadline"), _public_base_url(request) + "/login")
    ok, e = slack_notify.send_dm(user["email"], text)
    slack_notify.log_result("test", user["email"], user["name"], "test" if ok else "failed", e, user["email"])
    if ok:
        return back(f"{user['email']} 의 슬랙 DM으로 테스트 메시지를 보냈습니다. 슬랙에서 확인해보세요.", True)
    return back(f"테스트 발송 실패: {slack_notify.describe_error(e)}", False)


@app.get("/admin/reminders/job/{job_id}")
def admin_reminders_job(request: Request, job_id: str):
    if not _admin_or_none(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    snap = slack_notify.job_snapshot(job_id)
    if not snap:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(snap)


# --- 매핑 점검 화면 --------------------------------------------------------
@app.get("/admin/mappings", response_class=HTMLResponse)
def admin_mappings(request: Request, q: str = "", p: str = ""):
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    employees, mappings, keys = audit.build_db_state()
    ctx = {"empty": not employees, "q": q.strip(), "selected": None, "matches": []}
    if employees:
        result = audit.analyze(employees, mappings)
        resp = {(m["target_email"], m["writer_email"], m["relation_type"]): m["response_count"] for m in keys}
        ctx.update({"audit": result, "people": {e: audit._nm(employees, e) for e in employees},
                    "rows": sorted(result["per_person"].values(), key=lambda r: (r["org_group"], r["team"], r["role"] != "팀장", r["name"]))})
        needle = q.strip().lower()
        if needle:
            ctx["matches"] = [r for r in ctx["rows"] if needle in r["name"].lower() or needle in r["email"].lower()]
        if p and p in employees:
            out, inc = defaultdict(list), defaultdict(list)
            for (t, w, r), cnt in sorted(resp.items(), key=lambda kv: (employees.get(kv[0][0], {}).get("name", ""), kv[0][2])):
                if w == p:
                    out[r].append({"email": t, "label": audit._nm(employees, t), "grade": employees[t]["grade"], "responded": cnt > 0})
                if t == p:
                    inc[r].append({"email": w, "label": audit._nm(employees, w), "grade": employees[w]["grade"]})
            ctx["selected"] = {"info": result["per_person"][p], "label": audit._nm(employees, p),
                               "out": [(r, out.get(r, [])) for r in audit.RELATIONS if out.get(r)],
                               "inc": [(r, inc.get(r, [])) for r in audit.RELATIONS if inc.get(r)],
                               "flags": [g for g in result["findings"] if any(p in it["emails"] for it in g["items"])]}
    return templates.TemplateResponse(request, "mappings.html", ctx)


@app.get("/admin/mappings/export")
def admin_mappings_export(request: Request):
    """매핑 전체 + 인원별 요약 + 점검 결과를 엑셀로 내려받는다 (팀장·HR이 함께 눈으로 검토하는 용도). 응답 내용은 포함하지 않는다."""
    user = current_user(request)
    if not user or not user.get("is_admin"):
        return HTMLResponse("관리자만 접근 가능합니다.", status_code=403)
    employees, mappings, keys = audit.build_db_state()
    result = audit.analyze(employees, mappings)
    rows = []
    for m in sorted(keys, key=lambda m: (m["writer_name"], m["relation_type"], m["target_name"])):
        w, t = employees.get(m["writer_email"], {}), employees.get(m["target_email"], {})
        rows.append({"작성자": m["writer_name"], "작성자 이메일": m["writer_email"], "작성자 그룹": w.get("org_group", ""),
                     "작성자 팀": w.get("team", ""), "관계": m["relation_type"], "대상": m["target_name"],
                     "대상 이메일": m["target_email"], "대상 그룹": t.get("org_group", ""), "대상 팀": t.get("team", ""),
                     "응답 여부": "응답 있음" if m["response_count"] else "미응답"})
    summary = [{"이름": r["name"], "이메일": r["email"], "그룹": r["org_group"], "팀": r["team"], "직급": r["grade"], "역할": r["role"],
                "리더": ", ".join(r["leaders"]), "평가할 동료 수": r["peers"], "받는 팀장평가 수": r["raters"],
                "팀장간평가 수": r["lpeers"], "점검 경고 수": r["flags"]} for r in result["per_person"].values()]
    checks = [{"구분": g["level_label"], "항목": g["title"], "내용": it["text"]} for g in result["findings"] for it in g["items"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame(rows).to_excel(xw, sheet_name="매핑전체", index=False)
        pd.DataFrame(summary).to_excel(xw, sheet_name="인원별요약", index=False)
        pd.DataFrame(checks or [{"구분": "", "항목": "점검 결과 없음", "내용": ""}]).to_excel(xw, sheet_name="점검결과", index=False)
    buf.seek(0)
    db.log_access(user["email"], "매핑 점검 엑셀 다운로드", detail=f"매핑 {len(rows)}건")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename=mapping_check_{ts}.xlsx"})


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
    slack_notify.ensure_tables()
    purge.start_cleanup_thread()  # 앱 시작 시 만료된 임시 보관본 정리 + 주기적 정리 스레드

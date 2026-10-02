"""
미완료자 슬랙 DM 리마인드.

- 봇 토큰은 환경변수 SLACK_BOT_TOKEN 에서만 읽는다. (코드/DB/화면/로그 어디에도 남기지 않는다)
- 이메일로 Slack 사용자를 찾고(users.lookupByEmail) 그 사람에게 DM(chat.postMessage)을 보낸다.
- 발송은 백그라운드 스레드에서 한 명씩 간격을 두고 보낸다 (Slack 속도 제한 준수).
- 누구에게 언제 보냈는지/성공 여부는 reminder_log 테이블에 남긴다. 메시지 본문은 저장하지 않는다.
- 메시지에는 '남은 건수'만 들어가고, 누구를 아직 평가하지 않았는지는 절대 들어가지 않는다.
"""
import os
import re
import time
import uuid
import secrets
import threading
import datetime

import requests

import db

SLACK_API = "https://slack.com/api/"
SEND_INTERVAL = 1.3          # 사람 사이 대기(초). 조회+발송 2회 호출을 속도 제한 안에 유지
DUP_WINDOW = datetime.timedelta(hours=24)
MAX_MESSAGE_LEN = 1500

LINK_LABEL = "다면평가 접속하기"
DEFAULT_TEMPLATE = (
    "[다면평가 알림] {이름}님, 다면평가가 아직 {남은건수}건 남아 있어요 🙏\n"
    "진행 현황: {완료건수}/{전체건수}건 완료\n"
    "마감일: {마감일}\n"
    "👉 {접속링크}\n"
    "※ 이 메시지는 다면평가 알림봇이 발송했습니다. 문의: 인사총무팀"
)
# {접속링크} = 누르면 접속되는 하이퍼링크(글자는 LINK_LABEL 고정), {링크} = 주소 글자 그대로
PLACEHOLDERS = ["{이름}", "{남은건수}", "{완료건수}", "{전체건수}", "{마감일}", "{접속링크}", "{링크}"]

ERROR_KO = {
    "users_not_found": "Slack에서 이 이메일 계정을 찾지 못했습니다 (로스터 이메일과 Slack 가입 이메일이 다를 수 있음)",
    "invalid_auth": "봇 토큰이 유효하지 않습니다 (재발급·재설정 필요)",
    "not_authed": "봇 토큰이 유효하지 않습니다 (재발급·재설정 필요)",
    "token_revoked": "봇 토큰이 폐기되었습니다 (재발급 필요)",
    "token_expired": "봇 토큰이 만료되었습니다 (재발급 필요)",
    "account_inactive": "봇 계정이 비활성 상태입니다",
    "missing_scope": "봇 권한(scope)이 부족합니다: chat:write, users:read, users:read.email 확인 후 앱을 워크스페이스에 다시 설치하세요",
    "ratelimited": "Slack 속도 제한에 걸렸습니다 (잠시 후 다시 시도)",
    "user_disabled": "비활성화된 Slack 계정입니다",
    "inactive_user": "비활성화된 Slack 계정입니다",
    "cannot_dm_bot": "봇 계정에는 보낼 수 없습니다",
    "network": "Slack 서버에 연결하지 못했습니다",
    "timeout": "Slack 서버 응답 시간이 초과되었습니다",
}


def describe_error(code):
    return ERROR_KO.get(code, f"Slack 오류: {code}")


# ---------------------------------------------------------------
# 설정 / 상태
# ---------------------------------------------------------------
def bot_token():
    return (os.environ.get("SLACK_BOT_TOKEN") or "").strip()


def is_configured():
    return bool(bot_token())


def token_looks_valid():
    return bot_token().startswith("xoxb-")


def _redact(text):
    tok = bot_token()
    return text.replace(tok, "***") if tok and text else text


# ---------------------------------------------------------------
# Slack API 호출 (테스트에서 이 함수를 대체한다)
# ---------------------------------------------------------------
def _api(method, params=None, json_body=None, retries=3):
    """Slack Web API 호출. 항상 dict 를 반환하며 실패 시 {'ok': False, 'error': 코드}. 예외는 밖으로 던지지 않는다."""
    headers = {"Authorization": f"Bearer {bot_token()}"}
    for attempt in range(retries + 1):
        try:
            if json_body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
                res = requests.post(SLACK_API + method, headers=headers, json=json_body, timeout=10)
            else:
                res = requests.get(SLACK_API + method, headers=headers, params=params or {}, timeout=10)
        except requests.exceptions.Timeout:
            return {"ok": False, "error": "timeout"}
        except requests.exceptions.RequestException:
            return {"ok": False, "error": "network"}
        if res.status_code == 429:
            if attempt < retries:
                try:
                    wait = min(float(res.headers.get("Retry-After", "2")), 30)
                except ValueError:
                    wait = 2
                time.sleep(wait)
                continue
            return {"ok": False, "error": "ratelimited"}
        try:
            data = res.json()
        except ValueError:
            return {"ok": False, "error": f"http_{res.status_code}"}
        if not data.get("ok") and data.get("error") == "ratelimited" and attempt < retries:
            time.sleep(2)
            continue
        return data
    return {"ok": False, "error": "ratelimited"}


_status_cache = {"ts": 0.0, "value": None}


def connection_status(force=False):
    """봇 연결 상태 (auth.test). 60초 캐시. 토큰/세부 오류는 노출하지 않는다."""
    if not is_configured():
        return {"configured": False}
    now = time.time()
    if not force and _status_cache["value"] and now - _status_cache["ts"] < 60:
        return _status_cache["value"]
    data = _api("auth.test", retries=0)
    if data.get("ok"):
        val = {"configured": True, "ok": True, "bot": data.get("user", ""), "team": data.get("team", "")}
    else:
        val = {"configured": True, "ok": False, "error": describe_error(data.get("error", "unknown"))}
    _status_cache.update(ts=now, value=val)
    return val


_user_cache = {}


def lookup_user_id(email):
    """(slack_user_id, None) 또는 (None, 오류코드)"""
    key = email.lower()
    if key in _user_cache:
        return _user_cache[key], None
    data = _api("users.lookupByEmail", params={"email": email})
    if not data.get("ok"):
        return None, data.get("error", "unknown")
    user = data.get("user") or {}
    if user.get("deleted"):
        return None, "user_disabled"
    if user.get("is_bot"):
        return None, "cannot_dm_bot"
    _user_cache[key] = user.get("id")
    return user.get("id"), None


def send_dm(email, text):
    """(True, None) 또는 (False, 오류코드)"""
    uid, err = lookup_user_id(email)
    if err:
        return False, err
    data = _api("chat.postMessage", json_body={
        "channel": uid, "text": text, "unfurl_links": False, "unfurl_media": False, "link_names": False,
    })
    if data.get("ok"):
        return True, None
    return False, data.get("error", "unknown")


# ---------------------------------------------------------------
# 메시지
# ---------------------------------------------------------------
def slack_escape(value):
    """Slack mrkdwn 에서 특수 의미를 갖는 문자를 무력화한다 (이름에 <!channel> 같은 값이 들어가도 멘션이 되지 않도록)."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_message(template, name, done, total, deadline, link):
    """자리표시자를 채워 Slack 메시지 문자열을 만든다.
    순서가 중요하다: ① 관리자가 쓴 본문의 & < > 를 먼저 무력화(직접 쓴 <주소|글자> 링크나 멘션이 동작하지 않게)
    ② 그 뒤에 우리가 만드는 링크 서식(<주소|글자>)을 넣는다. 그래서 하이퍼링크는 {접속링크}로만 만들어진다."""
    out = slack_escape(template)
    values = {
        "{이름}": slack_escape(name),
        "{남은건수}": str(max(total - done, 0)),
        "{완료건수}": str(done),
        "{전체건수}": str(total),
        "{마감일}": slack_escape(deadline or "미정"),
        "{접속링크}": f"<{link}|{LINK_LABEL}>",
        "{링크}": slack_escape(link),
    }
    for k, v in values.items():
        out = out.replace(k, v)
    return out


def validate_template(template):
    t = (template or "").strip()
    if not t:
        return "메시지가 비어 있습니다."
    if len(t) > MAX_MESSAGE_LEN:
        return f"메시지가 너무 깁니다 ({MAX_MESSAGE_LEN}자 이하)."
    if re.search(r"<!(channel|here|everyone)", t, re.I):
        return "전체 멘션(@channel 등)은 사용할 수 없습니다."
    return None


# ---------------------------------------------------------------
# 발송 기록
# ---------------------------------------------------------------
def ensure_tables():
    conn = db.get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_id INTEGER NOT NULL,
                batch_id TEXT,
                email TEXT NOT NULL,
                name TEXT,
                status TEXT NOT NULL,          -- sent | failed | test
                error TEXT,
                sent_at TEXT NOT NULL,         -- UTC ISO
                admin_email TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminder_round_email ON reminder_log(round_id, email)")
        conn.commit()
    finally:
        conn.close()


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def log_result(batch_id, email, name, status, error, admin_email):
    ensure_tables()
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO reminder_log (round_id, batch_id, email, name, status, error, sent_at, admin_email) VALUES (?,?,?,?,?,?,?,?)",
            (db.get_current_round_id(), batch_id, email, name, status, error, _iso(_utcnow()), admin_email))
        conn.commit()
    finally:
        conn.close()


def last_reminders():
    """현재 회차에서 사람별 가장 최근 발송 기록(테스트 제외). {소문자 이메일: {status, error, sent_at}}"""
    ensure_tables()
    conn = db.get_conn()
    try:
        rows = conn.execute("""
            SELECT l.email, l.status, l.error, l.sent_at FROM reminder_log l
            JOIN (SELECT email, MAX(id) mid FROM reminder_log WHERE round_id=? AND status<>'test' GROUP BY email) x ON x.mid=l.id
        """, (db.get_current_round_id(),)).fetchall()
    finally:
        conn.close()
    return {r["email"].lower(): dict(r) for r in rows}


def recently_sent_emails(window=DUP_WINDOW):
    """최근 window 안에 '성공'으로 발송된 사람들(소문자 이메일 집합)."""
    ensure_tables()
    since = _iso(_utcnow() - window)
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT email FROM reminder_log WHERE round_id=? AND status='sent' AND sent_at>=?",
                            (db.get_current_round_id(), since)).fetchall()
    finally:
        conn.close()
    return {r["email"].lower() for r in rows}


# ---------------------------------------------------------------
# 백그라운드 발송 작업
# ---------------------------------------------------------------
_jobs = {}
_jobs_lock = threading.Lock()


def running_job():
    with _jobs_lock:
        for j in _jobs.values():
            if j["status"] == "running":
                return j["id"]
    return None


def start_job(admin_email, targets, template, deadline, link):
    """targets: [{name, email, done, total}]. 작업 id 를 반환한다."""
    job_id = secrets.token_hex(6)
    batch_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "admin": admin_email, "status": "running", "total": len(targets), "done": 0, "ok": 0, "fail": 0,
           "results": [], "started": time.time()}
    with _jobs_lock:
        for old in [k for k, v in _jobs.items() if v["status"] != "running" and time.time() - v["started"] > 6 * 3600]:
            _jobs.pop(old, None)
        _jobs[job_id] = job

    def run():
        try:
            for i, t in enumerate(targets):
                text = render_message(template, t["name"], t["done"], t["total"], deadline, link)
                ok, err = send_dm(t["email"], text)
                log_result(batch_id, t["email"], t["name"], "sent" if ok else "failed", err, admin_email)
                with _jobs_lock:
                    job["done"] += 1
                    job["ok" if ok else "fail"] += 1
                    job["results"].append({"name": t["name"], "email": t["email"], "ok": ok,
                                           "error": None if ok else describe_error(err)})
                if err in ("invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive", "missing_scope"):
                    # 토큰/권한 문제는 모든 발송이 실패하므로 즉시 중단 (나머지는 '미발송'으로 남는다)
                    with _jobs_lock:
                        job["aborted"] = describe_error(err)
                    break
                if i < len(targets) - 1 and SEND_INTERVAL:
                    time.sleep(SEND_INTERVAL)
        except Exception as e:  # noqa: BLE001
            with _jobs_lock:
                job["aborted"] = _redact(f"예기치 않은 오류로 중단되었습니다: {e}")
        finally:
            with _jobs_lock:
                job["status"] = "done"
            try:
                db.log_access(admin_email, "슬랙 리마인드 발송", detail=f"성공 {job['ok']}명 / 실패 {job['fail']}명 / 대상 {job['total']}명")
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=run, daemon=True).start()
    return job_id


def job_snapshot(job_id):
    with _jobs_lock:
        j = _jobs.get(job_id)
        if not j:
            return None
        return {k: (list(v) if isinstance(v, list) else v) for k, v in j.items() if k != "admin"}

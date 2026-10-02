"""
평가 데이터 '최종 백업 → 검증 → 선택 삭제 → 24시간 임시 보관 → 완전 삭제' 모듈.

설계 원칙
- 삭제는 '백업을 받은 뒤 그 백업 이후 데이터가 바뀌지 않았을 때'만 가능하다 (내용 해시로 검증).
- 삭제 직전의 DB 스냅샷을 DATA_DIR/trash 에 24시간만 보관하고, 만료되면 덮어쓰기 후 파일을 지운다.
- 임시 보관본 복구는 '삭제 직후 상태 그대로'일 때만 허용한다 (새 데이터를 덮어쓰지 않도록).
- 삭제는 하나의 트랜잭션으로 처리하고, secure_delete + VACUUM 으로 DB 파일 안의 흔적을 지운다.
- 로그에는 건수만 남기고 개인정보는 남기지 않는다.

한계(화면에도 안내): 앱은 Railway 볼륨 '스냅샷/백업'이나 SSD 내부의 잔존 블록까지는 지울 수 없다.
"""
import io
import os
import re
import json
import time
import secrets
import sqlite3
import hashlib
import zipfile
import tempfile
import threading
import datetime

import db
import slack_notify

TRASH_DIR = os.path.join(os.path.dirname(db.DB_PATH), "trash")
TRASH_TTL = datetime.timedelta(hours=24)
BACKUP_INFO_KEY = "final_backup_info"
FRESH_LOGIN_SECONDS = 30 * 60  # 삭제 실행 직전 재로그인 유효 시간

_TRASH_ID_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{8}$")
_lock = threading.Lock()

SCOPE_ORDER = ["responses", "mappings", "employees", "history"]
SCOPE_LABEL = {
    "responses": "응답·코멘트·총평 (현재 회차)",
    "mappings": "평가 관계(매핑) (현재 회차)",
    "employees": "인원·로스터 (현재 회차)",
    "history": "지난 회차 이력 전체 (현재 회차 제외)",
}


# ---------------------------------------------------------------
# 시간 유틸
# ---------------------------------------------------------------
def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso):
    return datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def to_kst(iso):
    """UTC ISO 문자열 -> 'YYYY-MM-DD HH:MM' (한국시간) 표시용."""
    if not iso:
        return ""
    try:
        return (_parse(iso) + datetime.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


# ---------------------------------------------------------------
# 파일 유틸
# ---------------------------------------------------------------
def _sqlite_copy(src, dst):
    """SQLite 백업 API로 일관된 스냅샷을 만든다 (쓰는 중에 파일을 그냥 복사하는 것보다 안전)."""
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _shred(path):
    """0으로 덮어쓴 뒤 삭제. (SSD/네트워크 볼륨에서는 물리적 완전 소거를 보장하지 못함)"""
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            remaining = size
            chunk = b"\0" * (1 << 20)
            while remaining > 0:
                n = min(remaining, len(chunk))
                f.write(chunk[:n])
                remaining -= n
            f.flush()
            os.fsync(f.fileno())
    except FileNotFoundError:
        return
    except OSError:
        pass
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _scalar(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------
# 현황 / 내용 해시
# ---------------------------------------------------------------
def data_digest():
    """평가 데이터(회차·인원·매핑·응답·총평) 전체의 내용 해시.
    백업 이후 응답 한 건이라도 바뀌면 값이 달라져 '오래된 백업으로 삭제'를 막는다.
    관리자 계정(is_admin=1)은 로그인 때 자동 생성/변경될 수 있어 제외한다."""
    conn = db.get_conn()
    h = hashlib.sha256()
    queries = [
        "SELECT id, name, is_current FROM rounds ORDER BY id",
        "SELECT id, round_id, name, email, org_group, team, job_family, grade, role "
        "FROM employees WHERE is_admin=0 ORDER BY id",
        "SELECT id, round_id, target_id, writer_id, relation_type, overall_comment FROM mappings ORDER BY id",
        "SELECT id, mapping_id, question_index, question_text, score, comment, submitted_at "
        "FROM responses ORDER BY id",
    ]
    try:
        for q in queries:
            h.update(q.encode("utf-8"))
            for row in conn.execute(q):
                h.update(repr(tuple(row)).encode("utf-8"))
    finally:
        conn.close()
    return h.hexdigest()


def totals():
    """DB 전체(모든 회차) 건수 — 백업 매니페스트/확인용."""
    conn = db.get_conn()
    try:
        return {
            "rounds": _scalar(conn, "SELECT COUNT(*) FROM rounds"),
            "employees": _scalar(conn, "SELECT COUNT(*) FROM employees"),
            "mappings": _scalar(conn, "SELECT COUNT(*) FROM mappings"),
            "responses": _scalar(conn, "SELECT COUNT(*) FROM responses"),
            "overall_comments": _scalar(
                conn, "SELECT COUNT(*) FROM mappings WHERE overall_comment IS NOT NULL AND overall_comment<>''"
            ),
        }
    finally:
        conn.close()


def scope_counts():
    """삭제 범위별 미리보기 건수 (현재 회차 / 지난 회차)."""
    rid = db.get_current_round_id()
    conn = db.get_conn()
    try:
        return {
            "cur_employees": _scalar(conn, "SELECT COUNT(*) FROM employees WHERE round_id=?", rid),
            "cur_mappings": _scalar(conn, "SELECT COUNT(*) FROM mappings WHERE round_id=?", rid),
            "cur_responses": _scalar(
                conn,
                "SELECT COUNT(*) FROM responses r JOIN mappings m ON m.id=r.mapping_id WHERE m.round_id=?", rid),
            "cur_overall": _scalar(
                conn,
                "SELECT COUNT(*) FROM mappings WHERE round_id=? AND overall_comment IS NOT NULL "
                "AND overall_comment<>''", rid),
            "past_rounds": _scalar(conn, "SELECT COUNT(*) FROM rounds WHERE id<>?", rid),
            "past_employees": _scalar(conn, "SELECT COUNT(*) FROM employees WHERE round_id<>?", rid),
            "past_mappings": _scalar(conn, "SELECT COUNT(*) FROM mappings WHERE round_id<>?", rid),
            "past_responses": _scalar(
                conn,
                "SELECT COUNT(*) FROM responses r JOIN mappings m ON m.id=r.mapping_id WHERE m.round_id<>?", rid),
        }
    finally:
        conn.close()


def normalize_scopes(scopes):
    """상위 항목을 고르면 하위 항목이 자동 포함되도록 서버에서도 강제한다."""
    s = {x for x in scopes if x in SCOPE_ORDER}
    if "employees" in s:
        s |= {"mappings", "responses"}
    if "mappings" in s:
        s.add("responses")
    return [x for x in SCOPE_ORDER if x in s]


# ---------------------------------------------------------------
# 1) 최종 백업 ZIP
# ---------------------------------------------------------------
def build_backup_zip(admin_email):
    """엑셀(회차별) + collect.db 스냅샷 + manifest.json 을 ZIP으로 묶어 (bytes, sha256) 반환.
    ZIP 자체는 서버에 저장하지 않는다. 서버에는 '해시/건수/데이터 지문'만 기록한다."""
    created = _utcnow()
    current = db.get_current_round()
    t = totals()
    digest = data_digest()

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _sqlite_copy(db.DB_PATH, tmp)
        with open(tmp, "rb") as f:
            db_bytes = f.read()
    finally:
        _shred(tmp)

    files = {"collect.db": db_bytes}
    round_rows = []
    for r in db.list_rounds():
        df = db.export_dataframe(r["id"])
        round_rows.append({"id": r["id"], "name": r["name"], "response_rows": int(len(df))})
        if df.empty:
            continue
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        files[f"export_round{r['id']}.xlsx"] = buf.getvalue()

    manifest = {
        "created_at_utc": _iso(created),
        "current_round": {"id": current["id"], "name": current["name"]} if current else None,
        "확인용_응답_건수(DB 전체)": t["responses"],
        "counts": t,
        "rounds": round_rows,
        "files_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()},
        "note": "삭제 화면에서 '확인용_응답_건수'를 입력해야 삭제할 수 있습니다. "
                "collect.db 또는 엑셀을 열어 건수가 맞는지 직접 확인하세요.",
    }

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for name, content in files.items():
            z.writestr(name, content)
    data = out.getvalue()
    zip_sha = hashlib.sha256(data).hexdigest()

    info = {
        "created_at": _iso(created),
        "zip_sha256": zip_sha,
        "digest": digest,
        "responses_total": t["responses"],
        "round_id": current["id"] if current else None,
        "admin": admin_email,
    }
    db.set_setting(BACKUP_INFO_KEY, json.dumps(info, ensure_ascii=False))
    db.log_access(admin_email, "최종 백업 ZIP 생성", detail=f"응답 {t['responses']}건 / sha256 {zip_sha[:12]}…")
    return data, zip_sha


def get_backup_status():
    raw = db.get_setting(BACKUP_INFO_KEY, "")
    if not raw:
        return {"exists": False}
    try:
        info = json.loads(raw)
    except ValueError:
        return {"exists": False}
    return {
        "exists": True,
        "created_kst": to_kst(info.get("created_at")),
        "zip_sha256": info.get("zip_sha256", ""),
        "admin": info.get("admin", ""),
        "stale": info.get("digest") != data_digest(),  # 백업 이후 데이터가 바뀌었는가
    }


# ---------------------------------------------------------------
# 2) 삭제 실행
# ---------------------------------------------------------------
def _write_meta(tid, meta):
    with open(os.path.join(TRASH_DIR, tid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def _read_meta(tid):
    with open(os.path.join(TRASH_DIR, tid + ".json"), "r", encoding="utf-8") as f:
        return json.load(f)


def execute_purge(admin_email, scopes, typed_round_name, typed_count):
    """모든 검증을 통과해야만 삭제한다. (ok, 메시지, 세션을 비워야 하는지) 반환."""
    scopes = normalize_scopes(scopes)
    if not scopes:
        return False, "삭제할 범위를 하나 이상 선택해주세요.", False

    with _lock:
        raw = db.get_setting(BACKUP_INFO_KEY, "")
        if not raw:
            return False, "먼저 '최종 백업 ZIP'을 받아주세요. 백업 없이는 삭제할 수 없습니다.", False
        info = json.loads(raw)

        if info.get("digest") != data_digest():
            return False, ("백업 이후 데이터가 변경되었습니다(새 응답 제출 등). "
                           "최신 상태로 백업 ZIP을 다시 받아주세요."), False

        try:
            count_ok = int(str(typed_count).strip()) == int(info.get("responses_total"))
        except (TypeError, ValueError):
            count_ok = False
        if not count_ok:
            return False, ("확인용 응답 건수가 일치하지 않습니다. 받은 ZIP 안의 manifest.json 에 적힌 "
                           "'확인용_응답_건수'를 입력해주세요."), False

        current = db.get_current_round()
        if not current or (typed_round_name or "").strip() != current["name"]:
            return False, "회차명이 일치하지 않습니다. 현재 회차명을 정확히 입력해주세요.", False

        rid = current["id"]
        before = scope_counts()

        # --- 삭제 직전 스냅샷을 임시 보관 (실패하면 삭제하지 않는다) ---
        os.makedirs(TRASH_DIR, exist_ok=True)
        try:
            os.chmod(TRASH_DIR, 0o700)
        except OSError:
            pass
        now = _utcnow()
        tid = f"{now.strftime('%Y%m%dT%H%M%SZ')}_{secrets.token_hex(4)}"
        snap_path = os.path.join(TRASH_DIR, tid + ".db")
        try:
            _sqlite_copy(db.DB_PATH, snap_path)
            meta = {
                "id": tid, "created_at": _iso(now), "expires_at": _iso(now + TRASH_TTL),
                "admin": admin_email, "scopes": scopes, "round_name": current["name"],
                "counts": before, "post_digest": None,  # 삭제 성공 후에 채움 (None이면 복구 불가)
            }
            _write_meta(tid, meta)
        except Exception as e:  # noqa: BLE001
            _shred(snap_path)
            return False, f"임시 보관본을 만들지 못해 삭제를 중단했습니다(데이터는 그대로입니다): {e}", False

        # --- 하나의 트랜잭션으로 삭제 ---
        slack_notify.ensure_tables()  # 트랜잭션 시작 전에 (오래된 DB에 발송 기록 테이블이 없을 수 있음)
        conn = db.get_conn()
        conn.isolation_level = None  # 명시적 BEGIN/COMMIT
        try:
            conn.execute("PRAGMA secure_delete = ON")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            if "history" in scopes:
                conn.execute("DELETE FROM reminder_log WHERE round_id<>?", (rid,))
                conn.execute("DELETE FROM responses WHERE mapping_id IN "
                             "(SELECT id FROM mappings WHERE round_id<>?)", (rid,))
                conn.execute("DELETE FROM mappings WHERE round_id<>?", (rid,))
                conn.execute("DELETE FROM employees WHERE round_id<>?", (rid,))
                conn.execute("DELETE FROM rounds WHERE id<>?", (rid,))
            if "responses" in scopes:
                conn.execute("DELETE FROM responses WHERE mapping_id IN "
                             "(SELECT id FROM mappings WHERE round_id=?)", (rid,))
                conn.execute("UPDATE mappings SET overall_comment=NULL WHERE round_id=?", (rid,))
            if "mappings" in scopes:
                conn.execute("DELETE FROM mappings WHERE round_id=?", (rid,))
            if "employees" in scopes:
                conn.execute("DELETE FROM reminder_log WHERE round_id=?", (rid,))
                conn.execute("DELETE FROM employees WHERE round_id=?", (rid,))
            conn.execute("COMMIT")
        except Exception as e:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            _shred(snap_path)
            _shred(os.path.join(TRASH_DIR, tid + ".json"))
            return False, f"삭제 중 오류가 발생해 모두 취소되었습니다(데이터는 그대로입니다): {e}", False
        conn.close()

        # --- 빈 공간에 남은 조각 제거 (VACUUM은 트랜잭션 밖에서) ---
        try:
            vc = db.get_conn()
            vc.isolation_level = None
            vc.execute("PRAGMA secure_delete = ON")
            vc.execute("VACUUM")
            vc.close()
        except sqlite3.Error:
            pass  # 삭제 자체는 이미 완료됨. VACUUM 실패는 치명적이지 않음

        meta["post_digest"] = data_digest()
        _write_meta(tid, meta)
        db.set_setting(BACKUP_INFO_KEY, "")  # 사용한 백업 정보는 폐기

        detail = (f"범위={','.join(scopes)} / 응답 {before['cur_responses']}건, 총평 {before['cur_overall']}건, "
                  f"매핑 {before['cur_mappings']}건, 인원 {before['cur_employees']}명 "
                  f"/ 지난회차 {before['past_rounds']}개 / 임시보관 {tid}")
        db.log_access(admin_email, "평가 데이터 삭제", detail=detail)

        return True, ("선택한 데이터를 삭제했습니다. 삭제 직전 상태는 24시간 동안 임시 보관되며, "
                      "그 안에는 복구할 수 있습니다."), ("employees" in scopes)


# ---------------------------------------------------------------
# 3) 임시 보관본 관리
# ---------------------------------------------------------------
def list_trash():
    items = []
    if not os.path.isdir(TRASH_DIR):
        return items
    for fn in sorted(os.listdir(TRASH_DIR)):
        if not fn.endswith(".json"):
            continue
        tid = fn[:-5]
        if not _TRASH_ID_RE.match(tid):
            continue
        try:
            meta = _read_meta(tid)
        except (OSError, ValueError):
            continue
        items.append({
            "id": tid,
            "created_kst": to_kst(meta.get("created_at")),
            "expires_kst": to_kst(meta.get("expires_at")),
            "scopes": [SCOPE_LABEL.get(s, s) for s in meta.get("scopes", [])],
            "round_name": meta.get("round_name", ""),
            "admin": meta.get("admin", ""),
            "counts": meta.get("counts", {}),
            "restorable": bool(meta.get("post_digest")),
        })
    return list(reversed(items))


def cleanup_trash():
    """만료된 임시 보관본(과 메타가 없는 고아 스냅샷)을 덮어쓰기 후 삭제. 삭제한 개수 반환."""
    if not os.path.isdir(TRASH_DIR):
        return 0
    removed = 0
    now = _utcnow()
    with _lock:
        for fn in os.listdir(TRASH_DIR):
            full = os.path.join(TRASH_DIR, fn)
            base, ext = os.path.splitext(fn)
            if not _TRASH_ID_RE.match(base) or not os.path.exists(full):
                continue  # 같은 목록에서 앞서 처리하며 이미 지워진 파일은 건너뜀
            if ext == ".json":
                try:
                    expires = _parse(_read_meta(base).get("expires_at", ""))
                except (OSError, ValueError, KeyError):
                    expires = now  # 읽을 수 없는 메타는 만료 처리
                if expires <= now:
                    _shred(os.path.join(TRASH_DIR, base + ".db"))
                    _shred(full)
                    removed += 1
            elif ext == ".db":
                if not os.path.exists(os.path.join(TRASH_DIR, base + ".json")):
                    age = time.time() - os.path.getmtime(full)
                    if age > TRASH_TTL.total_seconds():
                        _shred(full)
                        removed += 1
    return removed


def delete_trash_now(tid, admin_email):
    if not _TRASH_ID_RE.match(tid or ""):
        return False, "잘못된 요청입니다."
    with _lock:
        _shred(os.path.join(TRASH_DIR, tid + ".db"))
        _shred(os.path.join(TRASH_DIR, tid + ".json"))
    db.log_access(admin_email, "임시 보관본 즉시 완전 삭제", detail=tid)
    return True, "임시 보관본을 완전히 삭제했습니다."


def restore_trash(tid, admin_email):
    """삭제 직후 상태 그대로일 때만 복구한다. 회차/인원/매핑/응답 테이블을 스냅샷 내용으로 되돌린다."""
    if not _TRASH_ID_RE.match(tid or ""):
        return False, "잘못된 요청입니다.", False
    with _lock:
        try:
            meta = _read_meta(tid)
        except (OSError, ValueError):
            return False, "임시 보관본을 찾을 수 없습니다(이미 만료되어 삭제되었을 수 있습니다).", False
        snap_path = os.path.join(TRASH_DIR, tid + ".db")
        if not os.path.exists(snap_path):
            return False, "임시 보관본 파일이 없습니다.", False
        if _parse(meta["expires_at"]) <= _utcnow():
            return False, "보관 기간(24시간)이 지났습니다.", False
        if not meta.get("post_digest"):
            return False, "이 보관본은 복구할 수 없는 상태입니다(삭제가 완료되지 않았음).", False
        if data_digest() != meta["post_digest"]:
            return False, ("삭제 이후 데이터가 바뀌어(새 로스터 업로드, 새 회차 시작 등) 복구하면 "
                           "새 데이터를 덮어쓰게 됩니다. 복구할 수 없습니다."), False

        conn = db.get_conn()
        conn.isolation_level = None
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("ATTACH DATABASE ? AS snap", (snap_path,))
            tables = ["rounds", "employees", "mappings", "responses"]
            cols = {}
            for t in tables:
                main_cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({t})")]
                snap_cols = {r[1] for r in conn.execute(f"PRAGMA snap.table_info({t})")}
                cols[t] = [c for c in main_cols if c in snap_cols]
            conn.execute("BEGIN IMMEDIATE")
            for t in reversed(tables):
                conn.execute(f"DELETE FROM main.{t}")
            for t in tables:
                col_sql = ", ".join(cols[t])
                conn.execute(f"INSERT INTO main.{t} ({col_sql}) SELECT {col_sql} FROM snap.{t}")
            conn.execute("COMMIT")
            conn.execute("DETACH DATABASE snap")
        except Exception as e:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()
            return False, f"복구 중 오류가 발생해 취소되었습니다: {e}", False
        conn.close()

        _shred(snap_path)
        _shred(os.path.join(TRASH_DIR, tid + ".json"))
        db.log_access(admin_email, "임시 보관본 복구", detail=tid)
        return True, "삭제 직전 상태로 복구했습니다. 보관본은 복구 후 완전 삭제했습니다.", True


# ---------------------------------------------------------------
# 만료 정리 스레드 (앱 기동 시 즉시 1회 + 이후 10분마다)
# ---------------------------------------------------------------
_thread_started = False


def start_cleanup_thread():
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    try:
        cleanup_trash()  # 재시작 직후 만료분 즉시 정리
    except Exception as e:  # noqa: BLE001
        print(f"[경고] 임시 보관본 정리 실패: {e}")

    def loop():
        while True:
            time.sleep(600)
            try:
                cleanup_trash()
            except Exception as e:  # noqa: BLE001
                print(f"[경고] 임시 보관본 정리 실패: {e}")

    threading.Thread(target=loop, daemon=True).start()

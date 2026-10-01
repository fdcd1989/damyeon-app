"""
관리자가 올리는 '로스터'(인원 명단 + 조직 + 리더 정보)를 받아서,
5가지 관계유형(본인평가/동료평가/팀원→팀장평가/팀장 본인평가/팀장간평가)의
평가 매핑을 자동으로 생성한다.

로스터 필요 컬럼:
  이름, 이메일, 그룹, 팀, 직군, 직급, 역할(팀원|팀장)

로스터 선택 컬럼:
  리더이메일        — 팀원만 해당. 콤마로 복수 지정 가능 (예: "a@x.com,b@x.com")
  동료평가그룹      — 채워두면 '같은 그룹+팀'이 아니라 태그를 하나라도 공유하는 사람들끼리 동료평가로 묶인다.
                     팀 경계와 무관하게 태그로 교차평가/서브그룹을 만들 수 있다.
                     태그는 콤마(,)로 여러 개 지정할 수 있다. (대소문자·앞뒤 공백은 무시)
                     예) 직속팀 "SOL-N,SOL-E" / Network연구실 "SOL-N" / Endpoint연구실 "SOL-E"
                         → 직속팀은 양쪽 모두와 평가, Network↔Endpoint는 서로 평가하지 않음
"""
import re

import db


def _clean(value):
    """엑셀의 빈 칸(NaN)이 'nan' 문자열이 되지 않도록 빈 문자열로 정리한다."""
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except Exception:  # noqa: BLE001
        pass
    return str(value).strip()


def parse_tags(raw):
    """'SOL-N, sol-e' -> ['sol-n', 'sol-e'] (콤마/전각콤마/세미콜론 구분, 대소문자·공백 무시, 중복 제거).
    표시용 원문 라벨은 별도로 보관한다."""
    text = _clean(raw).replace("，", ",").replace(";", ",").replace("；", ",")
    seen, out = set(), []
    for part in text.split(","):
        label = " ".join(part.split())
        key = label.casefold()
        if key and key not in seen:
            seen.add(key)
            out.append((key, label))
    return out


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REQUIRED_COLS = ["이름", "이메일", "그룹", "팀", "직군", "직급", "역할"]


def build_plan(roster_df):
    """로스터를 검증하고 '무엇이 만들어질지'를 계산만 한다. DB는 건드리지 않는다.
    오류가 하나라도 있으면 ok=False 이며, 이 경우 아무것도 반영해서는 안 된다."""
    errors, notes = [], []
    missing = [c for c in REQUIRED_COLS if c not in roster_df.columns]
    if missing:
        errors.append("필수 컬럼 누락: " + ", ".join(missing))
        errors.append("업로드하신 파일의 실제 컬럼: " + ", ".join(str(c) for c in roster_df.columns.tolist()))
        return {"ok": False, "errors": errors, "notes": notes}

    df = roster_df.copy()
    for opt in ("리더이메일", "동료평가그룹"):
        if opt not in df.columns:
            df[opt] = ""

    employees, order, lower_map, rows = {}, [], {}, []
    bad_emails = set()  # 오류가 있는 행의 이메일 (리더 참조 검사에서 중복 오류를 피하기 위함)
    for i, (_, row) in enumerate(df.iterrows()):
        rownum = i + 2  # 엑셀 기준 행 번호 (1행은 헤더)
        name, email, role = _clean(row["이름"]), _clean(row["이메일"]), _clean(row["역할"])
        if not any(_clean(row[c]) for c in REQUIRED_COLS):
            continue  # 완전히 빈 줄은 무시
        who = f"{rownum}행 {name or '(이름 없음)'}"
        bad = False
        if not name:
            errors.append(f"{rownum}행: 이름이 비어 있습니다"); bad = True
        if not email:
            errors.append(f"{who}: 이메일이 비어 있습니다"); bad = True
        elif not EMAIL_RE.match(email):
            errors.append(f"{who}: 이메일 형식이 올바르지 않습니다 ('{email}')"); bad = True
        elif email.lower() in lower_map:
            errors.append(f"{who}: 이메일이 {lower_map[email.lower()][1]}행과 중복됩니다 ('{email}')"); bad = True
        if role not in ("팀원", "팀장"):
            errors.append(f"{who}: 역할 값이 '팀원'/'팀장'이 아님 ('{role}')"); bad = True
        for col in ("그룹", "팀"):
            if not _clean(row[col]):
                errors.append(f"{who}: '{col}'이 비어 있습니다 (같은 그룹+팀끼리 동료로 묶이므로 필수)"); bad = True
        for col in ("직군", "직급"):
            if not _clean(row[col]):
                notes.append({"level": "warn", "text": f"{who}: '{col}'이 비어 있습니다 (리포트 집계에 영향)"})
        if bad:
            if email:
                bad_emails.add(email.lower())
            continue
        lower_map[email.lower()] = (email, rownum)
        employees[email] = {
            "name": name, "org_group": _clean(row["그룹"]), "team": _clean(row["팀"]),
            "job_family": _clean(row["직군"]), "grade": _clean(row["직급"]), "role": role,
        }
        order.append(email)
        rows.append((rownum, email, row))
    if not employees:
        return {"ok": False, "errors": errors or ["로스터에 인원이 없습니다."], "notes": notes}

    # 동료평가 그룹핑: '동료평가그룹' 태그(콤마로 복수 가능)를 하나라도 공유하면 동료.
    # 태그가 없는 팀원은 같은 (그룹, 팀) 전체가 하나의 그룹이 된다.
    peer_groups, group_labels, member_keys, leaders = {}, {}, {}, []
    for rownum, email, row in rows:
        e = employees[email]
        tags = parse_tags(row.get("동료평가그룹", ""))
        if e["role"] == "팀원":
            keys = [("tag", k) for k, _ in tags] if tags else [("team", e["org_group"], e["team"])]
            member_keys[email] = keys
            for key in keys:
                peer_groups.setdefault(key, []).append(email)
            for k, label in tags:
                group_labels.setdefault(("tag", k), label)
            if not tags:
                group_labels.setdefault(keys[0], f"{e['org_group']} / {e['team']} (팀 자동 묶임)")
        else:
            leaders.append(email)
            if tags:
                notes.append({"level": "warn", "text": f"{rownum}행 {e['name']}(팀장): 팀장에게는 '동료평가그룹'이 적용되지 않습니다 "
                                                         "(팀장은 팀장간평가로만 평가). 의도한 입력인지 확인하세요."})
            if _clean(row.get("리더이메일", "")):
                notes.append({"level": "warn", "text": f"{rownum}행 {e['name']}(팀장): 팀장의 '리더이메일'은 사용되지 않습니다."})

    intended = set()  # (대상 이메일, 작성자 이메일, 관계유형)
    n_self = n_peer = n_l_from_m = n_l_self = n_l_peer = 0
    for rownum, email, row in rows:
        e = employees[email]
        if e["role"] != "팀원":
            continue
        intended.add((email, email, "본인평가")); n_self += 1
        peers = []
        for key in member_keys.get(email, []):
            for other in peer_groups.get(key, []):
                if other != email and other not in peers:
                    peers.append(other)
        for other in peers:
            intended.add((other, email, "동료평가")); n_peer += 1
        raw = _clean(row.get("리더이메일", ""))
        for le in [x.strip() for x in raw.replace("，", ",").replace(";", ",").split(",") if x.strip()]:
            hit = lower_map.get(le.lower())
            if not hit:
                if le.lower() in bad_emails:
                    continue  # 그 행의 오류가 이미 보고됨
                errors.append(f"{rownum}행 {e['name']}: 리더이메일 '{le}'이 로스터에 없습니다"); continue
            leader_email = hit[0]
            if leader_email == email:
                errors.append(f"{rownum}행 {e['name']}: 리더이메일에 본인이 지정되어 있습니다"); continue
            intended.add((leader_email, email, "팀장평가(팀원이줌)")); n_l_from_m += 1
    for le in leaders:
        intended.add((le, le, "본인평가(팀장)")); n_l_self += 1
        for other in leaders:
            if other != le:
                intended.add((other, le, "팀장간평가")); n_l_peer += 1
    if errors:
        return {"ok": False, "errors": errors, "notes": notes}

    return {
        "ok": True, "errors": [], "notes": notes,
        "employees": employees, "order": order, "intended": intended,
        "summary": {
            "인원수": len(employees), "본인평가": n_self, "동료평가": n_peer, "팀장평가(팀원이줌)": n_l_from_m,
            "본인평가(팀장)": n_l_self, "팀장간평가": n_l_peer,
        },
        "peer_group_stats": sorted(
            [{"label": group_labels.get(k, str(k)), "members": len(v)} for k, v in peer_groups.items()],
            key=lambda x: x["label"],
        ),
    }


def apply_plan(plan):
    """검증을 통과한 계획을 DB에 반영한다. 하나의 트랜잭션이라 중간에 실패하면 아무것도 바뀌지 않는다.
    - 같은 이메일(대소문자 무시)의 기존 인원은 같은 사람으로 보고 정보만 갱신 (매핑·응답 유지)
    - 로스터에 더 이상 없는 매핑: 응답이 없으면 정리, 응답이 있으면 지우지 않고 보고"""
    rid = db.get_current_round_id()
    conn = db.get_conn()
    conn.isolation_level = None
    emp_ids = {}  # 소문자 이메일 -> id
    removed_stale, kept_with_responses = [], []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for email, e in plan["employees"].items():
            row = conn.execute("SELECT id FROM employees WHERE lower(email)=lower(?) AND round_id=?", (email, rid)).fetchone()
            if row:
                conn.execute(
                    "UPDATE employees SET name=?, email=?, org_group=?, team=?, job_family=?, grade=?, role=? WHERE id=?",
                    (e["name"], email, e["org_group"], e["team"], e["job_family"], e["grade"], e["role"], row["id"]))
                emp_ids[email.lower()] = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO employees (round_id, name, email, org_group, team, job_family, grade, role, is_admin) "
                    "VALUES (?,?,?,?,?,?,?,?,0)",
                    (rid, e["name"], email, e["org_group"], e["team"], e["job_family"], e["grade"], e["role"]))
                emp_ids[email.lower()] = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO mappings (round_id, target_id, writer_id, relation_type) VALUES (?,?,?,?)",
            [(rid, emp_ids[t.lower()], emp_ids[w.lower()], r) for (t, w, r) in sorted(plan["intended"])])

        intended_l = {(t.lower(), w.lower(), r) for (t, w, r) in plan["intended"]}
        for m in conn.execute("""
            SELECT m.id as mapping_id, m.relation_type, e.name as target_name, e.email as target_email,
                   w.name as writer_name, w.email as writer_email,
                   (SELECT COUNT(*) FROM responses r WHERE r.mapping_id = m.id) as response_count
            FROM mappings m JOIN employees e ON e.id = m.target_id JOIN employees w ON w.id = m.writer_id
            WHERE m.round_id = ?""", (rid,)).fetchall():
            m = dict(m)
            if (m["target_email"].lower(), m["writer_email"].lower(), m["relation_type"]) in intended_l:
                continue
            if m["response_count"] == 0:
                conn.execute("DELETE FROM mappings WHERE id=?", (m["mapping_id"],))
                removed_stale.append(m)
            else:
                kept_with_responses.append(m)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()
    return {"removed_stale": removed_stale, "kept_with_responses": kept_with_responses}


def generate_mappings_from_roster(roster_df):
    """검증 + 반영을 한 번에 수행 (미리보기 없이 바로 반영하는 기존 호출 방식 호환용)."""
    plan = build_plan(roster_df)
    if not plan["ok"]:
        return {"ok": False, "errors": plan["errors"]}
    applied = apply_plan(plan)
    return {
        "ok": True, "errors": [], "summary": plan["summary"], "peer_group_stats": plan["peer_group_stats"],
        "removed_stale": applied["removed_stale"], "kept_with_responses": applied["kept_with_responses"],
    }

"""
관리자가 올리는 '로스터'(인원 명단 + 조직 + 리더 정보)를 받아서,
5가지 관계유형(본인평가/동료평가/팀원→팀장평가/팀장 본인평가/팀장간평가)의
평가 매핑을 자동으로 생성한다.

로스터 필요 컬럼:
  이름, 이메일, 그룹, 팀, 직군, 직급, 역할(팀원|팀장)

로스터 선택 컬럼:
  리더이메일        — 팀원만 해당. 콤마로 복수 지정 가능 (예: "a@x.com,b@x.com")
  동료평가그룹      — 채워두면 '같은 그룹+팀'이 아니라 이 값이 같은 사람들끼리 전부 동료평가로 묶인다.
                     팀 경계와 무관하게 태그 하나로 교차평가/서브그룹을 만들 수 있다.
                     (예: A팀·B팀 10명을 교차평가시키고 싶으면 10명 모두에게 같은 태그를 적으면 됨 —
                     이메일을 서로 여러 개씩 적을 필요가 없다)
"""
import db


def generate_mappings_from_roster(roster_df):
    errors = []
    required_cols = ["이름", "이메일", "그룹", "팀", "직군", "직급", "역할"]
    missing = [c for c in required_cols if c not in roster_df.columns]
    if missing:
        errors.append("필수 컬럼 누락: " + ", ".join(missing))
        errors.append("업로드하신 파일의 실제 컬럼: " + ", ".join(str(c) for c in roster_df.columns.tolist()))
        return {"ok": False, "errors": errors}

    if "리더이메일" not in roster_df.columns:
        roster_df["리더이메일"] = ""
    if "동료평가그룹" not in roster_df.columns:
        roster_df["동료평가그룹"] = ""

    email_to_id = {}
    for _, row in roster_df.iterrows():
        role = str(row["역할"]).strip()
        if role not in ("팀원", "팀장"):
            errors.append(f"{row['이름']}: 역할 값이 '팀원'/'팀장'이 아님 ({role})")
            continue
        emp_id = db.upsert_employee(
            name=str(row["이름"]).strip(),
            email=str(row["이메일"]).strip(),
            org_group=str(row["그룹"]).strip(),
            team=str(row["팀"]).strip(),
            job_family=str(row["직군"]).strip(),
            grade=str(row["직급"]).strip(),
            role=role,
        )
        email_to_id[str(row["이메일"]).strip()] = emp_id

    if errors:
        return {"ok": False, "errors": errors}

    # 동료평가 그룹핑 키: '동료평가그룹' 태그가 있으면 그 값, 없으면 (그룹, 팀)
    def peer_key(row):
        tag = str(row.get("동료평가그룹", "")).strip()
        if tag:
            return ("tag", tag)
        return ("team", str(row["그룹"]).strip(), str(row["팀"]).strip())

    peer_groups = {}
    leaders = []
    for _, row in roster_df.iterrows():
        role = str(row["역할"]).strip()
        email = str(row["이메일"]).strip()
        if role == "팀원":
            peer_groups.setdefault(peer_key(row), []).append(email)
        else:
            leaders.append(email)

    n_self = n_peer = n_leader_from_member = n_leader_self = n_leader_peer = 0
    intended = set()  # (대상 이메일, 작성자 이메일, 관계유형) — 재업로드 시 stale 매핑 판별용

    def add(target_email, writer_email, relation_type):
        db.add_mapping(email_to_id[target_email], email_to_id[writer_email], relation_type)
        intended.add((target_email, writer_email, relation_type))

    # 팀원: 본인평가 + 동료평가(그룹 키 기준) + 팀장평가(팀원이줌)
    for _, row in roster_df.iterrows():
        role = str(row["역할"]).strip()
        if role != "팀원":
            continue
        email = str(row["이메일"]).strip()

        add(email, email, "본인평가")
        n_self += 1

        for other_email in peer_groups.get(peer_key(row), []):
            if other_email == email:
                continue
            add(other_email, email, "동료평가")
            n_peer += 1

        leader_emails = [e.strip() for e in str(row.get("리더이메일", "")).split(",") if e.strip()]
        for le in leader_emails:
            if le not in email_to_id:
                errors.append(f"{row['이름']}: 리더이메일 '{le}'이 로스터에 없음")
                continue
            add(le, email, "팀장평가(팀원이줌)")
            n_leader_from_member += 1

    # 팀장: 본인평가(팀장) + 팀장간평가
    for le in leaders:
        add(le, le, "본인평가(팀장)")
        n_leader_self += 1
        for other_le in leaders:
            if other_le == le:
                continue
            add(other_le, le, "팀장간평가")
            n_leader_peer += 1

    if errors:
        return {"ok": False, "errors": errors}

    # --- 재업로드 안전장치 ---
    # 이번 로스터가 의도하는 매핑 집합(intended)에 더 이상 포함되지 않는 기존 매핑을 찾아서,
    # 응답이 하나도 없는 것만 조용히 정리하고, 응답이 이미 있는 건 절대 지우지 않고 목록으로만 보고한다.
    removed_stale = []
    kept_with_responses = []
    for m in db.get_all_mapping_keys():
        key = (m["target_email"], m["writer_email"], m["relation_type"])
        if key in intended:
            continue
        if db.delete_mapping(m["mapping_id"]):
            removed_stale.append(m)
        else:
            kept_with_responses.append(m)

    return {
        "ok": True,
        "errors": [],
        "summary": {
            "인원수": len(email_to_id),
            "본인평가": n_self,
            "동료평가": n_peer,
            "팀장평가(팀원이줌)": n_leader_from_member,
            "본인평가(팀장)": n_leader_self,
            "팀장간평가": n_leader_peer,
        },
        "removed_stale": removed_stale,
        "kept_with_responses": kept_with_responses,
    }

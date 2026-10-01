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
        role = _clean(row["역할"])
        if role not in ("팀원", "팀장"):
            errors.append(f"{row['이름']}: 역할 값이 '팀원'/'팀장'이 아님 ({role})")
            continue
        emp_id = db.upsert_employee(
            name=_clean(row["이름"]),
            email=_clean(row["이메일"]),
            org_group=_clean(row["그룹"]),
            team=_clean(row["팀"]),
            job_family=_clean(row["직군"]),
            grade=_clean(row["직급"]),
            role=role,
        )
        email_to_id[_clean(row["이메일"])] = emp_id

    if errors:
        return {"ok": False, "errors": errors}

    # 동료평가 그룹핑: '동료평가그룹' 태그(콤마로 복수 가능)를 하나라도 공유하면 동료.
    # 태그가 없는 팀원은 기존처럼 같은 (그룹, 팀) 전체가 하나의 그룹이 된다.
    def group_keys(row):
        tags = parse_tags(row.get("동료평가그룹", ""))
        if tags:
            return [("tag", k) for k, _ in tags]
        return [("team", _clean(row["그룹"]), _clean(row["팀"]))]

    peer_groups = {}     # 그룹키 -> [팀원 이메일]
    group_labels = {}    # 그룹키 -> 화면 표시용 이름
    member_keys = {}     # 팀원 이메일 -> [그룹키]
    leaders = []
    for _, row in roster_df.iterrows():
        role = _clean(row["역할"])
        email = _clean(row["이메일"])
        if role == "팀원":
            keys = group_keys(row)
            member_keys[email] = keys
            for key in keys:
                peer_groups.setdefault(key, []).append(email)
            for k, label in parse_tags(row.get("동료평가그룹", "")):
                group_labels.setdefault(("tag", k), label)
            if keys[0][0] == "team":
                group_labels.setdefault(keys[0], f"{keys[0][1]} / {keys[0][2]} (팀 자동 묶임)")
        else:
            leaders.append(email)

    n_self = n_peer = n_leader_from_member = n_leader_self = n_leader_peer = 0
    intended = set()  # (대상 이메일, 작성자 이메일, 관계유형) — 재업로드 시 stale 매핑 판별용

    def add(target_email, writer_email, relation_type):
        db.add_mapping(email_to_id[target_email], email_to_id[writer_email], relation_type)
        intended.add((target_email, writer_email, relation_type))

    # 팀원: 본인평가 + 동료평가(그룹 키 기준) + 팀장평가(팀원이줌)
    for _, row in roster_df.iterrows():
        role = _clean(row["역할"])
        if role != "팀원":
            continue
        email = _clean(row["이메일"])

        add(email, email, "본인평가")
        n_self += 1

        peers = []  # 여러 그룹에 같이 속한 사람은 한 번만 (순서 유지하며 중복 제거)
        for key in member_keys.get(email, []):
            for other_email in peer_groups.get(key, []):
                if other_email != email and other_email not in peers:
                    peers.append(other_email)
        for other_email in peers:
            add(other_email, email, "동료평가")
            n_peer += 1

        leader_emails = [e.strip() for e in _clean(row.get("리더이메일", "")).split(",") if e.strip()]
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
        "peer_group_stats": sorted(
            [{"label": group_labels.get(k, str(k)), "members": len(v)} for k, v in peer_groups.items()],
            key=lambda x: x["label"],
        ),
        "removed_stale": removed_stale,
        "kept_with_responses": kept_with_responses,
    }

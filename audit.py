"""
매핑 점검(audit) 모듈 — '누가 누구를 평가하는지'가 의도대로 연결됐는지 사람이 눈으로 다 볼 수 없는 규모(220명)에서
이상 징후를 자동으로 찾아준다. DB를 읽기만 하며 아무것도 바꾸지 않는다.

같은 분석 함수를 두 곳에서 쓴다.
  1) 로스터 업로드 미리보기: 아직 반영하기 전의 '반영 후 상태'를 점검
  2) 매핑 점검 화면: 현재 DB에 들어 있는 상태를 점검

분석 입력은 DB와 무관한 순수 자료구조다.
  employees: {이메일: {name, org_group, team, job_family, grade, role}}
  mappings : {(대상 이메일, 작성자 이메일, 관계유형), ...}
"""
import re
import hashlib
from collections import Counter, defaultdict

import db

SELF = "본인평가"
PEER = "동료평가"
MEM2L = "팀장평가(팀원이줌)"
LSELF = "본인평가(팀장)"
LPEER = "팀장간평가"
RELATIONS = [SELF, PEER, MEM2L, LSELF, LPEER]

PEER_MANY_THRESHOLD = 20  # 동료가 이 인원보다 많으면 '확인 필요' (조직 규모에 맞게 조정 가능)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2}
LEVEL_LABEL = {"error": "오류", "warn": "확인 필요", "info": "참고"}


def _nm(employees, email):
    e = employees.get(email)
    if not e:
        return email
    place = "/".join(x for x in (e.get("org_group"), e.get("team")) if x and x != "-")
    return f"{e['name']}({place})" if place else e["name"]


def analyze(employees, mappings, peer_threshold=PEER_MANY_THRESHOLD):
    """이상 징후와 인원별 통계를 계산한다. 결과는 템플릿에서 그대로 쓸 수 있는 dict."""
    peers_out = defaultdict(set)      # 작성자 -> 동료평가 대상들
    leaders_of = defaultdict(set)     # 팀원(작성자) -> 팀장평가 대상(리더)들
    raters_of = defaultdict(set)      # 리더(대상) -> 팀장평가를 하는 팀원들
    lpeer_out = defaultdict(set)      # 팀장(작성자) -> 팀장간평가 대상들
    selfs = set()
    counts = Counter()
    for t, w, r in mappings:
        counts[r] += 1
        if r == PEER:
            peers_out[w].add(t)
        elif r == MEM2L:
            leaders_of[w].add(t)
            raters_of[t].add(w)
        elif r == LPEER:
            lpeer_out[w].add(t)
        elif r in (SELF, LSELF):
            selfs.add((t, w, r))

    members = sorted(e for e, v in employees.items() if v["role"] == "팀원")
    leaders = sorted(e for e, v in employees.items() if v["role"] == "팀장")
    team_of = {e: (v["org_group"], v["team"]) for e, v in employees.items()}
    teams = defaultdict(list)
    for m in members:
        teams[team_of[m]].append(m)

    groups = {}  # code -> finding group

    def add(code, level, title, hint, emails, text):
        g = groups.setdefault(code, {"code": code, "level": level, "title": title, "hint": hint, "items": []})
        g["items"].append({"emails": list(emails), "text": text})

    N = lambda e: _nm(employees, e)  # noqa: E731

    # --- 팀장평가(리더 지정) 관련 ---
    for m in members:
        ls = leaders_of.get(m, set())
        if not ls:
            add("leader_missing", "warn", "리더(팀장)가 지정되지 않은 팀원",
                "이 팀원은 팀장평가를 하지 않고, 팀장도 이 팀원에게 평가받지 못합니다. 의도한 게 아니라면 '리더이메일'을 채우세요.",
                [m], f"{N(m)} — 리더 없음")
            continue
        for l in sorted(ls):
            if employees.get(l, {}).get("role") != "팀장":
                add("leader_not_leader", "warn", "'팀장'이 아닌 사람이 리더로 지정됨",
                    "리더이메일에 팀원 역할인 사람의 이메일이 들어간 경우입니다. 이메일 오입력일 수 있습니다.",
                    [m, l], f"{N(m)} → 리더 {N(l)} (역할: {employees.get(l, {}).get('role', '?')})")
            elif team_of.get(l) != team_of.get(m):
                add("leader_other_team", "warn", "다른 팀의 팀장이 리더로 지정됨",
                    "팀원과 리더의 그룹/팀이 다릅니다. 겸직·파견 등 의도된 경우가 아니면 이메일 오입력을 의심하세요.",
                    [m, l], f"{N(m)} → 리더 {N(l)}")
        if len(ls) >= 2:
            add("multi_leader", "info", "리더가 2명 이상인 팀원",
                "의도한 겸직/복수 리더라면 정상입니다. 모든 리더에 대해 팀장평가가 각각 생성됩니다.",
                [m] + sorted(ls), f"{N(m)} → " + ", ".join(N(l) for l in sorted(ls)))

    for l in leaders:
        if not raters_of.get(l):
            add("leader_unrated", "warn", "팀장평가를 받을 팀원이 없는 팀장",
                "이 팀장을 리더로 지정한 팀원이 없어 팀원→팀장 평가가 생성되지 않았습니다.",
                [l], f"{N(l)} — 이 팀장을 평가할 팀원 없음")

    # 같은 팀인데 리더가 서로 다른 경우 (소수 쪽을 지목)
    for team, ms in sorted(teams.items()):
        sets = {m: frozenset(leaders_of.get(m, ())) for m in ms if leaders_of.get(m)}
        if len({s for s in sets.values()}) > 1:
            common, n_common = Counter(sets.values()).most_common(1)[0]
            for m, s in sets.items():
                if s != common:
                    add("team_leader_mixed", "warn", "같은 팀인데 리더가 다른 팀원",
                        "같은 팀 팀원 대부분과 다른 리더가 지정된 사람입니다. 리더 이메일 오타일 가능성이 있습니다.",
                        [m] + sorted(s), f"{N(m)}: 리더 {', '.join(N(x) for x in sorted(s))} "
                                         f"(팀 다수 {n_common}명은 {', '.join(N(x) for x in sorted(common))})")

    # --- 동료평가 관련 ---
    for m in members:
        n = len(peers_out.get(m, ()))
        if n == 0:
            add("no_peers", "warn", "평가할 동료가 한 명도 없는 팀원",
                "팀에 혼자이거나, 동료평가그룹 태그 오타로 혼자 떨어진 경우입니다.",
                [m], f"{N(m)} — 동료 0명")
        elif n > peer_threshold:
            add("many_peers", "warn", f"평가할 동료가 {peer_threshold}명을 넘는 팀원",
                "그룹이 의도보다 크게 묶였을 수 있습니다(팀 구분 누락, 태그 중복 등). 의도한 큰 그룹이면 무시하세요.",
                [m], f"{N(m)} — 동료 {n}명")

    for team, ms in sorted(teams.items()):
        if len(ms) < 2:
            continue
        closed = {m: frozenset(peers_out.get(m, set()) | {m}) for m in ms}
        if len(set(closed.values())) > 1:
            common, n_common = Counter(closed.values()).most_common(1)[0]
            for m, s in closed.items():
                if s != common:
                    add("team_peer_mismatch", "warn", "같은 팀인데 동료 구성이 다른 팀원",
                        "같은 팀 대부분과 동료 구성이 다른 사람입니다. 일부만 동료평가그룹 태그가 있거나, 태그가 다르게 적힌 경우입니다. "
                        "교차 협업 때문에 의도한 것이면 무시하세요.",
                        [m], f"{N(m)} — 동료 {len(s) - 1}명 (같은 팀 다수 {n_common}명은 {len(common) - 1}명)")

    # --- 인원 정보(이름/이메일) ---
    by_name = defaultdict(list)
    for e, v in employees.items():
        by_name[v["name"]].append(e)
    for name, es in sorted(by_name.items()):
        if len(es) > 1:
            add("dup_name", "warn", "동명이인",
                "평가 화면에는 이름만 표시되어 평가자가 구분하기 어렵습니다. 이름에 팀/구분 표기를 붙이는 것을 권합니다.",
                es, f"{name} {len(es)}명: " + " / ".join(N(e) for e in es))

    domains = Counter(e.split("@")[-1].lower() for e in employees if "@" in e)
    top_domain, top_n = (domains.most_common(1) or [("", 0)])[0]
    for e in sorted(employees):
        if e != e.strip() or " " in e:
            add("email_format", "warn", "이메일 형식 의심", "공백이 포함된 이메일은 Slack 로그인과 일치하지 않습니다.",
                [e], f"{N(e)} — 공백 포함 ('{e}')")
        elif not EMAIL_RE.match(e):
            add("email_format", "warn", "이메일 형식 의심", "형식이 올바르지 않은 이메일입니다.",
                [e], f"{N(e)} — '{e}'")
        elif e != e.lower():
            add("email_case", "warn", "이메일에 대문자 포함",
                "현재 로그인은 이메일 대소문자를 구분해 비교합니다. Slack이 소문자로 돌려주면 이 사람은 '명단에 없음'으로 "
                "로그인이 막힐 수 있습니다. 소문자로 통일하길 권합니다.",
                [e], f"{N(e)} — {e}")
        d = e.split("@")[-1].lower() if "@" in e else ""
        if d and top_n and d != top_domain and domains[d] <= max(2, len(employees) * 0.05) and top_n / max(len(employees), 1) >= 0.6:
            add("email_domain", "warn", "다수와 다른 이메일 도메인",
                f"대부분 @{top_domain} 인데 이 사람만 다릅니다. 도메인 오타일 수 있습니다.",
                [e], f"{N(e)} — @{d}")

    # --- 팀장간평가 ---
    if len(leaders) == 1:
        add("single_leader", "info", "팀장이 1명뿐입니다", "팀장간평가가 생성되지 않습니다. 팀장 역할 입력이 맞는지 확인하세요.",
            leaders, f"{N(leaders[0])} 한 명")
    elif not leaders and members:
        add("single_leader", "info", "팀장 역할의 인원이 없습니다", "팀장평가·팀장간평가가 생성되지 않습니다.", [], "팀장 0명")

    # --- 내부 정합성 (정상이라면 나오지 않아야 함) ---
    for e, v in sorted(employees.items()):
        need = (e, e, SELF if v["role"] == "팀원" else LSELF)
        if need not in selfs:
            add("integrity_self", "warn", "본인평가가 없는 인원", "로스터 반영 중 문제가 있었을 수 있습니다. 로스터를 다시 업로드해 보세요.",
                [e], f"{N(e)} — 본인평가 매핑 없음")
    for w, ts in peers_out.items():
        for t in sorted(ts):
            if w not in peers_out.get(t, ()):
                add("integrity_peer", "warn", "한쪽 방향만 있는 동료평가",
                    "A→B 동료평가는 있는데 B→A는 없습니다. 로스터 변경 뒤 응답 때문에 남은 매핑이거나, 데이터 불일치입니다.",
                    [w, t], f"{N(w)} → {N(t)} 만 존재")

    # --- 정렬 / 인원별 통계 ---
    findings = sorted(groups.values(), key=lambda g: (LEVEL_ORDER[g["level"]], g["title"]))
    for g in findings:
        g["level_label"] = LEVEL_LABEL[g["level"]]
        g["count"] = len(g["items"])

    flags = Counter()
    for g in findings:
        if g["level"] == "info":
            continue
        for it in g["items"]:
            for e in set(it["emails"][:1] if g["code"] in ("leader_not_leader", "leader_other_team", "team_leader_mixed") else it["emails"]):
                flags[e] += 1

    per_person = {}
    for e, v in employees.items():
        per_person[e] = {
            "email": e, "name": v["name"], "org_group": v["org_group"], "team": v["team"], "grade": v["grade"], "role": v["role"],
            "peers": len(peers_out.get(e, ())),
            "leaders": [N(x) for x in sorted(leaders_of.get(e, ()))],
            "raters": len(raters_of.get(e, ())),
            "lpeers": len(lpeer_out.get(e, ())),
            "flags": flags.get(e, 0),
        }

    return {
        "findings": findings,
        "n_warn": sum(g["count"] for g in findings if g["level"] != "info"),
        "n_groups_warn": sum(1 for g in findings if g["level"] != "info"),
        "per_person": per_person,
        "stats": {
            "employees": len(employees), "members": len(members), "leaders": len(leaders), "teams": len(teams),
            "by_relation": {r: counts.get(r, 0) for r in RELATIONS},
            "total": sum(counts.values()),
        },
    }


# ---------------------------------------------------------------
# DB -> 분석 입력
# ---------------------------------------------------------------
def build_db_state():
    """현재 회차 DB를 분석 입력 형태로 변환. 매핑이 없는 관리자 전용 계정은 제외한다."""
    emps = db.list_employees()
    keys = db.get_all_mapping_keys()
    in_map = set()
    for m in keys:
        in_map.add(m["target_email"])
        in_map.add(m["writer_email"])
    employees = {}
    for e in emps:
        if e["is_admin"] and e["email"] not in in_map:
            continue
        employees[e["email"]] = {k: e[k] for k in ("name", "org_group", "team", "job_family", "grade", "role")}
    mappings = {(m["target_email"], m["writer_email"], m["relation_type"]) for m in keys}
    return employees, mappings, keys


# ---------------------------------------------------------------
# 로스터 업로드 미리보기: 반영하면 무엇이 바뀌는가
# ---------------------------------------------------------------
_EMP_FIELDS = [("name", "이름"), ("org_group", "그룹"), ("team", "팀"), ("job_family", "직군"), ("grade", "직급"), ("role", "역할")]


def diff_plan_with_db(plan):
    """로스터(plan)를 반영했을 때 인원/매핑이 어떻게 바뀌는지 계산한다 (DB는 읽기만)."""
    db_emps = {e["email"].lower(): e for e in db.list_employees()}
    keys = db.get_all_mapping_keys()
    plan_emps = {e.lower(): dict(v, email=e) for e, v in plan["employees"].items()}

    new_people = [v for k, v in plan_emps.items() if k not in db_emps]
    changed = []
    for k, v in plan_emps.items():
        old = db_emps.get(k)
        if not old:
            continue
        diffs = [(label, old[f], v[f]) for f, label in _EMP_FIELDS if (old[f] or "") != (v[f] or "")]
        if old["email"] != v["email"]:
            diffs.append(("이메일(대소문자)", old["email"], v["email"]))
        if diffs:
            changed.append({"name": v["name"], "email": v["email"], "diffs": diffs})
    missing = [e for k, e in db_emps.items() if k not in plan_emps and not e["is_admin"]]

    plan_keys = {(t.lower(), w.lower(), r) for (t, w, r) in plan["intended"]}
    db_keys = {(m["target_email"].lower(), m["writer_email"].lower(), m["relation_type"]): m for m in keys}
    add_keys = sorted(plan_keys - set(db_keys))
    stale = [m for k, m in db_keys.items() if k not in plan_keys]
    remove = [m for m in stale if m["response_count"] == 0]
    keep = [m for m in stale if m["response_count"] > 0]

    def pname(email_l):
        return plan_emps.get(email_l, {}).get("name", email_l)

    impact = defaultdict(lambda: {"add": 0, "remove": 0, "keep": 0})
    by_rel = {r: {"add": 0, "remove": 0, "keep": 0} for r in RELATIONS}
    for t, w, r in add_keys:
        impact[w]["add"] += 1
        by_rel.setdefault(r, {"add": 0, "remove": 0, "keep": 0})["add"] += 1
    for m in remove:
        impact[m["writer_email"].lower()]["remove"] += 1
        by_rel.setdefault(m["relation_type"], {"add": 0, "remove": 0, "keep": 0})["remove"] += 1
    for m in keep:
        impact[m["writer_email"].lower()]["keep"] += 1
        by_rel.setdefault(m["relation_type"], {"add": 0, "remove": 0, "keep": 0})["keep"] += 1

    def display(email_l):
        if email_l in plan_emps:
            return plan_emps[email_l]["name"], plan_emps[email_l]["email"]
        if email_l in db_emps:
            return db_emps[email_l]["name"], db_emps[email_l]["email"]
        return email_l, email_l

    impact_rows = []
    for k, v in impact.items():
        name, email = display(k)
        impact_rows.append({"name": name, "email": email, **v, "total": v["add"] + v["remove"] + v["keep"]})
    impact_rows.sort(key=lambda r: (-r["total"], r["name"]))

    existing_total = len(keys)
    first_upload = existing_total == 0 and not [e for e in db_emps.values() if not e["is_admin"]]
    ratio = (len(add_keys) + len(remove)) / existing_total if existing_total else 0.0

    sig_src = "|".join([
        ",".join(sorted(f"{t}>{w}>{r}" for t, w, r in add_keys)),
        ",".join(sorted(str(m["mapping_id"]) for m in remove)),
        ",".join(sorted(str(m["mapping_id"]) for m in keep)),
        ",".join(sorted(v["email"] for v in new_people)),
        ",".join(sorted(c["email"] for c in changed)),
    ])
    return {
        "first_upload": first_upload,
        "existing_total": existing_total,
        "new_people": sorted(new_people, key=lambda v: v["name"]),
        "changed": sorted(changed, key=lambda c: c["name"]),
        "missing": sorted(missing, key=lambda e: e["name"]),
        "n_add": len(add_keys), "n_remove": len(remove), "n_keep": len(keep),
        "remove": remove, "keep": keep,
        "by_relation": by_rel,
        "impact": impact_rows,
        "change_ratio": ratio,
        "large_change": existing_total > 0 and ratio > 0.2,
        "no_change": not (new_people or changed or add_keys or stale),
        "signature": hashlib.sha256(sig_src.encode("utf-8")).hexdigest(),
    }

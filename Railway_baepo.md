# Railway 배포 가이드 (터미널 명령어 없이, 화면 클릭으로만)

## 0. 순서 요약
1. GitHub에 이 폴더를 새 저장소로 올린다 (웹 화면에서 드래그&드롭, git 명령어 불필요)
2. Railway에서 "GitHub Repository"로 그 저장소를 연결한다
3. Railway 프로젝트의 **Variables** 탭에 환경변수를 넣는다
4. Railway 프로젝트에 **Volume(영구 저장공간)**을 하나 만들어서 DB가 안전하게 보관되게 한다
5. 배포 완료 후 생긴 주소를 Slack 앱의 Redirect URI에 등록한다

---

## 1. GitHub에 코드 올리기 (git 명령어 없이)
1. github.com 가입 (Railway 가입 때 이미 GitHub으로 로그인했다면 이미 계정 있음)
2. 우측 상단 `+` → `New repository` → 이름 입력(예: `damyeon-app`) → **Private**로 설정 → `Create repository`
3. 만들어진 빈 저장소 화면에서 `uploading an existing file` 링크 클릭
4. 이 폴더 안의 파일/폴더를 전부 끌어다 놓기 (`main.py`, `db.py`, `roster.py`, `questions.py`, `requirements.txt`, `Procfile`, `templates/`, `static/` 등)
5. 아래 `Commit changes` 버튼 클릭

> `collect.db`, `__pycache__` 폴더는 올리지 않아도 됨(있어도 무방하지만 불필요).

## 2. Railway에서 저장소 연결
1. Railway 프로젝트 화면에서 `GitHub Repository` 클릭
2. 방금 만든 저장소 선택 (처음이면 Railway가 GitHub 접근 권한을 요청함 → 허용)
3. 자동으로 `requirements.txt`를 보고 Python 프로젝트로 인식 → `Procfile`의 실행 명령으로 서버 시작

## 3. 환경변수 설정 (Railway → 프로젝트 클릭 → Variables 탭)

| 이름 | 값 | 설명 |
|---|---|---|
| `SESSION_SECRET_KEY` | 아무 긴 랜덤 문자열 (예: 32자 이상) | 로그인 세션 서명용. **꼭 직접 넣기** — 안 넣으면 서버 재시작마다 전원 로그아웃됨 |
| `SESSION_HTTPS_ONLY` | `true` | Railway가 기본으로 https 제공하므로 그대로 true |
| `SLACK_CLIENT_ID` | Slack 앱의 Client ID | 드라이런 때 이미 만든 로그인 전용 Slack 앱 값 |
| `SLACK_CLIENT_SECRET` | Slack 앱의 Client Secret | 절대 코드에 넣지 말 것 — 여기(환경변수)에만 |
| `SLACK_REDIRECT_URI` | `https://<배포된 주소>/auth/slack/callback` | 5단계에서 배포 주소가 나온 뒤에 채우고 다시 저장 |
| `SLACK_TEAM_ID` | 회사 Slack 워크스페이스 ID | 기존 드라이런 때 쓰던 값 그대로 |
| `DATA_DIR` | `/data` | 4단계에서 만들 Volume의 마운트 경로와 동일하게 |

위 값을 넣을 때마다 Railway가 자동으로 재배포함(정상).

## 4. Volume(영구 저장공간) 만들기 — 중요! 이거 안 하면 재배포될 때마다 DB가 사라짐
1. 프로젝트 화면에서 서비스 클릭 → `Settings` 탭 → `Volumes` 항목 → `New Volume`
2. Mount path에 `/data` 입력 (위 `DATA_DIR` 값과 똑같이)
3. 저장

## 5. 배포 확인 및 Slack Redirect URI 최종 등록
1. Railway 프로젝트 화면 상단에 생성된 도메인(`xxx.up.railway.app` 형태) 클릭해서 접속되는지 확인
2. api.slack.com/apps → 로그인용 Slack 앱 → `OAuth & Permissions` → Redirect URLs에 `https://그 도메인/auth/slack/callback` 등록
3. Railway Variables의 `SLACK_REDIRECT_URI`도 같은 주소로 맞추기

## 6. 이후 코드 수정이 필요할 때
GitHub 저장소 화면에서 파일 열고 연필(✏️) 아이콘으로 직접 수정 → Commit 하면, Railway가 자동으로 감지해서 재배포함. 터미널 필요 없음.

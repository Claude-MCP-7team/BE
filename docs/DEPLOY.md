# 배포

배포의 산출물은 **`Dockerfile` 하나**다. `render.yaml` 은 Render 용 편의 파일이고,
플랫폼을 바꾸면 그것만 버리면 된다.

CI 가 매 푸시마다 이 이미지를 실제로 빌드하고 띄워서 확인한다 (`docker` 잡) —
`/readyz` 준비, 판정 1건, CORS 의 ETag 노출, 비루트 실행, 배치 의존성 부재.
Dockerfile 을 고쳤는데 CI 가 초록이면 그 이미지는 뜬다.

```bash
docker build -t ypc-backend .
docker run --rm -p 8000:8000 \
  -e SNAPSHOT_PATH=/app/data/demo/snapshot.json \
  -e CORS_ORIGINS=https://ypc-fe.example \
  ypc-backend
```

## 환경변수

| 변수 | 없으면 | 비고 |
| --- | --- | --- |
| `SNAPSHOT_PATH` | **미준비로 기동** (`/readyz` 503) | 이미지 안에 `/app/data/demo/snapshot.json` 이 들어 있다 |
| `CORS_ORIGINS` | 브라우저에서 못 부른다 | 쉼표 구분. 서버 간 호출은 계속 된다 |
| `PROFILE_ENC_KEYS` | 세션 저장만 503 | 판정은 계속 된다. 평문 폴백은 없다 |
| `DATABASE_URL` | `/v1/sessions` 만 503 | 판정 경로는 DB 를 안 쓴다 (ADR-001) |
| `PORT` | 8000 | PaaS 가 주입한다 |
| `YPC_FIXED_TODAY` | 실제 KST 오늘 | 데모 기준일 고정용 |

**없어도 기동한다는 점이 설계다.** 설정 하나가 빠졌다고 프로세스가 죽으면, 그 설정과
무관한 기능까지 같이 멈춘다. 대신 무엇이 꺼졌는지 기동 로그에 남는다.

## 세션 저장을 켜는 절차 (Render)

`/v1/sessions` 가 503 `session-store-unavailable` 이면 **둘 중 하나가 없는 것**이다 —
DB 연결, 또는 암호화 키. 판정은 그동안에도 정상 동작한다 (ADR-001).

키가 없는 상태로 평문 저장하는 경로는 만들지 않았다. 설정 실수 한 번으로 개인정보가
평문으로 쌓이고, 그건 아무 증상 없이 계속된다.

### 1. PostgreSQL 만들기

Render 대시보드 → **New → PostgreSQL** (free 플랜). 생성되면 **Internal Database URL**
을 복사한다 (External 이 아니라 Internal — 같은 리전 안에서는 외부로 나갔다 오지
않는다).

### 2. 마이그레이션 적용

Render 의 무료 웹 서비스에는 셸이 없다. **로컬에서 External URL 로 한 번 적용한다:**

```bash
# Render 대시보드 → PostgreSQL → Connect → External Connection 의 psql 명령
psql "<EXTERNAL_DATABASE_URL>" -v ON_ERROR_STOP=1 -f db/migrations/0001_init.sql
psql "<EXTERNAL_DATABASE_URL>" -f db/verify_schema.sql   # 제약조건 확인
```

**Internal 이 아니라 External URL 이다.** Internal(`dpg-...-a`, 도메인 없음)은 Render
내부 네트워크에서만 열리고, 개발자 PC 에서는 붙지 않는다. Internal 은 3번에서
`DATABASE_URL` 로 쓴다.

`0001_init.sql` 은 재실행 안전하지 않다. **한 번만** 돌린다. 다만 파일 전체가
`BEGIN`/`COMMIT` 으로 감싸여 있어, 중간에 실패하면 전부 롤백된다 — 실패한 뒤에
다시 돌리는 것은 안전하다.

#### 한국어 Windows: `PGCLIENTENCODING` 을 먼저 준다

```powershell
$env:PGCLIENTENCODING = "UTF8"   # 이거 없으면 아래 에러가 난다
chcp 65001                       # 출력 한글이 깨지는 것도 같이 잡힌다
```

없으면 이렇게 멈춘다:

```
ERROR: character with byte sequence 0x80 0xec in encoding "UHC"
       has no equivalent in encoding "UTF8"
```

마이그레이션 파일에 한글 주석·제약조건명이 들어 있는데(비ASCII 1,678바이트),
`psql` 은 클라이언트 인코딩을 **콘솔 코드페이지**에서 가져온다. 한국어 Windows 는
그게 UHC(cp949) 라서, UTF-8 파일을 cp949 로 읽으려다 죽는다. 파일이 깨진 게 아니다.

CLAUDE.md 가 경고하는 그 cp949 함정이 배포 절차에서 다시 나온 경우다. 파이썬 쪽은
`encoding="utf-8"` 과 `force_utf8_console()` 로 막아 뒀지만, `psql` 은 우리 코드가
아니라서 환경변수로 줘야 한다.

#### `verify_schema.sql` 은 에러가 나는 게 정상이다

제약조건이 실제로 막는지 확인하려고 **일부러 위반을 시도**한다. `[MUST FAIL]` 로
표시된 항목에서 ERROR 가 나와야 통과다. 13건의 기대값:

| | 기대 |
| --- | --- |
| 1·2·4·5·10·11·13 | **ERROR** (제약조건이 막아야 함) |
| 3·6·12 | INSERT 성공 |
| 7 | INSERT 1건 후 ERROR (활성 스냅샷은 항상 1개) |
| 8·9 | 지역 접두체인 조회 — 각각 3건 / 1건 |

ERROR 가 **안 나오면** 그게 문제다. 제약조건이 빠진 채로 배포된 것이다.

### 3. 환경변수 3개

| 키 | 값 |
| --- | --- |
| `DATABASE_URL` | 1번의 **Internal** Database URL |
| `PROFILE_ENC_KEYS` | `1:<키>` — 아래 명령으로 생성 |
| `CORS_ORIGINS` | FE 출처 (아래 참고) |

```bash
python -c "from app.core.crypto import generate_key; print('1:' + generate_key())"
```

**`1:` 접두사가 필수다.** 키 회전을 위한 버전 번호이고, 빼면 키를 못 읽어 세션 API 가
계속 503 이다 (판정은 계속 된다).

### 4. 확인

```bash
curl -s https://<서비스>/readyz | python -m json.tool
# database.configured=true, database.ready=true 여야 한다
```

`configured=true, ready=false` 면 DSN 은 읽혔는데 연결이 안 되는 것이다 — Internal URL
을 썼는지, 같은 리전인지 본다.

## 재배포 (Render)

**서비스 주소는 `https://be-27y9.onrender.com` 이고, 대시보드의 서비스 이름은
`be-27y9` 다.** `render.yaml` 에는 `ypc-backend` 로 적혀 있지만 대시보드에서는
다른 이름으로 만들어졌다 — 이름으로 찾으면 못 찾는다.

DB(`ypc-db`) 페이지가 아니라 **웹 서비스** 페이지에서 한다. DB 페이지에 들어가
있다면 사이드바 맨 위 **← Environment** 로 나가서 `WEB SERVICE` 딱지가 붙은 쪽을
고른다.

우측 상단 **Manual Deploy → Deploy latest commit**.
`Clear build cache & deploy` 는 캐시가 꼬였을 때만 — 느리기만 하다.

### 배포됐는지는 대시보드가 아니라 응답으로 확인한다

대시보드에 **Live** 라고 떠 있어도 옛 커밋일 수 있다. 실제로 한 번 그랬고,
15커밋이 밀려 있는 걸 FE 가 500 을 보고해서야 알았다. 커밋 해시를 눈으로 맞추는
것도 놓치기 쉬우니 응답을 받아 본다.

```bash
curl -sS https://be-27y9.onrender.com/v1/meta/snapshot
# {"ready":true, ..., "policy_count":3, "rule_count":10, ...}
```

`policy_count` 가 기대한 건수인지 본다. 빌드 로그(**Logs** 탭)에도 같은 수가
`snapshot: N policies` 로 한 줄 찍힌다 — 무료 플랜은 셸이 없어서 이미지 안을
들여다볼 방법이 이것뿐이다.

목록의 내용까지 보려면:

```bash
curl -sS https://be-27y9.onrender.com/v1/policies
```

**자동 배포가 꺼져 있지 않은지도 한 번 본다.** Settings → Build & Deploy 의
**Auto-Deploy** 가 `Yes` 인지, **Branch** 가 `dev` 인지. 브랜치가 `main` 이면
`dev` 에 아무리 푸시해도 배포되지 않는데, 어디에도 에러가 뜨지 않는다.

### PowerShell 에서는 `curl.exe`

PowerShell 의 `curl` 은 `Invoke-WebRequest` 의 alias 라 `-sS` 같은 플래그를
못 받는다. `curl.exe` 라고 확장자까지 적어야 진짜 curl 이 돈다.

## CORS — preflight 가 405 면 설정이 빈 것이다

`CORS_ORIGINS` 가 비어 있으면 **미들웨어 자체가 등록되지 않는다.** 그러면 `OPTIONS` 를
처리할 핸들러가 없어서 **405 `method-not-allowed`** 가 나가고, 응답에
`access-control-allow-origin` 이 아예 없다. 브라우저는 그 시점에 본 요청을 중단하므로
화면에는 `Failed to fetch` 만 보인다 — CORS 라는 말이 안 나온다.

설정하면 preflight 가 바로 통과한다 (로컬 확인):

```
OPTIONS /v1/judge  (Origin: http://127.0.0.1:5173)
→ 405, allow-origin 없음                     # CORS_ORIGINS 비어 있을 때
→ 200                                        # 설정 후
   access-control-allow-origin: http://127.0.0.1:5173
   access-control-allow-methods: GET, POST, PUT, DELETE, OPTIONS
   access-control-allow-headers: ... Content-Type, If-None-Match, X-Session-Id
```

**출처에 경로를 붙이지 않는다.** `https://example.github.io/FE/` 가 아니라
`https://example.github.io` 다 — 브라우저가 보내는 `Origin` 헤더에는 경로가 없다.
끝의 `/` 는 코드가 떼지만, 경로는 떼지 않는다.

로컬 점검용 출처도 함께 넣을 수 있다:

```
CORS_ORIGINS=https://example.github.io,http://127.0.0.1:5173,http://127.0.0.1:5174
```

## 스냅샷을 이미지에 굽지 않는 이유

실데이터 스냅샷은 매일 바뀌고 수집 키가 필요하다. 이미지에 넣으면 배포할 때마다
정책 데이터가 굳고, 데이터를 갱신하려고 이미지를 다시 말아야 한다.

이미지에는 **데모 스냅샷 5건만** 들어간다. 그리고 **기본값으로 쓰지 않는다** —
운영자가 `SNAPSHOT_PATH` 를 명시해야 한다. 데모 데이터가 조용히 운영으로 나가면
사용자는 5건짜리 목록을 진짜 정책 목록으로 읽는데, 그건 에러가 아니라 정상 응답처럼
보여서 발견이 늦는다.

실데이터로 바꾸는 방법:

```bash
# 배치 기계에서
ONTONG_API_KEY=... python -m batch.collect.cli fetch
python -m batch.collect.cli normalize data/raw/<타임스탬프> -o data/policies.json
python -m batch.build_snapshot data/policies.json -o snapshot.json

# 영구 디스크나 오브젝트 스토리지에 올리고
SNAPSHOT_PATH=/var/data/snapshot.json
```

## 헬스체크 두 개를 다르게 쓴다

| | 보는 것 | 쓰는 곳 |
| --- | --- | --- |
| `/healthz` | 프로세스 생존 | Docker `HEALTHCHECK`, 무료 티어 keep-alive 핑 |
| `/readyz` | 스냅샷 적재 여부 | 로드밸런서 · **배포 판정** (`render.yaml`) |

배포 판정에 `/healthz` 를 쓰면 스냅샷을 못 읽은 인스턴스가 '정상'으로 올라가 모든
요청에 503 을 낸다. 반대로 Docker `HEALTHCHECK` 에 `/readyz` 를 쓰면 스냅샷이 없는
컨테이너가 고칠 기회 없이 무한 재시작한다.

## CORS — ETag 노출이 핵심이다

```
CORS_ORIGINS=https://ypc-fe.example,https://staging.ypc-fe.example
```

허용 출처만큼 중요한 것이 **노출 헤더**다. 판정·목록 응답은 `ETag` + `If-None-Match`
로 304 를 내도록 만들어져 있는데, 브라우저는 `Access-Control-Expose-Headers` 에 없는
응답 헤더를 자바스크립트에 넘기지 않는다. 빠뜨리면 FE 는 `ETag` 를 읽지 못하고 서버는
매번 전체 응답을 다시 만든다 — **에러가 아니라서 아무도 눈치채지 못한다.**
`app/main.py` 가 `ETag` 와 `X-Snapshot-Version` 을 노출하고,
`tests/unit/test_cors.py` 가 그걸 지킨다.

쿠키를 쓰지 않으므로 `allow_credentials` 는 `false` 다. 세션은 URL 의 UUID 와
`X-Session-Id` 헤더로만 식별되며, 그래서 그 헤더가 허용 목록에 있다.

`CORS_ORIGINS` 를 비워두면 아무 출처도 열지 않는다. 브라우저에서 못 부르는 것은
콘솔에 바로 뜨는 실패지만, 전부 열어두는 것은 아무 증상이 없어서 그대로 남는다.

## 워커 1개인 이유

스냅샷을 프로세스마다 RAM 에 통째로 올린다 (ADR-001). 워커를 늘리면 메모리가 배로
든다. 무료 티어에서는 1개가 맞고, 판정이 0.4ms 라 동시성이 병목이 되기 전에
메모리가 먼저 터진다.

## 무료 티어에서 알아둘 것

- 유휴 상태면 컨테이너가 내려간다. 첫 요청이 수십 초 걸린다 — 시연 전에 `/healthz`
  를 한 번 깨워둘 것. 재배포 직후 확인이 느린 것도 대개 이것이지 고장이 아니다
- 스냅샷을 다시 올리려면 영구 디스크가 필요하다. 없으면 재시작할 때마다 이미지
  안의 데모 스냅샷으로 돌아간다

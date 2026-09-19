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
  를 한 번 깨워둘 것
- 스냅샷을 다시 올리려면 영구 디스크가 필요하다. 없으면 재시작할 때마다 이미지
  안의 데모 스냅샷으로 돌아간다

# syntax=docker/dockerfile:1

# 3.11 로 고정한다. CI 와 같은 버전이어야 하고, 3.13+ 는 docstring 들여쓰기를
# 제거해 계약 JSON 이 달라진다 (docs/HANDOFF.md §5).
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 한글 로그·리포트가 콘솔 코드페이지 때문에 죽지 않도록
    PYTHONUTF8=1

WORKDIR /app

# --- 의존성 + 서버 코드 ---------------------------------------------------
# pyproject 가 의존성 목록이자 패키지 정의라 `pip install .` 에 app/ 이 필요하다.
# 그래서 app/ 이 바뀌면 이 레이어가 다시 돈다 — 설치가 1분 남짓이라 두 단계로
# 쪼개는 복잡함보다 낫다고 봤다. 자주 바뀌지 않는 batch/ · data/ 는 뒤에 온다.
#
# `.[batch]` 를 넣지 않는다. 수집기·A2 는 배치에서만 돌고, anthropic SDK 와
# pdfplumber 를 서버 이미지에 넣으면 API 가 쓰지도 않는 의존성이 배포 표면에 남는다.
# packages.find 가 있는 것만 잡으므로 batch/ 가 없어도 설치는 성공한다.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# --- 스냅샷 --------------------------------------------------------------
# 이미지 안에 데모 스냅샷을 만들어 둔다. 운영 스냅샷은 매일 바뀌고 수집 키가
# 필요하므로 이미지에 넣지 않는다 — 대신 SNAPSHOT_PATH 로 갈아끼운다.
#
# seed 는 실제 공고지만 **수집 시점에 얼어붙은 것**이다. 신청기간이 지나면
# 빌더가 걸러내므로 재빌드할 때마다 목록이 줄고, 마지막 공고가 마감되는
# 2026-10-02 이후에는 0건이 된다. 0건이면 빌더가 먼저 거부하고 종료코드 1 을
# 내므로 이미지가 만들어지지 않는다 — 빈 목록이 배포되는 경로는 없다.
#
# 아래 검사는 그 다음 줄의 안전망이자 **건수를 빌드 로그에 남기는 장치**다.
# 스냅샷이 몇 건짜리인지 이미지 밖에서 확인할 방법이 없어서(무료 플랜은 셸이
# 없다), 배포된 목록이 3건인지 1건인지 묻는 데 로그밖에 쓸 게 없었다.
COPY batch ./batch
COPY data/demo ./data/demo
RUN python -m batch.build_snapshot data/demo/policies.demo.json \
        -o /app/data/demo/snapshot.json --force \
    && python -c "import json, pathlib, sys; \
n = len(json.loads(pathlib.Path('/app/data/demo/snapshot.json').read_text(encoding='utf-8'))); \
print(f'snapshot: {n} policies'); \
sys.exit(0 if n else 'snapshot has no published policy - seed notices have all closed; collect fresh ones')"

COPY data/documents ./data/documents

# --- 실행 ----------------------------------------------------------------
# 루트로 돌리지 않는다. 컨테이너가 뚫렸을 때 할 수 있는 일을 줄인다.
RUN useradd --create-home --uid 10001 ypc && chown -R ypc:ypc /app
USER ypc

# Render 같은 PaaS 는 $PORT 를 주입한다. 없으면 8000.
ENV PORT=8000
EXPOSE 8000

# 헬스체크는 /healthz (프로세스 생존). /readyz 는 스냅샷까지 보므로 배포 판정용이고,
# 여기에 쓰면 스냅샷이 없는 컨테이너가 무한 재시작한다 — 고칠 기회 없이.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=2).status == 200 else 1)"

# uvicorn 워커 1개. 스냅샷을 프로세스마다 RAM 에 통째로 올리므로(ADR-001),
# 워커를 늘리면 메모리가 배로 든다. 무료 티어에서는 1개가 맞다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

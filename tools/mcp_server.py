"""YPC MCP 서버 — 판정 API 를 Claude(Code/Desktop)의 도구로 노출한다.

Claude Code 안에서 "나 24살이고 용인 수지구 살아, 받을 수 있는 정책 있어?" 라고 물으면
Claude 가 이 도구들을 불러 판정·역질문·조합·계획을 받아와 대화로 풀어준다.

경계는 그대로다: 여기는 `/v1/*` 를 그대로 감싸는 얇은 어댑터이고, 판정·날짜·조합은 전부
API 서버의 결정론 코드가 한다. Claude 는 도구를 고르고 결과를 말로 옮길 뿐, 판정하지 않는다.
설명문(C2)과 역질문(C1)이 실제 LLM — 사용자 앞의 Claude — 로 도는 유일한 경로이며,
그 LLM 은 API 키가 아니라 사용자의 Claude 구독이다.

실행 (API 서버를 먼저 띄운다):
  SNAPSHOT_PATH=data/manual/snapshot.json uvicorn app.main:app --port 8765
  YPC_API_BASE=http://127.0.0.1:8765 python tools/mcp_server.py        # stdio

Claude Code 에 등록: 저장소의 .mcp.json 이 이 서버를 가리킨다. 저장소 폴더에서 claude 를
열면 "ypc" 도구가 보인다 (mcp 패키지: pip install -e ".[mcp]").
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

API_BASE = os.environ.get("YPC_API_BASE", "http://127.0.0.1:8765")

INSTRUCTIONS = """청년정책 자격 판정 도구다.
판정은 서버의 결정론 룰 엔진이 하며, 너는 결과를 사용자 말로 옮긴다.
- 먼저 find_region_code 로 사용자의 시·군·구를 법정동 코드로 바꾼다.
  구가 있는 시(수원·성남·안양·부천·안산·고양·용인)는 반드시 구까지 물어라.
- judge 는 생년월일과 지역 코드만 있어도 된다. 나머지는 모르면 비워 둔다 —
  서버가 NEEDS_INFO 로 표시하고 questions 가 물을 것을 준다.
- 사용자가 답하면 answers 에 담아 judge 를 다시 부른다 (재판정).
- 결과에 없는 금액·날짜·조건을 말하지 마라. 각 결과의 explanation 과 source_quote 를 근거로 쓴다.
- ESTIMATED / NEEDS_REVIEW 결과에는 dept_name·dept_tel 로 담당부서 확인을 권한다.
- 마지막에 "판정 결과는 공고문 분석에 기반한 참고 정보이며 최종 자격은 담당부서 확인이
  필요합니다"를 붙인다.
"""

server = MCPServer("ypc", title="청년서랍 — 청년정책 자격 판정", instructions=INSTRUCTIONS)
# 경기도 시·군·구 법정동 코드 (5자리). 온통청년 API 의 zipCd 와 같은 체계.
# 구가 있는 시는 구 코드를 써야 한다. 시 코드는 구 단위로 적힌 정책과 겹치지 않는다 (HANDOFF #15).
GYEONGGI_CODES: dict[str, str] = {
    "수원시 장안구": "41111",
    "수원시 권선구": "41113",
    "수원시 팔달구": "41115",
    "수원시 영통구": "41117",
    "성남시 수정구": "41131",
    "성남시 중원구": "41133",
    "성남시 분당구": "41135",
    "의정부시": "41150",
    "안양시 만안구": "41171",
    "안양시 동안구": "41173",
    "부천시 원미구": "41192",
    "부천시 소사구": "41194",
    "부천시 오정구": "41196",
    "광명시": "41210",
    "평택시": "41220",
    "동두천시": "41250",
    "안산시 상록구": "41271",
    "안산시 단원구": "41273",
    "고양시 덕양구": "41281",
    "고양시 일산동구": "41285",
    "고양시 일산서구": "41287",
    "과천시": "41290",
    "구리시": "41310",
    "남양주시": "41360",
    "오산시": "41370",
    "시흥시": "41390",
    "군포시": "41410",
    "의왕시": "41430",
    "하남시": "41450",
    "용인시 처인구": "41461",
    "용인시 기흥구": "41463",
    "용인시 수지구": "41465",
    "파주시": "41480",
    "이천시": "41500",
    "안성시": "41550",
    "김포시": "41570",
    "화성시": "41590",
    "광주시": "41610",
    "양주시": "41630",
    "포천시": "41650",
    "여주시": "41670",
    "연천군": "41800",
    "가평군": "41820",
    "양평군": "41830",
}


def _post(path: str, body: dict[str, Any]) -> str:
    r = httpx.post(f"{API_BASE}{path}", json=body, timeout=15)
    if r.status_code >= 400:
        return json.dumps({"error": r.status_code, "detail": r.text[:500]}, ensure_ascii=False)
    return json.dumps(r.json(), ensure_ascii=False)


def _profile(
    birth_date: str,
    region_code: str,
    residence_start_date: str | None,
    education: str | None,
    employment_status: str | None,
    employment_start_date: str | None,
    marital_status: str | None,
    household_size: int | None,
    household_income_ratio_median: int | None,
    answers: dict[str, Any] | None,
) -> dict[str, Any]:
    core: dict[str, Any] = {"birth_date": birth_date, "region_code": region_code}
    for key, value in (
        ("residence_start_date", residence_start_date),
        ("education", education),
        ("employment_status", employment_status),
        ("employment_start_date", employment_start_date),
        ("marital_status", marital_status),
        ("household_size", household_size),
        ("household_income_ratio_median", household_income_ratio_median),
    ):
        if value is not None and value != "":
            core[key] = value
    profile: dict[str, Any] = {"core": core}
    if answers:
        profile["answers"] = answers
    return profile


PROFILE_DOC = """
입력(공통):
  birth_date  생년월일 YYYY-MM-DD (필수)
  region_code  법정동 코드 5자리 (필수, find_region_code 로 얻는다)
  residence_start_date  현재 지역 전입일 YYYY-MM-DD. 모르면 비움
  education  middle_or_below | high_school_enrolled | high_school_graduated |
             university_enrolled | university_graduated | graduate_school
  employment_status  employed | job_seeking | student | founder | neet
  employment_start_date  현 직장 입사일 YYYY-MM-DD
  marital_status  single | married | divorced | widowed
  household_size  가구원 수 (본인 포함)
  household_income_ratio_median  가구 소득의 기준 중위소득 대비 % (정수)
  answers  역질문에 대한 답. {"household_income_ratio_median": 120} 처럼 필드명이 키
"""

JUDGE_DOC = (
    "사용자 조건으로 전 정책을 한 번에 판정한다. 결과마다 verdict(ELIGIBLE/INELIGIBLE/NEEDS_INFO), "
    "confidence, explanation(사용자용 문장), matched/unmatched(근거 인용문·충족 예상일)/"
    "unknown(물어야 할 것), dept_tel 이 온다. include_ineligible=false 면 부적격 상세를 뺀다."
    + PROFILE_DOC
)
QUESTIONS_DOC = (
    "판정을 확정하려면 무엇을 더 물어야 하는지 — 필드별로 병합된 질문 큐. "
    "각 질문의 field 가 answers 의 키다. resolves 는 그 답으로 확정되는 정책 수." + PROFILE_DOC
)
COMBINATIONS_DOC = (
    "적격 정책 중 중복수혜 제한을 피해 함께 받을 수 있는 조합. 보수 조합(추정 상충까지 회피)과 "
    "최대 조합(명시된 상충만 회피) 두 안이 온다. "
    "제외된 정책에는 공고문 인용과 담당부서 연락처가 붙는다." + PROFILE_DOC
)
PLAN_DOC = (
    "적격 정책의 신청 계획 — 마감일, 서류별 발급 소요일, 권장 준비 시작일, 유효기간 구간. "
    "status 가 INFEASIBLE 이면 마감까지 서류 준비 영업일이 부족하다는 뜻이다." + PROFILE_DOC
)


@server.tool()
def find_region_code(name: str) -> str:
    """경기도 시·군·구 이름을 법정동 코드로 바꾼다. '용인 수지' 처럼 일부만 줘도 된다.

    구가 있는 시(수원·성남·안양·부천·안산·고양·용인)를 시 이름만으로 물으면 구 목록을 돌려주니
    사용자에게 구를 확인한 뒤 다시 부른다. 경기도 밖은 이 표에 없다 — 사용자에게 코드를 묻는다.
    """
    # 띄어쓰기로 나눈 조각이 전부 들어 있으면 매칭: "용인 수지" / "수지구" / "용인시 수지구"
    tokens = [t for t in name.split() if t]
    hits = {
        k: v
        for k, v in GYEONGGI_CODES.items()
        if tokens and all(t in k.replace(" ", "") for t in tokens)
    }
    if len(hits) == 1:
        k, v = next(iter(hits.items()))
        return json.dumps({"name": k, "region_code": v}, ensure_ascii=False)
    if hits:
        return json.dumps({"ambiguous": hits, "hint": "구까지 골라 주세요"}, ensure_ascii=False)
    return json.dumps(
        {"not_found": name, "hint": "경기도 밖이거나 표기가 다릅니다"}, ensure_ascii=False
    )


@server.tool(description=JUDGE_DOC)
def judge(
    birth_date: str,
    region_code: str,
    residence_start_date: str | None = None,
    education: str | None = None,
    employment_status: str | None = None,
    employment_start_date: str | None = None,
    marital_status: str | None = None,
    household_size: int | None = None,
    household_income_ratio_median: int | None = None,
    answers: dict[str, Any] | None = None,
    include_ineligible: bool = True,
) -> str:
    profile = _profile(
        birth_date,
        region_code,
        residence_start_date,
        education,
        employment_status,
        employment_start_date,
        marital_status,
        household_size,
        household_income_ratio_median,
        answers,
    )
    include = "all" if include_ineligible else "default"
    return _post(f"/v1/judge?include={include}&explain=template", profile)


@server.tool(description=QUESTIONS_DOC)
def questions(
    birth_date: str,
    region_code: str,
    residence_start_date: str | None = None,
    education: str | None = None,
    employment_status: str | None = None,
    employment_start_date: str | None = None,
    marital_status: str | None = None,
    household_size: int | None = None,
    household_income_ratio_median: int | None = None,
    answers: dict[str, Any] | None = None,
) -> str:
    profile = _profile(
        birth_date,
        region_code,
        residence_start_date,
        education,
        employment_status,
        employment_start_date,
        marital_status,
        household_size,
        household_income_ratio_median,
        answers,
    )
    return _post("/v1/questions", profile)


@server.tool(description=COMBINATIONS_DOC)
def combinations(
    birth_date: str,
    region_code: str,
    residence_start_date: str | None = None,
    education: str | None = None,
    employment_status: str | None = None,
    employment_start_date: str | None = None,
    marital_status: str | None = None,
    household_size: int | None = None,
    household_income_ratio_median: int | None = None,
    answers: dict[str, Any] | None = None,
) -> str:
    profile = _profile(
        birth_date,
        region_code,
        residence_start_date,
        education,
        employment_status,
        employment_start_date,
        marital_status,
        household_size,
        household_income_ratio_median,
        answers,
    )
    return _post("/v1/combinations", profile)


@server.tool(description=PLAN_DOC)
def plan(
    birth_date: str,
    region_code: str,
    residence_start_date: str | None = None,
    education: str | None = None,
    employment_status: str | None = None,
    employment_start_date: str | None = None,
    marital_status: str | None = None,
    household_size: int | None = None,
    household_income_ratio_median: int | None = None,
    answers: dict[str, Any] | None = None,
) -> str:
    profile = _profile(
        birth_date,
        region_code,
        residence_start_date,
        education,
        employment_status,
        employment_start_date,
        marital_status,
        household_size,
        household_income_ratio_median,
        answers,
    )
    return _post("/v1/plan", profile)


@server.tool()
def policy(policy_id: str) -> str:
    """정책 1건의 공고 내용 — 조건·근거 인용·서류·상충·담당부서·원문 링크."""
    r = httpx.get(f"{API_BASE}/v1/policies/{policy_id}", timeout=15)
    if r.status_code >= 400:
        return json.dumps({"error": r.status_code, "detail": r.text[:500]}, ensure_ascii=False)
    return json.dumps(r.json(), ensure_ascii=False)


if __name__ == "__main__":
    server.run("stdio")

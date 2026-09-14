"""스냅샷 빌더 · 공휴일 동기화 (BE-M1-4~7).

빌더의 기준은 하나다: **조용한 누락이 틀린 판정보다 나쁘다.**
정책이 빠지면 사용자는 '부적격'도 '확인 필요'도 아닌 아무것도 못 본다.
"""

from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import httpx
import msgspec
import pytest

from app.planner.businessday import calendar_from_rows
from app.schemas.policy import Dept, Meta, PolicySchema, Rule, Source
from batch.build_snapshot import (
    MIN_RETENTION_RATIO,
    BuildReport,
    SnapshotBuildError,
    build,
    load_policies,
    main,
    previous_count_of,
    version_of,
    write_snapshot,
)
from batch.holidays import (
    FIXED_HOLIDAYS,
    HolidayConfig,
    HolidaySyncError,
    SyncResult,
    _check,
    _parse_payload,
    fetch_year,
    to_rows,
    upsert_sql,
)

# --- 픽스처 ---------------------------------------------------------------


def _policy(pid: str, *, quote: str = "만 19세 이상", field_name: str = "age") -> PolicySchema:
    return PolicySchema(
        policy_id=pid,
        status="published",
        meta=Meta(
            title=f"정책 {pid}",
            category="job",
            authority_level="local",
            region_code=["41135"],
            dept=Dept(name="청년정책과", tel="031-000-0000"),
        ),
        source=Source(origin_url=f"https://example.kr/{pid}"),
        eligibility=[
            Rule(rule_id=f"{pid}-r1", field=field_name, op=">=", value=19, source_quote=quote)
        ],
    )


def _many(n: int) -> list[PolicySchema]:
    return [_policy(f"P{i:04d}") for i in range(n)]


# --- 검증 · 거부 ----------------------------------------------------------


def test_게시_상태가_아닌_정책은_내보내지_않고_리포트에_남긴다() -> None:
    """엔진은 status 를 보지 않는다. 마감 정책이 스냅샷에 있으면 '적격'으로 나간다."""
    ps = _many(3)
    ps[1].status = "expired"
    ps[2].status = "draft"
    accepted, report = build(ps)
    assert [p.policy_id for p in accepted] == ["P0000"]
    assert report.skipped_by_status == {"expired": 1, "draft": 1}
    assert not report.rejected  # 위반이 아니므로 빌드는 계속된다
    assert "미게시    2건" in report.render()


def test_정상_정책은_그대로_통과한다() -> None:
    accepted, report = build(_many(5))
    assert len(accepted) == 5
    assert report.accepted == 5
    assert report.rejected == []
    assert report.ok


def test_근거_없는_룰이_있으면_빌드가_멈춘다() -> None:
    """G1: source_quote 100%. 한 건이라도 없으면 전체를 세운다."""
    policies = [*_many(3), _policy("BAD", quote="   ")]
    with pytest.raises(SnapshotBuildError, match="스키마 검증에 실패"):
        build(policies)


def test_알_수_없는_필드는_거부된다() -> None:
    policies = [*_many(3), _policy("BAD", field_name="혈액형")]
    with pytest.raises(SnapshotBuildError):
        build(policies)


def test_거부_리포트는_무엇이_왜_걸렸는지_말한다() -> None:
    policies = [*_many(2), _policy("BAD", quote="")]
    _, report = build(policies, allow_partial=True)
    assert len(report.rejected) == 1
    rejected = report.rejected[0]
    assert rejected.policy_id == "BAD"
    assert rejected.violations
    text = report.render()
    assert "BAD" in text and "거부" in text


def test_부분_빌드는_명시적으로만_허용된다() -> None:
    """기본값이 '조용히 빼기'면 아무도 누락을 눈치채지 못한다."""
    policies = [*_many(3), _policy("BAD", quote="")]
    accepted, report = build(policies, allow_partial=True)
    assert len(accepted) == 3
    assert any("제외" in w for w in report.warnings)


def test_policy_id_중복은_거부된다() -> None:
    """중복이 남으면 뒤쪽 정책이 판정에서 통째로 사라진다 (index_of 가 앞만 가리킴)."""
    policies = [_policy("SAME"), _policy("SAME")]
    with pytest.raises(SnapshotBuildError):
        build(policies)

    _, report = build(policies, allow_partial=True)
    assert report.rejected[0].violations[0].code == "DUPLICATE_POLICY_ID"


# --- 관문 -----------------------------------------------------------------


def test_빈_스냅샷은_내보내지_않는다() -> None:
    """'해당 정책 없음'은 정상 응답처럼 보여서 장애를 감춘다."""
    with pytest.raises(SnapshotBuildError, match="0건"):
        build([])


def test_전부_거부되어도_빈_스냅샷은_안_나간다() -> None:
    with pytest.raises(SnapshotBuildError):
        build([_policy("BAD", quote="")], allow_partial=True)


def test_정책이_급감하면_빌드가_멈춘다() -> None:
    """어제 600건이 오늘 40건이면 정책이 사라진 게 아니라 수집이 깨진 것이다."""
    with pytest.raises(SnapshotBuildError, match="줄었습니다"):
        build(_many(40), previous_count=600)


def test_소폭_감소는_통과한다() -> None:
    kept = int(600 * MIN_RETENTION_RATIO) + 10
    _, report = build(_many(kept), previous_count=600)
    assert report.accepted == kept


def test_증가는_언제나_통과한다() -> None:
    _, report = build(_many(900), previous_count=600)
    assert report.accepted == 900


def test_첫_빌드는_급변_검사를_하지_않는다() -> None:
    _, report = build(_many(3), previous_count=None)
    assert report.accepted == 3


# --- 버전 -----------------------------------------------------------------


def test_같은_내용이면_같은_버전_꼬리를_가진다() -> None:
    """내용이 같은데 버전이 달라지면 ETag 가 매일 통째로 무효화된다."""
    payload = b'[{"a": 1}]'
    assert version_of(payload, 1).split("-", 1)[1] == version_of(payload, 1).split("-", 1)[1]


def test_내용이_다르면_버전도_다르다() -> None:
    a = version_of(b'[{"a": 1}]', 1).split("-")[-1]
    b = version_of(b'[{"a": 2}]', 1).split("-")[-1]
    assert a != b


def test_버전에_정책_수가_들어간다() -> None:
    assert "-7p-" in version_of(b"x", 7)


# --- 쓰기 -----------------------------------------------------------------


def test_스냅샷을_쓰고_다시_읽을_수_있다(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.json"
    accepted, _ = build(_many(4))
    version, size = write_snapshot(accepted, out, compress=False)
    assert out.exists() and size > 0
    assert "-4p-" in version
    assert [p.policy_id for p in load_policies(out)] == [p.policy_id for p in accepted]


def test_압축해서_쓸_수_있다(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.json.gz"
    accepted, _ = build(_many(30))
    _, size = write_snapshot(accepted, out, compress=True)
    assert size < len(msgspec.json.encode(accepted))
    assert len(load_policies(out)) == 30


def test_쓰기는_임시파일을_거친다(tmp_path: Path) -> None:
    """같은 자리에 직접 쓰면 도중에 죽었을 때 반쯤 쓰인 파일이 남는다."""
    out = tmp_path / "snapshot.json"
    accepted, _ = build(_many(3))
    write_snapshot(accepted, out, compress=False)
    assert not list(tmp_path.glob("*.tmp"))


def test_출력_디렉터리가_없으면_만든다(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "snapshot.json"
    accepted, _ = build(_many(2))
    write_snapshot(accepted, out, compress=False)
    assert out.exists()


def test_직전_건수를_읽는다(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.json"
    assert previous_count_of(out) is None
    accepted, _ = build(_many(6))
    write_snapshot(accepted, out, compress=False)
    assert previous_count_of(out) == 6


def test_직전_스냅샷이_깨져도_새_빌드를_막지_않는다(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.json"
    out.write_bytes(b"{{{ not json")
    assert previous_count_of(out) is None


def test_깨진_gz_직전본도_새_빌드를_막지_않는다(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.json.gz"
    out.write_bytes(b"not gzip at all")
    assert previous_count_of(out) is None


# --- CLI ------------------------------------------------------------------


def _write_input(tmp_path: Path, policies: list[PolicySchema]) -> Path:
    src = tmp_path / "in.json"
    src.write_bytes(msgspec.json.encode(policies))
    return src


def test_CLI_가_스냅샷을_만든다(tmp_path: Path, capsys) -> None:
    src = _write_input(tmp_path, _many(5))
    out = tmp_path / "snapshot.json"
    assert main([str(src), "-o", str(out)]) == 0
    assert len(load_policies(out)) == 5
    assert "통과      5건" in capsys.readouterr().out


def test_CLI_는_검증_실패시_0이_아닌_코드로_끝난다(tmp_path: Path) -> None:
    src = _write_input(tmp_path, [*_many(2), _policy("BAD", quote="")])
    out = tmp_path / "snapshot.json"
    assert main([str(src), "-o", str(out)]) == 1
    assert not out.exists()  # 실패했으면 아무것도 쓰지 않는다


def test_CLI_check_only_는_쓰지_않는다(tmp_path: Path) -> None:
    src = _write_input(tmp_path, _many(3))
    out = tmp_path / "snapshot.json"
    assert main([str(src), "-o", str(out), "--check-only"]) == 0
    assert not out.exists()


def test_CLI_가_리포트를_파일로_남긴다(tmp_path: Path) -> None:
    src = _write_input(tmp_path, _many(3))
    report = tmp_path / "reports" / "build.md"
    assert main([str(src), "-o", str(tmp_path / "s.json"), "--report", str(report)]) == 0
    assert "통과      3건" in report.read_text(encoding="utf-8")


def test_CLI_급변은_force_로_넘길_수_있다(tmp_path: Path) -> None:
    out = tmp_path / "snapshot.json"
    accepted, _ = build(_many(100))
    write_snapshot(accepted, out, compress=False)

    src = _write_input(tmp_path, _many(10))
    assert main([str(src), "-o", str(out)]) == 1
    assert main([str(src), "-o", str(out), "--force"]) == 0


def test_CLI_는_읽을_수_없는_입력에_2를_돌려준다(tmp_path: Path) -> None:
    src = tmp_path / "broken.json"
    src.write_bytes(b"not json at all")
    assert main([str(src), "-o", str(tmp_path / "s.json")]) == 2


def test_CLI_gzip_출력(tmp_path: Path) -> None:
    src = _write_input(tmp_path, _many(20))
    out = tmp_path / "snapshot.json.gz"
    assert main([str(src), "-o", str(out), "--gzip"]) == 0
    assert gzip.decompress(out.read_bytes())[:1] == b"["


def test_빌드_리포트는_출력이_없어도_렌더된다() -> None:
    assert "미생성" in BuildReport().render()


# --- 공휴일 동기화 --------------------------------------------------------


def _kasi_payload(items: list[dict[str, object]]) -> dict[str, object]:
    return {"response": {"body": {"items": {"item": items}}}}


def _full_year(year: int) -> list[dict[str, object]]:
    """고정 공휴일 + 검사를 통과할 만큼의 음력 명절."""
    items = [
        {"locdate": int(f"{year}{m:02d}{d:02d}"), "dateName": f"{m}/{d}", "isHoliday": "Y"}
        for m, d in FIXED_HOLIDAYS
    ]
    # 음력 명절 7일(설 3 · 추석 3 · 부처님오신날 1)을 더한다.
    # 실제 한국은 고정 8 + 음력 7 = 15일이 대체공휴일 없는 해의 하한이다.
    for day in (216, 217, 218, 524, 924, 925, 926):
        items.append(
            {"locdate": int(f"{year}{day:04d}"), "dateName": "명절", "isHoliday": "Y"}
        )
    return items


def test_응답에서_공휴일을_뽑는다() -> None:
    holidays = _parse_payload(_kasi_payload(_full_year(2027)), 2027)
    assert holidays[date(2027, 1, 1)] == "1/1"
    assert len(holidays) == len(FIXED_HOLIDAYS) + 7


def test_item_이_1건일_때_객체로_와도_읽는다() -> None:
    """공공데이터포털은 1건일 때 배열이 아니라 객체를 준다."""
    single = {"locdate": 20270101, "dateName": "신정", "isHoliday": "Y"}
    holidays = _parse_payload({"response": {"body": {"items": {"item": single}}}}, 2027)
    assert holidays == {date(2027, 1, 1): "신정"}


def test_공휴일이_아닌_특일은_거른다() -> None:
    """24절기·잡절은 쉬는 날이 아니다."""
    items = [
        {"locdate": 20270101, "dateName": "신정", "isHoliday": "Y"},
        {"locdate": 20270204, "dateName": "입춘", "isHoliday": "N"},
    ]
    assert _parse_payload(_kasi_payload(items), 2027) == {date(2027, 1, 1): "신정"}


def test_다른_해의_날짜는_섞이지_않는다() -> None:
    items = [
        {"locdate": 20270101, "dateName": "신정", "isHoliday": "Y"},
        {"locdate": 20281225, "dateName": "성탄절", "isHoliday": "Y"},
    ]
    assert list(_parse_payload(_kasi_payload(items), 2027)) == [date(2027, 1, 1)]


def test_망가진_날짜는_건너뛴다() -> None:
    items = [
        {"locdate": "20270101", "dateName": "신정", "isHoliday": "Y"},
        {"locdate": "몰라", "dateName": "?", "isHoliday": "Y"},
        {"locdate": "", "dateName": "?", "isHoliday": "Y"},
    ]
    assert len(_parse_payload(_kasi_payload(items), 2027)) == 1


def test_body_가_없으면_인증_문제로_본다() -> None:
    with pytest.raises(HolidaySyncError, match="인증키"):
        _parse_payload({"response": {}}, 2027)


def test_item_이_없으면_빈_표() -> None:
    assert _parse_payload({"response": {"body": {"items": {}}}}, 2027) == {}


def test_고정_공휴일이_빠지면_경고한다() -> None:
    """신정이 없는 공휴일표는 응답이 깨진 것이다."""
    items = [i for i in _full_year(2027) if i["locdate"] != 20270101]
    holidays = _parse_payload(_kasi_payload(items), 2027)
    warnings = _check(holidays, 2027)
    assert any("1/1" in w for w in warnings)


def test_건수가_너무_적으면_경고한다() -> None:
    holidays = {date(2027, 1, 1): "신정"}
    assert any("잘렸을" in w for w in _check(holidays, 2027))


def test_온전한_표는_경고가_없다() -> None:
    holidays = _parse_payload(_kasi_payload(_full_year(2027)), 2027)
    assert _check(holidays, 2027) == []


@pytest.mark.asyncio
async def test_한_해를_받아온다() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["solYear"] == "2027"
        return httpx.Response(200, json=_kasi_payload(_full_year(2027)))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_year(client, HolidayConfig(api_key="k"), 2027)

    assert result.count == len(FIXED_HOLIDAYS) + 7
    assert result.warnings == []


@pytest.mark.asyncio
async def test_불완전한_응답은_경고를_달고_돌아온다() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_kasi_payload([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_year(client, HolidayConfig(api_key="k"), 2027)

    assert result.count == 0
    assert result.warnings  # 빈 표를 조용히 통과시키지 않는다


@pytest.mark.asyncio
async def test_일시적_실패는_재시도한다(monkeypatch) -> None:
    monkeypatch.setattr("batch.holidays.BACKOFF_BASE_SECONDS", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=_kasi_payload(_full_year(2027)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_year(client, HolidayConfig(api_key="k"), 2027)

    assert calls["n"] == 3
    assert result.warnings == []


@pytest.mark.asyncio
async def test_계속_실패하면_예외를_올린다(monkeypatch) -> None:
    monkeypatch.setattr("batch.holidays.BACKOFF_BASE_SECONDS", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HolidaySyncError, match="모두 실패"):
            await fetch_year(client, HolidayConfig(api_key="k"), 2027)


def test_인증키가_없으면_설정을_만들_수_없다(monkeypatch) -> None:
    monkeypatch.delenv("KASI_API_KEY", raising=False)
    monkeypatch.delenv("DATA_GO_KR_KEY", raising=False)
    with pytest.raises(HolidaySyncError, match="KASI_API_KEY"):
        HolidayConfig.from_env()


def test_받은_공휴일로_달력을_바로_만들_수_있다() -> None:
    """배치 산출물이 플래너 입력에 그대로 들어간다 — 중간 변환이 없다."""
    holidays = _parse_payload(_kasi_payload(_full_year(2027)), 2027)
    rows = to_rows([SyncResult(year=2027, holidays=holidays)])
    cal = calendar_from_rows(rows, source_ref="kasi-2027")

    assert not cal.is_business_day(date(2027, 1, 1))  # 신정
    assert cal.covers(date(2027, 6, 1))
    # 2027-01-01(금)이 신정 → 다음 영업일은 1/4(월)
    assert cal.next_business_day(date(2026, 12, 31)) == date(2027, 1, 4)


def test_upsert_는_이름_갱신을_포함한다() -> None:
    """대체공휴일 지정이 나중에 바뀌면 이름도 따라가야 한다."""
    sql = upsert_sql()
    assert "ON CONFLICT" in sql and "DO UPDATE" in sql and "synced_at" in sql

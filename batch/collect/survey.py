"""M0 데이터 정합성 조사 — 응답 스키마를 '가정하지 않고 발견한다'.

BE-M0-1~4 의 산출물을 자동 생성한다:
  BE-M0-1  필드 목록 + 샘플            → inventory()
  BE-M0-2  원문 링크 제공률            → G0 'origin_url_coverage'
  BE-M0-3  지역별·카테고리별 정책 건수 → G0 'policy_count'
  BE-M0-4  수혜 금액 명시율            → G0 'amount_coverage'

왜 필드명을 하드코딩하지 않는가:
  공식 API 명세를 확인하기 전에는 어떤 키가 원문 링크인지 알 수 없다.
  대신 '값의 생김새'로 후보를 찾아 커버리지를 함께 보고한다.
  사람은 후보 표를 보고 고르기만 하면 되고, 그 선택이 mapping.py 에 남는다.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from batch.collect.parse import Record

# G0 게이트 기준 (역할별 마일스톤 v2.0 §M0)
GATE_URL_COVERAGE = 0.70
GATE_AMOUNT_COVERAGE = 0.50
GATE_POLICY_COUNT = (150, 600)

_URL_RE = re.compile(r"^https?://", re.I)
_AMOUNT_RE = re.compile(r"\d[\d,]*\s*(원|만원|천원|억원)")
_DATE_RE = re.compile(r"(19|20)\d{2}[-./]?(0[1-9]|1[0-2])[-./]?(0[1-9]|[12]\d|3[01])")
_REGION_CODE_RE = re.compile(r"^\d{5,10}$")
_SAMPLE_LIMIT = 3
_SAMPLE_CHARS = 80


@dataclass
class FieldStat:
    """필드 1개의 관측 결과."""

    path: str
    present: int = 0  # 키가 존재한 레코드 수
    non_empty: int = 0  # 값이 비어있지 않은 레코드 수
    types: Counter = field(default_factory=Counter)
    samples: list[str] = field(default_factory=list)

    # 값의 생김새별 적중 수 — 어떤 필드가 무엇인지 추정하는 근거
    looks_url: int = 0
    looks_amount: int = 0
    looks_date: int = 0
    looks_region_code: int = 0

    def coverage(self, total: int) -> float:
        return self.non_empty / total if total else 0.0

    def add(self, value: Any) -> None:
        self.present += 1
        self.types[type(value).__name__] += 1

        text = "" if value is None else str(value).strip()
        if not text:
            return
        self.non_empty += 1

        if len(self.samples) < _SAMPLE_LIMIT and text not in self.samples:
            self.samples.append(text[:_SAMPLE_CHARS])

        if _URL_RE.match(text):
            self.looks_url += 1
        if _AMOUNT_RE.search(text):
            self.looks_amount += 1
        if _DATE_RE.search(text):
            self.looks_date += 1
        if _REGION_CODE_RE.match(text):
            self.looks_region_code += 1


def inventory(records: list[Record]) -> dict[str, FieldStat]:
    """중첩 구조를 점 경로로 펴서 필드별 통계를 낸다."""
    stats: dict[str, FieldStat] = {}

    for rec in records:
        for path, value in _walk(rec):
            stats.setdefault(path, FieldStat(path=path)).add(value)

    return dict(sorted(stats.items(), key=lambda kv: -kv[1].non_empty))


def _walk(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """중첩 dict/list 를 (경로, 잎값) 쌍으로 편다. 배열은 인덱스를 [] 로 뭉갠다."""
    out: list[tuple[str, Any]] = []

    if isinstance(node, dict):
        for key, value in node.items():
            out += _walk(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(node, list):
        for item in node:
            out += _walk(item, f"{prefix}[]")
    else:
        out.append((prefix, node))

    return out


@dataclass
class Candidate:
    path: str
    coverage: float
    samples: list[str]


@dataclass
class GateResult:
    name: str
    measured: str
    threshold: str
    passed: bool
    note: str = ""


@dataclass
class G0Report:
    total_records: int
    fields: dict[str, FieldStat]
    url_candidates: list[Candidate]
    amount_candidates: list[Candidate]
    date_candidates: list[Candidate]
    region_candidates: list[Candidate]
    gates: list[GateResult]

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)


def build_report(records: list[Record]) -> G0Report:
    total = len(records)
    stats = inventory(records)

    def candidates(attr: str, min_hit_ratio: float = 0.5) -> list[Candidate]:
        """값의 절반 이상이 해당 생김새인 필드만 후보로 올린다."""
        out = []
        for s in stats.values():
            hits = getattr(s, attr)
            if s.non_empty and hits / s.non_empty >= min_hit_ratio:
                out.append(
                    Candidate(
                        path=s.path,
                        coverage=hits / total if total else 0.0,
                        samples=s.samples,
                    )
                )
        return sorted(out, key=lambda c: -c.coverage)

    url_c = candidates("looks_url")
    amount_c = candidates("looks_amount", min_hit_ratio=0.3)
    date_c = candidates("looks_date")
    region_c = candidates("looks_region_code")

    # 게이트는 '가장 커버리지가 높은 후보'를 기준으로 판정한다.
    # 어느 필드를 쓸지는 사람이 정하지만, 상한선은 이 값이다.
    best_url = url_c[0].coverage if url_c else 0.0
    best_amount = amount_c[0].coverage if amount_c else 0.0
    lo, hi = GATE_POLICY_COUNT

    gates = [
        GateResult(
            name="원문 링크 제공률 (BE-M0-2)",
            measured=f"{best_url:.1%}" + (f" [{url_c[0].path}]" if url_c else ""),
            threshold=f"≥ {GATE_URL_COVERAGE:.0%}",
            passed=best_url >= GATE_URL_COVERAGE,
            note="" if url_c else "URL 형태의 필드를 찾지 못했습니다",
        ),
        GateResult(
            name="수혜액 명시율 (BE-M0-4)",
            measured=f"{best_amount:.1%}" + (f" [{amount_c[0].path}]" if amount_c else ""),
            threshold=f"≥ {GATE_AMOUNT_COVERAGE:.0%}",
            passed=best_amount >= GATE_AMOUNT_COVERAGE,
            note="미달 시 조합 가중치를 금액→건수로 변경 (Q2)",
        ),
        GateResult(
            name="정책 건수 (BE-M0-3)",
            measured=f"{total}건",
            threshold=f"{lo}~{hi}건",
            passed=lo <= total <= hi,
            note="초과 시 용인+중앙으로 범위 축소",
        ),
    ]

    return G0Report(
        total_records=total,
        fields=stats,
        url_candidates=url_c,
        amount_candidates=amount_c,
        date_candidates=date_c,
        region_candidates=region_c,
        gates=gates,
    )


def render_markdown(report: G0Report) -> str:
    """M0 산출물 문서에 그대로 붙일 수 있는 형태로 렌더링한다."""
    lines = [
        "# M0 데이터 정합성 조사 결과",
        "",
        f"수집 레코드: **{report.total_records}건**",
        "",
        "## G0 게이트 판정",
        "",
        "| 항목 | 측정값 | 기준 | 판정 | 비고 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for g in report.gates:
        mark = "✅ PASS" if g.passed else "❌ NO-GO"
        lines.append(f"| {g.name} | {g.measured} | {g.threshold} | {mark} | {g.note} |")

    lines += ["", f"**종합: {'✅ Go' if report.passed else '❌ No-Go — 설계 변경 검토'}**", ""]

    for title, cands in [
        ("원문 링크 후보", report.url_candidates),
        ("수혜 금액 후보", report.amount_candidates),
        ("날짜 후보", report.date_candidates),
        ("지역 코드 후보", report.region_candidates),
    ]:
        lines += [f"## {title}", ""]
        if not cands:
            lines += ["_해당 형태의 필드가 없습니다._", ""]
            continue
        lines += ["| 필드 경로 | 커버리지 | 예시 |", "| --- | --- | --- |"]
        for c in cands[:10]:
            sample = c.samples[0] if c.samples else ""
            lines.append(f"| `{c.path}` | {c.coverage:.1%} | `{sample}` |")
        lines.append("")

    lines += [
        "## 전체 필드 인벤토리",
        "",
        "| 필드 경로 | 값 존재율 | 타입 | 예시 |",
        "| --- | --- | --- | --- |",
    ]
    for s in report.fields.values():
        types = ", ".join(sorted(s.types))
        sample = s.samples[0] if s.samples else ""
        lines.append(
            f"| `{s.path}` | {s.coverage(report.total_records):.1%} | {types} | `{sample}` |"
        )

    return "\n".join(lines) + "\n"

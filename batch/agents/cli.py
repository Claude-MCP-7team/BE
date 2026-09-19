"""A2 구조화 CLI.

  # 원본(API 수집분) → LLM 구조화 → PolicySchema 목록 (build_snapshot 의 입력)
  ANTHROPIC_API_KEY=... python -m batch.agents.cli structure data/raw/<timestamp> \
      -o data/policies.json --limit 5

  # 특정 정책만
  python -m batch.agents.cli structure data/raw/<timestamp> --ids R2026001,R2026002

  # API 없이 — 미리 만든 응답(<plcyNo>.json)을 같은 검증·병합 경로로 (데모 정책, 비용 0)
  python -m batch.agents.cli structure data/manual/raw --responses data/manual/a2 \
      -o data/manual/policies.json

--limit / --ids 로 고른 정책만 LLM 을 거치고, 나머지는 normalize 결과 그대로 출력된다.
빌더 입력이 부분집합이 되면 '조용한 누락'이 생기므로 전체를 항상 내보낸다.

같은 정책·같은 텍스트에 대한 응답은 data/a2-cache/ 에 남겨 재실행 때 다시 과금하지 않는다.
프롬프트를 바꾸면 캐시 키가 바뀌므로 자동으로 다시 호출된다.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import msgspec

from app.core.console import force_utf8_console
from app.llm.client import LLM, LLMError, load_prompt
from app.schemas.policy import PolicySchema
from batch.agents.structure import A2Report, structure_policy
from batch.agents.text import Record, assemble_text
from batch.collect.client import load_raw
from batch.collect.normalize import record_to_policy

REPORT_DIR = Path("docs/a2")
CACHE_DIR = Path("data/a2-cache")


class CachedLLM:
    """응답 캐시. 키 = 정책 ID + 시스템 프롬프트 + 사용자 텍스트의 해시."""

    def __init__(self, inner: LLM, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.hits = 0
        self.last_usage: dict[str, int] = {}

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        key = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:24]
        path = self.directory / f"{key}.json"
        if path.exists():
            self.hits += 1
            self.last_usage = {}
            return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        data = self.inner.complete_json(system=system, user=user, schema=schema)
        self.last_usage = dict(getattr(self.inner, "last_usage", {}) or {})
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


class FileLLM:
    """<plcyNo>.json 에 미리 만들어 둔 A2 응답을 돌려준다.

    사람이 프롬프트 규칙을 따라 직접 쓴 것이든 다른 도구가 만든 것이든, 검증·병합은
    모델 응답과 완전히 같은 경로를 탄다 — 여기서 특별 대우는 없다. API 비용 없이
    데모 정책 몇 건을 만들거나, 모델 응답을 손으로 고쳐 다시 태울 때 쓴다.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_usage: dict[str, int] = {}

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise LLMError(f"{self.path}: JSON 객체가 아닙니다")
        return data


def _select(records: list[Record], limit: int | None, ids: set[str] | None) -> list[int]:
    chosen = [
        i
        for i, r in enumerate(records)
        if (ids is None or str(r.get("plcyNo", "")).strip() in ids)
        and assemble_text(r)  # 텍스트가 없는 정책은 호출할 이유가 없다
    ]
    return chosen[:limit] if limit else chosen


def cmd_structure(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    records = load_raw(directory) if directory.is_dir() else []
    if not records:
        print(f"레코드가 없습니다: {directory}", file=sys.stderr)
        return 2
    crawled_at = directory.name.split("-")[0]
    policies = [record_to_policy(r, crawled_at=crawled_at) for r in records]

    ids = {s.strip() for s in args.ids.split(",")} if args.ids else None
    responses = Path(args.responses) if args.responses else None
    if responses:
        # 미리 만든 응답이 있는 정책만 대상이다. 없는 정책을 호출하러 가면 안 된다.
        have = {p.stem for p in responses.glob("*.json")}
        ids = have if ids is None else ids & have
    targets = _select(records, args.limit, ids)
    print(f"원본 {len(records)}건 중 {len(targets)}건을 구조화합니다")

    if args.dry_run:
        for i in targets:
            chars = len(assemble_text(records[i]))
            print(f"  {policies[i].policy_id}  {chars}자  {policies[i].meta.title}")
        return 0

    cache_hits = 0
    if responses is None:
        from app.llm.client import AnthropicLLM  # SDK 는 실제 호출할 때만 필요하다

        cached = CachedLLM(AnthropicLLM(model=args.model), CACHE_DIR)
    system = load_prompt("a2_structure")
    announcements = Path(args.announcements) if args.announcements else None

    reports: list[A2Report] = []
    for n, i in enumerate(targets, 1):
        pid = policies[i].policy_id
        announcement = None
        if announcements and (f := announcements / f"{pid}.txt").exists():
            announcement = f.read_text(encoding="utf-8")
        llm: LLM = FileLLM(responses / f"{pid}.json") if responses else cached
        try:
            policies[i], report = structure_policy(
                policies[i], records[i], llm, announcement=announcement, system_prompt=system
            )
        except LLMError as e:
            report = A2Report(policy_id=pid, error=str(e))
        reports.append(report)
        tag = (
            "ERR "
            if report.error
            else f"+{report.accepted_conditions}/{report.proposed_conditions}"
        )
        print(f"  [{n}/{len(targets)}] {pid} {tag}  {policies[i].meta.title[:40]}")

    if responses is None:
        cache_hits = cached.hits
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(msgspec.json.format(msgspec.json.encode(policies), indent=2))
    _write_report(reports, policies, out, cache_hits=cache_hits)
    return 0 if not any(r.error for r in reports) else 1


def _write_report(
    reports: list[A2Report], policies: list[PolicySchema], out: Path, *, cache_hits: int
) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"a2-report-{stamp}.json"
    path.write_text(
        json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    proposed = sum(r.proposed_conditions for r in reports)
    accepted = sum(r.accepted_conditions for r in reports)
    rejected = collections.Counter(x.code for r in reports for x in r.rejected)
    unrep = sum(len(r.unrepresentable) for r in reports)
    disagree = sum(len(r.disagreements) for r in reports)
    errors = sum(1 for r in reports if r.error)
    tokens_in = sum(r.usage.get("input_tokens", 0) for r in reports)
    tokens_out = sum(r.usage.get("output_tokens", 0) for r in reports)
    cached = sum(r.usage.get("cache_read_input_tokens", 0) for r in reports)

    print(f"\n출력: {out}  ({len(policies)}건)")
    print(f"리포트: {path}")
    print(f"  조건 제안 {proposed} → 채택 {accepted}  (거부 {dict(rejected) or 0})")
    print(f"  API↔텍스트 불일치 {disagree}건 · 규칙화 불가 조건 {unrep}건 · 호출 실패 {errors}건")
    print(f"  토큰 in {tokens_in:,} (캐시 읽기 {cached:,}) / out {tokens_out:,}")
    print(f"  응답 캐시 적중 {cache_hits}건 (data/a2-cache)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batch.agents.cli", description="A2 공고문 구조화")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("structure", help="원본 → LLM 구조화 → PolicySchema JSON")
    p.add_argument("directory", help="data/raw/<timestamp>")
    p.add_argument("-o", "--output", default="data/policies.json")
    p.add_argument("--limit", type=int, help="앞에서 N건만 LLM 을 거친다 (비용 제한)")
    p.add_argument("--ids", help="쉼표로 구분한 plcyNo 목록")
    p.add_argument("--model", help="기본 claude-opus-5 (또는 YPC_LLM_MODEL)")
    p.add_argument("--announcements", help="<plcyNo>.txt 형태의 원문 공고문 디렉터리")
    p.add_argument(
        "--responses",
        help="<plcyNo>.json 형태의 미리 만든 A2 응답 디렉터리 — API 없이 검증·병합만 한다",
    )
    p.add_argument("--dry-run", action="store_true", help="대상만 나열하고 호출하지 않는다")

    args = parser.parse_args(argv)
    return cmd_structure(args)


if __name__ == "__main__":
    force_utf8_console()
    raise SystemExit(main())

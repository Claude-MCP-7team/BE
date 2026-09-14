"""M0 조사 CLI.

  # 1. 실제 API 에서 수집 + 조사 (인증키 필요)
  ONTONG_API_KEY=... python -m batch.collect.cli fetch --region 41

  # 2. 이미 받아둔 원본으로 다시 조사 (네트워크 불필요)
  python -m batch.collect.cli survey data/raw/20260914T020000Z

두 경우 모두 docs/m0/ 아래에 G0 판정 리포트를 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from batch.collect.client import CollectConfig, YouthCenterClient, load_raw
from batch.collect.parse import Record
from batch.collect.survey import build_report, render_markdown

REPORT_DIR = Path("docs/m0")


def write_report(records: list[Record], source: str) -> bool:
    """조사 리포트를 쓰고 G0 통과 여부를 돌려준다."""
    report = build_report(records)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d")

    md_path = REPORT_DIR / f"g0-report-{stamp}.md"
    md_path.write_text(
        f"<!-- source: {source} -->\n" + render_markdown(report), encoding="utf-8"
    )

    # 샘플 50건은 AI 역할이 프롬프트 설계에 바로 쓴다
    sample_path = REPORT_DIR / f"sample-{stamp}.json"
    sample_path.write_text(
        json.dumps(records[:50], ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    print(f"\n리포트: {md_path}")
    print(f"샘플  : {sample_path}  ({min(len(records), 50)}건)\n")

    for g in report.gates:
        print(f"  {'✅' if g.passed else '❌'} {g.name}: {g.measured} (기준 {g.threshold})")
    print(f"\n종합: {'✅ Go' if report.passed else '❌ No-Go — 설계 변경을 검토하세요'}")

    return report.passed


async def cmd_fetch(args: argparse.Namespace) -> int:
    config = CollectConfig.from_env()
    async with YouthCenterClient(config) as client:
        records, raw_dir = await client.collect(region=args.region)
    print(f"수집 완료: {len(records)}건 → 원본 {raw_dir}")
    return 0 if write_report(records, str(raw_dir)) else 1


def cmd_survey(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"원본 디렉터리가 없습니다: {directory}", file=sys.stderr)
        return 2
    records = load_raw(directory)
    if not records:
        print(f"레코드가 없습니다: {directory}", file=sys.stderr)
        return 2
    print(f"원본 로드: {len(records)}건 ← {directory}")
    return 0 if write_report(records, str(directory)) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="batch.collect.cli", description="M0 데이터 정합성 조사")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="API 에서 수집하고 조사")
    p_fetch.add_argument("--region", help="지역 코드 (예: 41=경기)")

    p_survey = sub.add_parser("survey", help="저장된 원본으로 조사")
    p_survey.add_argument("directory", help="data/raw/<timestamp>")

    args = parser.parse_args(argv)
    if args.command == "fetch":
        return asyncio.run(cmd_fetch(args))
    return cmd_survey(args)


if __name__ == "__main__":
    raise SystemExit(main())

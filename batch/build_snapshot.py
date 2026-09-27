"""스냅샷 빌더 — 배치의 산출물이자 API 가 RAM 에 올리는 유일한 입력 (ADR-001).

야간 배치의 마지막 단계다. 여기서 나온 파일 하나가 다음 24시간 동안 모든
판정의 근거가 되므로, 이 모듈의 기본자세는 "의심스러우면 내보내지 않는다"다.

**빌드는 전부 아니면 전무다.**
  정책 600건 중 3건이 스키마를 어겼을 때 그 3건만 빼고 내보내면, 사용자는
  그 정책이 '없는' 것으로 본다 — 부적격이라는 설명도, 확인하라는 안내도 없이.
  조용한 누락은 틀린 판정보다 발견이 늦다. 그래서 위반이 하나라도 있으면
  빌드를 세우고, 무엇이 왜 걸렸는지 리포트로 남긴다.
  (단, `--allow-partial` 로 사람이 명시적으로 선택할 수는 있다. 마감 직전에
   3건 때문에 전체가 멈추는 상황을 운영자가 판단할 여지는 남긴다.)

**빈 스냅샷은 어떤 경우에도 내보내지 않는다.**
  수집이 조용히 0건을 돌려주면 모든 사용자가 "해당 정책 없음"을 본다.
  이건 장애가 아니라 정상 응답처럼 보여서 며칠씩 방치된다.

**직전 스냅샷과 비교해 급변을 막는다.**
  어제 600건이던 것이 오늘 40건이 됐다면 수집이 깨진 것이지 정책이 사라진 게
  아니다. 사람이 확인하기 전에는 내보내지 않는다.

**게시 상태가 아닌 정책은 내보내지 않는다.**
  엔진과 API 는 status 를 보지 않는다 — 스냅샷에 있으면 판정한다. 마감된(expired)
  정책이 '적격'으로 나가면 사용자는 없는 창구에 서류를 준비한다. 거르는 곳은
  여기 한 군데다. 리포트에 상태별 건수를 남겨서 조용히 사라지지는 않게 한다.

**신청 기간이 지난 정책도 마찬가지다 — status 가 뭐라고 적혀 있든.**
  status 는 생산자가 채우는 값이라 틀릴 수 있고, 실제로 틀렸다: 수집기는
  `aplyPrdSeCd=0057003`(마감 코드) 만 expired 로 적어서, 기간제 정책의 종료일이
  지나도 published 로 남았다. 국토부 청년월세(5/29 마감)가 9/19 판정에서 적격으로
  나오고 조합 추천에 480만원으로 들어갔다.
  `period.apply_end` 는 공고문에서 온 날짜다. 그 날짜가 지났는지는 여기서 직접
  보는 편이, 모든 생산자가 status 를 정확히 채우기를 바라는 것보다 확실하다.

**버전은 내용 해시다.**
  같은 데이터면 같은 버전이어야 API 의 ETag 가 의미를 가진다. 타임스탬프만
  쓰면 내용이 같아도 매일 캐시가 통째로 무효화된다.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import msgspec

from app.core.clock import today_kst
from app.core.console import force_utf8_console
from app.schemas.policy import PolicySchema
from app.schemas.validate import SchemaViolation, validate_policy

# 직전 스냅샷 대비 이 비율 아래로 줄면 수집 사고로 본다.
# 정책이 하룻밤에 30% 넘게 사라지는 일은 실제로는 일어나지 않는다.
MIN_RETENTION_RATIO = 0.7

# 이 건수 미만이면 어떤 경우에도 내보내지 않는다.
MIN_POLICY_COUNT = 1


class SnapshotBuildError(RuntimeError):
    """스냅샷을 내보낼 수 없다. 직전 스냅샷이 계속 서비스된다."""


@dataclass
class RejectedPolicy:
    policy_id: str
    title: str
    violations: list[SchemaViolation]


@dataclass
class BuildReport:
    """무엇이 들어가고 무엇이 걸렸는지. 관리자 검증 큐의 입력이 된다."""

    version: str = ""
    total_input: int = 0
    accepted: int = 0
    rejected: list[RejectedPolicy] = field(default_factory=list)
    skipped_by_status: Counter[str] = field(default_factory=Counter)  # 게시 상태가 아닌 것
    skipped_closed: int = 0  # 신청 기간이 지난 것
    warnings: list[str] = field(default_factory=list)
    previous_count: int | None = None
    output_path: str | None = None
    bytes_written: int = 0

    @property
    def ok(self) -> bool:
        return self.accepted > 0 and not self.rejected

    def render(self) -> str:
        lines = [
            f"스냅샷 빌드 리포트 — {self.version or '(미생성)'}",
            "",
            f"  입력      {self.total_input}건",
            f"  통과      {self.accepted}건",
            f"  거부      {len(self.rejected)}건",
        ]
        if self.skipped_by_status:
            detail = ", ".join(f"{k} {v}" for k, v in sorted(self.skipped_by_status.items()))
            total = sum(self.skipped_by_status.values())
            lines.append(f"  미게시    {total}건 ({detail}) — 판정에서 제외")
        if self.skipped_closed:
            lines.append(f"  마감      {self.skipped_closed}건 (신청 기간 종료) — 판정에서 제외")
        if self.previous_count is not None:
            delta = self.accepted - self.previous_count
            lines.append(f"  직전 대비 {self.previous_count} → {self.accepted} ({delta:+d})")
        if self.output_path:
            lines.append(f"  출력      {self.output_path} ({self.bytes_written:,} bytes)")

        if self.warnings:
            lines += ["", "경고:"]
            lines += [f"  ⚠️  {w}" for w in self.warnings]

        if self.rejected:
            lines += ["", "거부된 정책 (관리자 검증 필요):"]
            for r in self.rejected[:20]:
                lines.append(f"  ❌ {r.policy_id} — {r.title}")
                for v in r.violations[:3]:
                    lines.append(f"       {v.path}: [{v.code}] {v.message}")
            if len(self.rejected) > 20:
                lines.append(f"  … 외 {len(self.rejected) - 20}건")

        return "\n".join(lines)


def build(
    policies: list[PolicySchema],
    *,
    previous_count: int | None = None,
    allow_partial: bool = False,
    today: date | None = None,
) -> tuple[list[PolicySchema], BuildReport]:
    """검증을 통과한 정책 목록과 리포트를 돌려준다.

    여기서는 파일을 쓰지 않는다 — 검사와 쓰기를 분리해야 테스트가 디스크 없이
    전체 경로를 확인할 수 있고, 운영에서 '검사만' 돌려볼 수 있다.
    """
    report = BuildReport(total_input=len(policies), previous_count=previous_count)
    today = today or today_kst()

    accepted: list[PolicySchema] = []
    seen: dict[str, int] = {}
    for index, policy in enumerate(policies):
        # policy_id 중복은 스키마 검사로는 안 걸린다 (룰 단위 검사라서).
        # 중복이 있으면 컴파일 단계에서 index_of 가 먼저 나온 것만 가리켜,
        # 뒤쪽 정책이 판정에서 통째로 누락된다.
        if policy.policy_id in seen:
            report.rejected.append(
                RejectedPolicy(
                    policy_id=policy.policy_id,
                    title=policy.meta.title,
                    violations=[
                        SchemaViolation(
                            path=f"[{index}].policy_id",
                            code="DUPLICATE_POLICY_ID",
                            message=f"{seen[policy.policy_id]}번째 정책과 policy_id 가 같습니다",
                        )
                    ],
                )
            )
            continue
        seen[policy.policy_id] = index

        if policy.status != "published":
            report.skipped_by_status[policy.status] += 1
            continue

        if _is_closed(policy, today, report):
            report.skipped_closed += 1
            continue

        violations = validate_policy(policy)
        if violations:
            report.rejected.append(
                RejectedPolicy(
                    policy_id=policy.policy_id,
                    title=policy.meta.title,
                    violations=violations,
                )
            )
            continue
        accepted.append(policy)

    report.accepted = len(accepted)

    if report.rejected and not allow_partial:
        raise SnapshotBuildError(
            f"{len(report.rejected)}건이 스키마 검증에 실패했습니다. "
            "조용히 빼고 내보내면 사용자에게는 그 정책이 '없는' 것으로 보입니다.\n"
            + report.render()
        )
    if report.rejected:
        report.warnings.append(
            f"{len(report.rejected)}건을 제외하고 빌드했습니다 (--allow-partial). "
            "제외된 정책은 사용자에게 노출되지 않습니다."
        )

    _guard(report)
    return accepted, report


def _is_closed(policy: PolicySchema, today: date, report: BuildReport) -> bool:
    """신청 기간이 끝났는가. 마감일 당일은 아직 열려 있다."""
    raw = policy.period.apply_end
    if not raw or policy.period.is_rolling:
        return False
    try:
        return date.fromisoformat(raw) < today
    except ValueError:
        # 날짜를 못 읽었다고 정책을 빼면, 형식 오류 하나가 정책 하나의 실종이 된다.
        # 남기고 경고한다 — 사람이 보고 고칠 수 있는 형태로.
        report.warnings.append(
            f"{policy.policy_id}: apply_end 를 날짜로 읽을 수 없습니다 ({raw!r}) "
            "— 마감 검사를 건너뜁니다"
        )
        return False


def _guard(report: BuildReport) -> None:
    """내보내기 전 마지막 관문. 여기서 막지 못하면 24시간 동안 틀린 채로 돈다."""
    if report.accepted < MIN_POLICY_COUNT:
        raise SnapshotBuildError(
            "통과한 정책이 0건입니다 — 빈 스냅샷은 '해당 정책 없음'이라는 "
            "정상 응답으로 보여서 장애를 감춥니다."
        )

    if report.previous_count:
        ratio = report.accepted / report.previous_count
        if ratio < MIN_RETENTION_RATIO:
            raise SnapshotBuildError(
                f"정책이 {report.previous_count}건에서 {report.accepted}건으로 "
                f"{(1 - ratio) * 100:.0f}% 줄었습니다 — 수집 사고일 가능성이 높습니다. "
                "의도한 변화라면 --force 로 진행하세요."
            )


def version_of(payload: bytes, count: int) -> str:
    """내용 해시 기반 버전. 같은 데이터면 같은 버전이어야 ETag 가 유효하다."""
    digest = hashlib.sha256(payload).hexdigest()[:12]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%MZ")
    return f"{stamp}-{count}p-{digest}"


def write_snapshot(
    policies: list[PolicySchema], out_path: Path, *, compress: bool
) -> tuple[str, int]:
    """스냅샷을 쓴다. 임시 파일에 쓰고 마지막에 옮긴다.

    같은 자리에 직접 쓰면, 쓰는 도중 프로세스가 죽었을 때 반쯤 쓰인 파일이
    남는다. API 가 부팅하며 그 파일을 읽으면 스냅샷 적재에 실패한 채로 뜬다.
    """
    payload = msgspec.json.encode(policies)
    version = version_of(payload, len(policies))

    body = gzip.compress(payload) if compress else payload
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(body)
    tmp.replace(out_path)

    return version, len(body)


def load_policies(path: Path) -> list[PolicySchema]:
    """PolicySchema 배열 JSON 을 읽는다. .gz 도 받는다."""
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return msgspec.json.decode(raw, type=list[PolicySchema])


def previous_count_of(path: Path) -> int | None:
    """직전 스냅샷의 정책 수. 없으면 None (첫 빌드)."""
    if not path.exists():
        return None
    try:
        return len(load_policies(path))
    except (msgspec.ValidationError, msgspec.DecodeError, OSError, gzip.BadGzipFile):
        # 직전 것을 못 읽는다고 새 빌드를 막을 이유는 없다. 급변 검사만 건너뛴다.
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="정책 JSON → 스냅샷 빌드")
    parser.add_argument("input", type=Path, help="PolicySchema 배열 JSON (.json / .json.gz)")
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("snapshot.json"), help="출력 경로"
    )
    parser.add_argument("--gzip", action="store_true", help="gzip 으로 압축해 쓴다")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="검증 실패 정책을 제외하고 빌드한다 (제외분은 사용자에게 보이지 않는다)",
    )
    parser.add_argument("--force", action="store_true", help="급변 검사를 건너뛴다")
    parser.add_argument(
        "--report", type=Path, default=None, help="리포트를 쓸 경로 (기본: 표준출력만)"
    )
    parser.add_argument("--check-only", action="store_true", help="검사만 하고 쓰지 않는다")
    args = parser.parse_args(argv)

    try:
        policies = load_policies(args.input)
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        print(f"입력을 읽지 못했습니다: {e}", file=sys.stderr)
        return 2

    previous = None if args.force else previous_count_of(args.output)

    try:
        accepted, report = build(
            policies, previous_count=previous, allow_partial=args.allow_partial
        )
    except SnapshotBuildError as e:
        print(str(e), file=sys.stderr)
        return 1

    if not args.check_only:
        version, written = write_snapshot(accepted, args.output, compress=args.gzip)
        report.version = version
        report.output_path = str(args.output)
        report.bytes_written = written

    text = report.render()
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":  # pragma: no cover
    force_utf8_console()
    raise SystemExit(main())

"""수집한 원본에서 **지금 신청할 수 있는 공고**만 골라 seed 형태로 저장한다.

왜 필요한가
  `batch.collect.cli fetch` 는 전체 응답을 `data/raw/<타임스탬프>/` 에 떨군다.
  지역을 좁혀도 전국 정책이 함께 오기 때문에 3,000건 가까이 되고, 그 디렉터리는
  `.gitignore` 에 있다(수 MB 를 저장소에 쌓지 않으려고). 그래서 수집한 기계와
  작업하는 기계가 다르면 **원본을 넘길 방법이 없다.**

  이 스크립트는 그 중 쓸 것만 남긴다. 결과는 `data/manual/raw/` 와 같은 모양이라
  `batch.agents.cli structure` 와 `batch.collect.cli normalize` 에 그대로 들어간다.

무엇을 고르는가
  1. 신청기간이 **오늘을 포함**하는 것 (상시모집 포함)
  2. 원문 링크가 있는 것 — 없으면 빌더가 어차피 거부한다
  3. `--region` 을 주면 그 지역 코드로 시작하는 공고만 (전국 정책은 `--nationwide`)

  마감된 공고는 빼지만 **몇 건을 왜 뺐는지 출력한다.** 조용히 줄어들면 수집이
  덜 된 것인지 원래 그만큼인지 구별할 수 없다.

쓰는 법
  python -m batch.collect.cli fetch --region 41000
  python tools/pick_open_notices.py data/raw/<타임스탬프> --region 41 --limit 8 \
      -o data/manual/raw/page-004.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from app.core.clock import today_kst

Record = dict[str, Any]

# "20260915 ~ 20260929" 또는 여러 구간 중 첫 구간
_DATE_RANGE = re.compile(r"(\d{8})\s*~\s*(\d{8})")

ROLLING = "0057002"  # 상시
CLOSED = "0057003"  # 마감


def _iso(yyyymmdd: str) -> date:
    return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))


def is_open(rec: Record, on: date) -> tuple[bool, str]:
    """신청 가능한가. (판정, 이유) 를 돌려준다 — 이유는 리포트에 쓴다."""
    code = str(rec.get("aplyPrdSeCd") or "").strip()
    if code == CLOSED:
        return False, "마감 코드"
    if code == ROLLING:
        return True, "상시모집"

    m = _DATE_RANGE.search(str(rec.get("aplyYmd") or ""))
    if not m:
        # 날짜를 못 읽는 건 '마감'이 아니다. 빼면 조용한 누락이 되므로 남긴다.
        return True, "신청기간 불명 — 남김"
    start, end = _iso(m.group(1)), _iso(m.group(2))
    if on < start:
        return False, f"아직 안 열림 ({start})"
    if on > end:
        return False, f"마감됨 ({end})"
    return True, f"{start}~{end}"


def in_region(rec: Record, prefix: str | None, nationwide: bool) -> bool:
    codes = [c.strip() for c in str(rec.get("zipCd") or "").split(",") if c.strip()]
    if not codes:
        return False
    # 전국 정책은 시군구 코드가 통째로 들어온다 (실측: 시도 단위 최대 47, 전국 최소 189)
    if len(codes) >= 100:
        return nationwide
    if prefix is None:
        return True
    return any(c.startswith(prefix) for c in codes)


def load_records(directory: Path) -> list[Record]:
    out: list[Record] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload.get("result", payload)
        out += result.get("youthPolicyList") or []
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", help="data/raw/<타임스탬프>")
    ap.add_argument("--region", help="법정동 코드 접두사 (예: 41=경기, 41820=가평)")
    ap.add_argument("--nationwide", action="store_true", help="전국 정책도 포함")
    ap.add_argument("--limit", type=int, default=0, help="최대 건수 (0=전부)")
    ap.add_argument("-o", "--output", default="data/manual/raw/page-004.json")
    ap.add_argument("--today", help="기준일 YYYY-MM-DD (기본: 오늘)")
    args = ap.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"원본 디렉터리가 없습니다: {directory}", file=sys.stderr)
        return 2

    on = date.fromisoformat(args.today) if args.today else today_kst()
    records = load_records(directory)
    if not records:
        print(f"{directory} 에서 정책을 하나도 못 읽었습니다", file=sys.stderr)
        return 2

    kept: list[Record] = []
    dropped: dict[str, int] = {}
    for rec in records:
        if not in_region(rec, args.region, args.nationwide):
            dropped["지역 불일치"] = dropped.get("지역 불일치", 0) + 1
            continue
        ok, why = is_open(rec, on)
        if not ok:
            key = why.split(" (")[0]
            dropped[key] = dropped.get(key, 0) + 1
            continue
        if not str(rec.get("refUrlAddr1") or rec.get("aplyUrlAddr") or "").strip():
            dropped["원문 링크 없음"] = dropped.get("원문 링크 없음", 0) + 1
            continue
        kept.append(rec)

    # 마감이 가까운 것부터. 발표에서 '마감 임박'을 보여주려면 이 순서가 필요하다.
    def deadline(rec: Record) -> str:
        m = _DATE_RANGE.search(str(rec.get("aplyYmd") or ""))
        return m.group(2) if m else "99999999"

    kept.sort(key=deadline)
    if args.limit:
        kept = kept[: args.limit]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_comment": (
            f"{directory} 에서 {on.isoformat()} 기준으로 신청 가능한 공고만 추린 것. "
            f"tools/pick_open_notices.py 가 생성했다. 원본은 온통청년 OPEN API 응답 그대로다."
        ),
        "youthPolicyList": kept,
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"기준일 {on.isoformat()} · 입력 {len(records)}건 → 저장 {len(kept)}건 → {out}")
    if dropped:
        print("  제외:", ", ".join(f"{k} {v}" for k, v in sorted(dropped.items())))
    for rec in kept:
        m = _DATE_RANGE.search(str(rec.get("aplyYmd") or ""))
        window = f"{m.group(1)}~{m.group(2)}" if m else "상시/불명"
        print(f"    {rec.get('plcyNo')}  {window}  {str(rec.get('plcyNm'))[:36]}")
    return 0 if kept else 1


if __name__ == "__main__":
    from app.core.console import force_utf8_console

    force_utf8_console()
    raise SystemExit(main())

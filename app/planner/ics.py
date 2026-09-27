"""신청 일정 → iCalendar (.ics) 내보내기.

라이브러리를 쓰지 않는다. RFC 5545 에서 우리가 필요한 부분은 VEVENT 몇 줄이고,
직렬화기 하나 때문에 무료 티어 이미지에 의존성을 더하는 것은 남는 장사가 아니다.
대신 스펙에서 실제로 사람을 무는 세 가지를 지킨다:

  줄 접기(75옥텟)   한글 제목은 UTF-8 에서 글자당 3바이트라 제목 25자면 넘는다.
                    안 접으면 구글 캘린더가 줄 전체를 버린다.
  이스케이프        쉼표·세미콜론·역슬래시·개행. 정책명에 쉼표가 흔하다.
  종일 이벤트       VALUE=DATE 로 쓰고 DTEND 는 다음 날(배타적)로 둔다.
                    같은 날로 두면 캘린더에 따라 이벤트가 아예 안 보인다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

from app.schemas.plan import PlanResponse, PolicyPlan

_PRODID = "-//YPC//Youth Policy Coordinator//KO"

# 줄 접기 한계는 '문자' 75개가 아니라 '옥텟' 75개다 (RFC 5545 §3.1).
_FOLD_OCTETS = 75

# 알림 시점. 착수일 당일 아침에만 울리면 그날 하루를 통째로 쓰게 된다.
_ALARM_LEAD = "-P1D"

EventKind = Literal["start", "deadline"]


def to_ics(plan: PlanResponse, *, now: datetime | None = None) -> bytes:
    """계획을 .ics 바이트로 만든다.

    이벤트는 정책당 최대 2개다: 준비 착수일과 마감일.
    날짜가 없는 정책(ROLLING/UNKNOWN)은 이벤트를 만들지 않는다 —
    캘린더에 '언제인지 모르는 일정'을 넣을 자리는 없다.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%SZ")

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:청년정책 신청 일정",
        "X-WR-TIMEZONE:Asia/Seoul",
    ]

    for item in plan.plans:
        lines += _events_for(item, plan.generated_for_date, stamp)

    lines.append("END:VCALENDAR")

    folded = "\r\n".join(_fold(line) for line in lines)
    return (folded + "\r\n").encode("utf-8")


def _events_for(plan: PolicyPlan, generated_for: str, stamp: str) -> list[str]:
    events: list[str] = []

    if plan.recommended_start_date:
        events += _vevent(
            uid=f"{plan.policy_id}-start@ypc",
            stamp=stamp,
            day=plan.recommended_start_date,
            summary=f"[서류 준비 시작] {plan.title}",
            description=_start_description(plan),
            alarm=True,
        )

    if plan.deadline_date and plan.status not in ("CLOSED",):
        events += _vevent(
            uid=f"{plan.policy_id}-deadline@ypc",
            stamp=stamp,
            day=plan.deadline_date,
            summary=f"[신청 마감] {plan.title}",
            description=_deadline_description(plan, generated_for),
            alarm=True,
        )

    return events


def _start_description(plan: PolicyPlan) -> str:
    parts = [plan.reason]
    if plan.documents:
        names = ", ".join(
            f"{d.name}(영업일 {d.lead_time_business_days}일"
            + ("·추정" if d.lead_time_estimated else "")
            + ")"
            for d in plan.documents
        )
        parts.append(f"필요서류: {names}")
    parts.append(_contact_line(plan))
    if plan.outside_calendar_coverage:
        parts.append("※ 공휴일 정보가 없는 기간이 포함되어 날짜가 바뀔 수 있습니다.")
    return "\n".join(p for p in parts if p)


def _deadline_description(plan: PolicyPlan, generated_for: str) -> str:
    parts = [f"{generated_for} 기준 판정 결과입니다."]
    if plan.business_days_to_deadline is not None:
        parts.append(f"기준일로부터 영업일 {plan.business_days_to_deadline}일 남았습니다.")
    parts.append(_contact_line(plan))
    return "\n".join(p for p in parts if p)


def _contact_line(plan: PolicyPlan) -> str:
    bits = [b for b in (plan.dept_name, plan.dept_tel, plan.origin_url) if b]
    return "문의: " + " / ".join(bits) if bits else ""


def _vevent(
    *, uid: str, stamp: str, day: str, summary: str, description: str, alarm: bool
) -> list[str]:
    start = date.fromisoformat(day)
    end = start + timedelta(days=1)  # DTEND 는 배타적이다

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        "TRANSP:TRANSPARENT",
    ]
    if alarm:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:{_ALARM_LEAD}",
            f"DESCRIPTION:{_escape(summary)}",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11. 역슬래시를 먼저 바꿔야 이중 이스케이프가 나지 않는다."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """75옥텟 초과 줄을 접는다. 이어지는 줄은 공백 한 칸으로 시작한다.

    UTF-8 문자를 중간에서 자르면 안 되므로, 바이트가 아니라 문자 단위로 붙이며
    길이를 센다.
    """
    raw = line.encode("utf-8")
    if len(raw) <= _FOLD_OCTETS:
        return line

    chunks: list[str] = []
    current = ""
    current_len = 0
    # 첫 줄은 75옥텟, 이어지는 줄은 선행 공백 1옥텟을 빼고 74옥텟까지
    limit = _FOLD_OCTETS
    for ch in line:
        size = len(ch.encode("utf-8"))
        if current_len + size > limit:
            chunks.append(current)
            current = ch
            current_len = size
            limit = _FOLD_OCTETS - 1
        else:
            current += ch
            current_len += size
    chunks.append(current)
    return "\r\n ".join(chunks)

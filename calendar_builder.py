"""Google カレンダーで購読できる calendar.ics を作る

- 毎週の新情報枠（火曜夜）
- forecast.json の解禁予想
- 実際に公開された更新（state.json の履歴から）
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

HISTORY_ICON = {
    "release": "🎉", "new": "🆕", "change": "🔄", "minor": "🔧",
    "major": "📢", "past": "📌",
}
HISTORY_SHOW = {"release", "new", "change", "major", "past"}


def esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """75 オクテットごとに折り返す（RFC 5545）"""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 74:
            out.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def utc_stamp(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_jst(s: str):
    """'2026-12-08' → date、'2026-12-08T21:00' → JST の datetime"""
    if "T" in s:
        return datetime.fromisoformat(s).replace(tzinfo=JST)
    return date.fromisoformat(s)


def event(uid, summary, start, end=None, description="", url="", rrule=None, alarm_min=None):
    lines = ["BEGIN:VEVENT", f"UID:{uid}@mc-update-watcher"]
    if isinstance(start, datetime):
        end = end or start + timedelta(minutes=30)
        lines += [f"DTSTAMP:{utc_stamp(start)}", f"DTSTART:{utc_stamp(start)}", f"DTEND:{utc_stamp(end)}"]
    else:
        end = end or start + timedelta(days=1)
        lines += [f"DTSTAMP:{start.strftime('%Y%m%d')}T000000Z",
                  f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"]
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines.append(f"SUMMARY:{esc(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{esc(description)}")
    if url:
        lines.append(f"URL:{url}")
    lines.append("TRANSP:TRANSPARENT")
    if alarm_min is not None:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(summary)}",
                  f"TRIGGER:-PT{int(alarm_min)}M", "END:VALARM"]
    lines.append("END:VEVENT")
    return [fold(x) for x in lines]


def build(history: list[dict], forecast_path: Path, out_path: Path):
    fc = json.loads(Path(forecast_path).read_text(encoding="utf-8"))
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mc-update-watcher//JA", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", fold("X-WR-CALNAME:マイクラ新情報（解禁予想）"), "X-WR-TIMEZONE:Asia/Tokyo",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]

    for w in fc.get("weekly", []):
        start = parse_jst(w["start"])
        end = start + timedelta(minutes=w.get("minutes", 300))
        lines += event(w["id"], w["title"], start, end, w.get("description", ""),
                       rrule=w.get("rrule", "FREQ=WEEKLY"), alarm_min=w.get("alarm_min"))

    for f in fc.get("events", []):
        start = parse_jst(f["start"])
        end = parse_jst(f["end"]) if f.get("end") else None
        if isinstance(start, date) and not isinstance(start, datetime) and end is not None:
            end = end + timedelta(days=1)  # 終日予定の終わりは翌日を指定する決まり
        conf = f.get("confidence", "")
        title = f["title"] + (f"［{conf}］" if conf else "")
        desc = f.get("reason", "")
        if f.get("alt"):
            desc = f"対抗: {f['alt']}\n" + desc
        lines += event(f["id"], title, start, end, desc, f.get("url", ""), alarm_min=f.get("alarm_min"))

    for h in history:
        v = h.get("verdict", "past")
        if v not in HISTORY_SHOW:
            continue
        start = datetime.fromisoformat(h["date"].replace("Z", "+00:00"))
        label = {"release": "正式リリース", "new": "新要素あり", "change": "仕様変更あり",
                 "major": "公式発表", "past": "公開"}[v]
        lines += event(f"h-{h['source']}-{h['id']}"[:200].replace(" ", ""),
                       f"{HISTORY_ICON[v]} {h['title']}（{label}）", start, start + timedelta(minutes=15),
                       f"{h['kind']}\n{h['url']}", h["url"])

    lines.append("END:VCALENDAR")
    Path(out_path).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

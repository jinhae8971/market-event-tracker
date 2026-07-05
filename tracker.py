#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market-event-tracker
====================
2026년 주요 증시 이벤트의 '당일' 국내(KOSPI)/미국(S&P500, NASDAQ) 등락을
자동으로 누적 기록하고, 대시보드(README.md + docs/dashboard.html)를 갱신하며,
이벤트 당일 텔레그램으로 결과를 통지한다.

동작 원리
---------
- 매일 22:30 UTC(=익일 07:30 KST) 실행. 실행 시점 UTC 날짜까지 마감된
  국내/미국 세션 데이터가 모두 확보되므로 그날 이벤트를 기록할 수 있다.
- 백필(backfill) 방식: 과거 이벤트 중 미기록/미완료 항목을 매 실행 시 재시도하므로
  워크플로우가 하루 누락돼도 다음 실행에서 자동 복구된다.
- 데이터 원천: FinanceDataReader(주력) → yfinance(폴백).

파일
----
- events.json  : 추적 이벤트 정의(입력)
- history.json : 이벤트별 등락 결과 누적(자동 갱신)
- README.md / docs/dashboard.html : 대시보드(자동 생성)
"""

import os
import sys
import json
import warnings
from datetime import datetime, timezone, timedelta

warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
EVENTS_PATH = os.path.join(BASE, "events.json")
HISTORY_PATH = os.path.join(BASE, "history.json")
README_PATH = os.path.join(BASE, "README.md")
HTML_PATH = os.path.join(BASE, "docs", "dashboard.html")

KST = timezone(timedelta(hours=9))
REPO_URL = "https://github.com/jinhae8971/market-event-tracker"

# 추적 지수: (표시명, FDR 심볼, yfinance 심볼, 국기)
INDICES = [
    ("KOSPI",  "KS11",  "^KS11",  "🇰🇷"),
    ("S&P500", "US500", "^GSPC",  "🇺🇸"),
    ("NASDAQ", "IXIC",  "^IXIC",  "🇺🇸"),
]

IMPORTANCE_STAR = {"high": "⭐⭐", "mid": "⭐", "low": "·"}
FINALIZE_AFTER_DAYS = 3  # 이벤트 후 N일 지나면 (휴장 등으로) 일부 지수 없어도 확정


# ────────────────────────── 유틸 ──────────────────────────
def load_config():
    return {
        "telegram_token":   os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def kdate(d):
    """'2026-07-07' → '07/07 (화)'"""
    dt = datetime.strptime(d, "%Y-%m-%d")
    wd = "월화수목금토일"[dt.weekday()]
    return f"{dt.month:02d}/{dt.day:02d} ({wd})"


def direction_of(pct):
    if pct is None:
        return "closed"
    if pct > 0.05:
        return "up"
    if pct < -0.05:
        return "down"
    return "flat"


DIR_EMOJI = {"up": "🔺", "down": "🔻", "flat": "➖", "closed": "⚪", "pending": "⏳"}
DIR_LABEL = {"up": "상승", "down": "하락", "flat": "보합", "closed": "휴장", "pending": "대기"}


# ────────────────────────── 데이터 취득 ──────────────────────────
def _fetch_fdr(sym, event_date):
    import FinanceDataReader as fdr
    start = (datetime.strptime(event_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (datetime.strptime(event_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    df = fdr.DataReader(sym, start, end)
    return df


def _fetch_yf(sym, event_date):
    import yfinance as yf
    start = (datetime.strptime(event_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    end = (datetime.strptime(event_date, "%Y-%m-%d") + timedelta(days=2)).strftime("%Y-%m-%d")
    df = yf.Ticker(sym).history(start=start, end=end)
    if len(df):
        df.index = df.index.tz_localize(None)
    return df


def fetch_index_on(fdr_sym, yf_sym, event_date):
    """이벤트 당일 종가/전일比 등락률을 반환. 해당일 데이터 없으면 change=None(휴장)."""
    df = None
    for fetcher, sym in ((_fetch_fdr, fdr_sym), (_fetch_yf, yf_sym)):
        try:
            df = fetcher(sym, event_date)
            if df is not None and len(df) >= 1 and "Close" in df.columns:
                break
        except Exception:
            df = None
    if df is None or len(df) == 0:
        return {"close": None, "change_pct": None, "direction": "pending"}

    df = df[df["Close"].notna()]
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    if event_date not in dates:
        # 해당 지수는 이벤트 당일 휴장(예: 미국 독립기념일)
        return {"close": None, "change_pct": None, "direction": "closed"}

    idx = dates.index(event_date)
    close = float(df["Close"].iloc[idx])
    if idx == 0:
        return {"close": round(close, 2), "change_pct": None, "direction": "pending"}
    prev = float(df["Close"].iloc[idx - 1])
    pct = (close - prev) / prev * 100 if prev else None
    return {
        "close": round(close, 2),
        "change_pct": round(pct, 2) if pct is not None else None,
        "direction": direction_of(pct),
    }


# ────────────────────────── 처리 로직 ──────────────────────────
def process(events, history):
    today = datetime.now(timezone.utc).date()
    new_events = []  # 이번 실행에서 '처음' 기록된 이벤트(텔레그램 통지 대상)

    for ev in events:
        d = ev["date"]
        ev_date = datetime.strptime(d, "%Y-%m-%d").date()
        if ev_date > today:
            continue  # 아직 도래하지 않음

        existing = history.get(d)
        if existing and existing.get("finalized"):
            continue  # 확정된 항목은 재조회 안 함

        # 지수별 취득
        results = {}
        for label, fdr_sym, yf_sym, _flag in INDICES:
            results[label] = fetch_index_on(fdr_sym, yf_sym, d)

        age_days = (today - ev_date).days
        resolved = all(r["direction"] not in ("pending",) for r in results.values())
        finalized = resolved or age_days >= FINALIZE_AFTER_DAYS

        entry = {
            "event": ev["name"],
            "region": ev.get("region", ""),
            "importance": ev.get("importance", "mid"),
            "indices": results,
            "finalized": finalized,
            "recorded_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        }

        is_new = existing is None
        history[d] = entry
        if is_new:
            new_events.append((d, entry))

    return history, new_events


# ────────────────────────── 대시보드 렌더 ──────────────────────────
def _cell(r):
    dirn = r["direction"]
    emo = DIR_EMOJI.get(dirn, "⏳")
    if dirn in ("up", "down", "flat") and r.get("change_pct") is not None:
        return f"{emo} {r['change_pct']:+.2f}%"
    if dirn == "closed":
        return "⚪ 휴장"
    return "⏳ 대기"


def render_readme(events, history):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    done = {d: e for d, e in history.items()}
    done_dates = sorted(done.keys())
    upcoming = [e for e in events if e["date"] not in history]
    upcoming = sorted(upcoming, key=lambda x: x["date"])

    # 집계
    tally = {lbl: {"up": 0, "down": 0, "flat": 0, "closed": 0} for lbl, *_ in INDICES}
    for d in done_dates:
        for lbl, *_ in INDICES:
            dirn = done[d]["indices"][lbl]["direction"]
            if dirn in tally[lbl]:
                tally[lbl][dirn] += 1

    L = []
    L.append("# 📊 Market Event Tracker")
    L.append("")
    L.append("> 2026년 주요 증시 이벤트 **당일** 국내(KOSPI)·미국(S&P500·NASDAQ) 등락을 자동 누적하는 대시보드")
    L.append(f">")
    L.append(f"> 🕐 최종 업데이트: **{now} KST**  ·  🤖 GitHub Actions 자동 생성")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 📈 누적 요약")
    L.append("")
    L.append(f"- **추적 이벤트**: 총 {len(events)}건 (✅ 완료 {len(done_dates)} · ⏳ 예정 {len(upcoming)})")
    for lbl, *_ in INDICES:
        t = tally[lbl]
        L.append(f"- **{lbl}**: 🔺 상승 {t['up']}회 · 🔻 하락 {t['down']}회 · ➖ 보합 {t['flat']}회")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## ✅ 완료된 이벤트 — 당일 등락")
    L.append("")
    if done_dates:
        L.append("| 날짜 | 이벤트 | 중요도 | 🇰🇷 KOSPI | 🇺🇸 S&P500 | 🇺🇸 NASDAQ |")
        L.append("|---|---|:---:|:---:|:---:|:---:|")
        for d in done_dates:
            e = done[d]
            star = IMPORTANCE_STAR.get(e["importance"], "·")
            row = [kdate(d), e["event"], star]
            for lbl, *_ in INDICES:
                row.append(_cell(e["indices"][lbl]))
            L.append("| " + " | ".join(row) + " |")
    else:
        L.append("_아직 도래한 이벤트가 없습니다. 첫 이벤트(2026-07-07) 이후 자동 기록됩니다._")
    L.append("")
    L.append("## ⏳ 예정된 이벤트")
    L.append("")
    if upcoming:
        L.append("| 날짜 | 이벤트 | 중요도 |")
        L.append("|---|---|:---:|")
        for e in upcoming:
            L.append(f"| {kdate(e['date'])} | {e['name']} | {IMPORTANCE_STAR.get(e['importance'],'·')} |")
    else:
        L.append("_예정된 이벤트가 없습니다._")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 🗂 데이터 & 규칙")
    L.append("")
    L.append("- **원천**: FinanceDataReader (주력) → yfinance (폴백)")
    L.append("- **지수**: KOSPI(`KS11`) · S&P500(`US500`) · NASDAQ(`IXIC`)")
    L.append("- **등락 기준**: 이벤트 당일 종가의 **전일 대비** 변화율")
    L.append("- **표기**: 🔺 상승 · 🔻 하락 · ➖ 보합(±0.05% 이내) · ⚪ 휴장 · ⏳ 대기")
    L.append("- **참고**: 미국발 이벤트(FOMC 등)에 대한 KOSPI 반응은 익일 세션에 반영되는 경향이 있음")
    L.append("- **갱신 주기**: 매일 22:30 UTC(익일 07:30 KST) · 누락 시 자동 백필")
    L.append("")
    L.append(f"<sub>🤖 자동 관리 시스템 · [{REPO_URL.split('//')[1]}]({REPO_URL})</sub>")
    L.append("")
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def render_html(events, history):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    done_dates = sorted(history.keys())
    upcoming = sorted([e for e in events if e["date"] not in history], key=lambda x: x["date"])

    def color(dirn):
        return {"up": "#e03131", "down": "#1971c2", "flat": "#868e96",
                "closed": "#adb5bd", "pending": "#adb5bd"}.get(dirn, "#adb5bd")

    def cell_html(r):
        dirn = r["direction"]
        emo = DIR_EMOJI.get(dirn, "⏳")
        if dirn in ("up", "down", "flat") and r.get("change_pct") is not None:
            txt = f"{r['change_pct']:+.2f}%"
        elif dirn == "closed":
            txt = "휴장"
        else:
            txt = "대기"
        return f'<td style="color:{color(dirn)};font-weight:700;text-align:center">{emo} {txt}</td>'

    rows = ""
    for d in done_dates:
        e = history[d]
        star = IMPORTANCE_STAR.get(e["importance"], "·")
        cells = "".join(cell_html(e["indices"][lbl]) for lbl, *_ in INDICES)
        rows += (f'<tr><td>{kdate(d)}</td><td style="text-align:left">{e["event"]}</td>'
                 f'<td style="text-align:center">{star}</td>{cells}</tr>')
    if not rows:
        rows = '<tr><td colspan="6" style="text-align:center;color:#868e96;padding:24px">아직 도래한 이벤트가 없습니다</td></tr>'

    up_rows = ""
    for e in upcoming:
        up_rows += (f'<tr><td>{kdate(e["date"])}</td><td style="text-align:left">{e["name"]}</td>'
                    f'<td style="text-align:center">{IMPORTANCE_STAR.get(e["importance"],"·")}</td></tr>')

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Market Event Tracker</title>
<style>
:root{{color-scheme:light dark}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,'Segoe UI',Roboto,'Noto Sans KR',sans-serif;margin:0;background:#0d1117;color:#e6edf3;padding:24px}}
.wrap{{max-width:920px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:#8b949e;font-size:13px;margin-bottom:20px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:18px 20px;margin-bottom:18px}}
h2{{font-size:15px;margin:0 0 12px;color:#c9d1d9}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:9px 8px;border-bottom:1px solid #21262d}}
th{{text-align:center;color:#8b949e;font-weight:600;font-size:12px}}
td{{text-align:center}}
.legend{{font-size:12px;color:#8b949e;line-height:1.9}}
</style></head><body><div class="wrap">
<h1>📊 Market Event Tracker</h1>
<div class="sub">2026 주요 증시 이벤트 당일 국내/미국 등락 누적 · 최종 업데이트 {now}</div>
<div class="card"><h2>✅ 완료된 이벤트 — 당일 등락</h2>
<table><thead><tr><th>날짜</th><th style="text-align:left">이벤트</th><th>중요도</th><th>🇰🇷 KOSPI</th><th>🇺🇸 S&amp;P500</th><th>🇺🇸 NASDAQ</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="card"><h2>⏳ 예정된 이벤트</h2>
<table><thead><tr><th>날짜</th><th style="text-align:left">이벤트</th><th>중요도</th></tr></thead><tbody>{up_rows or '<tr><td colspan=3 style="color:#868e96">없음</td></tr>'}</tbody></table></div>
<div class="card legend">🔺 상승 · 🔻 하락 · ➖ 보합 · ⚪ 휴장 · ⏳ 대기<br>
원천: FinanceDataReader → yfinance 폴백 · 등락 = 당일 종가 전일比</div>
</div></body></html>"""
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


# ────────────────────────── 텔레그램 ──────────────────────────
def notify(new_events, history, cfg):
    token, chat = cfg["telegram_token"], cfg["telegram_chat_id"]
    if not token or not chat or not new_events:
        return
    import requests
    done_n = len(history)
    for d, e in new_events:
        star = IMPORTANCE_STAR.get(e["importance"], "·")
        lines = [f"🎯 <b>이벤트 데이 결과</b> · {kdate(d)}",
                 f"<b>{e['event']}</b> {star}", ""]
        for lbl, _fdr, _yf, flag in INDICES:
            r = e["indices"][lbl]
            dirn = r["direction"]
            emo = DIR_EMOJI.get(dirn, "⏳")
            if dirn in ("up", "down", "flat") and r.get("change_pct") is not None:
                val = f"{emo} {r['change_pct']:+.2f}%"
                if r.get("close") is not None:
                    val += f"  ({r['close']:,.1f})"
            elif dirn == "closed":
                val = "⚪ 휴장"
            else:
                val = "⏳ 데이터 대기"
            lines.append(f"{flag} {lbl:<7} {val}")
        lines += ["", f"📊 <a href='{REPO_URL}'>대시보드 열기</a>",
                  f"누적: 완료 {done_n}건"]
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": "\n".join(lines),
                                "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=20).raise_for_status()
        except Exception as ex:
            print(f"[WARN] telegram send failed for {d}: {ex}", file=sys.stderr)


# ────────────────────────── main ──────────────────────────
def main():
    cfg = load_config()
    events = load_json(EVENTS_PATH, {"events": []})["events"]
    events = sorted(events, key=lambda x: x["date"])
    history = load_json(HISTORY_PATH, {})
    # 메타키(_로 시작) 제거
    history = {k: v for k, v in history.items() if not k.startswith("_")}

    history, new_events = process(events, history)

    save_json(HISTORY_PATH, history)
    render_readme(events, history)
    render_html(events, history)
    notify(new_events, history, cfg)

    print(f"[OK] events={len(events)} recorded={len(history)} new_this_run={len(new_events)}")
    for d, e in new_events:
        dirs = " ".join(f"{lbl}:{e['indices'][lbl]['direction']}" for lbl, *_ in INDICES)
        print(f"     + {d} {e['event']} | {dirs}")


if __name__ == "__main__":
    main()

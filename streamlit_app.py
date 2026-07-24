"""삼성카드 네이버 SA 자동입찰 — 성과 모니터링 대시보드

실행(로컬):  python -m streamlit run out/naver_sa_dashboard.py --server.port 8502
배포:        Streamlit Community Cloud (share.streamlit.io) — 시크릿 불필요

데이터 소스 원칙 (⚠️ 중요):
  · 비용·클릭·노출·순위 → '네이버SA RAW'  (키워드×캠페인×일자, 누적)
  · 방문·유입          → 'CTS_LOGGER_Daily_RAW'  (키워드×랜딩×일자, 전환 진실값)
  · 유입단가=비용/유입, 방문단가=비용/방문  (일 단위 집계 후 계산. C19_최종-주문횟수=유입)
  · Mapped_Daily/Today 는 '전일 최근14일 누적 + 캠페인 팬아웃'이라 합산 금지.
    → 키워드 드릴다운·조치 뷰에서만, 그것도 (키워드+기기) 1회 조인으로 사용.

인증: 시트가 '링크 보기 가능' 공개라 gviz/export CSV로 시크릿 없이 라이브 read.
정확도: 위 소스 기준 정확. 조치효과 섹션만 추정(화면 명시).
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

# =============================================================================
# 설정
# =============================================================================
SPREADSHEET_ID = "1Fw3GhJgMQ1YA9oQQ7Sh9f16wyikiVLqCLlTeepcNePM"
CACHE_TTL = "5m"
GID_CONFIG = "1429929246"
GID_SUGGESTION = "2147299042"

# 캠페인명 기반 랜딩 (네이버 측)
LANDING_RULES = [
    ("탭탭O", "탭탭오"), ("추천TOP3", "추천TOP3"), ("앤마일리지", "앤마일리지"),
    ("워크인", "워크인"), ("개별", "개별(BIZ)"), ("모니모", "모니모매스트윈"),
    ("TOP3", "추천TOP3"),
]
# 로거 group 코드 → 랜딩
LOGGER_LANDING = {
    "taptap": "탭탭오", "taptapO": "탭탭오", "top3": "추천TOP3",
    "앤마": "앤마일리지", "nmilege": "앤마일리지", "LEADERS": "개별(BIZ)",
    "monimotwin": "모니모매스트윈", "99": "모니모매스트윈",
}

st.set_page_config(page_title="네이버 SA 자동입찰 모니터", page_icon="🎯", layout="wide")


# =============================================================================
# 로드 유틸
# =============================================================================
def _gviz_url(name: str) -> str:
    return (f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
            f"/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(name)}")


def _export_url(gid: str) -> str:
    return (f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
            f"/export?format=csv&gid={gid}")


@st.cache_data(ttl=CACHE_TTL, show_spinner="시트 불러오는 중...")
def load_tab(name: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(_gviz_url(name), dtype=str)
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception:
        return pd.DataFrame()


def num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0)


def landing_from_campaign(camp: str, grp: str = "") -> str:
    blob = f"{camp} {grp}"
    for tok, lab in LANDING_RULES:
        if tok in blob:
            return lab
    return "기타"


def logger_device(c3: str) -> str:
    return "모바일" if "모바일" in str(c3) else "PC"


# =============================================================================
# 소스별 로더
# =============================================================================
@st.cache_data(ttl=CACHE_TTL, show_spinner="네이버 성과 가공 중...")
def load_naver(tab: str) -> pd.DataFrame:
    """네이버SA RAW / 네이버SA_14시 — 비용·클릭·노출·순위 (진실 소스)."""
    df = load_tab(tab)
    if df.empty:
        return df
    if "디바이스" in df.columns:
        df = df.rename(columns={"디바이스": "기기"})
    for c in ["현재입찰가", "노출수", "클릭수", "총비용", "평균노출순위"]:
        if c in df.columns:
            df[c] = num(df[c])
    df["날짜"] = pd.to_datetime(df["날짜"], errors="coerce")
    df = df.dropna(subset=["날짜"])
    df["랜딩"] = [landing_from_campaign(c, g) for c, g in
                zip(df.get("캠페인명", ""), df.get("광고그룹명", ""))]
    return df


@st.cache_data(ttl=CACHE_TTL, show_spinner="로거 전환 가공 중...")
def load_logger_daily() -> pd.DataFrame:
    """CTS_LOGGER_Daily_RAW — 방문·유입 (전환 진실값, 일자별)."""
    df = load_tab("CTS_LOGGER_Daily_RAW")
    if df.empty:
        return df
    vcol = next((c for c in df.columns if "방문" in c), None)
    ocol = next((c for c in df.columns if "주문" in c), None)
    out = pd.DataFrame({
        "날짜": pd.to_datetime(df.get("pe", df.get("ps")), errors="coerce"),
        "키워드": df.get("keyword", ""),
        "기기": [logger_device(x) for x in df.get("C3_매체/프로그램", "")],
        "랜딩": [LOGGER_LANDING.get(str(g).strip(), "기타") for g in df.get("group", "")],
        "방문": num(df[vcol]) if vcol else 0,
        "유입": num(df[ocol]) if ocol else 0,  # C19_최종-주문횟수 = 유입(전환) 지표
    }).dropna(subset=["날짜"])
    return out


@st.cache_data(ttl=CACHE_TTL)
def load_logger_intraday() -> pd.DataFrame:
    """CTS_LOGGER_RAW — 당일 30분 스냅샷 (오늘 진행중 방문·유입)."""
    df = load_tab("CTS_LOGGER_RAW")
    if df.empty:
        return df
    vcol = next((c for c in df.columns if "방문" in c), None)
    ocol = next((c for c in df.columns if "주문" in c), None)
    out = pd.DataFrame({
        "날짜": pd.to_datetime(df.get("snapshot_date"), errors="coerce"),
        "시각": df.get("snapshot_hour", ""),
        "키워드": df.get("keyword", ""),
        "기기": [logger_device(x) for x in df.get("C3_매체/프로그램", "")],
        "랜딩": [LOGGER_LANDING.get(str(g).strip(), "기타") for g in df.get("group", "")],
        "방문": num(df[vcol]) if vcol else 0,
        "유입": num(df[ocol]) if ocol else 0,  # C19_최종-주문횟수 = 유입(전환) 지표
    }).dropna(subset=["날짜"])
    return out


@st.cache_data(ttl=CACHE_TTL)
def load_runlog() -> pd.DataFrame:
    df = load_tab("Bid_Run_Log")
    if df.empty:
        return df
    for c in ["total", "changed", "skipped", "error", "duration_s"]:
        if c in df.columns:
            df[c] = num(df[c])
    df["run_at"] = pd.to_datetime(df["run_at"], errors="coerce")
    df = df.dropna(subset=["run_at"]).sort_values("run_at")
    df["시간대"] = df["run_at"].dt.hour.map(
        lambda h: "오전입찰" if 8 <= h < 12 else ("오후입찰" if 12 <= h < 18 else "데이파팅/기타"))
    return df


@st.cache_data(ttl=CACHE_TTL)
def load_config() -> dict:
    try:
        df = pd.read_csv(_export_url(GID_CONFIG), dtype=str)
    except Exception:
        df = load_tab("Config")
    out = {}
    if df.empty:
        return out
    df.columns = [c.strip() for c in df.columns]
    k, v = df.columns[0], df.columns[1]
    for _, r in df.iterrows():
        key = str(r[k]).strip()
        if key and key.lower() != "nan" and not key.startswith("-"):
            out[key] = str(r[v]).strip()
    return out


@st.cache_data(ttl=CACHE_TTL)
def load_suggestion() -> tuple[str, pd.DataFrame]:
    try:
        raw = pd.read_csv(_export_url(GID_SUGGESTION), header=None, dtype=str)
    except Exception:
        return "", pd.DataFrame()
    if raw.empty:
        return "", pd.DataFrame()
    banner = str(raw.iloc[0, 0])
    hrow = 1
    for i in range(min(4, len(raw))):
        if str(raw.iloc[i, 0]).strip() == "키워드":
            hrow = i
            break
    df = raw.iloc[hrow + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hrow]]
    df = df.dropna(how="all")
    df = df[df["키워드"].notna() & (df["키워드"].astype(str).str.strip() != "")]
    for c in ["현재입찰가", "제안입찰가", "변경강도(%)", "유입단가", "CPA", "목표대비(%)", "총비용"]:
        if c in df.columns:
            df[c] = num(df[c])
    if "캠페인명" in df.columns:
        df["랜딩"] = [landing_from_campaign(c, g) for c, g in
                    zip(df.get("캠페인명", ""), df.get("광고그룹명", ""))]
    # 제안 라벨은 오전('입찰가 상향','하향보류'…)·오후('상향'/'하향')가 달라 방향으로 정규화
    if "제안" in df.columns:
        df["_dir"] = df["제안"].map(classify_proposal)
    return banner, df


def classify_proposal(v) -> str:
    v = str(v)
    if "보류" in v or "유지" in v:   # '하향보류'는 실제로 유지 → 먼저 판정
        return "유지"
    if "OFF" in v or "제외" in v:
        return "OFF"
    if "상향" in v:
        return "상향"
    if "하향" in v:
        return "하향"
    return "유지"


_CHANGE_RE = re.compile(r"(\S+?)\s+(\d+)\s*→\s*(\d+)\s*\(([-+]?\d+)%\)")


def parse_changes(details: str) -> list[dict]:
    rows = []
    for m in _CHANGE_RE.finditer(str(details)):
        frm, to = int(m.group(2)), int(m.group(3))
        rows.append({"키워드": m.group(1), "이전가": frm, "변경가": to, "변화율": m.group(4) + "%",
                     "방향": "상향" if to > frm else ("하향" if to < frm else "유지")})
    return rows


# =============================================================================
# 집계 (올바른 소스 조인)
# =============================================================================
def naver_daily(nv: pd.DataFrame) -> pd.DataFrame:
    if nv.empty:
        return pd.DataFrame()
    g = nv.groupby("날짜").apply(lambda x: pd.Series({
        "비용": x["총비용"].sum(), "클릭": x["클릭수"].sum(), "노출": x["노출수"].sum(),
        "평균순위": (x["평균노출순위"] * x["노출수"]).sum() / max(x["노출수"].sum(), 1),
    }), include_groups=False).reset_index()
    return g


def logger_daily_agg(lg: pd.DataFrame) -> pd.DataFrame:
    if lg.empty:
        return pd.DataFrame()
    return lg.groupby("날짜").agg(방문=("방문", "sum"), 유입=("유입", "sum")).reset_index()


def build_daily(nv: pd.DataFrame, lg: pd.DataFrame) -> pd.DataFrame:
    n, l = naver_daily(nv), logger_daily_agg(lg)
    if n.empty and l.empty:
        return pd.DataFrame()
    d = n.merge(l, on="날짜", how="outer").sort_values("날짜") if not l.empty else n
    for c in ["비용", "클릭", "노출", "방문", "유입"]:
        if c in d:
            d[c] = d[c].fillna(0)
    # 유입단가 = 비용/유입, 방문단가 = 비용/방문
    d["유입단가"] = (d["비용"] / d["유입"].replace(0, float("nan"))).round(0)
    d["방문단가"] = (d["비용"] / d["방문"].replace(0, float("nan"))).round(0)
    d["CTR"] = (d["클릭"] / d["노출"].replace(0, float("nan")) * 100).round(2)
    return d


def build_dim2(nv: pd.DataFrame, lg: pd.DataFrame) -> pd.DataFrame:
    """일자×기기×랜딩×캠페인×그룹 마스터.
    비용·클릭·노출·순위 = 네이버(정확). 방문·유입 = 로거를 (날짜·기기·랜딩) 그룹 내
    비용 비례로 캠페인/그룹에 배분 → 합계는 로거 원본과 일치, 캠페인/그룹 필터 가능.
    """
    if nv.empty:
        return pd.DataFrame()
    nkeys = ["날짜", "기기", "랜딩", "캠페인명", "광고그룹명"]
    n = nv.assign(_rw=nv["평균노출순위"] * nv["노출수"]).groupby(nkeys, as_index=False).agg(
        비용=("총비용", "sum"), 클릭=("클릭수", "sum"), 노출=("노출수", "sum"), rankw=("_rw", "sum"))
    key = ["날짜", "기기", "랜딩"]
    if not lg.empty:
        l = lg.groupby(key, as_index=False).agg(방문=("방문", "sum"), 유입=("유입", "sum"))
        n["_gc"] = n.groupby(key)["비용"].transform("sum")
        n["_cnt"] = n.groupby(key)["비용"].transform("size")
        n = n.merge(l, on=key, how="left")
        n[["방문", "유입"]] = n[["방문", "유입"]].fillna(0)
        # 비용 점유율(비용 0이면 균등 배분)
        share = np.where(n["_gc"] > 0, n["비용"] / n["_gc"].replace(0, np.nan), 1.0 / n["_cnt"])
        share = pd.Series(share, index=n.index).fillna(0)
        n["방문"] = n["방문"] * share
        n["유입"] = n["유입"] * share
        n = n.drop(columns=["_gc", "_cnt"])
        # 네이버 지출행이 없는 (날짜·기기·랜딩) 로거 전환은 '(미매핑)'으로 회수 → 합계 보존
        miss = l.merge(n[key].drop_duplicates().assign(_p=1), on=key, how="left")
        miss = miss[miss["_p"].isna()]
        if not miss.empty:
            add = miss[key + ["방문", "유입"]].copy()
            add["캠페인명"] = "(미매핑)"
            add["광고그룹명"] = "(미매핑)"
            for c in ["비용", "클릭", "노출", "rankw"]:
                add[c] = 0.0
            n = pd.concat([n, add[n.columns]], ignore_index=True)
    else:
        n["방문"] = 0.0
        n["유입"] = 0.0
    return n


def daily_from_dim(d: pd.DataFrame) -> pd.DataFrame:
    """dim2(필터 적용본) → 일자별 집계 + 파생지표."""
    if d.empty:
        return pd.DataFrame()
    g = d.groupby("날짜", as_index=False).agg(
        비용=("비용", "sum"), 클릭=("클릭", "sum"), 노출=("노출", "sum"),
        rankw=("rankw", "sum"), 방문=("방문", "sum"), 유입=("유입", "sum"))
    g["평균순위"] = (g["rankw"] / g["노출"].replace(0, float("nan"))).round(2)
    g["유입단가"] = (g["비용"] / g["유입"].replace(0, float("nan"))).round(0)
    g["방문단가"] = (g["비용"] / g["방문"].replace(0, float("nan"))).round(0)
    g["CTR"] = (g["클릭"] / g["노출"].replace(0, float("nan")) * 100).round(2)
    return g.sort_values("날짜")


def filt(df: pd.DataFrame, cols=("기기", "랜딩")) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if "기기" in cols and sel_dev and "기기" in out:
        out = out[out["기기"].isin(sel_dev)]
    if "랜딩" in cols and sel_land and "랜딩" in out:
        out = out[out["랜딩"].isin(sel_land)]
    if "캠페인명" in cols and sel_camp and "캠페인명" in out:
        out = out[out["캠페인명"].isin(sel_camp)]
    if "광고그룹명" in cols and sel_grp and "광고그룹명" in out:
        out = out[out["광고그룹명"].isin(sel_grp)]
    return out


# =============================================================================
# 로드
# =============================================================================
nv_raw = load_naver("네이버SA RAW")
nv_14 = load_naver("네이버SA_14시")
lg_daily = load_logger_daily()
lg_intra = load_logger_intraday()
runlog = load_runlog()
config = load_config()
sug_banner, sug = load_suggestion()

if nv_raw.empty:
    st.title("네이버 SA 자동입찰 — 성과 모니터")
    st.error("시트에서 데이터를 불러오지 못했습니다. 시트 공개 설정을 확인하세요.")
    st.stop()

# =============================================================================
# 사이드바
# =============================================================================
with st.sidebar:
    st.header("🎯 자동입찰 모니터")
    if st.button("🔄 지금 새로고침", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"자동 갱신 {CACHE_TTL}마다 · 시트 라이브 read")
    st.divider()

    dmin, dmax = nv_raw["날짜"].min().date(), nv_raw["날짜"].max().date()
    date_range = st.date_input("추세 기간", (dmin, dmax), min_value=dmin, max_value=dmax)
    devs = sorted(nv_raw["기기"].dropna().unique())
    sel_dev = st.multiselect("기기", devs, default=devs)
    lands = sorted(set(nv_raw["랜딩"]) | set(lg_daily["랜딩"] if not lg_daily.empty else []))
    sel_land = st.multiselect("랜딩", lands, default=lands)
    camps = sorted(nv_raw["캠페인명"].dropna().unique())
    sel_camp = st.multiselect("캠페인", camps, default=[])
    grp_pool = nv_raw[nv_raw["캠페인명"].isin(sel_camp)] if sel_camp else nv_raw
    sel_grp = st.multiselect("광고그룹", sorted(grp_pool["광고그룹명"].dropna().unique()), default=[])
    st.caption("필터는 인사이트·KPI·추세·조치·키워드 전체 적용 (미선택=전체). "
               "방문·유입은 캠페인/그룹 필터 시 비용비례 배분(추정).")

    st.divider()
    dry = str(config.get("DRY_RUN", "?")).upper()
    if dry == "FALSE":
        st.success("● LIVE 반영 중 (DRY_RUN=FALSE)")
    elif dry == "TRUE":
        st.warning("● 시뮬레이션 (DRY_RUN=TRUE)")
    else:
        st.info(f"DRY_RUN={dry}")
    st.caption(f"MIN {config.get('MIN_BID','?')} · MAX {config.get('MAX_BID','?')} · "
               f"변화상한 {config.get('MAX_CHANGE_PCT','?')}%")

# 필터 적용본 — dim2(캠페인 배분 마스터)에 기기·랜딩·캠페인·그룹 전부 적용
dim2 = build_dim2(nv_raw, lg_daily)
FULLCOLS = ("기기", "랜딩", "캠페인명", "광고그룹명")
dim2f = filt(dim2, cols=FULLCOLS)
if date_range and len(date_range) == 2:
    lo, hi = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
    dim2f_range = dim2f[(dim2f["날짜"] >= lo) & (dim2f["날짜"] <= hi)]
else:
    dim2f_range = dim2f
daily = daily_from_dim(dim2f_range)
nvf = filt(nv_raw, cols=FULLCOLS)         # 키워드/랜딩 세부용

# =============================================================================
# 헤더 & 신선도
# =============================================================================
st.title("네이버 SA 자동입찰 — 성과 모니터")
conf_day = daily["날짜"].max().date() if not daily.empty else None
run_last = runlog["run_at"].max() if not runlog.empty else None
h = st.columns(4)
h[0].caption(f"📅 최신 확정일: **{conf_day}**")
h[1].caption(f"🤖 마지막 입찰실행: **{run_last:%m-%d %H:%M}**" if run_last is not None else "실행로그 없음")
h[2].caption("💰비용=네이버RAW · 👣방문/유입=로거RAW")
h[3].caption(f"👀 화면갱신: {datetime.now():%H:%M:%S}")


# =============================================================================
# 💡 상단 인사이트 & 요약 (현재 필터 기준)
# =============================================================================
def _won(v):
    return "-" if v is None or pd.isna(v) else f"₩{v:,.0f}"


def _b2h(s):  # **bold** → <b>
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


with st.container():
    bullets = []
    base_dates = ""
    comment = ""
    if not daily.empty:
        last = daily.iloc[-1]
        prev = daily.iloc[-2] if len(daily) >= 2 else None
        ld = f"{last['날짜']:%m-%d}"
        pdt = f"{prev['날짜']:%m-%d}" if prev is not None else None
        if pdt:
            base_dates = f"{ld} vs {pdt}"
        if prev is not None and not pd.isna(last["유입단가"]) and not pd.isna(prev["유입단가"]) and prev["유입단가"]:
            dc = (last["유입단가"] - prev["유입단가"]) / prev["유입단가"] * 100
            emoji, word = ("🟢", "개선") if dc < 0 else ("🔴", "상승")
            bullets.append(f"{emoji} **유입단가 {_won(last['유입단가'])}** ({ld}) — {pdt} 대비 "
                           f"{abs(dc):.0f}% {word} ({_won(prev['유입단가'])} → {_won(last['유입단가'])})")
            di = last["유입"] - prev["유입"]
            if dc < 0:
                comment = ("#ecfdf5", "#a7f3d0", "#065f46",
                           f"✅ {ld} 유입단가가 {pdt} 대비 {abs(dc):.0f}% 개선 (유입 {di:+.0f}) — 자동조치가 효율 방향으로 작동 중.")
            else:
                comment = ("#fffbeb", "#fde68a", "#92400e",
                           f"⚠️ {ld} 유입단가가 {pdt} 대비 {dc:.0f}% 상승 — 상향 조치 과다/하향 대상 점검 권장.")
        else:
            bullets.append(f"**유입단가 {_won(last['유입단가'])}** ({ld} 기준)")
        bullets.append(f"💰 ({ld}) 비용 **{_won(last['비용'])}** · 유입 **{last['유입']:,.0f}** · "
                       f"방문 **{last['방문']:,.0f}** · 방문단가 **{_won(last['방문단가'])}** · "
                       f"평균순위 **{last['평균순위']:.1f}위**")
        latest_d = nvf["날짜"].max()
        lb = nvf[nvf["날짜"] == latest_d].groupby("랜딩")["총비용"].sum().sort_values(ascending=False)
        if len(lb):
            bullets.append(f"🎯 ({latest_d:%m-%d}) 비용 최다 랜딩: **{lb.index[0]}** ({_won(lb.iloc[0])})")
    if not sug.empty and "_dir" in sug.columns:
        vc = filt(sug)["_dir"].value_counts()
        bullets.append(f"⚙️ 현재 조치 제안: 상향 **{int(vc.get('상향',0))}** · "
                       f"하향 **{int(vc.get('하향',0))}** · OFF **{int(vc.get('OFF',0))}**")

    lines = "".join(f"<div style='margin:1px 0'>• {_b2h(b)}</div>" for b in bullets)
    cmt = ""
    if comment:
        bg, bd, fg, txt = comment
        cmt = (f"<div style='margin:8px 0 0;padding:8px 12px;border-radius:8px;background:{bg};"
               f"color:{fg};font-size:12.5px;font-weight:600'>{txt}</div>")
    st.markdown(
        "<div style='font-size:13.5px;line-height:1.6;color:#0f172a;"
        "border:1px solid #e2e8f0;border-radius:12px;padding:14px 16px;background:#fff'>"
        "<div style='font-size:15px;font-weight:800;margin-bottom:7px'>💡 인사이트 &amp; 요약</div>"
        f"{lines}"
        f"<div style='color:#94a3b8;font-size:11px;margin:4px 0 2px'>현재 필터 기준 · "
        f"비교 {base_dates or '—'} · 유입단가=비용/유입 · 정확도: 정확</div>"
        f"{cmt}"
        "</div>", unsafe_allow_html=True)


tab_now, tab_trend, tab_act, tab_kw, tab_log = st.tabs(
    ["① 현황(당일·확정일)", "② 추세", "③ 자동조치", "④ 키워드", "⑤ 로그·설정"])


# =============================================================================
# ① 현황
# =============================================================================
def money(v):
    return "-" if v is None or pd.isna(v) else f"₩{v:,.0f}"


with tab_now:
    today_actual = datetime.now().date()

    # 오늘(진행중): 방문/유입=로거 intraday 최신 스냅샷, 비용/클릭/순위=네이버14시
    st.subheader(f"진행중 · 당일 {today_actual}")
    lgi = filt(lg_intra)
    v_today = o_today = None
    v_date = None
    latest_hour = None
    if not lgi.empty:
        v_date = lgi["날짜"].max()
        day_rows = lgi[lgi["날짜"] == v_date]
        # 최신 스냅샷 시각만 (누적값)
        if "시각" in day_rows and day_rows["시각"].notna().any():
            latest_hour = day_rows["시각"].max()
            day_rows = day_rows[day_rows["시각"] == latest_hour]
        v_today, o_today = day_rows["방문"].sum(), day_rows["유입"].sum()

    nv14 = filt(nv_14)
    c_today = clk_today = rank_today = None
    n14_date = None
    if not nv14.empty:
        n14_date = nv14["날짜"].max()
        nr = nv14[nv14["날짜"] == n14_date]
        c_today = nr["총비용"].sum()
        clk_today = nr["클릭수"].sum()
        rank_today = (nr["평균노출순위"] * nr["노출수"]).sum() / max(nr["노출수"].sum(), 1)

    same_day = (v_date is not None and n14_date is not None and v_date == n14_date)

    # ── 오늘 데이터 반영 상태 배지 ──
    naver_ok = (n14_date is not None and n14_date.date() == today_actual)
    logger_ok = (v_date is not None and v_date.date() == today_actual)
    lg_h = f" {latest_hour}" if latest_hour else ""
    n14_d = n14_date.date() if n14_date is not None else "-"
    v_d = v_date.date() if v_date is not None else "-"
    if naver_ok and logger_ok:
        st.success(f"✅ 오늘({today_actual}) 반영 완료 — 네이버 14시 수집 완료 · 로거 최신 스냅샷{lg_h}")
    elif logger_ok and not naver_ok:
        st.warning(f"⏳ 네이버 14시 수집 전 — 비용·클릭·순위는 아직 **직전일({n14_d})** 기준 "
                   f"(매일 14시경 반영). 방문·유입은 로거 오늘{lg_h} 반영됨.")
    elif naver_ok and not logger_ok:
        st.warning(f"⏳ 로거 오늘 스냅샷 대기 — 방문·유입은 **직전 수집({v_d})** 기준. 비용은 네이버 오늘 14시 반영됨.")
    else:
        st.warning(f"⏳ 오늘 데이터 반영 전 — 비용은 직전일({n14_d}) 네이버, 방문·유입은 직전({v_d}) 로거 기준. 수집되면 자동 갱신.")

    c = st.columns(4)
    c[0].metric("총비용(당일14시)", money(c_today))
    c[1].metric("유입(로거 최신)", "—" if o_today is None else f"{o_today:,.0f}")
    c[2].metric("방문(로거 최신)", "—" if v_today is None else f"{v_today:,.0f}")
    c[3].metric("클릭(당일14시)", "—" if clk_today is None else f"{clk_today:,.0f}")
    c2 = st.columns(4)
    ipc_t = (c_today / o_today) if (same_day and o_today) else None   # 유입단가=비용/유입
    vpc_t = (c_today / v_today) if (same_day and v_today) else None   # 방문단가=비용/방문
    c2[0].metric("유입단가", money(ipc_t))
    c2[1].metric("방문단가", money(vpc_t))
    c2[2].metric("평균순위(14시)", "—" if rank_today is None else f"{rank_today:.1f}위")
    c2[3].metric("클릭률", "—" if not clk_today else f"{clk_today/max(nr['노출수'].sum(),1)*100:.2f}%")
    notes = []
    if v_date is not None:
        notes.append(f"방문/유입 = 로거 {v_date.date()} 최신 스냅샷")
    if n14_date is not None:
        notes.append(f"비용/클릭 = 네이버 {n14_date.date()} 14시")
    if not same_day:
        notes.append("⚠️ 네이버 당일 14시 수집 전이라 비용·유입 날짜가 달라 유입단가는 14시 이후 계산")
    if sel_camp or sel_grp:
        notes.append("ℹ️ 진행중은 기기·랜딩 기준(캠페인/그룹 필터는 확정 데이터에만 적용)")
    st.caption(" · ".join(notes) + " · 진행중(정확도: 각 시점 누적 기준)")

    st.divider()
    _prev_day = daily.iloc[-2]["날짜"] if len(daily) >= 2 else None
    _pd_str = f"{_prev_day:%m-%d}" if _prev_day is not None else "—"
    st.subheader(f"확정일 {conf_day}"
                 + (f"  (직전일 {_pd_str} 대비)" if _prev_day is not None else ""))
    if len(daily) >= 1:
        last = daily.iloc[-1]
        prev = daily.iloc[-2] if len(daily) >= 2 else None

        def d(key):
            if prev is None or pd.isna(prev[key]) or prev[key] == 0 or pd.isna(last[key]):
                return None
            return f"{(last[key]-prev[key])/prev[key]*100:+.1f}%"

        c = st.columns(4)
        c[0].metric("총비용", f"₩{last['비용']:,.0f}", d("비용"))
        c[1].metric("유입", f"{last['유입']:,.0f}", d("유입"))
        c[2].metric("방문", f"{last['방문']:,.0f}", d("방문"))
        c[3].metric("클릭", f"{last['클릭']:,.0f}", d("클릭"))
        c2 = st.columns(4)
        c2[0].metric("유입단가", money(last["유입단가"]), d("유입단가"), delta_color="inverse")
        c2[1].metric("방문단가", money(last["방문단가"]), d("방문단가"), delta_color="inverse")
        c2[2].metric("평균순위", f"{last['평균순위']:.1f}위", d("평균순위"), delta_color="inverse")
        c2[3].metric("CTR", f"{last['CTR']:.2f}%", d("CTR"))
        st.caption(f"증감%는 {_pd_str} 대비 · 비용=네이버RAW, 방문/유입=로거RAW · 유입단가=비용/유입 · 정확도: 정확")


# =============================================================================
# ② 추세
# =============================================================================
with tab_trend:
    if dim2f_range.empty:
        st.info("표시할 데이터가 없습니다. 필터를 확인하세요.")
    else:
        DIM_LABEL = {"캠페인명": "캠페인", "광고그룹명": "광고그룹", "랜딩": "랜딩"}

        # ── ① 전체 합계 일자별 추세 (상단) ──
        st.markdown("##### 📉 전체 합계 일자별 추세 (현재 필터)")
        if len(daily) >= 2:
            d = daily.copy()
            d["날짜"] = d["날짜"].dt.strftime("%m-%d")
            r1 = st.columns(2)
            with r1[0]:
                st.markdown("**비용 & 유입**")
                base = alt.Chart(d).encode(x=alt.X("날짜:O", title=None))
                st.altair_chart(alt.layer(
                    base.mark_bar(color="#c7d2fe").encode(y=alt.Y("비용:Q", title="비용(원)")),
                    base.mark_line(point=True, color="#4f46e5").encode(y=alt.Y("유입:Q", title="유입")),
                ).resolve_scale(y="independent").properties(height=240), width="stretch")
            with r1[1]:
                st.markdown("**유입단가 추세 (비용÷유입, 낮을수록 좋음)**")
                st.altair_chart(alt.Chart(d).mark_line(point=True, color="#dc2626").encode(
                    x=alt.X("날짜:O", title=None), y=alt.Y("유입단가:Q", title="유입단가(원)"),
                    tooltip=["날짜", "유입단가"]).properties(height=240), width="stretch")
            r2 = st.columns(2)
            with r2[0]:
                st.markdown("**방문 & 방문단가 (비용÷방문)**")
                base = alt.Chart(d).encode(x=alt.X("날짜:O", title=None))
                st.altair_chart(alt.layer(
                    base.mark_bar(color="#bbf7d0").encode(y=alt.Y("방문:Q", title="방문")),
                    base.mark_line(point=True, color="#059669").encode(y=alt.Y("방문단가:Q", title="방문단가(원)")),
                ).resolve_scale(y="independent").properties(height=240), width="stretch")
                st.caption("방문은 유입보다 자주 발생 → 방문단가는 유입단가의 선행지표")
            with r2[1]:
                st.markdown("**평균 노출순위 (낮을수록 상위)**")
                st.altair_chart(alt.Chart(d).mark_line(point=True, color="#ea580c").encode(
                    x=alt.X("날짜:O", title=None),
                    y=alt.Y("평균순위:Q", title="순위", scale=alt.Scale(reverse=True)),
                    tooltip=["날짜", "평균순위"]).properties(height=240), width="stretch")
        else:
            st.info("전체 추세는 2일 이상 데이터가 필요합니다.")

        st.divider()
        # ── ② 기준별 성과 비교 (리더보드) ──
        st.markdown("##### 🏆 기준별 성과 비교 — 어떤 캠페인/그룹이 잘 나오나 (선택 기간 합계)")
        cc = st.columns(3)
        dimsel = cc[0].radio("기준", list(DIM_LABEL), format_func=lambda x: DIM_LABEL[x], horizontal=False)
        sortby = cc[1].radio("정렬", ["비용", "유입", "유입단가(효율)"], horizontal=False)
        metsel = cc[2].selectbox("아래 일자추세 지표", ["유입단가", "비용", "유입", "방문단가"])
        label = DIM_LABEL[dimsel]
        agg = dim2f_range.groupby(dimsel, as_index=False).agg(
            비용=("비용", "sum"), 클릭=("클릭", "sum"), 노출=("노출", "sum"),
            방문=("방문", "sum"), 유입=("유입", "sum"))
        agg["유입단가"] = (agg["비용"] / agg["유입"].replace(0, float("nan"))).round(0)
        agg["방문단가"] = (agg["비용"] / agg["방문"].replace(0, float("nan"))).round(0)
        agg["CTR"] = (agg["클릭"] / agg["노출"].replace(0, float("nan")) * 100).round(2)
        target = agg["비용"].sum() / max(agg["유입"].sum(), 1)   # 평균 유입단가(효율 기준선)
        # 정렬
        if sortby == "유입단가(효율)":
            board = agg.sort_values("유입단가", ascending=True, na_position="last")
        else:
            board = agg.sort_values(sortby, ascending=False)
        board = board.head(15).reset_index(drop=True)
        board.insert(0, "순위", range(1, len(board) + 1))
        show = board[["순위", dimsel, "비용", "유입", "방문", "유입단가", "CTR"]].rename(columns={dimsel: label})

        def ipc_color(col):
            out = []
            for v in col:
                if pd.isna(v):
                    out.append("color:#94a3b8")
                elif v <= target:
                    out.append("color:#059669;font-weight:700")
                elif v <= target * 1.5:
                    out.append("color:#334155")
                else:
                    out.append("color:#dc2626;font-weight:700")
            return out

        sty = (show.style
               .bar(subset=["비용"], color="#c7d2fe", vmin=0)
               .bar(subset=["유입"], color="#bbf7d0", vmin=0)
               .apply(ipc_color, subset=["유입단가"])
               .format({"비용": "₩{:,.0f}", "유입단가": "₩{:,.0f}", "방문": "{:,.0f}",
                        "유입": "{:,.0f}", "CTR": "{:.2f}%"}, na_rep="-"))
        st.dataframe(sty, hide_index=True, width="stretch",
                     height=min(560, 44 + len(show) * 35))
        st.caption(f"상위 15개 {label} · 비용·유입 = 막대 길이 · **유입단가 색**: 🟢평균이하(효율↑) / 🔴평균 1.5배↑ "
                   f"(평균 ₩{target:,.0f}) · 방문/유입은 캠페인·그룹 시 비용비례 배분(추정)")

        st.divider()
        # ── ③ 일자별 (기준별 멀티라인) ──
        st.markdown(f"##### 📈 일자별 {metsel} — {label} 상위 6 비교")
        top_dims = agg.sort_values("비용", ascending=False).head(6)[dimsel].tolist()
        dd = dim2f_range[dim2f_range[dimsel].isin(top_dims)].groupby(
            ["날짜", dimsel], as_index=False).agg(
            비용=("비용", "sum"), 방문=("방문", "sum"), 유입=("유입", "sum"))
        dd["유입단가"] = (dd["비용"] / dd["유입"].replace(0, float("nan"))).round(0)
        dd["방문단가"] = (dd["비용"] / dd["방문"].replace(0, float("nan"))).round(0)
        dd["d"] = dd["날짜"].dt.strftime("%m-%d")
        rev = (metsel in ("유입단가", "방문단가"))
        st.altair_chart(alt.Chart(dd).mark_line(point=True).encode(
            x=alt.X("d:O", title=None),
            y=alt.Y(f"{metsel}:Q", title=metsel),
            color=alt.Color(f"{dimsel}:N", title=label, legend=alt.Legend(orient="bottom")),
            tooltip=["d", dimsel, metsel]).properties(height=320), width="stretch")
        st.caption(f"비용 상위 6개 {label}의 일자별 {metsel}" + (" (낮을수록 좋음)" if rev else ""))


# =============================================================================
# ③ 자동조치
# =============================================================================
with tab_act:
    st.subheader("조치 요약")
    if sug.empty:
        st.info("Bid_Suggestion에서 현재 제안을 읽지 못했습니다.")
    else:
        st.caption(f"🗒️ {sug_banner.strip()}")
        if "_dir" in sug.columns:
            vc = sug["_dir"].value_counts()
            m = st.columns(4)
            m[0].metric("⬆️ 상향", int(vc.get("상향", 0)))
            m[1].metric("⬇️ 하향", int(vc.get("하향", 0)))
            m[2].metric("➡️ 유지", int(vc.get("유지", 0)))
            off = int(vc.get("OFF", 0))
            if off:
                m[3].metric("⛔ OFF/제외", off)
            else:
                m[3].metric("대상 키워드", len(sug))

    st.subheader("조치 상세 — 무엇을 · 왜 조정했나")
    if not sug.empty and "_dir" in sug.columns:
        sv = filt(sug, cols=("기기", "랜딩", "캠페인명", "광고그룹명")).copy()
        ind_col = "유입단가" if "유입단가" in sv.columns else ("CPA" if "CPA" in sv.columns else None)
        opts = [x for x in ["상향", "하향", "유지", "OFF"] if x in sv["_dir"].unique()]
        pick = st.multiselect("방향 필터", opts, default=[x for x in opts if x in ("상향", "하향")])
        if pick:
            sv = sv[sv["_dir"].isin(pick)]
        if "제안입찰가" in sv.columns and "현재입찰가" in sv.columns:
            sv["변화율"] = ((sv["제안입찰가"] - sv["현재입찰가"]) /
                          sv["현재입찰가"].replace(0, float("nan")) * 100).round(0)
        if "총비용" in sv.columns:
            sv = sv.sort_values("총비용", ascending=False)
        cols = [c for c in ["키워드", "기기", "랜딩", "캠페인명", "현재입찰가", "제안입찰가", "변화율",
                            ind_col, "분류", "제안"] if c and c in sv.columns]
        sv = sv[cols].head(120)

        def color(row):
            bg = ""
            if row.get("제안") and "상향" in str(row.get("제안")) and "보류" not in str(row.get("제안")):
                bg = "background-color: rgba(79,70,229,0.12)"
            elif row.get("제안") and "하향" in str(row.get("제안")) and "보류" not in str(row.get("제안")):
                bg = "background-color: rgba(220,38,38,0.12)"
            return [bg] * len(row)

        fmt = {"현재입찰가": "₩{:,.0f}", "제안입찰가": "₩{:,.0f}", "변화율": "{:+.0f}%"}
        if ind_col:
            fmt[ind_col] = "₩{:,.0f}"
        sty = sv.style.apply(color, axis=1).format(fmt, na_rep="-")
        st.dataframe(sty, width="stretch", hide_index=True, height=420)
        st.caption(f"파랑=상향 · 빨강=하향 · 분류=조치 사유 · 지표={ind_col or '—'} · 비용순 상위 120 · 정확도: 정확(제안 원본)")

    st.subheader("조치 이력 (실행 로그)")
    if not runlog.empty:
        rows = []
        for _, r in runlog.iterrows():
            for ch in parse_changes(r["details"]):
                rows.append({"실행시각": r["run_at"], "시간대": r["시간대"], "모드": r["mode"], **ch})
        hist = pd.DataFrame(rows)
        if not hist.empty:
            hist = hist.sort_values("실행시각", ascending=False)
            st.caption(f"전체 조치 이력 {len(hist)}건")
            st.dataframe(hist[["실행시각", "시간대", "모드", "키워드", "이전가", "변경가", "변화율", "방향"]].head(300),
                         width="stretch", hide_index=True, height=340,
                         column_config={"실행시각": st.column_config.DatetimeColumn(format="MM-DD HH:mm"),
                                        "이전가": st.column_config.NumberColumn(format="₩%d"),
                                        "변경가": st.column_config.NumberColumn(format="₩%d")})
        bid = runlog[runlog["시간대"].isin(["오전입찰", "오후입찰"])]
        if not bid.empty:
            lr = bid.iloc[-1]
            if str(lr["mode"]).upper().startswith("LIVE") and lr["total"] > 0 \
                    and lr["changed"] / lr["total"] < 0.1 and lr["changed"] <= 5:
                st.warning(f"⚠️ 마지막 LIVE 입찰 {int(lr['total'])}건 중 {int(lr['changed'])}건만 반영 — "
                           f"MAX_KEYWORDS 제한/오류 확인 필요.")


# =============================================================================
# ④ 키워드 (네이버 비용 + 로거 방문/유입, 키워드+기기 1회 조인)
# =============================================================================
with tab_kw:
    st.subheader("키워드 드릴다운 · 최신 확정일")
    nvk = filt(nv_raw, cols=("기기", "랜딩", "캠페인명", "광고그룹명"))
    if not nvk.empty:
        latest = nvk["날짜"].max()
        nk = nvk[nvk["날짜"] == latest].groupby(["키워드", "기기", "랜딩"]).apply(
            lambda x: pd.Series({
                "노출수": x["노출수"].sum(), "클릭수": x["클릭수"].sum(), "총비용": x["총비용"].sum(),
                "평균순위": (x["평균노출순위"] * x["노출수"]).sum() / max(x["노출수"].sum(), 1),
                "현재입찰가": x["현재입찰가"].max(),
            }), include_groups=False).reset_index()
        # 로거 방문/유입 (같은 날, 키워드+기기 1회 조인 → 중복 없음)
        lgk = filt(lg_daily)
        lgk = lgk[lgk["날짜"] == latest].groupby(["키워드", "기기"]).agg(
            방문=("방문", "sum"), 유입=("유입", "sum")).reset_index()
        k = nk.merge(lgk, on=["키워드", "기기"], how="left")
        k[["방문", "유입"]] = k[["방문", "유입"]].fillna(0)
        k["유입단가"] = (k["총비용"] / k["유입"].replace(0, float("nan"))).round(0)

        tot_cost = daily.iloc[-1]["비용"] if len(daily) else 0
        tot_in = daily.iloc[-1]["유입"] if len(daily) else 0
        target = (tot_cost / tot_in) if tot_in else 0   # 목표 유입단가 참고값

        def flag(r):
            if r["유입"] == 0 and target and r["총비용"] >= target:
                return "🔴 OFF후보"
            if r["유입"] == 0 and r["평균순위"] <= 3 and r["총비용"] > 0:
                return "🟠 상위·성과無"
            if not pd.isna(r["유입단가"]) and target and r["유입단가"] > target * 1.6:
                return "🟠 유입단가높음"
            return "🟢 유입有" if r["유입"] > 0 else ""
        k["점검"] = k.apply(flag, axis=1)
        only = st.checkbox("점검 필요만 보기", value=False)
        view = k[["키워드", "기기", "랜딩", "현재입찰가", "노출수", "클릭수", "총비용",
                  "평균순위", "방문", "유입", "유입단가", "점검"]].sort_values("총비용", ascending=False)
        if only:
            view = view[view["점검"].isin(["🔴 OFF후보", "🟠 상위·성과無", "🟠 유입단가높음"])]
        # NaN(유입 0 → 유입단가 계산 불가)은 "-"로 표시
        sty = view.style.format({
            "현재입찰가": "₩{:,.0f}", "총비용": "₩{:,.0f}", "유입단가": "₩{:,.0f}",
            "노출수": "{:,.0f}", "클릭수": "{:,.0f}", "방문": "{:,.0f}",
            "유입": "{:,.0f}", "평균순위": "{:.1f}",
        }, na_rep="-")
        st.dataframe(sty, width="stretch", hide_index=True, height=460)
        st.caption(f"기준일 {latest.date()} · 목표 유입단가 참고 ₩{target:,.0f} · "
                   f"비용=네이버RAW, 방문/유입=로거(키워드+기기 1회 조인) · 유입단가=비용/유입 · 정확도: 정확")


# =============================================================================
# ⑤ 로그·설정
# =============================================================================
with tab_log:
    a, b = st.columns([2, 1])
    with a:
        st.markdown("**최근 자동입찰 실행 (Bid_Run_Log)**")
        if not runlog.empty:
            st.dataframe(runlog.tail(20).sort_values("run_at", ascending=False)[
                ["run_at", "시간대", "mode", "total", "changed", "skipped", "error", "duration_s"]],
                width="stretch", hide_index=True, height=420,
                column_config={"run_at": st.column_config.DatetimeColumn(format="MM-DD HH:mm")})
    with b:
        st.markdown("**현재 설정 (Config)**")
        if config:
            st.dataframe(pd.DataFrame(list(config.items()), columns=["항목", "값"]),
                         width="stretch", hide_index=True, height=420)

st.divider()
st.caption("데이터: 자동입찰 시트 라이브 read (인증 없음) · 비용=네이버SA RAW · 방문/유입=CTS_LOGGER_Daily_RAW · "
           "Mapped_*는 드릴다운/조치 전용 · 비용 단위 원(KRW)")

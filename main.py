# 관심 아파트 실거래가 모니터링 → 카카오 나에게 보내기 알림 스크립트

import requests
import json
import yaml
import os
import sys
from datetime import datetime, timedelta
from urllib.parse import quote
import xmltodict

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.yaml")
SEEN_FILE   = os.path.join(os.path.dirname(__file__), "seen.json")
LOG_DIR     = os.path.join(os.path.dirname(__file__), "logs")

MOLIT_URL      = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
KAKAO_MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"


# ── 파일 I/O ──────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # GitHub Actions 환경변수 우선 적용
    if os.environ.get("MOLIT_API_KEY"):
        config["molit_api_key"] = os.environ["MOLIT_API_KEY"]
    if os.environ.get("KAKAO_ACCESS_TOKEN"):
        config["kakao_access_token"] = os.environ["KAKAO_ACCESS_TOKEN"]
    return config

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


# ── 조회 대상 월 산출 ─────────────────────────────────────────────────────────

def get_query_months():
    """당월 조회. 월초 5일 이내면 전월도 포함 (등록 지연 대응)."""
    now = datetime.now()
    months = [now.strftime("%Y%m")]
    if now.day <= 5:
        prev = now.replace(day=1) - timedelta(days=1)
        months.append(prev.strftime("%Y%m"))
    return months


# ── 국토부 실거래가 API ───────────────────────────────────────────────────────

def fetch_transactions(lawd_cd, deal_ymd, service_key):
    url = (
        f"{MOLIT_URL}"
        f"?serviceKey={quote(service_key, safe='')}"
        f"&LAWD_CD={lawd_cd}"
        f"&DEAL_YMD={deal_ymd}"
        f"&numOfRows=1000"
        f"&pageNo=1"
    )
    try:
        res  = requests.get(url, timeout=15)
        data = xmltodict.parse(res.content)
        items = data["response"]["body"]["items"]["item"]
        if isinstance(items, dict):
            items = [items]
        return items
    except Exception as e:
        log(f"[WARN] API 오류 (LAWD_CD={lawd_cd}, {deal_ymd}): {e}")
        return []


# ── 매칭 & 포매팅 유틸 ────────────────────────────────────────────────────────

def name_match(api_name: str, target: str) -> bool:
    a = api_name.replace(" ", "").lower()
    b = target.replace(" ", "").lower()
    return b in a or a in b

def make_key(item: dict) -> str:
    return "_".join([
        item.get("aptNm", "").replace(" ", ""),
        str(item.get("dealYear", "")),
        str(item.get("dealMonth", "")).zfill(2),
        str(item.get("dealDay",   "")).zfill(2),
        str(item.get("excluUseAr", "")),
        item.get("dealAmount", "").replace(",", "").replace(" ", ""),
    ])

def fmt_price(raw: str) -> str:
    try:
        v = int(raw.replace(",", "").strip())
        uk  = v // 10000
        man = v %  10000
        if uk > 0 and man > 0:
            return f"{uk}억 {man:,}만원"
        elif uk > 0:
            return f"{uk}억"
        return f"{v:,}만원"
    except Exception:
        return raw.strip()

def pyeong(area_str: str) -> str:
    try:
        return f"{float(area_str) / 3.3058:.1f}평"
    except Exception:
        return ""

def build_message(item: dict) -> str:
    area = item.get('excluUseAr', '')
    return (
        f"🏠 실거래가 알림\n"
        f"━━━━━━━━━━━━━━\n"
        f"단지: {item.get('aptNm', '')}\n"
        f"위치: {item.get('umdNm', '')}\n"
        f"면적(전용): {area}㎡ ({pyeong(area)})  |  {item.get('floor', '')}층\n"
        f"금액: {fmt_price(item.get('dealAmount', ''))}\n"
        f"계약일: {item.get('dealYear','')}.{str(item.get('dealMonth','')).zfill(2)}.{str(item.get('dealDay','')).zfill(2)}"
    )


# ── 카카오 나에게 보내기 ──────────────────────────────────────────────────────

def send_kakao(access_token: str, text: str) -> bool:
    template = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url":        "https://rt.molit.go.kr",
            "mobile_web_url": "https://rt.molit.go.kr",
        },
    }
    res = requests.post(
        KAKAO_MEMO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        timeout=10,
    )
    if res.status_code != 200:
        log(f"[WARN] 카카오 발송 실패: {res.status_code} {res.text[:120]}")
    return res.status_code == 200


# ── 로그 ─────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, datetime.now().strftime("%Y%m") + ".log")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    """
    일반 실행 : python main.py
    초기 실행 : python main.py --seed   (기존 거래 등록만, 알림 없음)
    """
    seed_mode = "--seed" in sys.argv

    config = load_config()
    seen   = load_seen()
    months = get_query_months()

    lawd_groups: dict = {}
    for apt in config["apartments"]:
        lawd_groups.setdefault(apt["lawd_cd"], []).append(apt)

    new_alerts = []

    for lawd_cd, apts in lawd_groups.items():
        for ym in months:
            items = fetch_transactions(lawd_cd, ym, config["molit_api_key"])
            for item in items:
                api_name = item.get("aptNm", "")
                for apt in apts:
                    if not name_match(api_name, apt["name"]):
                        continue
                    key = make_key(item)
                    if key in seen:
                        continue
                    seen[key] = datetime.now().strftime("%Y-%m-%d")
                    if not seed_mode:
                        new_alerts.append(build_message(item))

    save_seen(seen)

    if seed_mode:
        log(f"[초기화 완료] {len(seen)}건 등록됨. 이후부터 신규 거래 알림 시작.")
        return

    if new_alerts:
        ok = sum(
            send_kakao(config["kakao_access_token"], msg)
            for msg in new_alerts
        )
        log(f"신규 거래 {len(new_alerts)}건 감지 → {ok}건 발송 완료")
    else:
        log("신규 거래 없음")


if __name__ == "__main__":
    main()

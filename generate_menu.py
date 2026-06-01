#!/usr/bin/env python3
"""
週次トレーニングメニュー自動生成スクリプト
毎週月曜 7:00 に launchd から自動実行される
"""

import os
import sys
import json
import requests
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from anthropic import Anthropic

# ── 設定 ──────────────────────────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

INTERVALS_API_KEY     = os.environ["INTERVALS_API_KEY"]
INTERVALS_ATHLETE_ID  = os.environ["INTERVALS_ATHLETE_ID"]
NOTION_API_KEY        = os.environ["NOTION_API_KEY"]
NOTION_DB_ID          = "34107bda38ec81eb9e14c4a28715a60f"
ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
SLACK_WEBHOOK_URL     = os.environ["SLACK_WEBHOOK_URL"]
STRYD_EMAIL           = os.environ["STRYD_EMAIL"]
STRYD_PASSWORD        = os.environ["STRYD_PASSWORD"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "menu.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)
claude = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── intervals.icu ────────────────────────────────────────────────────
def _intervals_get(path: str, params: dict = None):
    url = f"https://intervals.icu{path}"
    r = requests.get(url, auth=("API_KEY", INTERVALS_API_KEY), params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def get_wellness() -> list:
    """直近14日間のウェルネスデータ（CTL/ATL/TSB等）を取得"""
    today = datetime.now()
    return _intervals_get(
        f"/api/v1/athlete/{INTERVALS_ATHLETE_ID}/wellness",
        {
            "oldest": (today - timedelta(days=14)).strftime("%Y-%m-%d"),
            "newest": today.strftime("%Y-%m-%d"),
        },
    )


def get_activities() -> list:
    """直近14日間のランニング活動を取得"""
    today = datetime.now()
    activities = _intervals_get(
        f"/api/v1/athlete/{INTERVALS_ATHLETE_ID}/activities",
        {
            "oldest": (today - timedelta(days=14)).strftime("%Y-%m-%d"),
            "newest": today.strftime("%Y-%m-%d"),
        },
    )
    # ランニングのみ、必要フィールドだけ返す
    runs = []
    for a in activities:
        if a.get("type") in ("Run", "VirtualRun", "TrailRun"):
            runs.append({
                "date":             a.get("start_date_local", "")[:10],
                "name":             a.get("name", ""),
                "distance_km":      round(a.get("distance", 0) / 1000, 1),
                "duration_min":     round(a.get("moving_time", 0) / 60, 0),
                "avg_hr":           a.get("average_heartrate"),
                "training_load":    a.get("training_load"),
                "avg_watts":        a.get("icu_average_watts"),        # STRYDパワー平均
                "normalized_watts": a.get("icu_weighted_avg_watts"),   # ノーマライズドパワー
                "power_ftp":        a.get("icu_pm_ftp_watts"),         # ランニングパワーFTP
            })
    return runs


# ── STRYD PowerCenter ────────────────────────────────────────────────
def get_stryd_data() -> dict:
    """STRYD PowerCenter からフォームメトリクスを取得（直近14日）"""
    # 認証
    r = requests.post(
        "https://www.stryd.com/b/email/signin",
        json={"email": STRYD_EMAIL, "password": STRYD_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json().get("token")

    # アクティビティ取得
    two_weeks_ago = int((datetime.now() - timedelta(days=14)).timestamp())
    r2 = requests.get(
        "https://www.stryd.com/b/api/v1/users/calendar",
        headers={"Authorization": f"Bearer: {token}"},
        params={"updated_after": two_weeks_ago, "include_deleted": "false"},
        timeout=15,
    )
    r2.raise_for_status()
    raw = r2.json()
    activities = raw.get("activities", raw) if isinstance(raw, dict) else raw

    # ランニング（sport_type=1）のみ
    runs = [a for a in activities if isinstance(a, dict) and a.get("sport_type") == 1]
    if not runs:
        return {}

    # 最新アクティビティからFTPとゾーンを取得
    latest = runs[0]
    ftp = latest.get("ftp")
    zones = latest.get("zones", [])

    # ゾーン名→ワット数のマップ
    zone_map = {z["name"]: z for z in zones} if zones else {}

    # 直近14日のフォームメトリクス（平均）
    def avg(key):
        vals = [a[key] for a in runs if a.get(key)]
        return round(sum(vals) / len(vals), 1) if vals else None

    # トレンド判定（直近3件 vs それ以前）
    def trend(key, higher_is_worse=True):
        vals = [a[key] for a in runs if a.get(key)]
        if len(vals) < 4:
            return "データ不足"
        recent = sum(vals[:3]) / 3
        older  = sum(vals[3:]) / len(vals[3:])
        diff_pct = (recent - older) / older * 100
        if abs(diff_pct) < 2.0:
            return "横ばい"
        if diff_pct > 0:
            return "悪化傾向（疲労サイン）" if higher_is_worse else "改善傾向"
        return "改善傾向" if higher_is_worse else "悪化傾向（疲労サイン）"

    return {
        "ftp_w":           round(ftp, 1) if ftp else None,
        "zones":           zone_map,
        "avg_gct_ms":      avg("average_ground_time"),
        "gct_trend":       trend("average_ground_time", higher_is_worse=True),
        "avg_lss":         avg("average_leg_spring"),
        "lss_trend":       trend("average_leg_spring", higher_is_worse=False),
        "avg_oscillation": avg("average_oscillation"),
        "osc_trend":       trend("average_oscillation", higher_is_worse=True),
        "avg_cadence":     avg("average_cadence"),
        "run_count":       len(runs),
    }


# ── Notion ───────────────────────────────────────────────────────────
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def get_upcoming_races() -> list:
    """Notionのトレーニングプランから直近90日以内のレース予定を取得"""
    today = datetime.now().strftime("%Y-%m-%d")
    ahead = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
    payload = {
        "filter": {
            "and": [
                {"property": "Tags", "multi_select": {"contains": "レース"}},
                {"property": "Date", "date": {"on_or_after": today}},
                {"property": "Date", "date": {"on_or_before": ahead}},
            ]
        }
    }
    r = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query",
        headers=NOTION_HEADERS,
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    races = []
    for page in r.json().get("results", []):
        props = page.get("properties", {})
        date = props.get("Date", {}).get("date", {}) or {}
        name_arr = props.get("Name", {}).get("title", [])
        races.append({
            "date": date.get("start", ""),
            "name": name_arr[0]["plain_text"] if name_arr else "",
        })
    return races


def _h2_green(text: str) -> dict:
    """緑背景のH2ブロック"""
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "color": "green_background",
        },
    }


def _numbered(text: str, children: list = None) -> dict:
    """番号付きリストブロック（children は省略可）"""
    block = {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
        },
    }
    if children:
        block["numbered_list_item"]["children"] = children
    return block


def _bullet(text: str) -> dict:
    """箇条書きブロック"""
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
        },
    }


def _build_menu_blocks(item: dict) -> list:
    """
    トレーニングメニューセクションのブロック群を構築する。

    質的練習（has_schedule=True）の例:
      1. (C60秒+R60秒)×15
          1. トレーニング強度：苦しすぎず、楽でもない強度
          2. トレーニング内容（65分）
              1. ウォーミングアップジョグ：15分
              ...

    Eペース（has_schedule=False）の例:
      1. 40~60分リラックスジョグ
          1. トレーニング強度：笑顔で会話しながら走れる強度
          2. 15分経過したら、10秒程度のダッシュを4本
    """
    intensity_block = _numbered(f"トレーニング強度：{item['intensity']}")

    if item.get("has_schedule") and item.get("schedule"):
        duration = item.get("total_duration_min", "")
        schedule_children = [_numbered(s) for s in item["schedule"]]
        content_block = _numbered(f"トレーニング内容（{duration}分）", schedule_children)
        level2 = [intensity_block, content_block]
    else:
        level2 = [intensity_block]
        for note in item.get("extra_notes_in_menu", []):
            level2.append(_numbered(note))

    main_item = _numbered(item["name"], level2)
    return [_h2_green("トレーニングメニュー"), main_item]


def create_notion_pages(menu_items: list) -> list:
    """7日分のNotionページを作成し、URLリストを返す"""
    today = datetime.now()
    days_to_monday = (7 - today.weekday()) % 7 or 7
    next_monday = today + timedelta(days=days_to_monday)

    urls = []
    for item in menu_items:
        date = next_monday + timedelta(days=item["day_offset"])
        title = f"📝 【AM】{item['name']}"

        # 注意事項ブロック
        notes_list = item.get("notes", [])
        if isinstance(notes_list, str):
            notes_list = [notes_list] if notes_list else []
        notes_blocks = [_bullet(n) for n in notes_list if n]

        children = (
            _build_menu_blocks(item)
            + [_h2_green("このトレーニングの目的")]
            + [_bullet(item.get("training_purpose", ""))]
            + [_h2_green("注意事項")]
            + (notes_blocks if notes_blocks else [_bullet("")])
        )

        payload = {
            "parent": {"database_id": NOTION_DB_ID},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "Date": {"date": {"start": date.strftime("%Y-%m-%d")}},
                "Tags": {
                    "multi_select": [{"name": t} for t in item.get("tags", ["練習"])]
                },
                "トレーニングの目的": {
                    "multi_select": [{"name": p} for p in item.get("purposes", [])]
                },
            },
            "children": children,
        }
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        urls.append(r.json()["url"])
        log.info(f"  作成: {date.strftime('%m/%d')} {title}")

    return urls


# ── Claude でメニュー生成 ────────────────────────────────────────────
def generate_menu(wellness: list, runs: list, races: list, stryd: dict) -> list:
    # 最新のフィットネス指標
    latest = wellness[-1] if wellness else {}
    ctl = latest.get("ctl", "不明")
    atl = latest.get("atl", "不明")
    tsb = latest.get("tsb", "不明")

    race_text = "\n".join(
        f"  - {r['date']} {r['name']}" for r in races
    ) or "  直近90日以内のレースなし"

    # レースまでの残り週数を計算
    weeks_to_race = None
    if races:
        try:
            race_date = datetime.strptime(races[0]["date"], "%Y-%m-%d")
            days_to_race = (race_date - datetime.now()).days
            weeks_to_race = max(0, days_to_race // 7)
        except Exception:
            pass
    weeks_to_race_text = f"{weeks_to_race}週" if weeks_to_race is not None else "不明"

    # STRYDパワーゾーンテキスト
    ftp_w = stryd.get("ftp_w")
    zones = stryd.get("zones", {})
    if ftp_w and zones:
        easy  = zones.get("Easy",      {})
        mod   = zones.get("Moderate",  {})
        thr   = zones.get("Threshold", {})
        power_zone_text = (
            f"- ランニングパワーFTP（STRYD）: {ftp_w}W\n"
            f"- Easyゾーン: {round(easy.get('power_low',0))}〜{round(easy.get('power_high',0))}W\n"
            f"- Moderateゾーン: {round(mod.get('power_low',0))}〜{round(mod.get('power_high',0))}W\n"
            f"- Thresholdゾーン: {round(thr.get('power_low',0))}〜{round(thr.get('power_high',0))}W"
        )
        easy_low    = round(easy.get("power_low",  ftp_w * 0.65))
        easy_high   = round(easy.get("power_high", ftp_w * 0.78))
        thresh_low  = round(thr.get("power_low",   ftp_w * 0.89))
        thresh_high = round(thr.get("power_high",  ftp_w * 1.00))
    elif ftp_w:
        easy_low, easy_high     = int(ftp_w * 0.65), int(ftp_w * 0.78)
        thresh_low, thresh_high = int(ftp_w * 0.89), int(ftp_w * 1.00)
        power_zone_text = (
            f"- ランニングパワーFTP（STRYD）: {ftp_w}W\n"
            f"- Eペース目安: {easy_low}〜{easy_high}W\n"
            f"- 閾値走目安: {thresh_low}〜{thresh_high}W"
        )
    else:
        easy_low = easy_high = thresh_low = thresh_high = "？"
        power_zone_text = "- パワーデータなし（HR・ペースで判断）"

    # STRYDフォームメトリクステキスト
    gct  = stryd.get("avg_gct_ms")
    lss  = stryd.get("avg_lss")
    osc  = stryd.get("avg_oscillation")
    cad  = stryd.get("avg_cadence")
    if gct or lss or osc:
        form_text = (
            f"- 接地時間（GCT）: {gct}ms　トレンド: {stryd.get('gct_trend','不明')}\n"
            f"  （目安: 200〜250ms。増加は疲労サイン）\n"
            f"- 脚バネ剛性（LSS）: {lss}kN/m　トレンド: {stryd.get('lss_trend','不明')}\n"
            f"  （目安: 8〜12kN/m。減少は疲労・フォーム崩壊サイン）\n"
            f"- 垂直振幅: {osc}cm　トレンド: {stryd.get('osc_trend','不明')}\n"
            f"  （目安: 5〜8cm。増加は無駄なエネルギー消費のサイン）\n"
            f"- ケイデンス: {cad}spm"
        )
    else:
        form_text = "- フォームデータなし"

    runs_text = "\n".join(
        f"  - {r['date']} {r['name']} {r['distance_km']}km "
        f"({r['duration_min']}分) HR:{r['avg_hr']} "
        + (f"Avg:{r['avg_watts']}W NP:{r['normalized_watts']}W " if r.get("avg_watts") else "")
        + f"負荷:{r['training_load']}"
        for r in runs
    ) or "  データなし"

    prompt = f"""あなたは高岡尚司（ZEROBASEコーチ）のトレーニングコーチです。
以下のデータと制約をもとに、{{week_label}}の練習メニューを作成してください。

【現在のフィットネス状態】
- CTL（フィットネス）: {ctl}
- ATL（疲労）: {atl}
- TSB（フォーム: プラスが良好）: {tsb}

【STRYDパワーゾーン】
{power_zone_text}

【STRYDフォームメトリクス（直近14日平均）】
{form_text}

【直近14日間のランニング活動】
{runs_text}

【目標レース】
{race_text}
- レースまでの残り日数: {weeks_to_race_text}

【目標レースの特性】
- OSJ ONTAKE100（100km）：7月開催・長野県王滝村
- コース：未舗装林道中心（技術的トレイルは少ない）
- 距離：約106〜108km
- 獲得標高：約3,000m（D+）
- 特徴：直射日光の林道が延々と続く「走る座禅」。非常に暑い
- スタート：深夜（夜間走の経験も必要）
- 必要な能力：長時間の有酸素持久力、暑熱耐性、補給スキル

【練習可能時間の制約（厳守）】
- 月曜：短め（40〜60分まで）
- 火曜：唯一の長時間練習日。ロング走を必ず入れる（2〜4時間）
- 水曜：短〜中程度（40〜70分まで）
- 木曜：短〜中程度（40〜70分まで）
- 金曜：短め（40〜60分まで）
- 土曜：短め or 完全休養（30〜45分まで、または休養）
- 日曜：午後3時間程度（ミドルロング可、90〜180分）

【フェーズ別の判断基準】
現在のレースまでの残り: {weeks_to_race_text}

◆ 負荷フェーズ（レースまで8週以上）
  → 有酸素ベース構築。ロング走重視、閾値走週1回まで
◆ レース特異的フェーズ（レースまで4〜7週）← 現在このフェーズ
  → 時間走・暑熱順化・補給練習を最優先。夜間ジョグを週1回入れる
◆ テーパリングフェーズ（レースまで2〜3週）
  → 週間総量を30%削減。ロング走は90分以内に短縮。強度は維持
◆ 仕上げフェーズ（レースまで1週）
  → 短め・流しのみ。完全休養を複数日入れる

【閾値走の実施条件（厳守）】
- TSB ≥ 0 の場合のみ閾値走・クルーズインターバルを実施する
- TSB < 0 の場合は閾値走を禁止。代わりに「Eペース＋流し」に置き換えること
- 現在のTSB: {tsb}

【暑熱順化プロトコル（レース4〜7週前に適用）】
暑熱順化の生理学的効果（血漿量増加・発汗閾値低下）は連続10〜14日の暑熱曝露で得られる。
- レース4〜7週前フェーズでは、火曜ロング走・日曜ミドルロング走は必ず13〜15時台に実施
- notes に「13〜15時台に実施すること（暑熱順化プロトコル）」を必ず含める
- 「暑い時間帯に走る」という曖昧な記述は不可。時刻を明記すること

【ウルトラトレイル向けメニューの種類】
パワー目安はSTRYD FTP({ftp_w}W)をもとに算出。intensity フィールドに必ずワット数を添える。

A) Eペース リラックスジョグ（短〜中）
   - name 例: "40〜60分リラックスジョグ"
   - has_schedule: false
   - intensity: "笑顔で会話しながら走れる強度（目安: {easy_low}〜{easy_high}W）"
   - extra_notes_in_menu: ["15分経過したら、10秒程度のダッシュを4本"]

B) ロング走（火曜メイン）
   - name 例: "2〜3時間 時間走（ウルトラ対策）"
   - has_schedule: true
   - intensity: "笑顔で会話しながら走れる強度。後半も同じワット数を維持（目安: {easy_low}〜{easy_high}W）"
   - schedule 例: ["ウォーミングアップジョグ：15分", "メイン走（Eペース）：90〜150分", "クーリングダウン：15分"]
   - total_duration_min: 120〜180
   - extra_notes_in_menu: ["補給の練習を兼ねる（30〜45分ごとに補給）"]
   - ※レース4〜7週前は notes に「13〜15時台に実施すること（暑熱順化プロトコル）」を含める

C) 閾値走／クルーズインターバル（TSB ≥ 0 のときのみ・週1回まで）
   - name 例: "(C60秒+R60秒)×15"
   - has_schedule: true
   - intensity: "苦しすぎず、楽でもない強度（目安: {thresh_low}〜{thresh_high}W）"
   - schedule: ["ウォーミングアップジョグ：15分","流し（4本）：5分","息を整える：5分","メイントレーニング：30分","クーリングダウンジョグ：10分"]
   - total_duration_min: 65

D) ミドルロング走（日曜）
   - name 例: "90〜150分 ミドルロング走"
   - has_schedule: false
   - intensity: "笑顔で会話しながら走れる強度（目安: {easy_low}〜{easy_high}W）"
   - extra_notes_in_menu: ["補給を1〜2回練習する", "後半ペースアップ（任意）"]
   - ※レース4〜7週前は notes に「13〜15時台に実施すること（暑熱順化プロトコル）」を含める

E) 完全休養
   - name: "完全休養"
   - has_schedule: false
   - intensity: "休む"
   - extra_notes_in_menu: ["ストレッチや足のケアに時間を使う"]

F) 夜間ジョグ（レース4〜7週前・週1回）
   - name 例: "30〜40分 夜間ジョグ（ONTAKE深夜スタート対策）"
   - has_schedule: false
   - intensity: "笑顔で会話しながら走れる強度（目安: {easy_low}〜{easy_high}W）"
   - notes: ["20〜22時の間に実施する", "ヘッドライトを装着して走る（夜間感覚を養うため）", "距離やペースより「夜に体を動かす感覚」を優先する"]
   - レース4〜7週前の週に水・木・金のいずれか1日に必ず入れる

【出力形式（JSONのみ・説明文なし）】
[
  {{
    "day_offset": 0,
    "day_label": "月",
    "name": "40~60分リラックスジョグ",
    "tags": ["練習"],
    "purposes": ["脂質代謝改善"],
    "intensity": "笑顔で会話しながら走れる強度",
    "has_schedule": false,
    "total_duration_min": 50,
    "schedule": [],
    "extra_notes_in_menu": ["15分経過したら、10秒程度のダッシュを4本"],
    "training_purpose": "有酸素能力の底上げとリカバリー",
    "notes": ["横隔膜の動きをイメージしましょう", "ウォッチを見ないようにしましょう"]
  }}
]

day_offset: {{offset_label}}
purposes 選択肢: リカバリー、ランニングエコノミー、最大酸素摂取量、乳酸性作業閾値、脂質代謝改善、フォームチェック、測定
tags 選択肢: 練習、調整、レース、練習会
notes は必ず配列（文字列のリスト）で返すこと。
JSONのみ返すこと（コードブロック記号不要）。"""

    def _call(label: str, offset_label: str) -> list:
        filled = prompt.replace("{week_label}", label).replace("{offset_label}", offset_label)
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": filled}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(raw)

    log.info("   第1週生成中...")
    week1 = _call("第1週（月〜日）7日分", "月=0 火=1 水=2 木=3 金=4 土=5 日=6")
    log.info("   第2週生成中...")
    week2 = _call("第2週（月〜日）7日分", "月=7 火=8 水=9 木=10 金=11 土=12 日=13")

    # day_offsetを第2週用に補正（モデルが0始まりで返した場合）
    for item in week2:
        if item.get("day_offset", 0) < 7:
            item["day_offset"] += 7

    return week1 + week2


# ── Slack 通知 ────────────────────────────────────────────────────────
def notify_slack(page_urls: list, ctl, atl, tsb):
    notion_url = f"https://www.notion.so/{NOTION_DB_ID.replace('-', '')}"
    msg = (
        f"📅 *2週間分の練習メニュー（下書き）が完成しました*\n\n"
        f"*フィットネス状態* CTL:{ctl}　ATL:{atl}　TSB:{tsb}\n\n"
        f"確認・修正してから📝マークを消してください:\n{notion_url}\n\n"
        f"（{len(page_urls)}日分を作成しました）"
    )
    r = requests.post(SLACK_WEBHOOK_URL, json={"text": msg}, timeout=10)
    r.raise_for_status()


# ── 実行間隔チェック ──────────────────────────────────────────────────
LAST_RUN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_run")
INTERVAL_DAYS = 14  # 2週間おきに実行

def should_run() -> bool:
    """前回実行から14日以上経過していれば True を返す"""
    if not os.path.exists(LAST_RUN_FILE):
        return True
    try:
        with open(LAST_RUN_FILE) as f:
            last = datetime.fromisoformat(f.read().strip())
        elapsed = (datetime.now() - last).days
        if elapsed < INTERVAL_DAYS:
            log.info(f"前回実行から {elapsed} 日（{INTERVAL_DAYS}日未満）のためスキップします")
            return False
    except Exception:
        pass
    return True

def record_run():
    """実行日時を記録する"""
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now().isoformat())


# ── メイン ────────────────────────────────────────────────────────────
def main():
    log.info("=== 隔週メニュー生成 開始 ===")

    if not should_run():
        return

    log.info("① intervals.icu からデータ取得中...")
    wellness = get_wellness()
    runs = get_activities()
    log.info(f"   ウェルネス: {len(wellness)}件 / ランニング: {len(runs)}件")

    log.info("② Notion からレース予定を取得中...")
    races = get_upcoming_races()
    log.info(f"   レース予定: {len(races)}件")

    log.info("③ STRYD からフォームメトリクスを取得中...")
    try:
        stryd = get_stryd_data()
        log.info(f"   GCT:{stryd.get('avg_gct_ms')}ms  LSS:{stryd.get('avg_lss')}kN/m  "
                 f"振幅:{stryd.get('avg_oscillation')}cm  FTP:{stryd.get('ftp_w')}W")
    except Exception as e:
        log.warning(f"   STRYD取得失敗（スキップ）: {e}")
        stryd = {}

    log.info("④ Claude でメニューを生成中...")
    menu = generate_menu(wellness, runs, races, stryd)
    log.info(f"   生成完了: {len(menu)}日分")

    log.info("⑤ Notion にページを作成中...")
    page_urls = create_notion_pages(menu)

    log.info("⑥ Slack に通知中...")
    latest = wellness[-1] if wellness else {}
    notify_slack(page_urls, latest.get("ctl","?"), latest.get("atl","?"), latest.get("tsb","?"))

    record_run()
    log.info(f"=== 完了: {len(page_urls)}日分のメニューを作成しました ===")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ZBR Lab 練習プラン 月次メニュー自動生成スクリプト
マラソンチームDB と チームMD DBの両方に月次メニューを一括作成する

使い方:
  # 翌月分を自動生成（毎月20日実行想定）
  python generate_zbr_lab_menu.py

  # 月・年を明示指定
  python generate_zbr_lab_menu.py --month 8 --year 2026

練習会開催日: 毎月第2・第4日曜（自動計算）
環境変数: NOTION_API_KEY（必須）
"""

import os
import json
import sys
import logging
import argparse
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

NOTION_API_KEY = os.environ["NOTION_API_KEY"]

# データベースID（ページID）。data_source_id とは別物なので注意。
MARATHON_DB_ID = "85c3760829894c409c7ea68311ae0892"
TEAM_MD_DB_ID  = "1ac07bda38ec806989bdd5ac1f63b896"

# チームMDの「場所」プロパティ名は先頭がノーブレークスペース(U+00A0)
MD_PLACE_PROP = " 場所"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "menu.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── メニューサイクル定義 ────────────────────────────────────────────────
# 起点: 2026-04-07（火曜・サイクル第1週）
CYCLE_ORIGIN_TUESDAY = date(2026, 4, 7)

TUESDAY_CYCLE = [
    "10~15×(LT90秒+R30秒)",
    "7~10×(LT150秒+R30秒)",
    "5~8×(LT210秒+R30秒)",
    "10~15×(LT90秒+R30秒)",  # 4週目 = 1週目と同じ
]

THURSDAY_CYCLE = [
    "4~6×(LT270秒+R30秒)",
    "14~20×(LT60秒+R30秒)",
    "7~10×(LT330秒+R30秒)",
    "10~15×(LT60秒+R60秒)",
]

# 土曜は2サイクル（0=TT, 1=BU）
CYCLE_ORIGIN_SATURDAY = date(2026, 4, 4)


def _cycle4_index(target: date, origin_tuesday: date) -> int:
    """target の weekday における起点からの通算週番号を 4 で割った余り（0〜3）"""
    weekday_delta = (target.weekday() - origin_tuesday.weekday()) % 7
    first_occurrence = origin_tuesday + timedelta(days=weekday_delta)
    if target < first_occurrence:
        return 0
    weeks = (target - first_occurrence).days // 7
    return weeks % 4


def _cycle2_index(target: date, origin_saturday: date) -> int:
    """土曜サイクル用: 0=TT, 1=BU"""
    if target < origin_saturday:
        return 0
    weeks = (target - origin_saturday).days // 7
    return weeks % 2


# ── 外部リンク ──────────────────────────────────────────────────────────
URL_WALK_DRILL = "https://youtu.be/_Xoh3Oo-Br0"
URL_STEP_DRILL = "https://youtu.be/D6tD7oWbXBE"
URL_TICKET_SPOT = "https://checkout.square.site/merchant/MLXRHT2M852BM/checkout/PHOYKAWI2DLQE4HCEYTAP6B7"
URL_TICKET_PASS = "https://app.matakul.jp/sign-up/?salonId=kGeKKlY0AdjfwevuD0gC"
URL_MAP = "https://goo.gl/maps/RRcrD9uGA5Dumipr9"


# ── Notion ブロックビルダー ──────────────────────────────────────────────
def _toc() -> dict:
    return {
        "object": "block",
        "type": "table_of_contents",
        "table_of_contents": {"color": "gray"},
    }


def _h1_blue(text: str) -> dict:
    return {
        "object": "block",
        "type": "heading_1",
        "heading_1": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "color": "blue_background",
        },
    }


def _numbered(text: str, children: list = None) -> dict:
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


def _bullet(text: str, url: str = None) -> dict:
    rt = {"type": "text", "text": {"content": text}}
    if url:
        rt["text"]["link"] = {"url": url}
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [rt]},
    }


def _warmup_children(minimal: bool = False) -> list:
    children = [
        _bullet("ウォークドリル", URL_WALK_DRILL),
        _bullet("ステップドリル", URL_STEP_DRILL),
    ]
    if not minimal:
        children += [
            _bullet("15分ジョグ（ゆっくりペースから少しずつペースアップ）"),
            _bullet("100m流し（ダッシュ）を4本（1本ずつ段階的にペースを上げる、最後はほぼ全力）"),
            _bullet("5分呼吸を整える"),
        ]
    return children


def _standard_blocks(
    menu_name: str,
    intensity_detail: str,
    purpose_lines: list,
    notes_lines: list,
    is_practice_day: bool = False,
    minimal_warmup: bool = False,
    include_review: bool = True,
) -> list:
    """
    共通ブロック構成:
    目次 → 1.メニュー → 2.目的 → 3.注意事項
    practice_day=True の場合は 4.練習会について を追加
    （include_review=True かつ practice_day=True なら 5.振り返り も追加）
    minimal_warmup=True の場合、ウォームアップはドリル2種のみ
    """
    wu = _numbered("ウォーミングアップ", _warmup_children(minimal=minimal_warmup))
    intensity_block = _numbered(f"トレーニング強度\n- {intensity_detail}")
    main = _numbered(f"本練習：{menu_name}", [intensity_block])
    cd = _numbered("クーリングダウン", [_bullet("10分ゆっくりジョグ")])

    blocks = (
        [_toc(), _h1_blue("1. トレーニングメニュー"), wu, main, cd]
        + [_h1_blue("2. トレーニングの目的")]
        + [_numbered(p) for p in purpose_lines]
        + [_h1_blue("3. トレーニングの注意事項")]
        + [_numbered(n) for n in notes_lines]
    )

    if is_practice_day:
        blocks += [
            _h1_blue("4. 練習会について"),
            _bullet("チケット購入（都度払い）", URL_TICKET_SPOT),
            _bullet("チケット購入（回数券）", URL_TICKET_PASS),
            _bullet("集合場所: 新横浜公園・第3レストハウス"),
            _bullet("Google Map", URL_MAP),
            _bullet("8:50集合 / 9:00 WS / 9:50 ラン / 11:30終了"),
            _bullet("緊急連絡: 090-6527-9438（高岡携帯）"),
        ]
        if include_review:
            blocks += [
                _h1_blue("5. 練習会振り返り"),
                _bullet("今日の調子（5段階）:"),
                _bullet("メインメニューのペース・感触:"),
                _bullet("気づいたこと・課題:"),
                _bullet("次回への申し送り:"),
            ]

    return blocks


# ── メニュー種別ごとのブロック生成 ─────────────────────────────────────
def lt_interval_blocks(menu_name: str) -> list:
    intensity = "ぎりぎり会話できる程度の強度（LTペース）"
    purpose = ["乳酸性作業閾値（LT）の向上。筋肉が乳酸を処理する能力を高め、速いペースを長く維持できるようにする"]
    notes = [
        "レスト（R）は次のセットが始まる直前まで。30秒は短いので素早く準備する",
        "全セットを通じてペースが落ちるようなら本数を減らしてよい（最低本数が目安）",
        "LTペースは「きつい」と感じる入口。心拍数が安定していれば強度は正確",
    ]
    return _standard_blocks(menu_name, intensity, purpose, notes)


def tt_5km_blocks(is_practice_day: bool = False) -> list:
    menu_name = "5kmタイムトライアル"
    intensity = "最初の1kmは抑えて入り（全力の85%）、2km以降は徐々にペースアップ。ラスト1kmは出し切る"
    purpose = ["現在の5km実力を測定し、LTペース・Eペースの目安を更新する"]
    notes = [
        "タイムより「今の自分の実力を正確に知る」ことが目的",
        "ウォーミングアップをしっかり行い、体が温まった状態でスタートする",
        "ラップを500mごとに確認し、前半突っ込みすぎに注意",
    ]
    return _standard_blocks(menu_name, intensity, purpose, notes, is_practice_day)


def bu_run_blocks(duration: str, is_practice_day: bool = False) -> list:
    menu_name = f"{duration}BU（ビルドアップ走）"
    intensity = "前半はEペース（楽に話せる強度）、後半に向けて徐々にペースアップ。ラスト10〜15分はLTペース近くまで上げる"
    purpose = ["有酸素能力とLT付近の走力を同時に鍛える。終盤の粘りを養う"]
    notes = [
        "前半を突っ込みすぎると後半にペースが落ちる。前半は「物足りない」くらいで良い",
        "ペースアップは急激に行わず、2〜3分ごとに少しずつ上げる",
        "終盤がきつくても「フォームを崩さない」ことを意識する",
    ]
    return _standard_blocks(
        menu_name, intensity, purpose, notes, is_practice_day, minimal_warmup=True
    )


def hs_blocks(is_practice_day: bool = False) -> list:
    menu_name = "3×4×HS150m"
    intensity = "1セット目は80%、2セット目は90%、3セット目は100%の強度。インターバル（150m間）はゆっくりジョグまたは歩きで呼吸を回復させる。セット間は3分"
    purpose = [
        "ランニングエコノミーの向上（神経筋系の活性化）",
        "速いペースでの正しいフォームを体に覚えさせる",
    ]
    notes = [
        "「速く動かす」より「地面を押す力」を意識する",
        "接地時間を短く。ピッチ（回転数）より一歩一歩のパワーを重視",
        "疲れてきたらフォームが崩れるサイン。崩れたらそのセットは終了してよい",
    ]
    return _standard_blocks(
        menu_name, intensity, purpose, notes, is_practice_day, include_review=False
    )


# ── プロパティ組み立て ──────────────────────────────────────────────────
def _multi_select(values: list) -> dict:
    """multi_selectプロパティ"""
    return {"multi_select": [{"name": v} for v in values]}


def _select(value: str) -> dict:
    """selectプロパティ"""
    return {"select": {"name": value}}


def _venue_and_coach(is_practice_day: bool):
    if is_practice_day:
        return ["新横浜公園"], ["高岡 尚司"]
    return ["メニューのみ"], ["メニューのみ"]


# ── Notionページ作成 ────────────────────────────────────────────────────
def create_notion_page(
    db_id: str,
    title_prop: str,
    title: str,
    date_str: str,
    training_effect: list,
    venue: list,
    coach: list,
    month_label: str,
    blocks: list,
    place_prop: str = "場所",
) -> str:
    properties = {
        title_prop: {"title": [{"type": "text", "text": {"content": title}}]},
        "開催日": {"date": {"start": date_str}},
        "トレーニング効果": _multi_select(training_effect),
        place_prop: _multi_select(venue),
        "担当コーチ": _multi_select(coach),
        "開催月": _select(month_label),
    }

    payload = {
        "parent": {"database_id": db_id},
        "properties": properties,
        "children": blocks[:100],
    }

    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json=payload,
        timeout=15,
    )
    if not r.ok:
        log.error(f"  API error {r.status_code}: {r.text[:300]}")
        r.raise_for_status()

    page = r.json()
    page_id = page["id"]
    url = page.get("url", "")

    # 100ブロック超の場合は追記
    extra = blocks[100:]
    if extra:
        r2 = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": extra},
            timeout=15,
        )
        r2.raise_for_status()

    return url


# ── 月次メニュー生成メイン ──────────────────────────────────────────────
def generate_monthly_menu(month: int, year: int, practice_days: dict):
    """
    month: 対象月 (1〜12)
    year:  対象年
    practice_days: {"sunday": [日付リスト]}  例: {"sunday": [6, 20]}
    """
    month_label = f"{month}月"
    sunday_practice = set(practice_days.get("sunday", []))

    # 対象月の全日付を列挙
    d = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    created = []

    while d < end:
        wd = d.weekday()  # 0=月 ... 6=日
        date_str = d.strftime("%Y-%m-%d")
        is_practice = (wd == 6 and d.day in sunday_practice)  # 日曜練習会

        menu_name = None
        marathon_effect = []   # マラソンチームDBのトレーニング効果（選択肢名が異なる）
        md_effect = []         # チームMD DBのトレーニング効果
        marathon_blocks = None
        md_blocks = None

        if wd == 1:  # 火曜
            idx = _cycle4_index(d, CYCLE_ORIGIN_TUESDAY)
            menu_name = TUESDAY_CYCLE[idx]
            marathon_effect = ["乳酸性作業閾値"]
            md_effect = ["乳酸性作業閾値の改善"]
            marathon_blocks = lt_interval_blocks(menu_name)
            md_blocks = lt_interval_blocks(menu_name)

        elif wd == 3:  # 木曜
            idx = _cycle4_index(d, CYCLE_ORIGIN_TUESDAY)  # 同じ起点で週番号を算出
            menu_name = THURSDAY_CYCLE[idx]
            marathon_effect = ["乳酸性作業閾値"]
            md_effect = ["乳酸性作業閾値の改善"]
            marathon_blocks = lt_interval_blocks(menu_name)
            md_blocks = lt_interval_blocks(menu_name)

        elif wd == 5:  # 土曜
            idx = _cycle2_index(d, CYCLE_ORIGIN_SATURDAY)
            if idx == 0:
                menu_name = "5kmタイムトライアル"
                marathon_effect = ["VO2max"]
                md_effect = ["VO2maxの改善"]
                marathon_blocks = tt_5km_blocks()
                md_blocks = tt_5km_blocks()
            else:
                marathon_effect = ["疲労耐性の改善"]
                md_effect = ["疲労耐性の改善"]
                # マラソンとMDで距離が異なる
                marathon_blocks = bu_run_blocks("90~120分")
                md_blocks = bu_run_blocks("60~90分")
                menu_name = "BU走"

        elif wd == 6:  # 日曜
            menu_name = "3×4×HS150m"
            marathon_effect = ["ランニングエコノミー"]
            md_effect = ["ランニングエコノミーの改善"]
            marathon_blocks = hs_blocks(is_practice)
            md_blocks = hs_blocks(is_practice)

        if marathon_blocks is None:
            d += timedelta(days=1)
            continue

        venue, coach = _venue_and_coach(is_practice)

        # メニュー名部分（BUのみDB別に距離が異なる）
        if wd == 5 and idx != 0:
            marathon_menu = "90~120分BU"
            md_menu       = "60~90分BU"
        else:
            marathon_menu = menu_name
            md_menu       = menu_name

        # タイトル: {M}/{D}【{曜日}練】{メニュー名}
        weekday_label = {1: "火", 3: "木", 5: "土", 6: "日"}[wd]
        date_prefix = f"{d.month}/{d.day}【{weekday_label}曜練】"
        marathon_title = f"{date_prefix}{marathon_menu}"
        md_title       = f"{date_prefix}{md_menu}"

        log.info(f"  作成中: {date_str} ({['月','火','水','木','金','土','日'][wd]}) {marathon_title}")

        # マラソンチームDB
        url_m = create_notion_page(
            db_id=MARATHON_DB_ID,
            title_prop="Name",
            title=marathon_title,
            date_str=date_str,
            training_effect=marathon_effect,
            venue=venue,
            coach=coach,
            month_label=month_label,
            blocks=marathon_blocks,
            place_prop="場所",
        )

        # チームMD DB
        url_md = create_notion_page(
            db_id=TEAM_MD_DB_ID,
            title_prop="名前",
            title=md_title,
            date_str=date_str,
            training_effect=md_effect,
            venue=venue,
            coach=coach,
            month_label=month_label,
            blocks=md_blocks,
            place_prop=MD_PLACE_PROP,  # 先頭はノーブレークスペース(U+00A0)
        )

        created.append({"date": date_str, "menu": marathon_title, "marathon": url_m, "md": url_md})
        d += timedelta(days=1)

    return created


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> int:
    """month の第 n 週目の weekday（0=月,6=日）の日付（日）を返す"""
    d = date(year, month, 1)
    # その月の最初の weekday を探す
    first = d + timedelta(days=(weekday - d.weekday()) % 7)
    result = first + timedelta(weeks=n - 1)
    if result.month != month:
        raise ValueError(f"{year}年{month}月に第{n}週の weekday={weekday} は存在しない")
    return result.day


def next_month(today: date) -> tuple[int, int]:
    """today の翌月の (year, month) を返す"""
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


# ── 実行 ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZBR Lab 月次練習メニュー生成")
    parser.add_argument("--month", type=int, help="対象月 (1-12)。省略時は翌月")
    parser.add_argument("--year",  type=int, help="対象年。省略時は翌月の年")
    parser.add_argument(
        "--practice-days", type=str,
        help="練習会開催日（日曜）の日付をカンマ区切りで指定。省略時は第2・第4日曜を自動計算。例: 5,26",
    )
    args = parser.parse_args()

    if args.month and args.year:
        year, month = args.year, args.month
    else:
        year, month = next_month(date.today())

    if args.practice_days:
        sunday_2nd, sunday_4th = [int(x.strip()) for x in args.practice_days.split(",")]
        practice_days = {"sunday": [sunday_2nd, sunday_4th]}
        log.info(f"=== ZBR Lab 練習プラン {year}年{month}月 生成開始 ===")
        log.info(f"    練習会開催日（日曜・指定）: {month}/{sunday_2nd}, {month}/{sunday_4th}")
        results = generate_monthly_menu(month, year, practice_days)
        log.info(f"=== 完了: {len(results)}件作成 ===")
        for r in results:
            log.info(f"  {r['date']} {r['menu']}")
            log.info(f"    マラソン: {r['marathon']}")
            log.info(f"    チームMD: {r['md']}")
        sys.exit(0)

    # 練習会開催日: 毎月第2・第4日曜
    sunday_2nd = nth_weekday_of_month(year, month, weekday=6, n=2)
    sunday_4th = nth_weekday_of_month(year, month, weekday=6, n=4)
    practice_days = {"sunday": [sunday_2nd, sunday_4th]}

    log.info(f"=== ZBR Lab 練習プラン {year}年{month}月 生成開始 ===")
    log.info(f"    練習会開催日（日曜）: {month}/{sunday_2nd}, {month}/{sunday_4th}")
    results = generate_monthly_menu(month, year, practice_days)
    log.info(f"=== 完了: {len(results)}件作成 ===")
    for r in results:
        log.info(f"  {r['date']} {r['menu']}")
        log.info(f"    マラソン: {r['marathon']}")
        log.info(f"    チームMD: {r['md']}")

"""
Google Patents Public Datasets (BigQuery) 特許検索スクリプト
=============================================================

【API 取得方法】
─────────────────────────────────────────────────────────────
1. Google Cloud プロジェクトを用意する
   https://console.cloud.google.com/
   - 未作成なら「プロジェクトを作成」
   - 課金アカウントを紐づける（BigQuery の無料枠: 1TB/月 クエリ無料）

2. BigQuery API を有効化する
   コンソール左メニュー > APIとサービス > ライブラリ
   > "BigQuery API" を検索して「有効にする」

3. 認証情報を取得する（2 択）

   [A] サービスアカウントキー（本番・CI 向け）
       IAM と管理 > サービスアカウント > 「サービスアカウントを作成」
       ロール付与:
         - BigQuery ジョブユーザー  （クエリ実行権限）
         - BigQuery データ閲覧者    （データ読み取り権限）
       作成後 > 「キー」タブ > 「鍵を追加」> JSON でダウンロード
       → .env の GOOGLE_APPLICATION_CREDENTIALS にパスを設定

   [B] ローカル開発用（gcloud CLI）
       $ gcloud auth application-default login
       → ブラウザで Google アカウント認証するだけ。キーファイル不要。
       インストール: https://cloud.google.com/sdk/docs/install

4. .env ファイルを作成する
   $ cp .env.example .env
   .env を編集して GCP_PROJECT_ID と GOOGLE_APPLICATION_CREDENTIALS を設定

【データセット情報】
─────────────────────────────────────────────────────────────
テーブル : `patents-public-data.patents.publications`
更新頻度 : 週次
収録件数 : 約 1 億 2 千万件（全世界）
費用     : クエリデータ処理量で課金。無料枠 1TB/月。
           LIKE 検索はフルスキャンになるため、件数を LIMIT で絞ること。

【出力可能フィールド一覧】は --schema オプションで確認できます。
"""

import os
import sys
import json
import textwrap
import argparse
from dotenv import load_dotenv
from google.cloud import bigquery
from tabulate import tabulate

load_dotenv()

# ─── 定数 ────────────────────────────────────────────────────────────────────
TABLE = "patents-public-data.patents.publications"

# 出力可能フィールドの説明（BigQuery スキーマより抜粋・整理）
FIELD_CATALOG = [
    # 識別子
    ("publication_number",    "STRING",           "公開番号（例: JP-2020123456-A）"),
    ("country_code",          "STRING",           "国コード（JP / US / EP …）"),
    ("kind_code",             "STRING",           "文献種別コード（A / B / U …）"),
    ("application_number",    "STRING",           "出願番号"),
    ("family_id",             "STRING",           "パテントファミリー ID"),
    # 日付（YYYYMMDD 整数）
    ("filing_date",           "INTEGER",          "出願日 YYYYMMDD"),
    ("grant_date",            "INTEGER",          "登録日 YYYYMMDD（未登録は 0）"),
    ("publication_date",      "INTEGER",          "公開日 YYYYMMDD"),
    # テキスト（REPEATED RECORD: language, text）
    ("title_localized",       "REPEATED RECORD",  "発明の名称。language='ja' で日本語"),
    ("abstract_localized",    "REPEATED RECORD",  "要約。language='ja' で日本語"),
    ("claims_localized",      "REPEATED RECORD",  "請求の範囲。language='ja' で日本語"),
    ("description_localized", "REPEATED RECORD",  "詳細説明。language='ja' で日本語（非常に大きい）"),
    # 人・組織
    ("inventor",              "REPEATED RECORD",  "発明者。{name, country_code}"),
    ("assignee_harmonized",   "REPEATED RECORD",  "出願人（名寄せ済）。{name, country_code}"),
    ("examiner",              "REPEATED RECORD",  "審査官。{name, level, department}"),
    # 分類
    ("ipc",                   "REPEATED RECORD",  "IPC 分類。{code, inventive, first, tree}"),
    ("cpc",                   "REPEATED RECORD",  "CPC 分類。{code, inventive, first, tree}"),
    # 引用
    ("citation",              "REPEATED RECORD",  "この文献が引用する文献リスト"),
    ("cited_by",              "REPEATED RECORD",  "この文献を引用する文献リスト"),
    # その他
    ("entity_status",         "STRING",           "事業者区分（LARGE / SMALL / UNDISCOUNTED）"),
    ("priority_claim",        "REPEATED RECORD",  "優先権主張。{application_number, filing_date, kind_code, country_code}"),
    ("pct_number",            "STRING",           "PCT 出願番号（国際出願）"),
]


# ─── クライアント初期化 ───────────────────────────────────────────────────────
def get_client() -> tuple["bigquery.Client", "bigquery.QueryJobConfig"]:
    project = os.environ.get("GCP_PROJECT_ID")
    if not project:
        sys.exit(
            "[ERROR] GCP_PROJECT_ID が未設定です。\n"
            "  .env ファイルを作成し GCP_PROJECT_ID=<あなたのプロジェクト> を設定してください。"
        )

    # 1 クエリあたりのスキャン上限（超えたらクエリを拒否して課金を防ぐ）
    max_gb = float(os.environ.get("MAX_GB_PER_QUERY", "1.0"))
    max_bytes = int(max_gb * 1e9)
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes)

    return bigquery.Client(project=project), job_config


# ─── スキーマ表示 ────────────────────────────────────────────────────────────
def show_schema():
    rows = [(f, t, d) for f, t, d in FIELD_CATALOG]
    print("\n=== 出力可能フィールド一覧 ===")
    print(f"テーブル: {TABLE}\n")
    print(
        tabulate(
            rows,
            headers=["フィールド名", "型", "説明"],
            tablefmt="simple",
            colalign=("left", "left", "left"),
        )
    )
    print(
        "\n[REPEATED RECORD の取り出し方]\n"
        "  単一値: (SELECT t.text FROM UNNEST(title_localized) t WHERE t.language='ja' LIMIT 1)\n"
        "  結合値: (SELECT STRING_AGG(a.name,'; ') FROM UNNEST(assignee_harmonized) a)\n"
        "  フィルタ: EXISTS (SELECT 1 FROM UNNEST(claims_localized) c WHERE c.language='ja' AND c.text LIKE '%キーワード%')\n"
    )


# ─── 特許検索 ────────────────────────────────────────────────────────────────
def build_query(keywords: list[str], country: str, limit: int) -> str:
    """
    請求の範囲（claims_localized）に全キーワードを含む特許を検索する SQL を生成する。
    """
    # 各キーワードを AND 条件として LIKE 句に展開
    kw_conditions = "\n          AND ".join(
        f"c.text LIKE '%{kw}%'" for kw in keywords
    )

    query = f"""
SELECT
  publication_number,
  (
    SELECT t.text
    FROM UNNEST(title_localized) t
    WHERE t.language = 'ja'
    LIMIT 1
  ) AS title_ja,
  filing_date,
  publication_date,
  (
    SELECT STRING_AGG(a.name, '; ')
    FROM UNNEST(assignee_harmonized) a
  ) AS assignees,
  (
    SELECT STRING_AGG(inv.name, '; ')
    FROM UNNEST(inventor_harmonized) inv
  ) AS inventors,
  (
    SELECT STRING_AGG(ip.code, '  ')
    FROM UNNEST(ipc) ip
  ) AS ipc_codes,
  (
    SELECT c.text
    FROM UNNEST(claims_localized) c
    WHERE c.language = 'ja'
    LIMIT 1
  ) AS claims_ja
FROM `{TABLE}`
WHERE country_code = '{country}'
  AND EXISTS (
    SELECT 1
    FROM UNNEST(claims_localized) c
    WHERE c.language = 'ja'
          AND {kw_conditions}
  )
ORDER BY publication_date DESC
LIMIT {limit}
"""
    return query.strip()


def search_patents(
    keywords: list[str],
    country: str = "JP",
    limit: int = 10,
    dry_run: bool = False,
    show_claims: bool = False,
):
    client, job_config = get_client()
    query = build_query(keywords, country, limit)

    print("\n=== 実行クエリ ===")
    print(query)

    if dry_run:
        # ドライラン: スキャン量だけ確認してクエリは実行しない
        dry_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(query, job_config=dry_config)
        gb = job.total_bytes_processed / 1e9
        max_gb = float(os.environ.get("MAX_GB_PER_QUERY", "1.0"))
        over = " ⚠️  上限超過！クエリは拒否されます" if gb > max_gb else " ✓ 上限内"
        print(f"\n[DRY RUN] 推定スキャン量: {gb:.2f} GB / 上限 {max_gb:.1f} GB{over}")
        return

    print(f"\n検索中... (国={country}, キーワード={keywords}, 上限={limit}件)\n")
    rows = list(client.query(query, job_config=job_config).result())

    if not rows:
        print("ヒットなし")
        return

    print(f"=== 検索結果: {len(rows)} 件 ===\n")

    for i, row in enumerate(rows, 1):
        pub_date = str(row.publication_date) if row.publication_date else "-"
        fil_date = str(row.filing_date) if row.filing_date else "-"
        pub_date_fmt = f"{pub_date[:4]}-{pub_date[4:6]}-{pub_date[6:]}" if len(pub_date) == 8 else pub_date
        fil_date_fmt = f"{fil_date[:4]}-{fil_date[4:6]}-{fil_date[6:]}" if len(fil_date) == 8 else fil_date

        print(f"[{i}] {row.publication_number}")
        print(f"    タイトル  : {row.title_ja or '(なし)'}")
        print(f"    出願日    : {fil_date_fmt}  公開日: {pub_date_fmt}")
        print(f"    出願人    : {row.assignees or '(なし)'}")
        print(f"    発明者    : {row.inventors or '(なし)'}")
        print(f"    IPC (主)  : {row.ipc_first or '(なし)'}")

        if show_claims and row.claims_ja:
            wrapped = textwrap.fill(row.claims_ja[:800], width=90, initial_indent="    ", subsequent_indent="    ")
            print(f"    請求の範囲:\n{wrapped}{'...' if len(row.claims_ja) > 800 else ''}")

        print()


# ─── エントリポイント ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Google Patents Public Datasets (BigQuery) 特許検索",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            使用例:
              # スキーマ（出力可能フィールド）を確認
              python search.py --schema

              # 請求の範囲に「電極」AND「酸素欠陥」を含む JP 公報を検索
              python search.py --keywords 電極 酸素欠陥

              # 請求の範囲の本文も表示
              python search.py --keywords 電極 酸素欠陥 --show-claims

              # スキャン量だけ確認（課金なし）
              python search.py --keywords 電極 酸素欠陥 --dry-run

              # 件数・国を変更
              python search.py --keywords 電極 酸素欠陥 --limit 5 --country JP
        """),
    )
    parser.add_argument("--schema", action="store_true", help="出力可能フィールド一覧を表示して終了")
    parser.add_argument("--keywords", nargs="+", default=["電極", "酸素欠陥"], help="請求の範囲で AND 検索するキーワード群")
    parser.add_argument("--country", default="JP", help="国コード（デフォルト: JP）")
    parser.add_argument("--limit", type=int, default=10, help="最大取得件数（デフォルト: 10）")
    parser.add_argument("--dry-run", action="store_true", help="クエリを実行せず推定スキャン量だけ表示")
    parser.add_argument("--show-claims", action="store_true", help="請求の範囲テキストも表示する")

    args = parser.parse_args()

    if args.schema:
        show_schema()
        return

    search_patents(
        keywords=args.keywords,
        country=args.country,
        limit=args.limit,
        dry_run=args.dry_run,
        show_claims=args.show_claims,
    )


if __name__ == "__main__":
    main()

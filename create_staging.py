"""
ステージングテーブル作成スクリプト
====================================

Google Patents Public Datasets から JP 特許（デフォルト2000年以降）を
ユーザーの GCP プロジェクト内のテーブルにコピーし、
REPEATED RECORD をフラット化・必要列のみに絞ることでクエリコストを削減する。

【効果】
  元テーブル: 約156GB/クエリ（全世界フルスキャン）
  ステージング後: 約15〜17GB/クエリ（JP のみ・パーティション・クラスタリング済）

【使い方】
  # まずスキャン量を確認（何も作らない）
  python create_staging.py

  # 実際にテーブルを作成
  python create_staging.py --execute

  # 再作成（既存テーブルを DROP してから作成）
  python create_staging.py --execute --recreate

  # 2010年以降に絞る場合
  python create_staging.py --execute --year-from 2010

【必要な権限（サービスアカウントに追加）】
  - BigQuery ジョブユーザー
  - BigQuery データ閲覧者  （元テーブルへのアクセス）
  - BigQuery データ編集者  （ステージングテーブルの作成・書き込み）
"""

import os
import sys
import argparse
from dotenv import load_dotenv
from google.cloud import bigquery
from google.api_core.exceptions import NotFound, Conflict

load_dotenv()

SOURCE_TABLE = "patents-public-data.patents.publications"


# ─── SQL 生成 ────────────────────────────────────────────────────────────────
def build_create_sql(
    project: str,
    dataset: str,
    table: str,
    year_from: int,
    country: str,
) -> str:
    full_table_id = f"{project}.{dataset}.{table}"
    return f"""
CREATE TABLE `{full_table_id}`
PARTITION BY RANGE_BUCKET(
  publication_date,
  GENERATE_ARRAY(20000101, 20301231, 100)
)
CLUSTER BY kind_code, application_number
OPTIONS (
  require_partition_filter = FALSE,
  description = "Flattened {country} patents since {year_from}. Source: {SOURCE_TABLE}"
)
AS
SELECT
  publication_number,
  country_code,
  kind_code,
  application_number,
  family_id,
  filing_date,
  grant_date,
  publication_date,
  (
    SELECT t.text
    FROM UNNEST(title_localized) t
    WHERE t.language = 'ja'
    LIMIT 1
  ) AS title_ja,
  (
    SELECT t.text
    FROM UNNEST(abstract_localized) t
    WHERE t.language = 'ja'
    LIMIT 1
  ) AS abstract_ja,
  (
    SELECT STRING_AGG(c.text, '\\n')
    FROM UNNEST(claims_localized) c
    WHERE c.language = 'ja'
  ) AS claims_ja,
  (
    SELECT STRING_AGG(a.name, '; ')
    FROM UNNEST(assignee_harmonized) a
  ) AS assignees,
  (
    SELECT STRING_AGG(inv.name, '; ')
    FROM UNNEST(inventor_harmonized) inv
  ) AS inventors,
  (
    SELECT STRING_AGG(ip.code, ' ')
    FROM UNNEST(ipc) ip
  ) AS ipc_codes
FROM `{SOURCE_TABLE}`
WHERE country_code = '{country}'
  AND publication_date >= {year_from}0101
""".strip()


# ─── BigQuery 操作 ────────────────────────────────────────────────────────────
def get_client(project: str) -> bigquery.Client:
    return bigquery.Client(project=project)


def run_dry_run(client: bigquery.Client, sql: str) -> dict:
    """ドライランでスキャン量・コストを推定する（実際には何も作らない）"""
    # CTAS は dry_run 非対応のため SELECT 部分だけ dry_run で計測
    select_sql = sql[sql.index("AS\nSELECT") + 3:]  # "SELECT ..." 以降を抜き出す
    config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    job = client.query(select_sql, job_config=config)
    gb = job.total_bytes_processed / 1e9
    cost_usd = max(0.0, (gb - 1000) / 1000 * 5)
    return {"gb": gb, "cost_usd": cost_usd}


def create_dataset_if_not_exists(
    client: bigquery.Client, project: str, dataset: str
) -> None:
    dataset_ref = f"{project}.{dataset}"
    try:
        client.get_dataset(dataset_ref)
        print(f"  dataset `{dataset_ref}` は既に存在します")
    except NotFound:
        ds = bigquery.Dataset(dataset_ref)
        ds.location = "US"
        client.create_dataset(ds)
        print(f"  dataset `{dataset_ref}` を作成しました")


def drop_table_if_exists(client: bigquery.Client, full_table_id: str) -> None:
    try:
        client.delete_table(full_table_id)
        print(f"  既存テーブル `{full_table_id}` を削除しました")
    except NotFound:
        pass


def create_table(client: bigquery.Client, sql: str, full_table_id: str) -> None:
    print(f"\nテーブルを作成中: `{full_table_id}`")
    print("（数分〜数十分かかる場合があります）\n")
    job = client.query(sql)
    job.result()  # 完了まで待機
    print(f"\n作成完了: `{full_table_id}`")

    # テーブル情報を表示
    table = client.get_table(full_table_id)
    rows = table.num_rows
    size_gb = (table.num_bytes or 0) / 1e9
    print(f"  行数   : {rows:,} 件")
    print(f"  サイズ : {size_gb:.1f} GB")


def create_search_index(client: bigquery.Client, full_table_id: str) -> None:
    print(f"\nSearch Index を作成中（claims_ja 列）...")
    sql = f"CREATE SEARCH INDEX claims_search_idx ON `{full_table_id}`(claims_ja)"
    client.query(sql).result()
    print("Search Index 作成完了")


# ─── メイン ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Google Patents Public Datasets → ステージングテーブル作成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT_ID"),
        help="GCP プロジェクト ID（デフォルト: 環境変数 GCP_PROJECT_ID）",
    )
    parser.add_argument(
        "--dataset", default="patents_staging",
        help="作成先 BigQuery データセット名（デフォルト: patents_staging）",
    )
    parser.add_argument(
        "--table", default="jp_patents",
        help="作成するテーブル名（デフォルト: jp_patents）",
    )
    parser.add_argument(
        "--year-from", type=int, default=2000,
        help="抽出開始年（デフォルト: 2000）",
    )
    parser.add_argument(
        "--country", default="JP",
        help="対象国コード（デフォルト: JP）",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="このフラグを付けると実際にテーブルを作成する（ないと dry-run のみ）",
    )
    parser.add_argument(
        "--recreate", action="store_true",
        help="既存テーブルを DROP して再作成する",
    )
    args = parser.parse_args()

    if not args.project:
        sys.exit(
            "[ERROR] --project が未指定です。\n"
            "  例: python create_staging.py --project your-gcp-project-id\n"
            "  または .env に GCP_PROJECT_ID=your-project を設定してください。"
        )

    full_table_id = f"{args.project}.{args.dataset}.{args.table}"
    sql = build_create_sql(
        project=args.project,
        dataset=args.dataset,
        table=args.table,
        year_from=args.year_from,
        country=args.country,
    )

    print("=== ステージングテーブル作成 ===")
    print(f"  作成先     : {full_table_id}")
    print(f"  ソース     : {SOURCE_TABLE}")
    print(f"  フィルタ   : country_code='{args.country}', publication_date >= {args.year_from}0101")
    print(f"  パーティション: publication_date（月単位）")
    print(f"  クラスタリング: kind_code, application_number")
    print()
    print("=== 生成 SQL ===")
    print(sql)
    print()

    client = get_client(args.project)

    # ドライラン（常に実行）
    print("=== スキャン量の見積もり（dry-run）===")
    try:
        result = run_dry_run(client, sql)
        gb = result["gb"]
        cost = result["cost_usd"]
        within_free = gb <= 1000
        print(f"  推定スキャン量: {gb:.1f} GB")
        print(f"  推定コスト    : {'無料枠内 ($0)' if within_free else f'${cost:.2f}'}")
        print(f"  ※ BigQuery 無料枠: 1,000 GB/月")
    except Exception as e:
        print(f"  [WARN] dry-run 失敗（権限不足の可能性）: {e}")

    if not args.execute:
        print()
        print("── dry-run のみ完了。実際に作成するには --execute を付けて実行してください ──")
        print(f"  python create_staging.py --execute")
        return

    # 実際の作成
    print()
    print("=== テーブル作成を開始します ===")
    create_dataset_if_not_exists(client, args.project, args.dataset)

    if args.recreate:
        drop_table_if_exists(client, full_table_id)

    try:
        create_table(client, sql, full_table_id)
        create_search_index(client, full_table_id)
    except Conflict:
        print(
            f"\n[ERROR] テーブル `{full_table_id}` は既に存在します。\n"
            "  既存テーブルを削除して再作成するには --recreate を追加してください。\n"
            "  例: python create_staging.py --execute --recreate"
        )
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] テーブル作成に失敗しました: {e}")
        sys.exit(1)

    print()
    print("=== 次のステップ ===")
    print(f"  .env または Streamlit Secrets に以下を追加してステージングを有効化してください:")
    print(f"    USE_STAGING=true")
    print(f"    STAGING_TABLE={full_table_id}")
    print(f"    MAX_GB_PER_QUERY=20.0  # ステージング時は小さくて OK")


if __name__ == "__main__":
    main()

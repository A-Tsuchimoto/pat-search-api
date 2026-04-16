"""
特許検索 Web アプリ — Streamlit フロントエンド
================================================

ローカル実行:
  streamlit run app.py

Streamlit Community Cloud へのデプロイ:
  1. このリポジトリを GitHub に push
  2. https://share.streamlit.io でリポジトリを接続
  3. Main file: app.py
  4. Settings > Secrets に .streamlit/secrets.toml.example の内容を貼り付け

認証の動作:
  - Streamlit Cloud: st.secrets["gcp_service_account"] を使用（サービスアカウントキー）
  - ローカル     : gcloud auth application-default login の認証情報を使用
"""

import os
import re
import streamlit as st
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import BadRequest, Forbidden, NotFound, GoogleAPIError

from search import TABLE, FIELD_CATALOG, build_query

# ─── ページ設定 ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="特許検索",
    page_icon="🔍",
    layout="wide",
)

# ─── 認証・クライアント ───────────────────────────────────────────────────────
@st.cache_resource
def get_client() -> bigquery.Client:
    """
    Streamlit Cloud: st.secrets["gcp_service_account"] からサービスアカウント認証
    ローカル       : Application Default Credentials (gcloud auth application-default login)
    """
    if "gcp_service_account" in st.secrets:
        creds = service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"],
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        project = st.secrets["gcp_service_account"]["project_id"]
    else:
        creds = None
        project = (
            st.secrets.get("GCP_PROJECT_ID")
            or os.environ.get("GCP_PROJECT_ID")
        )
        if not project:
            st.error(
                "GCP_PROJECT_ID が未設定です。\n"
                "ローカルは `.env` に、Streamlit Cloud は Secrets に設定してください。"
            )
            st.stop()

    return bigquery.Client(credentials=creds, project=project)


def get_max_gb() -> float:
    # Secrets → 環境変数 → デフォルト 200GB の順で取得
    val = st.secrets.get("MAX_GB_PER_QUERY") or os.environ.get("MAX_GB_PER_QUERY", "200.0")
    return float(val)


def run_query(query: str) -> list:
    client = get_client()
    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=int(get_max_gb() * 1e9)
    )
    return list(client.query(query, job_config=job_config).result())


# ─── エラーハンドリング ───────────────────────────────────────────────────────
def handle_bq_error(e: Exception) -> None:
    """BigQuery エラーを分類して分かりやすいメッセージを表示する"""
    err_str = str(e)

    # ① スキャン量超過（bytesBilledLimitExceeded）
    if "bytesBilledLimitExceeded" in err_str or "bytes billed" in err_str.lower():
        m = re.search(r"(\d+) or higher required", err_str)
        if m:
            required_gb = int(m.group(1)) / 1e9
            st.error(
                f"スキャン量が上限を超えました。\n\n"
                f"このクエリには **約 {required_gb:.0f} GB** のスキャンが必要です。\n"
                f"Streamlit Cloud の **Settings > Secrets** で以下を更新してください：\n\n"
                f"```toml\nMAX_GB_PER_QUERY = \"{int(required_gb) + 10}\"\n```"
            )
        else:
            st.error(
                "スキャン量が上限を超えました。\n"
                "Secrets の `MAX_GB_PER_QUERY` を増やしてください（例: `\"200\"`）。"
            )

    # ② クエリ構文 / スキーマ不一致
    elif isinstance(e, BadRequest) or "invalidQuery" in err_str or "Unrecognized name" in err_str:
        st.error(f"クエリエラー（構文またはスキーマ不一致）:\n```\n{e}\n```")

    # ③ 権限不足
    elif isinstance(e, Forbidden) or "Access Denied" in err_str or "403" in err_str:
        st.error(
            "権限エラー: サービスアカウントに以下のロールがあるか確認してください。\n"
            "- BigQuery ジョブユーザー\n"
            "- BigQuery データ閲覧者"
        )

    # ④ テーブル・データセット不存在
    elif isinstance(e, NotFound) or "Not found" in err_str:
        st.error(f"テーブルが見つかりません: `{TABLE}`")

    # ⑤ その他
    else:
        st.error(f"予期しないエラー:\n```\n{e}\n```")


# ─── サイドバー ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("検索条件")

    keywords_input = st.text_area(
        "キーワード（1行1語・AND検索）",
        value="電極\n酸素欠陥",
        height=120,
        help="請求の範囲（claims）に対して全キーワードを AND 検索します",
    )

    country = st.selectbox(
        "国コード",
        ["JP", "US", "EP", "CN", "KR", "DE", "FR", "GB"],
        index=0,
    )

    limit = st.slider("最大取得件数", min_value=1, max_value=50, value=10)

    show_claims = st.toggle("請求の範囲テキストを表示", value=False)

    run_button = st.button("検索する", type="primary", use_container_width=True)

    st.divider()
    st.caption(f"スキャン上限: {get_max_gb():.0f} GB / クエリ")
    if st.toggle("出力可能フィールド一覧"):
        st.caption(f"テーブル: `{TABLE}`")
        for field, ftype, desc in FIELD_CATALOG:
            st.markdown(f"**`{field}`** `{ftype}`  \n{desc}")

# ─── メインエリア ─────────────────────────────────────────────────────────────
st.title("特許検索")
st.caption("データソース: Google Patents Public Datasets (BigQuery)")

if run_button:
    keywords = [k.strip() for k in keywords_input.splitlines() if k.strip()]

    if not keywords:
        st.warning("キーワードを1つ以上入力してください")
        st.stop()

    query = build_query(keywords, country, limit)

    with st.expander("実行 SQL", expanded=False):
        st.code(query, language="sql")

    with st.spinner(f"検索中… ({', '.join(keywords)})"):
        try:
            rows = run_query(query)
        except (BadRequest, Forbidden, NotFound, GoogleAPIError, Exception) as e:
            handle_bq_error(e)
            st.stop()

    if not rows:
        st.info("ヒットなし。キーワードを変えて試してください。")
        st.stop()

    st.success(f"{len(rows)} 件ヒット")

    def fmt_date(d):
        s = str(d) if d else ""
        return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else s

    table_data = [
        {
            "公開番号": row.publication_number,
            "タイトル": row.title_ja or "",
            "出願日": fmt_date(row.filing_date),
            "公開日": fmt_date(row.publication_date),
            "出願人": row.assignees or "",
            "発明者": row.inventors or "",
            "IPC": row.ipc_codes or "",
        }
        for row in rows
    ]
    st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)

    if show_claims:
        st.subheader("請求の範囲")
        for i, row in enumerate(rows, 1):
            label = f"[{i}] {row.publication_number} — {row.title_ja or '(タイトルなし)'}"
            with st.expander(label):
                st.write(row.claims_ja or "(テキストなし)")

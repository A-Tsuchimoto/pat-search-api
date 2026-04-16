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
import streamlit as st
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from google.api_core.exceptions import BadRequest

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
        # ローカル: ADC に任せる（creds=None で自動検出）
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


def run_query(query: str) -> list:
    client = get_client()
    max_gb = float(
        st.secrets.get("MAX_GB_PER_QUERY")
        or os.environ.get("MAX_GB_PER_QUERY", "1.0")
    )
    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=int(max_gb * 1e9)
    )
    return list(client.query(query, job_config=job_config).result())


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
        except BadRequest as e:
            if "exceeded" in str(e).lower() and "bytes" in str(e).lower():
                st.error(
                    "スキャン量が上限を超えたためクエリを中断しました。\n"
                    "`MAX_GB_PER_QUERY` を増やすか、キーワードを絞ってください。"
                )
            else:
                st.error(f"BigQuery エラー: {e}")
            st.stop()
        except Exception as e:
            st.error(f"エラー: {e}")
            st.stop()

    if not rows:
        st.info("ヒットなし。キーワードを変えて試してください。")
        st.stop()

    st.success(f"{len(rows)} 件ヒット")

    # テーブル表示
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

    # 請求の範囲テキスト（トグル時）
    if show_claims:
        st.subheader("請求の範囲")
        for i, row in enumerate(rows, 1):
            label = f"[{i}] {row.publication_number} — {row.title_ja or '(タイトルなし)'}"
            with st.expander(label):
                st.write(row.claims_ja or "(テキストなし)")

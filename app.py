"""
特許検索 Web アプリ — Streamlit フロントエンド
================================================
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

from search import TABLE, FIELD_CATALOG, build_query, _USE_STAGING

# ─── ページ設定 ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="特許検索", page_icon="🔍", layout="wide")

# ─── Streamlit Secrets → 環境変数 bridge ─────────────────────────────────────
# search.py の _USE_STAGING / TABLE は os.environ を参照するため、
# Streamlit Cloud では secrets の値を env に反映してから import が必要だが、
# モジュールは既にインポート済みのため、ここでは app.py 内の動作に影響する変数のみ橋渡し。
for _key in ("USE_STAGING", "STAGING_TABLE", "MAX_GB_PER_QUERY"):
    if _key in st.secrets and _key not in os.environ:
        os.environ[_key] = str(st.secrets[_key])

# ─── 認証・クライアント ───────────────────────────────────────────────────────
@st.cache_resource
def get_client() -> bigquery.Client:
    if "gcp_service_account" in st.secrets:
        creds = service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"],
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        project = st.secrets["gcp_service_account"]["project_id"]
    else:
        creds = None
        project = st.secrets.get("GCP_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID")
        if not project:
            st.error("GCP_PROJECT_ID が未設定です。Secrets または .env に設定してください。")
            st.stop()
    return bigquery.Client(credentials=creds, project=project)


def get_max_gb() -> float:
    val = st.secrets.get("MAX_GB_PER_QUERY") or os.environ.get("MAX_GB_PER_QUERY", "200.0")
    return float(val)


def estimate_bytes(query: str) -> tuple[float | None, Exception | None]:
    """ドライランで推定スキャン量（GB）を返す。エラー時は (None, exception)。"""
    try:
        client = get_client()
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(query, job_config=config)
        return job.total_bytes_processed / 1e9, None
    except Exception as e:
        return None, e


def run_query(query: str) -> list:
    client = get_client()
    job_config = bigquery.QueryJobConfig(maximum_bytes_billed=int(get_max_gb() * 1e9))
    return list(client.query(query, job_config=job_config).result())


# ─── エラーハンドリング ───────────────────────────────────────────────────────
def handle_bq_error(e: Exception) -> None:
    err_str = str(e)
    if "bytesBilledLimitExceeded" in err_str or "bytes billed" in err_str.lower():
        m = re.search(r"(\d+) or higher required", err_str)
        if m:
            required_gb = int(m.group(1)) / 1e9
            st.error(
                f"スキャン量が上限を超えました。\n\n"
                f"このクエリには **約 {required_gb:.0f} GB** が必要です。\n"
                f"Secrets の `MAX_GB_PER_QUERY` を更新してください：\n\n"
                f"```toml\nMAX_GB_PER_QUERY = \"{int(required_gb) + 10}\"\n```"
            )
        else:
            st.error("スキャン量が上限を超えました。Secrets の `MAX_GB_PER_QUERY` を増やしてください。")
    elif isinstance(e, BadRequest) or "invalidQuery" in err_str or "Unrecognized name" in err_str:
        st.error(f"クエリエラー（構文またはスキーマ不一致）:\n```\n{e}\n```")
    elif isinstance(e, Forbidden) or "Access Denied" in err_str or "403" in err_str:
        st.error(
            "権限エラー: サービスアカウントに以下のロールがあるか確認してください。\n"
            "- BigQuery ジョブユーザー\n- BigQuery データ閲覧者"
        )
    elif isinstance(e, NotFound) or "Not found" in err_str:
        st.error(f"テーブルが見つかりません: `{TABLE}`")
    else:
        st.error(f"予期しないエラー:\n```\n{e}\n```")


# ─── サイドバー ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("検索条件")

    keywords_input = st.text_area(
        "キーワード（1行1語・AND検索）",
        value="電極\n酸素欠陥",
        height=110,
        help="請求の範囲（claims）に対して全キーワードを AND 検索します",
    )

    country = st.selectbox("国コード", ["JP", "US", "EP", "CN", "KR", "DE", "FR", "GB"], index=0)

    st.subheader("絞り込み（結果フィルタ）")

    if not _USE_STAGING:
        st.info(
            "元テーブルでは年・種別フィルタは **結果件数を絞る**だけで"
            "スキャン量は変わりません。\n"
            "スキャン量を削減するには **ステージングテーブル**を使ってください。",
            icon="ℹ️",
        )

    this_year = 2025
    year_range = st.slider(
        "公開年",
        min_value=1976,
        max_value=this_year,
        value=(2010, this_year),
        help="結果を絞る。ステージングテーブル使用時のみスキャン量も削減される。",
    )
    year_from, year_to = year_range

    # 国別の主要文献種別
    KIND_OPTIONS_BY_COUNTRY = {
        "JP": {
            "A — 公開特許公報（未審査）": "A",
            "B — 特許公報（登録）": "B",
            "U — 実用新案登録": "U",
            "Y — 公開実用新案": "Y",
        },
        "US": {
            "A1 — 公開出願": "A1",
            "B1 — 登録特許（初回公開）": "B1",
            "B2 — 登録特許（公開済）": "B2",
        },
        "EP": {
            "A1 — 公開出願": "A1",
            "A2 — 公開出願（サーチレポートなし）": "A2",
            "B1 — 登録特許": "B1",
        },
    }
    KIND_OPTIONS = KIND_OPTIONS_BY_COUNTRY.get(country, {})
    default_kinds = list(KIND_OPTIONS.keys())[:2] if KIND_OPTIONS else []
    selected_kinds = st.multiselect(
        "文献種別 (kind_code)",
        options=list(KIND_OPTIONS.keys()),
        default=default_kinds,
        help="結果を絞る。ステージングテーブル使用時のみスキャン量も削減される。",
    )
    kind_codes = [KIND_OPTIONS[k] for k in selected_kinds] or None

    limit = st.slider("最大取得件数", min_value=1, max_value=50, value=10)
    show_claims = st.toggle("請求の範囲テキストを表示", value=False)

    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        estimate_button = st.button("スキャン量\nを確認", use_container_width=True)
    with col2:
        run_button = st.button("検索する", type="primary", use_container_width=True)

    st.caption(f"上限: {get_max_gb():.0f} GB / クエリ")
    if _USE_STAGING:
        st.caption(f"テーブル: ステージング `{TABLE.split('.')[-1]}`")
    else:
        st.caption("テーブル: 元テーブル（フルスキャン）")

    st.divider()
    if st.toggle("出力可能フィールド一覧"):
        st.caption(f"テーブル: `{TABLE}`")
        for field, ftype, desc in FIELD_CATALOG:
            st.markdown(f"**`{field}`** `{ftype}`  \n{desc}")

# ─── メインエリア ─────────────────────────────────────────────────────────────
st.title("特許検索")
st.caption("データソース: Google Patents Public Datasets (BigQuery)")


def get_keywords() -> list[str]:
    return [k.strip() for k in keywords_input.splitlines() if k.strip()]


def fmt_date(d) -> str:
    s = str(d) if d else ""
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else s


# ── スキャン量確認ボタン ──────────────────────────────────────────────────────
if estimate_button:
    keywords = get_keywords()
    if not keywords:
        st.warning("キーワードを1つ以上入力してください")
    else:
        query = build_query(keywords, country, limit, year_from, year_to, kind_codes)
        with st.expander("実行 SQL", expanded=False):
            st.code(query, language="sql")

        with st.spinner("スキャン量を見積もり中…"):
            gb, err = estimate_bytes(query)

        if err:
            handle_bq_error(err)
        else:
            max_gb = get_max_gb()
            cost_usd = max(0.0, (gb - 1000) / 1000 * 5)  # 無料枠 1TB 超過分のみ課金

            c1, c2, c3 = st.columns(3)
            c1.metric("推定スキャン量", f"{gb:.1f} GB")
            c2.metric("上限設定", f"{max_gb:.0f} GB", delta=f"余裕 {max_gb - gb:.0f} GB" if gb < max_gb else "超過")
            c3.metric("推定コスト / 回", f"${cost_usd:.2f}" if cost_usd > 0 else "無料枠内")

            if not _USE_STAGING:
                st.caption(
                    "年・種別フィルタを変えてもスキャン量は変わりません。"
                    "元テーブルは `publication_date` INTEGER ではパーティション pruning が効かないためです。"
                    "スキャン量を減らすには `create_staging.py` でステージングテーブルを作成してください。"
                )

            if gb > max_gb:
                st.error(f"上限超過。Secrets の `MAX_GB_PER_QUERY` を `\"{int(gb) + 10}\"` 以上にしてください。")
            elif gb > 500:
                st.warning(f"スキャン量が大きめです。公開年を絞るとさらに削減できます。")
            else:
                st.success("上限内です。検索を実行できます。")

# ── 検索ボタン ────────────────────────────────────────────────────────────────
if run_button:
    keywords = get_keywords()
    if not keywords:
        st.warning("キーワードを1つ以上入力してください")
        st.stop()

    query = build_query(keywords, country, limit, year_from, year_to, kind_codes)
    with st.expander("実行 SQL", expanded=False):
        st.code(query, language="sql")

    with st.spinner(f"検索中… ({', '.join(keywords)})"):
        try:
            rows = run_query(query)
        except (BadRequest, Forbidden, NotFound, GoogleAPIError, Exception) as e:
            handle_bq_error(e)
            st.stop()

    if not rows:
        st.info("ヒットなし。キーワードや年範囲を変えて試してください。")
        st.stop()

    st.success(f"{len(rows)} 件ヒット")

    table_data = [
        {
            "公開番号": row.publication_number,
            "種別": row.kind_code or "",
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
            label = f"[{i}] {row.publication_number} ({row.kind_code}) — {row.title_ja or '(タイトルなし)'}"
            with st.expander(label):
                st.write(row.claims_ja or "(テキストなし)")

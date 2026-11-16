"""Dashboard do marketplace + analista de IA.

  python -m mbi.generate            # cria data/marketplace.db
  streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from mbi import analytics as A

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "marketplace.db"
load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / ".config" / "portfolio" / ".env")

st.set_page_config(page_title="Marketplace BI", page_icon="🛋️", layout="wide")


def brl(v: float, casas: int = 0) -> str:
    return "R$ " + f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v: float) -> str:
    return f"{v:+.1f}%".replace(".", ",")


if not DB.exists():
    st.error("Rode `python -m mbi.generate` para criar a base.")
    st.stop()
d = A.load(str(DB))

st.title("🛋️ Marketplace BI · Casa Lume (fictícia)")
MESES = "jan fev mar abr mai jun jul ago set out nov dez".split()
st.caption(f"Dados sintéticos de jan/2024 a {MESES[d.last_day.month - 1]}/{d.last_day.year} · "
           f"{len(d.orders):,} pedidos".replace(",", "."))

cmp = A.compare_periods(d, "2026-05-01", "2026-07-31", "2026-08-01", "2026-10-31")
b, v = cmp["periodo_b"], cmp["variacao_pct_b_sobre_a"]
cols = st.columns(6)
for col, (label, key, fmt, inverse) in zip(cols, [
        ("Receita (ago–out)", "receita", brl, False), ("Pedidos", "pedidos", lambda x: f"{x:,}".replace(",", "."), False),
        ("Ticket médio", "ticket_medio", lambda x: brl(x, 2), False), ("Conversão", "conversao_pct",
                                                                         lambda x: f"{x:.2f}%".replace(".", ","), False),
        ("Cancelamento", "taxa_cancelamento_pct", lambda x: f"{x:.2f}%".replace(".", ","), True),
        ("Nota média", "nota_media", lambda x: f"{x:.2f}".replace(".", ","), False)]):
    col.metric(label, fmt(b[key]), pct(v[key]) + " vs. mai–jul" if key in v else None,
               delta_color="inverse" if inverse else "normal")

tab_vis, tab_cat, tab_reg, tab_ret, tab_ia = st.tabs(
    ["📈 Vendas", "🏷️ Categorias", "🚚 Entrega × satisfação", "🔁 Retenção", "🤖 Analista de IA"])

with tab_vis:
    ok = d.orders[d.orders["status"] == "entregue"]
    m = ok.groupby(ok["data"].dt.to_period("M"))["valor_total"].sum().reset_index()
    m["mes"] = m["data"].dt.to_timestamp()
    f = A.forecast(d)
    fc = pd.DataFrame({"mes": pd.to_datetime(list(f["previsao_receita"])), "valor_total": list(f["previsao_receita"].values())})
    chart = alt.layer(
        alt.Chart(m).mark_line(point=True, color="#2563eb").encode(
            x=alt.X("mes:T", title=None, axis=alt.Axis(format="%m/%y")), y=alt.Y("valor_total:Q", title="Receita (R$)",
                                                      axis=alt.Axis(labelExpr="replace(format(datum.value, ',.0f'), /,/g, '.')"))),
        alt.Chart(fc).mark_line(point=True, strokeDash=[5, 4], color="#9ca3af").encode(x="mes:T", y="valor_total:Q"),
    ).properties(height=320)
    st.altair_chart(chart, use_container_width=True)
    s = A.seasonality(d)
    st.caption(f"Linha tracejada: previsão sazonal com tendência (erro médio no backtest de 6 meses: "
               f"{f['mape_backtest_pct']:.1f}%). Pico sazonal em {s['pico']}, vale em {s['vale']}.".replace(".", ",", 1))

with tab_cat:
    ct = A.category_trends(d, months=12)
    df = pd.DataFrame(ct["categorias"])
    c1, c2 = st.columns([3, 2])
    with c1:
        st.altair_chart(alt.Chart(df).mark_bar().encode(
            x=alt.X("tendencia_participacao_mensal_pct:Q", title="Tendência da participação na receita (% ao mês)"),
            y=alt.Y("categoria:N", sort="x", title=None),
            color=alt.Color("classificacao:N", scale=alt.Scale(domain=["queda", "estavel", "alta"],
                                                                 range=["#dc2626", "#9ca3af", "#16a34a"]), title=None),
            tooltip=["categoria", "tendencia_participacao_mensal_pct", "estatistica_t", "tendencia_receita_mensal_pct"],
        ).properties(height=260), use_container_width=True)
    with c2:
        st.markdown(f"**Últimos 12 meses** ({ct['meses'][0]} a {ct['meses'][-1]}). Mercado total: "
                    f"{pct(ct['tendencia_mercado_mensal_pct'])} ao mês.")
        st.markdown("Uma categoria só é marcada como queda ou alta se a tendência passar de "
                    f"{ct['limite_pct']:.0f}% ao mês **e** for estatisticamente distinguível de zero (|t| ≥ 2).")
        st.dataframe(df[["categoria", "classificacao", "tendencia_participacao_mensal_pct", "estatistica_t"]],
                     hide_index=True, use_container_width=True)
    st.markdown("##### Produtos com maior queda (últimos 3 meses vs. 3 anteriores)")
    st.dataframe(A.product_trends(d)["produtos_em_queda"], hide_index=True, use_container_width=True)

with tab_reg:
    corte = st.date_input("Comparar antes e depois de", value=pd.Timestamp("2026-03-01"))
    rd = A.region_delivery(d, str(corte))
    rows = [{"regiao": r, "fase": f, **vals} for r, fases in rd["regioes"].items() for f, vals in fases.items()]
    df = pd.DataFrame(rows)
    c1, c2, c3 = st.columns(3)
    for col, metric, title in [(c1, "prazo_medio_dias", "Prazo médio (dias)"), (c2, "nota_media", "Nota média"),
                               (c3, "recompra_120d_pct", "Recompra em 120 dias (%)")]:
        col.altair_chart(alt.Chart(df).mark_bar().encode(
            x=alt.X("fase:N", title=None, sort=["antes", "depois"]), y=alt.Y(f"{metric}:Q", title=title),
            color=alt.Color("fase:N", legend=None, scale=alt.Scale(range=["#93c5fd", "#1d4ed8"])),
            column=alt.Column("regiao:N", title=None)).properties(width=48, height=200))
    st.info(f"Maior piora de prazo: **{rd['maior_piora_de_prazo']}**.")

with tab_ret:
    cr = A.cohort_retention(d)
    df = pd.DataFrame(cr["coortes"]).T.reset_index(names="coorte").melt("coorte", var_name="mes", value_name="ret")
    st.altair_chart(alt.Chart(df).mark_rect().encode(
        x=alt.X("mes:O", title="Meses após a 1ª compra"), y=alt.Y("coorte:O", title="Coorte"),
        color=alt.Color("ret:Q", title="% ativos", scale=alt.Scale(scheme="blues")),
        tooltip=["coorte", "mes", "ret"]).properties(height=340), use_container_width=True)
    ch = A.churn(d)
    st.caption(f"Clientes sem comprar há mais de {ch['dias_sem_comprar']} dias: {ch['churn_pct']:.1f}% · "
               f"por região: " + ", ".join(f"{k} {v:.1f}%" for k, v in ch["por_regiao_pct"].items()))

with tab_ia:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        st.info("Defina GEMINI_API_KEY para conversar com o analista. Exemplo de pergunta: "
                "\"Quais produtos apresentam queda nas vendas e quais ações poderiam melhorar os resultados?\"")
    else:
        from mbi.analyst import ask
        from mbi.llm import GeminiLLM
        q = st.text_area("Pergunta", "Quais produtos apresentam queda nas vendas e quais ações poderiam melhorar "
                                     "os resultados?")
        if st.button("Analisar", type="primary"):
            with st.spinner("Analisando…"):
                res = ask(GeminiLLM(key, os.getenv("GEMINI_MODEL", "gemini-3.8-flash")), DB, q)
            st.markdown(res["resposta"])
            with st.expander(f"Ferramentas usadas ({len(res['ferramentas'])})"):
                st.json(res["ferramentas"])

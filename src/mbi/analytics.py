"""Análises de negócio determinísticas (pandas + SQLite). São as ferramentas do analista de IA.

Cada função recebe parâmetros simples, devolve um dicionário serializável e não depende de LLM.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd


class Data:
    def __init__(self, db: Path, limpar: bool = True):
        con = sqlite3.connect(db)
        self.orders = pd.read_sql("SELECT * FROM pedidos", con, parse_dates=["data", "data_entrega"])
        self.items = pd.read_sql("SELECT i.*, p.categoria, p.nome FROM itens_pedido i JOIN produtos p USING(produto_id)",
                                 con)
        self.reviews = pd.read_sql("SELECT * FROM avaliacoes", con)
        self.customers = pd.read_sql("SELECT * FROM clientes", con)
        self.sessions = pd.read_sql("SELECT * FROM sessoes", con, parse_dates=["data"])
        con.close()
        self.qualidade = limpar_dados(self) if limpar else {"limpeza": "desligada"}
        self.orders = self.orders.merge(self.customers[["cliente_id", "regiao"]], on="cliente_id")
        delivered = self.orders[self.orders["status"] == "entregue"][["pedido_id", "data"]]
        self.lines = self.items.merge(delivered, on="pedido_id")
        self.lines["receita"] = self.lines["quantidade"] * self.lines["preco_unitario"]
        self.last_day = self.orders["data"].max()


def limpar_dados(d: Data) -> dict:
    """Corrige problemas comuns de bases reais antes de qualquer análise e devolve o que foi feito.

    - status e região: espaços e maiúsculas padronizados;
    - pedido repetido (mesmo pedido_id): fica uma linha, e os itens repetidos da ingestão dupla saem;
    - preço vazio ou não positivo: linha descartada;
    - preço mais de 20 vezes acima da mediana do produto: tratado como erro de digitação e descartado;
    - prazo de entrega negativo: vira ausente (não entra em médias de prazo).
    """
    rep: dict = {}
    o = d.orders
    status = o["status"].astype(str).str.strip().str.lower()
    rep["status_padronizados"] = int((status != o["status"]).sum())
    o["status"] = status
    regiao = d.customers["regiao"].astype(str).str.strip().str.title()
    rep["regioes_padronizadas"] = int((regiao != d.customers["regiao"]).sum())
    d.customers["regiao"] = regiao

    copias = o.groupby("pedido_id").size()
    rep["pedidos_duplicados_removidos"] = int((copias - 1).sum())
    d.orders = o.drop_duplicates("pedido_id", keep="first").copy()
    it = d.items
    if rep["pedidos_duplicados_removidos"]:
        k = it["pedido_id"].map(copias).fillna(1)
        chave = ["pedido_id", "produto_id", "quantidade", "preco_unitario"]
        ordem = it.groupby(chave, dropna=False).cumcount()
        total = it.groupby(chave, dropna=False)["pedido_id"].transform("size")
        manter = ordem < (total / k)
        rep["itens_duplicados_removidos"] = int((~manter).sum())
        it = it[manter]
    preco = pd.to_numeric(it["preco_unitario"], errors="coerce")
    invalido = preco.isna() | (preco <= 0)
    mediana = preco.groupby(it["produto_id"]).transform("median")
    suspeito = ~invalido & (preco > 20 * mediana)
    rep["itens_preco_invalido"] = int(invalido.sum())
    rep["itens_preco_suspeito"] = int(suspeito.sum())
    it = it[~invalido & ~suspeito].copy()
    it["preco_unitario"] = preco[~invalido & ~suspeito]
    d.items = it
    neg = d.orders["dias_entrega"] < 0
    rep["prazos_negativos_anulados"] = int(neg.sum())
    d.orders.loc[neg, "dias_entrega"] = np.nan
    rep["total_de_correcoes"] = int(sum(v for v in rep.values()))
    return rep


@lru_cache(maxsize=4)
def load(db: str, limpar: bool = True) -> Data:
    return Data(Path(db), limpar)


def data_quality(d: Data) -> dict:
    """O que a limpeza corrigiu na carga dos dados (zero em tudo = base já estava limpa)."""
    return d.qualidade


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


def _period(d: Data, start: str, end: str) -> pd.DataFrame:
    o = d.orders
    return o[(o["data"] >= start) & (o["data"] <= end)]


def kpis(d: Data, start: str, end: str) -> dict:
    o = _period(d, start, end)
    ok = o[o["status"] == "entregue"]
    s = d.sessions[(d.sessions["data"] >= start) & (d.sessions["data"] <= end)]
    rv = d.reviews.merge(ok[["pedido_id"]], on="pedido_id")
    return {"periodo": [start, end], "receita": _r(ok["valor_total"].sum()), "pedidos": int(len(o)),
            "pedidos_entregues": int(len(ok)), "ticket_medio": _r(ok["valor_total"].mean()),
            "taxa_cancelamento_pct": _r(100 * (o["status"] == "cancelado").mean()),
            "conversao_pct": _r(100 * s["pedidos"].sum() / s["sessoes"].sum()) if len(s) else None,
            "clientes_compradores": int(o["cliente_id"].nunique()), "nota_media": _r(rv["nota"].mean()),
            "prazo_entrega_medio_dias": _r(ok["dias_entrega"].mean(), 1)}


def compare_periods(d: Data, a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
    a, b = kpis(d, a_start, a_end), kpis(d, b_start, b_end)
    var = {k: _r(100 * (b[k] / a[k] - 1)) for k in a if k != "periodo" and a[k] and b[k] is not None}
    return {"periodo_a": a, "periodo_b": b, "variacao_pct_b_sobre_a": var}


def _monthly(df: pd.DataFrame, key: str, end: pd.Timestamp, months: int) -> pd.DataFrame:
    m = df.assign(mes=df["data"].dt.to_period("M")).groupby(["mes", key])["receita"].sum().unstack(fill_value=0)
    last = end.to_period("M")
    if end.day < end.days_in_month:  # descarta mês incompleto
        last -= 1
    return m.loc[last - months + 1: last]


def _trend_pct(series: pd.Series) -> float:
    return _trend(series)[0]


def _trend(series: pd.Series) -> tuple[float, float]:
    """Tendência mensal (%) por regressão log-linear e estatística t da inclinação."""
    y = np.log(series.clip(lower=1).to_numpy(dtype=float))
    x = np.arange(len(y))
    (slope, intercept), cov = np.polyfit(x, y, 1, cov=True)
    se = float(np.sqrt(cov[0, 0])) if cov[0, 0] > 0 else float("inf")
    return (np.exp(slope) - 1) * 100, slope / se if se else 0.0


def category_trends(d: Data, months: int = 12, threshold_pct: float = 2.0, min_t: float = 2.0) -> dict:
    """Tendência de cada categoria em receita absoluta e em participação na receita total.

    A classificação usa a participação: ela separa o que é da categoria do que é movimento do
    mercado inteiro (sazonalidade, base de clientes). Uma categoria pode cair em reais só porque
    o mês foi fraco para todas; a participação mostra quem perdeu espaço.
    """
    m = _monthly(d.lines, "categoria", d.last_day, months)
    share = m.div(m.sum(axis=1), axis=0)
    rows = []
    for cat in m.columns:
        t_share, t_stat = _trend(share[cat] * 1e4)
        significant = abs(t_stat) >= min_t
        rows.append({"categoria": cat, "tendencia_participacao_mensal_pct": _r(t_share), "estatistica_t": _r(t_stat),
                     "tendencia_receita_mensal_pct": _r(_trend_pct(m[cat])),
                     "participacao_primeiro_mes_pct": _r(100 * share[cat].iloc[0], 1),
                     "participacao_ultimo_mes_pct": _r(100 * share[cat].iloc[-1], 1),
                     "receita_ultimo_mes": _r(m[cat].iloc[-1]),
                     "classificacao": "queda" if significant and t_share <= -threshold_pct else
                     "alta" if significant and t_share >= threshold_pct else "estavel"})
    rows.sort(key=lambda r: r["tendencia_participacao_mensal_pct"])
    return {"meses": [str(p) for p in m.index], "limite_pct": threshold_pct,
            "tendencia_mercado_mensal_pct": _r(_trend_pct(m.sum(axis=1))), "categorias": rows}


def product_trends(d: Data, months: int = 3, top: int = 5, min_revenue: float = 500.0) -> dict:
    end = d.last_day
    cur = d.lines[d.lines["data"] > end - pd.DateOffset(months=months)]
    prev = d.lines[(d.lines["data"] <= end - pd.DateOffset(months=months)) &
                   (d.lines["data"] > end - pd.DateOffset(months=2 * months))]
    a = prev.groupby(["produto_id", "nome", "categoria"])["receita"].sum()
    b = cur.groupby(["produto_id", "nome", "categoria"])["receita"].sum()
    df = pd.concat([a.rename("anterior"), b.rename("atual")], axis=1).fillna(0)
    df = df[df["anterior"] >= min_revenue]
    df["variacao_pct"] = 100 * (df["atual"] / df["anterior"] - 1)
    worst = df.sort_values("variacao_pct").head(top).reset_index()
    return {"janela_meses": months, "produtos_em_queda": [
        {"produto": r.nome, "categoria": r.categoria, "receita_anterior": _r(r.anterior), "receita_atual": _r(r.atual),
         "variacao_pct": _r(r.variacao_pct)} for r in worst.itertuples()],
        "queda_por_categoria": worst["categoria"].value_counts().to_dict()}


def region_delivery(d: Data, split_date: str) -> dict:
    """Compara, por região, prazo, nota e recompra em 120 dias antes e depois de uma data."""
    o = d.orders[d.orders["status"] == "entregue"].merge(d.reviews[["pedido_id", "nota"]], on="pedido_id", how="left")
    o = o.sort_values("data")
    nxt = o.groupby("cliente_id")["data"].shift(-1)
    horizon_ok = o["data"] <= d.last_day - pd.Timedelta(days=120)
    o["recompra_120d"] = np.where(horizon_ok, (nxt - o["data"]).dt.days.le(120).astype(float), np.nan)
    o["fase"] = np.where(o["data"] < split_date, "antes", "depois")
    g = o.groupby(["regiao", "fase"]).agg(prazo_medio=("dias_entrega", "mean"), nota_media=("nota", "mean"),
                                          recompra_120d_pct=("recompra_120d", "mean"), pedidos=("pedido_id", "count"))
    out = {}
    for (reg, fase), r in g.iterrows():
        out.setdefault(reg, {})[fase] = {"prazo_medio_dias": _r(r.prazo_medio, 1), "nota_media": _r(r.nota_media),
                                         "recompra_120d_pct": _r(100 * r.recompra_120d_pct, 1),
                                         "pedidos": int(r.pedidos)}
    worst = max(out, key=lambda k: out[k].get("depois", {}).get("prazo_medio_dias", 0) -
                out[k].get("antes", {}).get("prazo_medio_dias", 0))
    return {"data_corte": split_date, "regioes": out, "maior_piora_de_prazo": worst}


def cohort_retention(d: Data, months: int = 6, last_cohorts: int = 12) -> dict:
    o = d.orders[d.orders["status"] == "entregue"].copy()
    o["mes"] = o["data"].dt.to_period("M")
    first = o.groupby("cliente_id")["mes"].min().rename("coorte")
    o = o.join(first, on="cliente_id")
    o["idade"] = (o["mes"] - o["coorte"]).apply(lambda x: x.n)
    sizes = o.groupby("coorte")["cliente_id"].nunique()
    active = o[o["idade"].between(1, months)].groupby(["coorte", "idade"])["cliente_id"].nunique().unstack(fill_value=0)
    ret = (active.div(sizes, axis=0) * 100).round(1)
    complete = ret.index[ret.index <= d.last_day.to_period("M") - months][-last_cohorts:]
    ret = ret.loc[complete]
    return {"coortes": {str(c): {f"M{k}": _r(v, 1) for k, v in row.items()} for c, row in ret.iterrows()},
            "media": {f"M{k}": _r(v, 1) for k, v in ret.mean().items()}}


def churn(d: Data, inactive_days: int = 120) -> dict:
    last = d.orders.groupby("cliente_id").agg(ultima=("data", "max"), regiao=("regiao", "first"))
    last["churn"] = (d.last_day - last["ultima"]).dt.days > inactive_days
    by_region = (last.groupby("regiao")["churn"].mean() * 100).round(1).to_dict()
    return {"dias_sem_comprar": inactive_days, "churn_pct": _r(100 * last["churn"].mean(), 1),
            "por_regiao_pct": by_region}


def seasonality(d: Data) -> dict:
    ok = d.orders[d.orders["status"] == "entregue"]
    m = ok.groupby(ok["data"].dt.to_period("M"))["valor_total"].sum()
    by_month = m.groupby(m.index.month).mean()
    idx = (by_month / by_month.mean() * 100).round(1)
    names = "jan fev mar abr mai jun jul ago set out nov dez".split()
    return {"indice_por_mes": {names[k - 1]: float(v) for k, v in idx.items()},
            "pico": names[int(idx.idxmax()) - 1], "vale": names[int(idx.idxmin()) - 1]}


def forecast(d: Data, categoria: str | None = None, horizon: int = 3, backtest: int = 6) -> dict:
    """Previsão sazonal ingênua com tendência: valor do mesmo mês do ano anterior × tendência dos últimos
    12 meses. Simples e explicável; o backtest (MAPE) mostra o quanto confiar."""
    lines = d.lines if categoria is None else d.lines[d.lines["categoria"] == categoria]
    if lines.empty:
        return {"erro": f"categoria desconhecida: {categoria}"}
    m = lines.groupby(lines["data"].dt.to_period("M"))["receita"].sum()
    last = d.last_day.to_period("M") - (0 if d.last_day.day == d.last_day.days_in_month else 1)
    m = m.loc[:last]

    def predict(hist: pd.Series, target: pd.Period) -> float:
        g = hist.iloc[-12:].sum() / hist.iloc[-24:-12].sum() if len(hist) >= 24 else 1.0
        base = hist.get(target - 12, hist.iloc[-12:].mean())
        return float(base * g)

    errs = []
    for i in range(backtest, 0, -1):
        hist, target = m.iloc[:-i], m.index[-i]
        errs.append(abs(predict(hist, target) - m.iloc[-i]) / m.iloc[-i])
    preds = {}
    hist = m.copy()
    for h in range(1, horizon + 1):
        t = last + h
        preds[str(t)] = _r(predict(hist, t))
        hist.loc[t] = preds[str(t)]
    return {"categoria": categoria or "todas", "previsao_receita": preds,
            "mape_backtest_pct": _r(100 * np.mean(errs), 1), "meses_backtest": backtest}

"""Testes: as análises encontram os padrões plantados, o SQL é realmente somente leitura e o analista
usa as ferramentas (LLM roteirizado)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from mbi import analytics as A
from mbi.analyst import UnsafeQuery, ask, run_sql
from mbi.generate import generate
from mbi.llm import LLMResponse, ScriptedLLM, ToolCall, Usage


@pytest.fixture(scope="session")
def db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("mbi") / "marketplace.db"
    generate(path, seed=42)
    return path


@pytest.fixture(scope="session")
def data(db):
    return A.load(str(db))


@pytest.fixture(scope="session")
def truth(db):
    return json.loads((db.parent / "padroes_plantados.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- gabarito

def test_finds_planted_declining_and_growing_categories(data, truth):
    rows = {r["categoria"]: r["classificacao"] for r in A.category_trends(data, months=12)["categorias"]}
    assert rows[truth["categoria_em_queda"]["categoria"]] == "queda"
    assert rows[truth["categoria_em_alta"]["categoria"]] == "alta"
    assert sum(v != "estavel" for v in rows.values()) == 2  # sem falsos positivos


def test_short_window_does_not_call_noise_a_trend(data):
    rows = A.category_trends(data, months=6)["categorias"]
    assert all(r["classificacao"] in ("estavel", "alta") for r in rows if r["categoria"] != "Luminárias")


def test_finds_planted_delivery_problem(data, truth):
    res = A.region_delivery(data, truth["regiao_com_atraso"]["desde"])
    reg = truth["regiao_com_atraso"]["regiao"]
    assert res["maior_piora_de_prazo"] == reg
    before, after = res["regioes"][reg]["antes"], res["regioes"][reg]["depois"]
    assert after["prazo_medio_dias"] > before["prazo_medio_dias"] + 5
    assert after["nota_media"] < before["nota_media"] - 0.5
    assert after["recompra_120d_pct"] < before["recompra_120d_pct"] - 15


def test_finds_seasonality_peaks(data):
    s = A.seasonality(data)
    top2 = sorted(s["indice_por_mes"], key=s["indice_por_mes"].get)[-2:]
    assert set(top2) == {"nov", "dez"}


def test_kpis_are_consistent(data):
    k = A.kpis(data, "2026-01-01", "2026-06-30")
    assert k["pedidos_entregues"] <= k["pedidos"]
    assert k["ticket_medio"] == pytest.approx(k["receita"] / k["pedidos_entregues"], rel=1e-3)


def test_forecast_reports_backtest_error(data):
    f = A.forecast(data, "Têxteis")
    assert len(f["previsao_receita"]) == 3 and f["mape_backtest_pct"] > 0
    assert "erro" in A.forecast(data, "Inexistente")


# ---------------------------------------------------------------- SQL seguro

def test_sql_select_works(db):
    r = run_sql(db, "SELECT regiao, COUNT(*) FROM clientes GROUP BY regiao ORDER BY 2 DESC")
    assert r["colunas"] == ["regiao", "COUNT(*)"] and len(r["linhas"]) == 5


@pytest.mark.parametrize("q", [
    "DELETE FROM pedidos",
    "UPDATE pedidos SET valor_total = 0",
    "SELECT 1; DROP TABLE pedidos",
    "DROP TABLE pedidos",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "PRAGMA writable_schema = ON",
    "WITH x AS (SELECT 1) INSERT INTO clientes VALUES (0, 'X', '2020-01-01')",
])
def test_sql_blocks_writes_and_tricks(db, q):
    with pytest.raises((UnsafeQuery, sqlite3.DatabaseError)):
        run_sql(db, q)
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0] > 0
    con.close()


def test_sql_limits_rows(db):
    r = run_sql(db, "SELECT * FROM itens_pedido")
    assert len(r["linhas"]) == 200 and r["truncado"]


def test_sql_times_out(db):
    with pytest.raises(UnsafeQuery):
        run_sql(db, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c",
                timeout_s=0.5)


# ---------------------------------------------------------------- analista

def test_analyst_uses_tools_and_reports_blocked_sql(db):
    llm = ScriptedLLM([
        LLMResponse(None, [ToolCall("category_trends", {"meses": 12}),
                           ToolCall("run_sql", {"query": "DELETE FROM pedidos"})], Usage(calls=1)),
        LLMResponse("**Resposta** Luminárias perdem participação.", [], Usage(calls=1)),
    ])
    res = ask(llm, db, "Quais categorias estão caindo?")
    assert [t["ferramenta"] for t in res["ferramentas"]] == ["category_trends", "run_sql"]
    assert res["ferramentas"][1].get("bloqueada")
    tool_results = dict(llm.chat_inputs[-1][-1].tool_results)
    assert "Consulta bloqueada" in tool_results["run_sql"]["erro"]
    assert any(c["classificacao"] == "queda" for c in tool_results["category_trends"]["categorias"])


# ------------------------------------------------------------------ dados sujos e limpeza
def test_base_limpa_nao_recebe_correcoes(db):
    q = A.Data(db).qualidade
    assert q["total_de_correcoes"] == 0


def test_limpeza_recupera_padroes_e_receita_na_base_suja(db, tmp_path):
    from mbi.dirty import sujar

    suja = tmp_path / "sujo.db"
    inj = sujar(db, suja)
    sem, com, ref = A.Data(suja, limpar=False), A.Data(suja), A.Data(db)
    assert len(A.region_delivery(sem, "2026-03-01")["regioes"]) > 5  # variantes de escrita viram "regiões"
    assert len(A.region_delivery(com, "2026-03-01")["regioes"]) == 5
    assert com.qualidade["pedidos_duplicados_removidos"] == inj["pedidos_duplicados"]
    receita = lambda d: A.kpis(d, "2024-01-01", "2026-10-31")["receita"]  # noqa: E731
    assert abs(receita(com) / receita(ref) - 1) < 0.001 < abs(receita(sem) / receita(ref) - 1)
    trends = {c["categoria"]: c["classificacao"] for c in A.category_trends(com)["categorias"]}
    assert trends["Luminárias"] == "queda" and trends["Têxteis"] == "alta"


def test_ferramenta_de_qualidade_disponivel_para_o_analista():
    from mbi.analyst import TOOLS

    assert "data_quality" in TOOLS

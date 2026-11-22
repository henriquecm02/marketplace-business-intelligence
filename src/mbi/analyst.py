"""Analista de negócios com IA: responde perguntas usando ferramentas de análise e SQL somente leitura.

Camadas de proteção do SQL (defesa em profundidade, não confiar no prompt):
  1. só um comando, e ele precisa começar com SELECT ou WITH;
  2. conexão aberta em modo somente leitura (URI `mode=ro`) e `PRAGMA query_only`;
  3. authorizer do SQLite que nega qualquer ação que não seja leitura (bloqueia ATTACH, PRAGMA etc.);
  4. limite de linhas e de tempo de execução.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import analytics as A
from .llm import LLM, Message, ToolSpec, Usage

PROMPTS = Path(__file__).parent / "prompts"
MAX_ROWS = 200
SCHEMA_DOC = """Tabelas (SQLite):
- pedidos(pedido_id, cliente_id, data TEXT AAAA-MM-DD, valor_total, status 'entregue'|'cancelado', data_entrega, dias_entrega)
- itens_pedido(pedido_id, produto_id, quantidade, preco_unitario)
- produtos(produto_id, nome, categoria, preco)
- clientes(cliente_id, regiao, cadastro)
- avaliacoes(pedido_id, nota 1-5, data)
- sessoes(data, sessoes, pedidos)"""


class UnsafeQuery(ValueError):
    pass


_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _authorizer(action, *args):
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def run_sql(db: Path, query: str, timeout_s: float = 5.0) -> dict:
    q = query.strip().rstrip(";").strip()
    if ";" in q:
        raise UnsafeQuery("Apenas um comando por consulta.")
    if not re.match(r"(?is)^(select|with)\b", q):
        raise UnsafeQuery("Apenas consultas SELECT/WITH são permitidas.")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        con.set_authorizer(_authorizer)
        deadline = time.monotonic() + timeout_s
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            cur = con.execute(q)
        except sqlite3.DatabaseError as e:
            if "not authorized" in str(e) or "interrupted" in str(e):
                raise UnsafeQuery(str(e)) from e
            raise
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    finally:
        con.close()
    return {"colunas": cols, "linhas": [list(r) for r in rows[:MAX_ROWS]], "truncado": len(rows) > MAX_ROWS}


def _obj(props: dict, req: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": req or []}


DATE = {"type": "string", "description": "AAAA-MM-DD"}
TOOLS = {
    "kpis": (ToolSpec("kpis", "KPIs de um período: receita, pedidos, ticket médio, cancelamento, conversão, "
                      "nota média, prazo de entrega.", _obj({"inicio": DATE, "fim": DATE}, ["inicio", "fim"])),
             lambda d, db, a: A.kpis(d, a["inicio"], a["fim"])),
    "compare_periods": (ToolSpec("compare_periods", "Compara KPIs de dois períodos (B sobre A, em %).",
                                 _obj({"a_inicio": DATE, "a_fim": DATE, "b_inicio": DATE, "b_fim": DATE},
                                      ["a_inicio", "a_fim", "b_inicio", "b_fim"])),
                        lambda d, db, a: A.compare_periods(d, a["a_inicio"], a["a_fim"], a["b_inicio"], a["b_fim"])),
    "category_trends": (ToolSpec("category_trends", "Tendência de cada categoria (participação e receita), com "
                                 "teste de significância. Use para 'o que está caindo/crescendo'.",
                                 _obj({"meses": {"type": "integer"}})),
                        lambda d, db, a: A.category_trends(d, months=int(a.get("meses", 12)))),
    "product_trends": (ToolSpec("product_trends", "Produtos com maior queda de receita (últimos N meses vs. "
                                "N anteriores).", _obj({"meses": {"type": "integer"}, "top": {"type": "integer"}})),
                       lambda d, db, a: A.product_trends(d, int(a.get("meses", 3)), int(a.get("top", 5)))),
    "region_delivery": (ToolSpec("region_delivery", "Prazo de entrega, nota e recompra por região, antes e depois "
                                 "de uma data.", _obj({"data_corte": DATE}, ["data_corte"])),
                        lambda d, db, a: A.region_delivery(d, a["data_corte"])),
    "cohort_retention": (ToolSpec("cohort_retention", "Retenção mensal por coorte de primeira compra.", _obj({})),
                         lambda d, db, a: A.cohort_retention(d)),
    "churn": (ToolSpec("churn", "Percentual de clientes sem comprar há N dias, por região.",
                       _obj({"dias": {"type": "integer"}})), lambda d, db, a: A.churn(d, int(a.get("dias", 120)))),
    "seasonality": (ToolSpec("seasonality", "Índice de sazonalidade da receita por mês do ano.", _obj({})),
                    lambda d, db, a: A.seasonality(d)),
    "forecast": (ToolSpec("forecast", "Previsão de receita dos próximos meses (total ou por categoria), com erro "
                          "de backtest (MAPE).", _obj({"categoria": {"type": "string"}, "meses": {"type": "integer"}})),
                 lambda d, db, a: A.forecast(d, a.get("categoria"), int(a.get("meses", 3)))),
    "data_quality": (ToolSpec("data_quality", "O que a limpeza automática corrigiu nos dados (duplicatas, preços "
                              "inválidos, grafias de status e região). Use quando os números parecerem estranhos ou "
                              "para citar a confiabilidade da análise.", _obj({})),
                     lambda d, db, a: A.data_quality(d)),
    "run_sql": (ToolSpec("run_sql", "Consulta SQL somente leitura (SELECT/WITH, até 200 linhas) para perguntas que "
                         "as outras ferramentas não cobrem.\n" + SCHEMA_DOC, _obj({"query": {"type": "string"}},
                                                                                  ["query"])),
                lambda d, db, a: run_sql(db, a["query"])),
}


def ask(llm: LLM, db: Path, question: str, max_steps: int = 8) -> dict:
    d = A.load(str(db))
    system = (PROMPTS / "analyst_v1.md").read_text(encoding="utf-8").strip()
    msgs = [Message(role="user", text=f"Os dados vão até {d.last_day.date()}. Pergunta: {question}")]
    specs = [spec for spec, _ in TOOLS.values()]
    trail, usage = [], Usage()
    for _ in range(max_steps):
        resp = llm.chat(system, msgs, specs)
        usage.add(resp.usage)
        msgs.append(Message(role="assistant", text=resp.text, tool_calls=resp.tool_calls, raw=resp.raw))
        if not resp.tool_calls:
            return {"resposta": (resp.text or "").strip(), "ferramentas": trail, "chamadas_llm": usage.calls}
        results = []
        for c in resp.tool_calls:
            entry = {"ferramenta": c.name, "args": c.args}
            try:
                fn = TOOLS[c.name][1]
                res = fn(d, db, c.args)
            except KeyError:
                res = {"erro": f"Ferramenta desconhecida: {c.name}"}
            except UnsafeQuery as e:
                res = {"erro": f"Consulta bloqueada: {e}"}
                entry["bloqueada"] = True
            except Exception as e:
                res = {"erro": f"{type(e).__name__}: {e}"}
            trail.append(entry)
            results.append((c.name, json.loads(json.dumps(res, default=str))))
        msgs.append(Message(role="tool", tool_results=results))
    return {"resposta": "Não concluí a análise dentro do limite de passos.", "ferramentas": trail,
            "chamadas_llm": usage.calls}

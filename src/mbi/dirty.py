"""Suja uma cópia do banco com os problemas mais comuns de uma base real de e-commerce.

Serve para testar se as análises continuam certas quando os dados não são limpos:
  - pedidos duplicados (a mesma venda ingerida duas vezes, com os itens repetidos);
  - status e região escritos de jeitos diferentes ("ENTREGUE", " entregue", "norte ");
  - preço unitário digitado errado (×100) ou vazio;
  - prazo de entrega negativo (data de entrega antes da compra).

Uso: python -m mbi.dirty data/marketplace.db data/marketplace_sujo.db
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def sujar(origem: Path, destino: Path, seed: int = 7, dup_pct: float = 0.04, status_pct: float = 0.10,
          regiao_pct: float = 0.08, preco_x100_pct: float = 0.005, preco_nulo_pct: float = 0.005,
          prazo_negativo_pct: float = 0.003) -> dict:
    shutil.copy(origem, destino)
    rng = np.random.default_rng(seed)
    con = sqlite3.connect(destino)
    ped = pd.read_sql("SELECT * FROM pedidos", con)
    itens = pd.read_sql("SELECT * FROM itens_pedido", con)
    cli = pd.read_sql("SELECT * FROM clientes", con)

    dup_ids = rng.choice(ped["pedido_id"], int(len(ped) * dup_pct), replace=False)
    ped = pd.concat([ped, ped[ped["pedido_id"].isin(dup_ids)]], ignore_index=True)
    itens = pd.concat([itens, itens[itens["pedido_id"].isin(dup_ids)]], ignore_index=True)

    idx = rng.choice(len(ped), int(len(ped) * status_pct), replace=False)
    ped.loc[idx, "status"] = [rng.choice([s.upper(), " " + s, s.capitalize() + " "]) for s in ped.loc[idx, "status"]]

    idx = rng.choice(len(cli), int(len(cli) * regiao_pct), replace=False)
    cli.loc[idx, "regiao"] = [rng.choice([r.lower(), r.upper(), r + " "]) for r in cli.loc[idx, "regiao"]]

    n = len(itens)
    x100 = rng.choice(n, int(n * preco_x100_pct), replace=False)
    itens.loc[x100, "preco_unitario"] = itens.loc[x100, "preco_unitario"] * 100
    nulos = rng.choice(np.setdiff1d(np.arange(n), x100), int(n * preco_nulo_pct), replace=False)
    itens["preco_unitario"] = itens["preco_unitario"].astype(object)
    itens.loc[nulos, "preco_unitario"] = None

    neg = rng.choice(len(ped), int(len(ped) * prazo_negativo_pct), replace=False)
    ped.loc[neg, "dias_entrega"] = -ped.loc[neg, "dias_entrega"].abs()

    for nome, df in (("pedidos", ped), ("itens_pedido", itens), ("clientes", cli)):
        df.to_sql(nome, con, index=False, if_exists="replace")
    con.commit()
    con.close()
    return {"pedidos_duplicados": len(dup_ids), "status_variantes": int(len(ped) * status_pct),
            "regioes_variantes": int(len(cli) * regiao_pct), "precos_x100": len(x100), "precos_nulos": len(nulos),
            "prazos_negativos": len(neg)}


if __name__ == "__main__":
    origem, destino = Path(sys.argv[1]), Path(sys.argv[2])
    print(json.dumps(sujar(origem, destino), ensure_ascii=False))

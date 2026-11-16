"""Gera um marketplace fictício (casa e decoração) com padrões de negócio plantados e conhecidos.

Padrões plantados (o "gabarito" fica em data/padroes_plantados.json):
  1. A categoria "Luminárias" perde demanda de forma constante nos últimos 6 meses (−6% a.m.).
  2. A região Norte tem entregas mais lentas a partir de 2026-03; atraso derruba a nota da avaliação
     e reduz a chance de o cliente voltar a comprar.
  3. Promoções de novembro (Black Friday) e dezembro multiplicam a demanda.
  4. "Têxteis" cresce de forma constante (+4% a.m.).
Tudo é determinístico pela semente.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

CATEGORIES = {  # categoria: (preço médio, peso de demanda)
    "Luminárias": (189.0, 1.0), "Têxteis": (89.0, 1.4), "Organização": (59.0, 1.6),
    "Cozinha": (129.0, 1.3), "Decoração": (99.0, 1.2), "Banho": (69.0, 0.9),
}
REGIONS = {"Sudeste": 0.45, "Sul": 0.17, "Nordeste": 0.2, "Centro-Oeste": 0.09, "Norte": 0.09}
BASE_DELIVERY = {"Sudeste": 3, "Sul": 4, "Nordeste": 6, "Centro-Oeste": 5, "Norte": 8}
START, END = date(2024, 1, 1), date(2026, 10, 31)
DECLINE_FROM, NORTE_DELAY_FROM = date(2026, 5, 1), date(2026, 3, 1)


def _month_index(d: date) -> int:
    return (d.year - START.year) * 12 + d.month - START.month


def generate(db: Path, seed: int = 42, n_customers: int = 6000, n_products: int = 120) -> dict:
    rng = np.random.default_rng(seed)
    cats = list(CATEGORIES)
    products = pd.DataFrame({
        "produto_id": range(1, n_products + 1),
        "categoria": rng.choice(cats, n_products, p=np.array([CATEGORIES[c][1] for c in cats]) / sum(
            CATEGORIES[c][1] for c in cats)),
    })
    products["preco"] = [round(CATEGORIES[c][0] * rng.uniform(0.5, 1.8), 2) for c in products["categoria"]]
    products["nome"] = [f"{c} item {i:03d}" for i, c in zip(products["produto_id"], products["categoria"])]

    customers = pd.DataFrame({"cliente_id": range(1, n_customers + 1),
                              "regiao": rng.choice(list(REGIONS), n_customers, p=list(REGIONS.values()))})
    customers["cadastro"] = [START + timedelta(days=int(x)) for x in rng.integers(0, (END - START).days - 30,
                                                                                  n_customers)]

    orders, items, reviews, sessions = [], [], [], []
    by_cat = {c: products[products["categoria"] == c][["produto_id", "preco"]].to_numpy() for c in cats}
    region_of = dict(zip(customers["cliente_id"], customers["regiao"]))
    next_id = [0]

    def category_weights(d: date) -> np.ndarray:
        w = []
        for c in cats:
            weight = CATEGORIES[c][1]
            if c == "Luminárias" and d >= DECLINE_FROM:
                weight *= 0.94 ** (_month_index(d) - _month_index(DECLINE_FROM) + 1)
            if c == "Têxteis":
                weight *= 1.04 ** (_month_index(d) / 3)
            w.append(weight)
        return np.array(w) / sum(w)

    def place(cid: int, d: date) -> int:
        """Cria um pedido (itens, entrega, avaliação) e devolve o prazo de entrega em dias."""
        next_id[0] += 1
        oid = next_id[0]
        chosen = rng.choice(cats, int(rng.choice([1, 2, 3], p=[0.6, 0.3, 0.1])), p=category_weights(d))
        total = 0.0
        for c in chosen:
            pid, base_price = by_cat[c][rng.integers(len(by_cat[c]))]
            qty = int(rng.choice([1, 2], p=[0.85, 0.15]))
            price = round(float(base_price) * (0.85 if d.month == 11 else 1.0), 2)  # 15% off em novembro
            items.append((oid, int(pid), qty, price))
            total += qty * price
        region = region_of[cid]
        delay = BASE_DELIVERY[region] + int(rng.poisson(1.0))
        if region == "Norte" and d >= NORTE_DELAY_FROM:
            delay += int(rng.integers(5, 11))
        cancelled = rng.random() < (0.04 + (0.05 if delay > 12 else 0))
        orders.append((oid, cid, d.isoformat(), round(total, 2), "cancelado" if cancelled else "entregue",
                       None if cancelled else (d + timedelta(days=delay)).isoformat(), delay))
        if not cancelled and rng.random() < 0.55:
            score = 5 - (2 if delay > 12 else 1 if delay > 7 else 0) - (1 if rng.random() < 0.15 else 0)
            reviews.append((oid, int(max(1, min(5, score + rng.integers(-1, 1)))),
                            (d + timedelta(days=delay + 2)).isoformat()))
        return delay

    for cid, signup in zip(customers["cliente_id"].tolist(), customers["cadastro"]):
        d, active, propensity = signup, True, rng.uniform(0.6, 1.4)
        while active and d <= END:
            delay = place(cid, d)
            # recompra: atraso reduz a chance de voltar
            active = rng.random() < min(0.72 * propensity * (0.55 if delay > 12 else 1.0), 0.95)
            nxt = d + timedelta(days=int(rng.gamma(2.0, 40)) + 5)
            # campanhas: clientes ativos fazem compras extras na Black Friday e no Natal
            if active:
                for year in range(d.year, nxt.year + 1):
                    for camp_day, p_extra in ((date(year, 11, 24), 0.45), (date(year, 12, 12), 0.25)):
                        day = camp_day + timedelta(days=int(rng.integers(-4, 5)))
                        if d < day < nxt and day <= END and rng.random() < p_extra:
                            place(cid, day)
            d = nxt

    # sessões (para conversão): ~ pedidos/2,8% com ruído e picos de promoção
    days = pd.date_range(START, END, freq="D")
    od = pd.Series(pd.to_datetime([o[2] for o in orders])).dt.normalize().value_counts()
    for day in days:
        n_orders = int(od.get(day, 0))
        conv = 0.028 * (1.25 if day.month == 11 else 1.0)
        sessions.append((day.date().isoformat(), int(max(n_orders, 1) / conv * rng.uniform(0.9, 1.1)), n_orders))

    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    products.to_sql("produtos", con, index=False)
    customers.to_sql("clientes", con, index=False)
    orders.sort(key=lambda o: o[2])
    pd.DataFrame(orders, columns=["pedido_id", "cliente_id", "data", "valor_total", "status", "data_entrega",
                                  "dias_entrega"]).to_sql("pedidos", con, index=False)
    pd.DataFrame(items, columns=["pedido_id", "produto_id", "quantidade", "preco_unitario"]).to_sql(
        "itens_pedido", con, index=False)
    pd.DataFrame(reviews, columns=["pedido_id", "nota", "data"]).to_sql("avaliacoes", con, index=False)
    pd.DataFrame(sessions, columns=["data", "sessoes", "pedidos"]).to_sql("sessoes", con, index=False)
    for stmt in ["CREATE INDEX ix_ped_data ON pedidos(data)", "CREATE INDEX ix_itens_ped ON itens_pedido(pedido_id)",
                 "CREATE INDEX ix_ped_cli ON pedidos(cliente_id)"]:
        con.execute(stmt)
    con.commit()
    con.close()
    truth = {
        "categoria_em_queda": {"categoria": "Luminárias", "desde": DECLINE_FROM.isoformat(), "ritmo_mensal_pct": -6},
        "categoria_em_alta": {"categoria": "Têxteis", "ritmo_trimestral_pct": 4},
        "regiao_com_atraso": {"regiao": "Norte", "desde": NORTE_DELAY_FROM.isoformat(),
                              "atraso_extra_dias": "5 a 10", "efeitos": ["nota menor", "menos recompra"]},
        "sazonalidade": ["novembro (Black Friday, 15% de desconto)", "dezembro"],
        "periodo": [START.isoformat(), END.isoformat()],
    }
    (db.parent / "padroes_plantados.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                                                       encoding="utf-8")
    return {"pedidos": len(orders), "itens": len(items), "avaliacoes": len(reviews), "clientes": n_customers}


if __name__ == "__main__":
    print(generate(Path("data/marketplace.db")))

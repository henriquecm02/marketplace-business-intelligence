"""As ferramentas de análise encontram os padrões plantados? Roda sem LLM.

Três cenários: base limpa; base suja (mbi.dirty) carregada sem limpeza; base suja com limpeza.
Uso: python eval/padroes.py   (grava eval/resultados_padroes.json e .md)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mbi import analytics as A  # noqa: E402
from mbi.dirty import sujar  # noqa: E402

DB = ROOT / "data" / "marketplace.db"


def checar(d: A.Data, receita_ref: float | None) -> dict:
    ct = {c["categoria"]: c for c in A.category_trends(d)["categorias"]}
    rd = A.region_delivery(d, "2026-03-01")
    se = A.seasonality(d)
    k = A.kpis(d, "2024-01-01", "2026-10-31")
    norte = rd["regioes"].get("Norte", {})
    casos = {
        "Luminárias classificada como queda": ct.get("Luminárias", {}).get("classificacao") == "queda",
        "Têxteis classificada como alta": ct.get("Têxteis", {}).get("classificacao") == "alta",
        "nenhuma outra categoria em queda": all(c["classificacao"] != "queda" for n, c in ct.items() if n != "Luminárias"),
        "Norte é a região com maior piora de prazo": rd["maior_piora_de_prazo"] == "Norte",
        "nota do Norte cai depois de mar/2026": (norte.get("depois", {}).get("nota_media") or 9) < (norte.get("antes", {}).get("nota_media") or 0),
        "só 5 regiões (sem variantes de escrita)": len(rd["regioes"]) == 5,
        "pico de receita em nov ou dez": se["pico"] in ("nov", "dez"),
        "prazo médio sem valores negativos": (k["prazo_entrega_medio_dias"] or 0) > 0,
    }
    if receita_ref is not None:
        erro = abs(k["receita"] / receita_ref - 1) * 100
        casos["receita total a até 1% da base limpa"] = erro <= 1
    return {"casos": casos, "acertos": sum(casos.values()), "total": len(casos), "receita": k["receita"],
            "erro_receita_pct": round(erro, 2) if receita_ref is not None else None,
            "categorias_queda": [n for n, c in ct.items() if c["classificacao"] == "queda"],
            "regioes": sorted(rd["regioes"]), "qualidade": d.qualidade}


def main() -> int:
    limpa = A.Data(DB)
    res = {"base_limpa": checar(limpa, None)}
    ref = res["base_limpa"]["receita"]
    with tempfile.TemporaryDirectory() as tmp:
        suja = Path(tmp) / "sujo.db"
        injetado = sujar(DB, suja)
        res["problemas_injetados"] = injetado
        res["suja_sem_limpeza"] = checar(A.Data(suja, limpar=False), ref)
        res["suja_com_limpeza"] = checar(A.Data(suja, limpar=True), ref)
    (ROOT / "eval" / "resultados_padroes.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    nomes = list(res["suja_com_limpeza"]["casos"])
    md = ["# Padrões plantados: as ferramentas encontram?\n",
          "Problemas injetados na base suja: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in injetado.items()) + ".\n",
          "| Verificação | Base limpa | Suja, sem limpeza | Suja, com limpeza |", "|---|---|---|---|"]
    for n in nomes:
        linha = [res[c]["casos"].get(n) for c in ("base_limpa", "suja_sem_limpeza", "suja_com_limpeza")]
        md.append(f"| {n} | " + " | ".join("—" if v is None else ("ok" if v else "FALHOU") for v in linha) + " |")
    md.append("| **Acertos** | " + " | ".join(f"{res[c]['acertos']}/{res[c]['total']}"
                                              for c in ("base_limpa", "suja_sem_limpeza", "suja_com_limpeza")) + " |")
    md.append(f"\nErro na receita total: {res['suja_sem_limpeza']['erro_receita_pct']}% sem limpeza e "
              f"{res['suja_com_limpeza']['erro_receita_pct']}% com limpeza.")
    md.append(f"\nCorreções feitas pela limpeza na base suja: {res['suja_com_limpeza']['qualidade']}")
    (ROOT / "eval" / "resultados_padroes.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0 if res["suja_com_limpeza"]["acertos"] == res["suja_com_limpeza"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

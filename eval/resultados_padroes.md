# Padrões plantados: as ferramentas encontram?

Problemas injetados na base suja: pedidos duplicados 866, status variantes 2252, regioes variantes 480, precos x100 169, precos nulos 169, prazos negativos 67.

| Verificação | Base limpa | Suja, sem limpeza | Suja, com limpeza |
|---|---|---|---|
| Luminárias classificada como queda | ok | FALHOU | ok |
| Têxteis classificada como alta | ok | FALHOU | ok |
| nenhuma outra categoria em queda | ok | ok | ok |
| Norte é a região com maior piora de prazo | ok | FALHOU | ok |
| nota do Norte cai depois de mar/2026 | ok | ok | ok |
| só 5 regiões (sem variantes de escrita) | ok | FALHOU | ok |
| pico de receita em nov ou dez | ok | ok | ok |
| prazo médio sem valores negativos | ok | ok | ok |
| receita total a até 1% da base limpa | — | FALHOU | ok |
| **Acertos** | 8/8 | 4/9 | 9/9 |

Erro na receita total: 6.31% sem limpeza e 0.0% com limpeza.

Correções feitas pela limpeza na base suja: {'status_padronizados': 2252, 'regioes_padronizadas': 480, 'pedidos_duplicados_removidos': 866, 'itens_duplicados_removidos': 1276, 'itens_preco_invalido': 169, 'itens_preco_suspeito': 169, 'prazos_negativos_anulados': 66, 'total_de_correcoes': 5278}

# tp2intervals-endure

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Sincronizador automatizado de treinos do **TrainingPeaks** para o **Intervals.icu**.

Desenvolvido pela [Endure.LabbS](https://endure-labbs.github.io/) para gerenciar atletas de endurance (triathlon: natação, corrida, ciclismo).

## Visão Geral

| | |
|---|---|
| **Versão** | 3.1 |
| **Linguagem** | Python 3.11+ |
| **Dependências** | `requests`, `playwright` |
| **Cookie** | Persistente com renovação automática via browser |

## Funcionalidades

- Sincronização automática de treinos TP -> Intervals.icu
- Conversão de workouts estruturados para o Workout Builder do Intervals
- **Natação**: usa labels brutos do TP (A0, A1, AN1...) interpretados via DE:PARA configurado no Intervals
- **Corrida/Bike**: usa zonas estruturadas (Z1-Z7) com mapeamento automatico de % FTP/LTHR -> zona
- Cookie persistente com renovação automatica via Playwright (Chrome headless)
- Deduplicação por `external_id` do TP para evitar duplicação
- Suporte multi-atleta

## Requisitos

- Python 3.11+
- Google Chrome instalado (para renovação de cookie via Playwright)

## Instalação

```bash
pip install requests playwright
playwright install chromium
```

## Uso

```bash
# Sincronizar um athlete (7 dias a frente, 7 dias atras)
python sincronizador.py sync <athlete_key>

# Sincronizar todos os athletes configurados
python sincronizador.py sync-all

# Capturar cookie via browser (sem sincronizar, so para setup inicial)
python sincronizador.py capture <athlete_key>
```

## Configuração de Athletes

Edite o dicionário `ATHLETES` no inicio do `sincronizador.py`:

```python
ATHLETES = {
    "nome-atleta": {
        "tp_username": "usuario_tp",
        "tp_password": "senha_tp",
        "intervals_athlete_id": "i123456",
    },
}
```

## Mapeamento de Zonas

### Corrida/Bike (Z1-Z7)

| % Threshold | Zona TP | Zona Intervals |
|---|---|---|
| 0-77% | Z1 | Z1 |
| 77-87% | Z2 | Z2 |
| 87-94% | Z3 | Z3 |
| 94-100% | Z4 | Z4 |
| 100-105% | Z5A | Z5 |
| 105-120% | Z5B | Z6 |
| 120%+ | Z5C | Z7 |

### Natação (Labels Brutus)

Labels do TP (A0, A1, AN1...) sao passados sem conversao para a description.
O Intervals.icu interpreta via DE:PARA configurado diretamente na plataforma.

## Estrutura

```
sincronizador.py          # Engine principal
tests/
  test_format_duration.py # Testes unitarios (_format_duration)
```

## Autor

Endure.LabbS - [GitHub](https://github.com/endure-labbs)

## Licença

MIT

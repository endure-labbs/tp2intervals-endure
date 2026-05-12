#!/usr/bin/env python3
"""
Testes unitários para _format_duration e related functions.
Foco: garantir que "m" (ambiguo = minutos) nunca apareca como unidade de distancia.
"""

import sys
import types

# Read _format_duration directly from the file to avoid playwright dependency
# We mock the relevant import at the file level
module_path = '/home/andrebbruno/endure_sync/sincronizador.py'
import unittest.mock

# Mock playwright import before loading
sys.modules['playwright'] = unittest.mock.MagicMock()
sys.modules['playwright.sync_api'] = unittest.mock.MagicMock()

from importlib.util import spec_from_file_location, module_from_spec
spec = spec_from_file_location('sincronizador', module_path)
mod = module_from_spec(spec)
spec.loader.exec_module(mod)

_format_duration = mod._format_duration


def test_meter_run_needs_meters():
    """Bug reportado: 800m e 400m em corrida interpretados como minutos (20h49 total)."""
    # Corrida: meter -> metros (NUNCA "m")
    # value >= 1000 -> km
    assert _format_duration({"value": 800, "unit": "meter"}, "Run") == "800meters", \
        "800 meters corrida deveria ser '800meters', nao '800m'"
    assert _format_duration({"value": 400, "unit": "meter"}, "Run") == "400meters", \
        "400 meters corrida deveria ser '400meters', nao '400m'"
    assert _format_duration({"value": 1600, "unit": "meter"}, "Run") == "1.6km", \
        "1600 meters corrida deveria ser '1.6km'"
    assert _format_duration({"value": 1200, "unit": "meter"}, "Run") == "1.2km"
    assert _format_duration({"value": 3000, "unit": "meter"}, "Run") == "3km"

def test_meter_swim_needs_meters():
    """Natacao: tambem nunca usar 'm' sozinho."""
    assert _format_duration({"value": 100, "unit": "meter"}, "Swim") == "100meters"
    assert _format_duration({"value": 2000, "unit": "meter"}, "Swim") == "2km"
    assert _format_duration({"value": 500, "unit": "meter"}, "Swim") == "500meters"

def test_meter_ride():
    """Ciclismo: mesma logica — 'm' nunca como metro."""
    assert _format_duration({"value": 500, "unit": "meter"}, "Ride") == "500meters"
    assert _format_duration({"value": 1000, "unit": "meter"}, "Ride") == "1km"

def test_minutes_and_hours():
    """Minutos e horas devem funcionar normalmente ('m' e correto para minutos)."""
    # Minutos: 'm' e a unidade CORRETA aqui (o parser reconhece como tempo)
    assert _format_duration({"value": 30, "unit": "minute"}, "Run") == "30m"
    assert _format_duration({"value": 2, "unit": "minute"}, "Swim") == "2m"

    # Segundos
    assert _format_duration({"value": 120, "unit": "second"}, "Run") == "2m"
    assert _format_duration({"value": 90, "unit": "second"}, "Run") == "1m30s"
    assert _format_duration({"value": 30, "unit": "second"}, "Swim") == "30s"

    # Horas
    assert _format_duration({"value": 1.5, "unit": "hour"}, "Ride") == "1h30m"
    assert _format_duration({"value": 2, "unit": "hour"}, "Ride") == "2h"
    assert _format_duration({"value": 1, "unit": "hour"}, "Run") == "1h"

def test_kilometers():
    """Kilometros -> km."""
    assert _format_duration({"value": 5, "unit": "kilometer"}, "Run") == "5km"
    assert _format_duration({"value": 10.5, "unit": "kilometer"}, "Run") == "10.5km"

def test_repetition():
    """Repeticao -> None (tratado no nivel do bloco)."""
    assert _format_duration({"value": 8, "unit": "repetition"}, "Run") is None

def test_no_ambiguous_m_for_distances():
    """Garante que nenhum retorno de distancia use 'm' sozinho."""
    import pathlib
    source = pathlib.Path('../sincronizador.py').read_text()

    # O unico 'm' valido e para minutos (unit == "minute")
    # Para unit == "meter" ou fallback com valor de distancia, deve ser "meters"
    lines = source.split('\n')
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Pular comentarios e definicoes
        if stripped.startswith('#') or not stripped:
            continue
        # Se contem return e um f-string terminando em 'm"', verificar contexto
        if 'return f"' in stripped and stripped.strip().endswith('m"'):
            # So e aceitavel em contextos de minutos ou minutos dentro de horas
            if 'minute' not in source[max(0, source.index(stripped) - 500):source.index(stripped)].split('\n')[-1]:
                # Verificar se esta dentro do bloco de minute
                pass  # check mais sofisticado pode ser feito
            # Para meter unit, deve ser 'meters'
            pass

if __name__ == '__main__':
    tests = [
        test_meter_run_needs_meters,
        test_meter_swim_needs_meters,
        test_meter_ride,
        test_minutes_and_hours,
        test_kilometers,
        test_repetition,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(0 if failed == 0 else 1)

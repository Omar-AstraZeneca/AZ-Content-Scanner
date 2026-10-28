from datetime import date

import pytest

from app.scanner.extractors import (
    analyze_text,
    classify_status,
    find_all_approval_ids,
    find_expiry,
    find_veeva_id,
    market_of_approval_id,
)


# ------------------------------------------------------------------ Approval IDs (punto 4)
def test_veeva_labeled():
    vid, conf = find_veeva_id("Veeva Approval ID: Z4-123456 | Uso exclusivo")
    assert vid == "Z4-123456"
    assert conf >= 0.9


def test_veeva_unlabeled_seven_digits():
    vid, conf = find_veeva_id("Material de uso interno MX-1234567 Fecha 2026")
    assert vid == "MX-1234567"
    assert 0.5 <= conf < 0.9


@pytest.mark.parametrize("text,expected,market", [
    ("US-123456", "US-123456", "US"),
    ("Código GB-12345 vigente", "GB-12345", "UK"),
    ("UK-12345", "UK-12345", "UK"),
    ("AU-54321", "AU-54321", "AU"),
    ("CC-11115", "CC-11115", "CAMCAR"),
    ("MX — 12345", "MX-12345", "MX"),          # guion largo / OCR
])
def test_ids_of_all_regions_are_detected(text, expected, market):
    vid, _ = find_veeva_id(text)
    assert vid == expected
    assert market_of_approval_id(vid) == market


@pytest.mark.parametrize("text", [
    "ES-2024",          # prefijo desconocido
    "CO-2024",          # parece un año
    "co-12345",         # minúsculas sin etiqueta
    "XCC-12345",        # parte de otro código
    "CC-1234567890",    # demasiados dígitos
    "256GB-1024",       # pegado a otra palabra
])
def test_false_positive_ids_are_ignored(text):
    assert find_veeva_id(text)[0] is None


def test_labeled_id_accepts_lowercase_prefix():
    assert find_veeva_id("Approval ID: mx-12345")[0] == "MX-12345"


def test_multiple_ids_are_all_returned():
    ids = dict(find_all_approval_ids("MX-12345 y CC-22222"))
    assert set(ids) == {"MX-12345", "CC-22222"}


# ------------------------------------------------------------------ Etiquetas (punto 1)
@pytest.mark.parametrize("text", [
    "Approval date 2023",       # «val» dentro de «appro-val»
    "Our expertise 2022",       # «exp» dentro de «exp-ertise»
    "Interval 2025",
    "Export 2024",
    "Evaluación 2023",
])
def test_words_containing_labels_are_not_expiry(text):
    assert find_expiry(text)[0] is None or find_expiry(text)[2] < 0.6
    assert analyze_text(text)["status"] in ("SIN_DATO", "REVISAR")


def test_expiry_labeled_year():
    year, exact, conf = find_expiry("Fecha de vencimiento: 2026")
    assert year == 2026 and exact is None and conf >= 0.8


def test_expiry_full_date():
    year, exact, conf = find_expiry("Expiry date: 30/06/2025")
    assert exact == date(2025, 6, 30) and year == 2025


def test_expiry_month_year_numeric():
    year, exact, _ = find_expiry("Valid until 12/2027")
    assert year == 2027 and exact == date(2027, 12, 31)


def test_abbreviated_labels():
    assert find_expiry("Exp. 12/2024")[1] == date(2024, 12, 31)
    assert find_expiry("Val.: 10/2026")[1] == date(2026, 10, 31)
    assert find_expiry("Venc. 03/2027")[1] == date(2027, 3, 31)


def test_bare_hasta_is_not_a_label():
    # «Estudio hasta 2024» es un rango de años, no un vencimiento
    assert analyze_text("Estudio hasta 2024")["status"] != "VENCIDO"


# ------------------------------------------------------------------ Fechas de elaboración (punto 2)
@pytest.mark.parametrize("text", [
    "Date of preparation: March 2023",
    "Last updated: January 2022",
    "Fecha de elaboración: 2023",
    "Fecha de aprobación 2022 | Material para profesionales de la salud",
    "Actualizado en 2021",
    "© 2019-2025 AstraZeneca. Todos los derechos reservados.",
    "Copyright AstraZeneca 2025",
    "Texto largo ... © 2027 AstraZeneca",
])
def test_publication_dates_are_not_expiry(text):
    assert analyze_text(text)["status"] == "SIN_DATO"


def test_loose_year_in_footer_is_only_review():
    year, exact, conf = find_expiry("Material promocional. Julio 2024 ... 2027")
    assert year == 2027 and exact is None and conf < 0.6
    assert analyze_text("Sitio institucional 2025")["status"] == "REVISAR"


def test_labeled_expiry_beats_publication_context():
    r = analyze_text("Elaborado: 2023. Válido hasta 2027. MX-12345")
    assert r["expiry_year"] == 2027 and r["status"] == "VIGENTE"


def test_earliest_labeled_date_wins():
    # Conservador: si hay varias fechas de vencimiento se reporta la más temprana
    year, exact, _ = find_expiry("Vence 2025 · Valid until 2027")
    assert year == 2025


# ------------------------------------------------------------------ Meses y orden de fecha (punto 3)
@pytest.mark.parametrize("text,expected", [
    ("Vence marzo 2025", date(2025, 3, 31)),
    ("Vence en febrero de 2028", date(2028, 2, 29)),
    ("Válido até março de 2026", date(2026, 3, 31)),
    ("Expires March 2026", date(2026, 3, 31)),
    ("Expires March 31, 2026", date(2026, 3, 31)),
    ("Vence el 15 de junio de 2026", date(2026, 6, 15)),
    ("Valid until 2026-09-30", date(2026, 9, 30)),
    ("Data de validade: 10/2026", date(2026, 10, 31)),
])
def test_month_names_are_used(text, expected):
    assert find_expiry(text)[1] == expected


def test_month_is_not_lost_when_classifying():
    today = date(2025, 6, 1)
    year, exact, conf = find_expiry("Vence marzo 2025")
    assert classify_status(year, exact, today, conf) == "VENCIDO"   # antes: VIGENTE (31/12)


def test_numeric_date_order_depends_on_market():
    assert find_expiry("Expiry date: 03/04/2026")[1] == date(2026, 4, 3)          # día/mes
    assert find_expiry("Expiry date: 03/04/2026", "US")[1] == date(2026, 3, 4)    # mes/día
    assert find_expiry("Expiry date: 03/04/2026", "UK")[1] == date(2026, 4, 3)


def test_unambiguous_dates_ignore_market():
    assert find_expiry("Exp 25/12/2026", "US")[1] == date(2026, 12, 25)
    assert find_expiry("Exp 12/25/2026")[1] == date(2026, 12, 25)


def test_impossible_date_is_discarded():
    assert find_expiry("Vence 31/02/2026")[1] is None


# ------------------------------------------------------------------ Clasificación
def test_classify_status():
    today = date(2026, 6, 1)
    assert classify_status(2024, None, today) == "VENCIDO"
    assert classify_status(2026, None, today) == "VIGENTE"               # faltan 213 días
    assert classify_status(2026, None, date(2026, 10, 15)) == "VIGENTE"  # faltan 77 (> 60)
    assert classify_status(2026, None, date(2026, 11, 15)) == "PROXIMO_A_VENCER"  # faltan 46
    assert classify_status(2030, None, today) == "VIGENTE"
    assert classify_status(None, None, today) == "SIN_DATO"


def test_low_confidence_is_review_not_expired():
    today = date(2026, 6, 1)
    assert classify_status(2024, None, today, confidence=0.45) == "REVISAR"
    assert classify_status(2024, None, today, confidence=0.85) == "VENCIDO"
    assert classify_status(None, None, today, confidence=0.45) == "SIN_DATO"


def test_analyze_text_empty():
    r = analyze_text("")
    assert r["status"] == "SIN_DATO" and r["expiry_confidence"] == 0.0


def test_analyze_text_exposes_expiry_confidence():
    r = analyze_text("Veeva Approval ID: MX-12345. Fecha de vencimiento: 31/12/2040")
    assert r["veeva_id"] == "MX-12345" and r["expiry_confidence"] == 0.95
    assert r["status"] == "VIGENTE"

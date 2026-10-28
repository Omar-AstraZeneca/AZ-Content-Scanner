"""
Extracción de Veeva Approval ID y fecha/año de vencimiento a partir de texto.

FORMATO DE LOS APPROVAL ID
  prefijo de 2-3 caracteres + guion + 4 a 7 dígitos, p. ej. MX-12345, CC-11115, US-123456.
  El prefijo identifica el mercado (ver DEFAULT_APPROVAL_PREFIXES). Solo se aceptan
  prefijos conocidos para evitar falsos positivos con otros códigos («ES-2024»).
  Para añadir prefijos sin tocar el código: APPROVAL_PREFIXES_EXTRA="XX:MERCADO,YY:MERCADO"
  en el .env.

  ⚠ Los prefijos US, GB, UK, AU y Z4 son una SUPOSICIÓN razonable: confírmalos con
  IDs reales de Veeva de cada mercado y ajusta la lista si difieren.

REGLA DE ESTADO (según la fecha de hoy, ventana fija WARNING_DAYS = 60)
  VENCIDO           -> la fecha de vencimiento ya pasó
  PROXIMO_A_VENCER  -> vence dentro de WARNING_DAYS días
  VIGENTE           -> vence después de WARNING_DAYS días
  REVISAR           -> se detectó una fecha, pero con confianza baja (< REVIEW_CONFIDENCE):
                       la herramienta NO afirma que esté vencido ni vigente
  SIN_DATO          -> no se encontró una fecha utilizable

Si solo se detecta un año, se asume vencimiento el 31 de diciembre de ese año; si se
detecta mes y año («marzo 2025», «03/2025»), el último día de ese mes.
"""
import re
import unicodedata
from datetime import date, timedelta

from app.config import get_settings

# Ventana de "próximo a vencer", en días. Debe coincidir con WARNING_DAYS de app.js
WARNING_DAYS = 60

# Por debajo de esta confianza en la FECHA, el estado es REVISAR (no se afirma nada)
REVIEW_CONFIDENCE = 0.6

# Si solo se detecta el año, se asume el vencimiento el 31 de diciembre de ese año
YEAR_END_MONTH = 12
YEAR_END_DAY = 31

# Mercados que escriben las fechas numéricas como mes/día/año. El resto: día/mes/año.
MONTH_FIRST_MARKETS = {"US"}


# ------------------------------------------------------------------ Veeva ID
DEFAULT_APPROVAL_PREFIXES = {
    # Latinoamérica (confirmados por el equipo)
    "MX": "MX",
    "CC": "CAMCAR",
    "CO": "ANDINO",
    "PE": "ANDINO",
    "EC": "ANDINO",
    "BR": "BR",
    "XC": "SOUTH_CONE",
    "AR": "SOUTH_CONE",
    "CL": "SOUTH_CONE",
    "UY": "SOUTH_CONE",
    # Otras regiones del catálogo (SUPOSICIÓN: validar con IDs reales)
    "US": "US",
    "GB": "UK",
    "UK": "UK",
    "AU": "AU",
    "Z4": "GLOBAL",
}

# Cantidad de dígitos esperada tras el guion
APPROVAL_DIGITS = (4, 7)


def _load_prefixes() -> dict[str, str]:
    prefixes = dict(DEFAULT_APPROVAL_PREFIXES)
    extra = (getattr(get_settings(), "approval_prefixes_extra", "") or "").strip()
    for item in re.split(r"[,;]", extra):
        prefix, _, market = item.strip().partition(":")
        prefix = prefix.strip().upper()
        if re.fullmatch(r"[A-Z0-9]{2,3}", prefix):
            prefixes[prefix] = market.strip().upper() or prefix
    return prefixes


APPROVAL_PREFIXES = _load_prefixes()
_PREFIX_ALTERNATION = "|".join(sorted(APPROVAL_PREFIXES, key=len, reverse=True))
_DIGITS = rf"(\d{{{APPROVAL_DIGITS[0]},{APPROVAL_DIGITS[1]}}})(?![0-9A-Za-z])"
# Guion normal, de Unicode, de OCR (en/em dash, signo menos), con espacio opcional
_SEP = r"\s?[\-‐‑‒–—−]\s?"

# Sin etiqueta: el prefijo debe ir en MAYÚSCULAS (los IDs reales lo están; evita «co-2024»)
APPROVAL_ID_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])({_PREFIX_ALTERNATION}){_SEP}{_DIGITS}"
)

# Etiqueta explícita + ID (confianza máxima): «Veeva Approval ID: CC-11115»
_ID_LABEL = (
    r"(?:veeva\s*(?:approval\s*)?id|approval\s*id|promomats(?:\s*id)?|"
    r"c[oó]digo\s*(?:de\s*)?(?:aprobaci[oó]n|promomats|material)|job\s*(?:code|number)|"
    r"material\s*(?:id|code)|aprobaci[oó]n)"
)
VEEVA_LABELED = re.compile(
    rf"(?<![A-Za-z])(?i:{_ID_LABEL})\s*[:#\-–]?\s*(?i:({_PREFIX_ALTERNATION})){_SEP}{_DIGITS}"
)


# ------------------------------------------------------------------ Año / fecha
YEAR_MIN, YEAR_MAX = 2020, 2049

# Etiquetas que indican VENCIMIENTO. Se exigen límites de palabra: antes «val» y «exp»
# coincidían dentro de «appro-val» o «exp-ertise» y generaban falsos VENCIDO.
_EXPIRY_LABEL = (
    r"(?:expir\w*|exp\.?|expires?|"
    r"venc(?:e|en|imiento|imento|\.)|caduc\w*|"
    r"valid(?:ade|ez)?\s+(?:hasta|at[eé]|until|through|thru|till|to)|"
    r"v[aá]lid[oa]\s+(?:hasta|at[eé]|until)|valid\s+thru|"
    r"validade|val\.|vigencia(?:\s+hasta)?|vigente\s+(?:hasta|at[eé]))"
)
_LABEL_RE = re.compile(
    rf"(?<![A-Za-zÀ-ÿ0-9]){_EXPIRY_LABEL}(?![A-Za-zÀ-ÿ])", re.IGNORECASE
)
_WINDOW = 60  # caracteres tras la etiqueta donde se busca la fecha

# Meses en español, portugués e inglés (sin acentos; se normaliza antes de buscar)
_MONTH_NAMES = {
    1: ["enero", "ene", "january", "jan", "janeiro"],
    2: ["febrero", "feb", "february", "fevereiro", "fev"],
    3: ["marzo", "mar", "march", "março", "marco"],
    4: ["abril", "abr", "april", "apr"],
    5: ["mayo", "may", "maio", "mai"],
    6: ["junio", "jun", "june", "junho"],
    7: ["julio", "jul", "july", "julho"],
    8: ["agosto", "ago", "august", "aug"],
    9: ["septiembre", "setiembre", "sept", "sep", "september", "setembro", "set"],
    10: ["octubre", "oct", "october", "outubro", "out"],
    11: ["noviembre", "nov", "november", "novembro"],
    12: ["diciembre", "dic", "december", "dezembro", "dez"],
}


def _strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", value) if unicodedata.category(c) != "Mn"
    )


_MONTH_LOOKUP = {
    _strip_accents(name): number for number, names in _MONTH_NAMES.items() for name in names
}
_MONTH_ALT = "|".join(
    sorted({n for names in _MONTH_NAMES.values() for n in names}, key=len, reverse=True)
)
_MONTH = rf"({_MONTH_ALT})(?![A-Za-zÀ-ÿ])"
_Y = r"(20[2-4]\d)"
_S = r"[/\-.]"

# Se prueban en este orden, ancladas al inicio de la ventana tras la etiqueta
_DATE_PATTERNS = (
    ("dmy_num", re.compile(rf"(\d{{1,2}}){_S}(\d{{1,2}}){_S}{_Y}(?!\d)")),
    ("iso", re.compile(rf"{_Y}{_S}(\d{{1,2}}){_S}(\d{{1,2}})(?!\d)")),
    ("my_num", re.compile(rf"(\d{{1,2}}){_S}{_Y}(?!\d)")),
    ("dmy_text", re.compile(
        rf"(\d{{1,2}})\s*(?:de\s+|of\s+)?{_MONTH}\.?,?\s*(?:de\s+|del\s+|of\s+)?{_Y}(?!\d)",
        re.IGNORECASE)),
    ("mdy_text", re.compile(
        rf"{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+{_Y}(?!\d)", re.IGNORECASE)),
    ("my_text", re.compile(
        rf"{_MONTH}\.?,?\s*(?:de\s+|del\s+|of\s+)?{_Y}(?!\d)", re.IGNORECASE)),
    ("year", re.compile(rf"{_Y}(?!\d|[/\-.]\d)")),
)

# Separadores y palabras de enlace entre la etiqueta y la fecha («vence el», «válido hasta:»)
_LEAD_SKIP = re.compile(
    r"^(?:[\s:;,.\-–—#=()\[\]|/]"
    r"|(?:en|el|em|a|al|de|del|of|on|in|at|to|the|hasta|until|till|at[eé]|thru|through|date|fecha|data)"
    r"(?![A-Za-zÀ-ÿ]))*",
    re.IGNORECASE,
)

# Confianza según la precisión de lo detectado
_CONFIDENCE = {
    "dmy_num": 0.95, "iso": 0.95, "my_num": 0.95,
    "dmy_text": 0.95, "mdy_text": 0.95, "my_text": 0.90, "year": 0.85,
}

# --- Año suelto en el pie de página (baja confianza) ---
FOOTER_YEAR = re.compile(r"(?<![\d/\-.])(20[2-4]\d)(?![\d/\-.])")

# «© 2019-2025», «Copyright AstraZeneca 2025»: año de publicación del sitio, no vencimiento
_COPYRIGHT_SPAN = re.compile(
    r"(?:©|\(c\)|copyright)\D{0,40}20[2-4]\d(?:\s*[-–—,]\s*20[2-4]\d)*", re.IGNORECASE
)

# Un año precedido por estas palabras es una fecha de elaboración/actualización/aprobación,
# NO un vencimiento («Date of preparation: March 2023», «Last updated: January 2022»)
_PUBLICATION_CONTEXT = re.compile(
    r"(?:prepar\w*|elabor\w*|aprob\w*|approv\w*|creat\w*|cread\w*|creaci\w*|"
    r"actualiz\w*|atualiz\w*|updat\w*|revis\w*|publi\w*|emisi\w*|emitid\w*|issued|"
    r"fecha\s+de|data\s+de|date\s+of|©|\(c\)|copyright)",
    re.IGNORECASE,
)


def _format_id(prefix: str, digits: str) -> str:
    return f"{prefix.upper()}-{digits}"


def find_all_approval_ids(text: str) -> list[tuple[str, float]]:
    """Devuelve todos los Approval ID del texto como (id, confianza), sin duplicados.

    Un material puede mostrar más de un ID (por ejemplo, uno por país); por eso
    se devuelven todos y no solo el primero.
    """
    if not text:
        return []
    found: dict[str, float] = {}
    for match in VEEVA_LABELED.finditer(text):
        found[_format_id(match.group(1), match.group(2))] = 0.95
    for match in APPROVAL_ID_PATTERN.finditer(text):
        digits = match.group(2)
        # Sin etiqueta, «CO-2024» es casi seguro un año, no un ID
        if len(digits) == 4 and 1990 <= int(digits) <= 2049:
            continue
        found.setdefault(_format_id(match.group(1), digits), 0.75)
    return list(found.items())


def find_veeva_id(text: str) -> tuple[str | None, float]:
    """Devuelve el Approval ID principal (id, confianza). Prefiere el que lleva etiqueta."""
    ids = find_all_approval_ids(text)
    if not ids:
        return None, 0.0
    return max(ids, key=lambda item: item[1])


def market_of_approval_id(approval_id: str | None) -> str | None:
    """Mercado al que pertenece un Approval ID según su prefijo (CC -> CAMCAR, etc.)."""
    if not approval_id or "-" not in approval_id:
        return None
    return APPROVAL_PREFIXES.get(approval_id.split("-", 1)[0].upper())


def _last_day_of_month(year: int, month: int) -> date:
    """Último día de un mes (resuelve correctamente diciembre)."""
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _day_month(a: int, b: int, year: int, month_first: bool) -> date | None:
    """Resuelve «a/b/año». Si un valor es > 12 no hay ambigüedad; si no, manda el mercado."""
    if a > 12 and b <= 12:
        return _safe_date(year, b, a)
    if b > 12 and a <= 12:
        return _safe_date(year, a, b)
    return _safe_date(year, a, b) if month_first else _safe_date(year, b, a)


def _parse_full_date(raw: str, month_first: bool = False) -> date | None:
    """Compatibilidad: analiza «dd/mm/yyyy», «yyyy-mm-dd» o «mm/yyyy»."""
    nums = re.split(r"[/\-.]", raw.strip())
    try:
        if len(nums) == 3:
            a, b, c = (int(x) for x in nums)
            if a > 1900:
                return _safe_date(a, b, c)
            return _day_month(a, b, c, month_first)
        if len(nums) == 2:
            m, y = int(nums[0]), int(nums[1])
            return _last_day_of_month(y, m) if 1 <= m <= 12 else None
    except (ValueError, OverflowError):
        return None
    return None


def _parse_after_label(window: str, month_first: bool) -> tuple[int, date | None, float] | None:
    """Interpreta la fecha que sigue a una etiqueta de vencimiento -> (año, fecha, confianza)."""
    window = window[_LEAD_SKIP.match(window).end():]
    for kind, pattern in _DATE_PATTERNS:
        m = pattern.match(window)
        if not m:
            continue
        g = m.groups()
        exact: date | None = None
        if kind == "dmy_num":
            exact = _day_month(int(g[0]), int(g[1]), int(g[2]), month_first)
        elif kind == "iso":
            exact = _safe_date(int(g[0]), int(g[1]), int(g[2]))
        elif kind == "my_num":
            month = int(g[0])
            exact = _last_day_of_month(int(g[1]), month) if 1 <= month <= 12 else None
        elif kind == "dmy_text":
            exact = _safe_date(int(g[2]), _MONTH_LOOKUP[_strip_accents(g[1].lower())], int(g[0]))
        elif kind == "mdy_text":
            exact = _safe_date(int(g[2]), _MONTH_LOOKUP[_strip_accents(g[0].lower())], int(g[1]))
        elif kind == "my_text":
            exact = _last_day_of_month(int(g[1]), _MONTH_LOOKUP[_strip_accents(g[0].lower())])
        else:  # year
            return int(g[0]), None, _CONFIDENCE["year"]
        if exact is None:          # fecha imposible (31/02): se descarta
            return None
        return exact.year, exact, _CONFIDENCE[kind]
    return None


def _footer_year(text: str) -> int | None:
    """Año suelto en los últimos 400 caracteres, descartando copyright y fechas de publicación."""
    tail = text[-400:]
    copyright_spans = [m.span() for m in _COPYRIGHT_SPAN.finditer(tail)]
    years = []
    for m in FOOTER_YEAR.finditer(tail):
        year = int(m.group(1))
        if not YEAR_MIN <= year <= YEAR_MAX:
            continue
        if any(start <= m.start() < end for start, end in copyright_spans):
            continue
        if _PUBLICATION_CONTEXT.search(tail[max(0, m.start() - 45):m.start()]):
            continue
        years.append(year)
    return max(years) if years else None


def find_expiry(text: str, market: str | None = None) -> tuple[int | None, date | None, float]:
    """Devuelve (año, fecha_exacta_si_existe, confianza).

    1) Fecha tras una etiqueta de vencimiento (confianza 0.85-0.95). Si hay varias en el
       texto se elige la más temprana: es la opción conservadora para compliance.
    2) Año suelto en el pie, sin copyright ni fechas de elaboración (confianza 0.45 -> REVISAR).
    `market` define el orden de las fechas numéricas ambiguas (US: mes/día; resto: día/mes).
    """
    if not text:
        return None, None, 0.0
    month_first = (market or "").upper() in MONTH_FIRST_MARKETS

    candidates = []
    for m in _LABEL_RE.finditer(text):
        parsed = _parse_after_label(text[m.end():m.end() + _WINDOW], month_first)
        if parsed:
            candidates.append(parsed)
    if candidates:
        year, exact, conf = min(
            candidates, key=lambda c: (effective_expiry_date(c[0], c[1]), -c[2])
        )
        return year, exact, conf

    year = _footer_year(text)
    if year:
        return year, None, 0.45
    return None, None, 0.0


def effective_expiry_date(year: int | None, exact: date | None) -> date | None:
    """Fecha de vencimiento efectiva.

    - Si hay fecha exacta (incluye «mes/año» -> último día del mes), se usa tal cual.
    - Si solo hay año, se asume el final de ese año (31 de diciembre).
    """
    if exact:
        return exact
    if year:
        try:
            return date(year, YEAR_END_MONTH, YEAR_END_DAY)
        except ValueError:
            return date(year, 12, 31)
    return None


def days_until_expiry(year: int | None, exact: date | None, today: date | None = None) -> int | None:
    """Días entre hoy y el vencimiento. Negativo = ya venció. None = sin fecha."""
    effective = effective_expiry_date(year, exact)
    if effective is None:
        return None
    return (effective - (today or date.today())).days


def classify_status(
    year: int | None,
    exact: date | None,
    today: date | None = None,
    confidence: float | None = None,
) -> str:
    """Clasifica según la fecha de hoy y la ventana WARNING_DAYS (60 días).

        sin fecha                      -> SIN_DATO
        confianza < REVIEW_CONFIDENCE  -> REVISAR (no se afirma vencido ni vigente)
        días < 0                       -> VENCIDO
        0 <= días <= WARNING_DAYS      -> PROXIMO_A_VENCER
        días > WARNING_DAYS            -> VIGENTE

    `confidence` es la confianza en la FECHA; si es None no se aplica el filtro.
    """
    days = days_until_expiry(year, exact, today)
    if days is None:
        return "SIN_DATO"
    if confidence is not None and confidence < REVIEW_CONFIDENCE:
        return "REVISAR"
    if days < 0:
        return "VENCIDO"
    if days <= WARNING_DAYS:
        return "PROXIMO_A_VENCER"
    return "VIGENTE"


def analyze_text(text: str, market: str | None = None) -> dict:
    """Análisis completo de un bloque de texto. `market` (p. ej. «US») ordena las fechas."""
    vid, vconf = find_veeva_id(text)
    year, exact, yconf = find_expiry(text, market)

    # Confianza global: promedio si se encontraron ambos datos; si no, el que exista
    if vid and year:
        confidence = (vconf + yconf) / 2
    else:
        confidence = max(vconf, yconf)

    return {
        "veeva_id": vid,
        "veeva_market": market_of_approval_id(vid),
        "expiry_year": year,
        "expiry_date": exact,
        "confidence": round(confidence, 2),
        "expiry_confidence": round(yconf, 2),
        "status": classify_status(year, exact, confidence=yconf if year else None),
    }
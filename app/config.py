from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/scanner.db"

    year_end_month: int = 12
    year_end_day: int = 31

    # Crawler. 0 = sin límite.
    max_pages_per_scan: int = 0
    max_depth: int = 0
    page_timeout_ms: int = 45000
    request_delay_seconds: float = 1.0
    headless: bool = True
    # Debe parecer un navegador real: los WAF (AEM/Akamai) bloquean UA de bots.
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    tesseract_cmd: str = ""
    ocr_langs: str = "eng+spa+por"
    ocr_bottom_strip: float = 0.30
    min_image_width: int = 200
    min_image_height: int = 100

    enable_vlm: bool = False
    vlm_endpoint: str = "http://localhost:11434/api/generate"
    vlm_model: str = "llava"

    enable_llm_summary: bool = False
    llm_model: str = "llama3"

    enable_kaltura: bool = False
    kaltura_service_url: str = "https://www.kaltura.com"
    kaltura_partner_id: int = 0
    kaltura_admin_secret: str = ""

    enable_veeva: bool = False
    veeva_dns: str = ""
    veeva_username: str = ""
    veeva_password: str = ""
    veeva_api_version: str = "v24.1"

    # --- Login del crawler (opcionales; solo con autorización de IT/Seguridad) ---
    # Recomendado: sesión guardada con scripts/save_session.py (admite MFA).
    crawler_login_username: str = ""
    crawler_login_password: str = ""
    # Hosts adicionales del Identity corporativo (coma). Los conocidos ya vienen incluidos.
    crawler_identity_hosts: str = ""
    crawler_login_url: str = ""
    # Hosts adicionales (además de CRAWLER_LOGIN_URL) donde el crawler puede enviar el login
    crawler_login_hosts: str = ""
    crawler_login_field_user: str = ""
    crawler_login_field_password: str = ""
    crawler_login_submit: str = ""
    crawler_mfa_field: str = ""
    crawler_mfa_code: str = ""
    # Heredadas: siguen funcionando, pero usa las CRAWLER_LOGIN_* de arriba
    crawler_http_username: str = ""
    crawler_http_password: str = ""

    # --- Login guiado de sesión (AzMed / Veeva / Kaltura) ---
    # URL que se abre en cada paso. Cámbialas si el portal de tu mercado usa otra entrada.
    auth_azmed_url: str = "https://www.azmed.cr/"
    auth_veeva_url: str = (
        "https://login.veevavault.com/auth/login"
        "?retURL=https%3A%2F%2Fastrazeneca-promomats.veevavault.com%2Fui%2F"
    )
    auth_kaltura_url: str = "https://kmc.kaltura.com/index.php/kmcng/login"
    # Navegador del login: auto (Chrome/Edge instalado, luego Chromium),
    # chromium, chrome o edge. Safari no se puede automatizar con Playwright.
    auth_browser: str = "auto"
    # Tiempo máximo por plataforma y para toda la ventana de login.
    auth_step_timeout_seconds: int = 300
    auth_prep_max_seconds: int = 1200

    # Si tantas páginas SEGUIDAS exigen iniciar sesión, se detiene el escaneo. 0 = nunca se detiene.
    # Por defecto 0: un sitio con muchas áreas protegidas NO debe abortar el rastreo público.
    crawler_max_auth_pages: int = 0
    # Si el total de páginas protegidas supera este valor, se detiene aunque no sean seguidas. 0 = sin tope.
    crawler_max_auth_total: int = 0

    # --- Seguridad ---
    # Contraseña del dashboard/API (HTTP Basic). Si está vacía, SOLO se acepta acceso desde
    # esta misma máquina (loopback) y se rechaza cualquier petición con cabeceras de proxy.
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    # Valores permitidos de la cabecera Host (protege contra DNS rebinding), separados por coma
    allowed_hosts: str = "localhost,127.0.0.1,[::1],testserver"
    # Permite rastrear/descargar hosts que resuelven a redes privadas (VPN, sitios internos).
    # Por defecto NO: es la barrera principal contra SSRF.
    allow_private_networks: bool = False

    # Prefijos de Approval ID adicionales, formato "PREFIJO:MERCADO,PREFIJO:MERCADO"
    approval_prefixes_extra: str = ""

    manual_minutes_per_page: float = 6.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
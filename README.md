# AZ Content Expiry Scanner

Piloto interno para encontrar material digital de AstraZeneca que pueda estar vencido o próximo a vencer. El escáner relaciona páginas, imágenes, videos y PDFs con Veeva Approval ID y fechas visibles, y centraliza los hallazgos en un dashboard.

El catálogo inicial contiene 8 regiones y 20 sitios semilla. Es un inventario inicial, no un registro exhaustivo de todos los dominios, marcas, áreas terapéuticas o programas de pacientes.

## Problema

El material promocional y médico puede estar publicado en múltiples sitios regionales y de marca. Sus códigos y fechas pueden aparecer en texto, en letra pequeña dentro de imágenes o en metadatos de video. La revisión manual página por página es lenta y dificulta priorizar vencimientos, comparar mercados y conservar evidencia consistente.

El riesgo que aborda este piloto es identificar material que merece revisión humana. El resultado del escáner no reemplaza la aprobación de Compliance, la validación en Veeva ni la decisión de retirar contenido.

## Solución

El flujo procesa cada sitio autorizado, registra una ejecución y sus activos detectados, y presenta resultados filtrables por región, país, categoría, estado y tipo.

| Componente | Función | Disponibilidad |
| --- | --- | --- |
| Crawler Playwright | Renderiza JavaScript, recorre enlaces permitidos, extrae texto y guarda capturas de página | Implementado |
| Extracción de texto | Busca Approval IDs y fechas/años de vencimiento con patrones configurables | Implementado; patrones deben calibrarse con ejemplos reales |
| OCR | Analiza primero la franja inferior de imágenes y después la imagen completa con Tesseract | Implementado; requiere Tesseract y sus idiomas |
| VLM local | Puede intentar extraer ID/año de una imagen cuando falla el OCR | Opcional, desactivado por defecto; compatible con endpoint tipo Ollama |
| Kaltura | Detecta entry IDs y, si se configura la API, consulta nombre, descripción, tags y fecha de fin | Opcional; no analiza el contenido audiovisual del video |
| Veeva Vault | Busca un documento por Approval ID y puede priorizar su fecha de expiración real | Opcional; requiere credenciales, permisos y ajustar la consulta/campos al Vault |
| Clasificación | Asigna `VENCIDO`, `PROXIMO_A_VENCER`, `VIGENTE` o `SIN_DATO` | Implementado |
| Recomendación | Sugiere eliminar/actualizar, planificar renovación o revisar manualmente | Reglas deterministas; no es una decisión automática de Compliance |
| Interfaz única | Escaneo, KPIs, métricas, validación, historial, evidencia y exportación CSV/PDF en una sola página | Implementado |
| Validación piloto | Guarda revisiones correctas/incorrectas y falsos negativos para calcular métricas | Implementado |

El estado **Vigente** se analiza y se incluye en su KPI, filtro y gráfico. Se asigna cuando la fecha efectiva de expiración está a más de `WARNING_DAYS` desde hoy. Si solo se detecta el año, la fecha efectiva se asume el 31 de diciembre (`YEAR_END_MONTH`/`YEAR_END_DAY`). Una cifra cero significa que el último conjunto analizado no produjo activos clasificables como vigentes; no significa que ese estado se haya omitido. `SIN_DATO` identifica páginas o activos donde no se encontró una fecha utilizable.

La IA es opcional. La configuración inicial apunta a modelos locales; si se configura un endpoint remoto, el contenido enviado a ese endpoint deja de ser local. OCR, reglas y recomendaciones básicas funcionan sin un LLM.

## Regiones y sitios

El catálogo está en [`data/markets.json`](data/markets.json). Cada región contiene países, sitios tipificados por país/categoría y una `domain_allowlist`.

Regiones actuales:

- `GLOBAL`: corporativo, ensayos clínicos y sitio de marca HCP.
- `US`: Estados Unidos.
- `UK`: Reino Unido.
- `BR`: Brasil.
- `MX`: México.
- `ANDINO`: Colombia, Ecuador y Perú.
- `CAMCAR`: Belice, Costa Rica, El Salvador, Guatemala, Honduras, Nicaragua y Panamá.
- `AU`: Australia.

ANDINO y CAMCAR son agrupaciones operativas del piloto. La lista de países no significa que todos sus portales estén ya identificados. Para registrar un sitio verificado, añade un objeto `url`, `country` y `category` a `sites`, e incluye su dominio en `domain_allowlist`.

En el dashboard también se puede pegar una URL no catalogada. Se debe seleccionar una región y confirmar que el dominio está autorizado. El backend restringe esa ejecución al host introducido; no amplía automáticamente el rastreo a otros sitios. No se infieren dominios usando patrones de nombres.

## Uso del dashboard

1. Inicia la aplicación y abre `http://localhost:8000`.
2. El dashboard único presenta primero el escaneo y sus resultados; las cinco gráficas están juntas en un solo bloque, antes del detalle de activos e historial. Las métricas/validación quedan al final como tareas secundarias.
3. Selecciona región y país; busca el sitio por nombre/dominio y selecciónalo. También puedes pegar una URL y especificar la categoría.
4. Para un dominio fuera de la lista autorizada de la región, marca la confirmación que aparece bajo la URL.
5. Pulsa **Escanear sitio**. El panel de progreso muestra páginas analizadas, fallidas y tiempo transcurrido; como los enlaces se descubren durante el rastreo, el avance es indeterminado hasta completar. Consulta el resultado en **Historial de escaneos**; **Ver análisis** abre el detalle de cada ejecución.
6. Los indicadores de estado llevan al detalle filtrado. **Filtros adicionales** permite acotar por estado, tipo o URL/Veeva ID; abre **Detalle de activos** para consultar los resultados y evidencias.
7. En **Métricas y validación**, valida detecciones, registra falsos negativos y exporta las métricas a CSV o mediante impresión/PDF.

### Cómo leer las gráficas

- **Activos por estado y región:** eje X = región; eje Y = número de activos. Las barras apiladas y su leyenda separan los cuatro estados.
- **Distribución por tipo de activo:** gráfico circular sin ejes; la leyenda identifica el tipo y el tooltip muestra cantidad y porcentaje.
- **Activos por año de vencimiento:** eje X = año de vencimiento identificado; eje Y = número de activos. No incluye activos sin año detectado.
- **Precisión por método:** eje X = método de detección; eje Y = porcentaje de detecciones revisadas manualmente que fueron correctas.
- **Tiempo manual estimado y escáner:** barras horizontales; eje X = tiempo en horas, eje Y = proceso. La revisión manual es una estimación basada en `MANUAL_MINUTES_PER_PAGE`.

**Limpiar filtros** restablece la selección y no borra runs, activos, métricas ni capturas almacenadas. La URL manual no inicia sesión por sí sola; la autenticación opcional para formularios/SSO y AzMed se configura en [Autenticación del crawler](#autenticación-del-crawler).
Los errores de escaneo, correcciones manuales y confirmaciones se muestran en diálogos con el estilo del dashboard, no en ventanas nativas del navegador.

## Instalación

Requisitos: Python 3.11 o posterior, Tesseract OCR y Chromium instalado por Playwright. Para el login puedes usar Chrome o Edge instalados; el Chromium incluido sigue siendo el respaldo.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

En Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
```

Instala Tesseract y los idiomas configurados en `OCR_LANGS` (`eng+spa+por`). 
- Windows: [UB Mannheim builds](https://github.com/UB-Mannheim/tesseract/wiki). Configura `TESSERACT_CMD` con la ruta a `tesseract.exe` si no está en `PATH`.
- macOS: `brew install tesseract tesseract-lang`
- Linux: instala `tesseract-ocr` y los paquetes de idioma correspondientes

`TESSERACT_CMD` puede quedar vacío si el ejecutable está en `PATH`.


Edita únicamente el `.env` local para secretos. **No guardes usuarios, contraseñas, tokens ni claves en `.env.example`, `markets.json` o el código.** El archivo `.env` está excluido por `.gitignore`.

## Configuración opcional

### Visión y resumen local

Instala Ollama y los modelos elegidos, por ejemplo `llava` para visión y `llama3` para resumen. Después configura en `.env`:

```dotenv
ENABLE_VLM=true
VLM_ENDPOINT=http://localhost:11434/api/generate
VLM_MODEL=llava
ENABLE_LLM_SUMMARY=true
LLM_MODEL=llama3
```

El VLM recibe la imagen como entrada y debe devolver JSON con `veeva_id`, `expiry_year` y `confidence`. El resumen usa el endpoint configurado en `VLM_ENDPOINT`.

### Autenticación del crawler

**Recomendado (SSO/MFA):** inicia sesión una vez, a mano, y reutiliza la sesión.

El dashboard **no puede leer las cookies** de `azmed.cr`, `veevavault.com` ni `kaltura.com`: la política de mismo origen y las cookies `HttpOnly` lo impiden. Tampoco se pueden incrustar esos portales en un iframe o pop-up por `X-Frame-Options`/CSP. Por eso el login se hace en una ventana controlada por Playwright, que exporta `storage_state` a `data/auth_state.json`.

**Navegador del login.** Playwright puede controlar el Chromium incluido o los Chrome/Edge instalados en el sistema. El selector de la pantalla inicial usa `AUTO` por defecto: prefiere **Google Chrome**, luego **Microsoft Edge** y, si ninguno está instalado, el Chromium incluido. También puedes fijar `AUTH_BROWSER=chromium|chrome|edge` en `.env` o pasar `--browser` a `scripts/save_session.py`. **Safari no se puede automatizar** con Playwright para exportar la sesión; en macOS se ofrece Chrome, Edge o Chromium y la interfaz lo explica.

**Flujo guiado desde el dashboard:**

1. Al abrir la aplicación aparece como pantalla inicial el diálogo **“Inicia sesión para auditar contenido protegido”**. Muestra el estado de AzMed, Veeva y Kaltura, el selector de navegador y los botones **Iniciar sesión** / **Continuar sin logueo**.
2. **Iniciar sesión** abre una sola ventana del navegador elegido y recorre **AzMed → Veeva → Kaltura**. La app detecta cuándo completaste el login de cada plataforma, cierra esa pestaña y pasa a la siguiente; al terminar guarda la sesión y cierra el navegador sola. La pantalla inicial muestra el paso actual y permite **Omitir paso actual** o **Detener**.
3. **Continuar sin logueo** cierra la pantalla inicial y permite usar el escáner. Al escanear un sitio protegido, la app vuelve a ofrecer **Iniciar sesión** o **Continuar sin logueo** como respaldo. Sin sesión solo se recorre el contenido público y las páginas protegidas se registran como no auditables (`use_login=false`, `use_saved_session=false`).
4. El backend solo lanza, consulta y detiene ese proceso; nunca maneja credenciales desde el navegador del dashboard.

**Kaltura (KMC) usa un token KS, no cookies.** Tras el login, KMC guarda el token en `sessionStorage` (`auth.login.ks`) y en `window.kmcConfig.ks`. Playwright no exporta `sessionStorage`, así que el escáner lo captura de la página o de las peticiones de red y lo guarda aparte en `data/kaltura_session.json` (permisos `0600`, caducidad máxima de 24 h). En cada contexto del crawler se inyecta antes de cargar KMC para reutilizar la sesión. El valor del KS nunca se muestra en la interfaz ni se escribe en los logs.

**Modo clásico desde la terminal** (todas las plataformas en pestañas, guardado cada 5 s y cierre manual del navegador):

```bash
.venv\Scripts\python.exe scripts/save_session.py
```

Se abre una ventana del navegador con una pestaña por plataforma:

- AzMed / IDAZ (Identity corporativo)
- Veeva Vault (PromoMats)
- Kaltura (KMC)

Inicia sesión con SSO/MFA en cada una. La sesión se guarda automáticamente cada 5 segundos en `data/auth_state.json` y el token KS de KMC en `data/kaltura_session.json`; puedes cerrar el navegador cuando termines. El crawler combina ese archivo con las sesiones antiguas de `data/` (`auth_state_promomats.json`, `auth_state_veeva.json`, `auth_state_kaltura.json`), así que no se pierden las cookies de otras plataformas.

Una misma sesión sirve para **cualquier sitio del catálogo**: AzMed, IDAZ, Veeva, Kaltura o dominios de marca. En el siguiente escaneo el crawler carga las cookies, el `localStorage` y el KS de KMC en cada contexto y permite las redirecciones de los proveedores de identidad. Protege `data/auth_state.json` y `data/kaltura_session.json` como secretos (se recomienda `chmod 600`).

La cobertura se calcula con cookies de sesión reales (y el KS para Kaltura), no con cualquier cookie del dominio: se ignoran `__cf_bm`, `AWSALB`, analítica y el `JSESSIONID` de la pantalla de login de Veeva. Si una plataforma aparece sin cubrir, vuelve a abrir el login y completa ese paso. Los sitios marcados con `requires_login` (o cuya categoría sea Portal/IDAZ/Programa de pacientes) activan el aviso al escanear; puedes forzarlo en `data/markets.json` con `"requires_login": true|false`.

Para sitios autorizados con formularios web o credenciales de servicio (sin MFA), configura credenciales genéricas del sitio y, si aplica, un código de MFA temporal:


```dotenv
CRAWLER_LOGIN_URL=https://<sitio-autorizado>/login
CRAWLER_LOGIN_USERNAME=
CRAWLER_LOGIN_PASSWORD=
CRAWLER_MFA_CODE=
```

El crawler intentará detectar campos comunes de usuario/contraseña, enviar el formulario y, si aparece un código de verificación, completar el campo OTP con `CRAWLER_MFA_CODE`. El flujo sigue siendo opt-in y solo se activa para sitios autorizados. Para AzMed, la compatibilidad específica sigue usando `CRAWLER_HTTP_USERNAME` y `CRAWLER_HTTP_PASSWORD`.

### HTTP Basic del crawler

Solo para sitios autorizados que usen el diálogo HTTP Basic del navegador:

```dotenv
CRAWLER_HTTP_USERNAME=
CRAWLER_HTTP_PASSWORD=
```

Completa esos campos directamente en `.env` o usa un gestor de secretos en despliegues. No intentes eludir controles de acceso ni forzar la autenticación a dominios no autorizados.

### Kaltura

La integración está desactivada por defecto. Habilítala con `ENABLE_KALTURA=true`, partner ID y secreto en `.env`, e instala el cliente opcional `KalturaApiClient`. Sin API, el escáner solo registra los entry IDs detectados y no obtiene fechas del video.

### Veeva Vault

La integración está desactivada por defecto. Configura `ENABLE_VEEVA`, `VEEVA_DNS`, usuario, contraseña y versión API en `.env`. `VQL_TEMPLATE` y los campos de respuesta de `app/scanner/veeva_client.py` son ejemplos que deben adaptarse y probarse contra el Vault autorizado.

## Ejecución

```bash
# Dashboard y API local
# IMPORTANTE: sin --reload. El recargador reinicia el servidor en mitad de un
# escaneo y lo deja como ERROR con el resumen vacío.
uvicorn app.main:app --port 8000

# Escanear una región o todas las regiones configuradas
python run_scan.py --market CAMCAR
python run_scan.py --market ANDINO
python run_scan.py --all
```

`--all` inicia los sitios definidos de cada región. Para un solo sitio o una URL manual, utiliza el dashboard. El crawler respeta la allowlist, los límites de páginas/profundidad y el retraso configurado.

## Datos, evidencia y privacidad

- Por defecto, los runs, activos, validaciones y falsos negativos se guardan en SQLite: `data/scanner.db`.
- Las capturas de página se escriben en `data/evidence/run_<id>/` y los activos apuntan a la ruta de evidencia.
- La vista del navegador empieza vacía y no se guarda en `localStorage`; los datos del servidor persisten entre sesiones y pestañas.
- Cerrar una pestaña o pulsar **Limpiar vista** no elimina información de SQLite ni archivos de evidencia.
- No existe todavía un botón de borrado permanente, retención automática ni autenticación/roles del dashboard.
- Las capturas pueden contener información visible en la página completa. Confirma la autorización, minimiza datos sensibles y acuerda retención antes de usar sitios internos o autenticados.
- La aplicación de desarrollo se sirve localmente. Para compartirla, despliega una instancia central con HTTPS, autenticación corporativa, permisos, backups y almacenamiento compartido.

## Métricas del piloto

Las métricas no deben presentarse como resultados validados hasta revisar una muestra representativa.

- **Precisión:** TP / (TP + FP), donde las detecciones se etiquetan como correctas o incorrectas.
- **Recall:** TP / (TP + FN); los falsos negativos se registran en el formulario de validación.
- **F1:** balance entre precisión y recall.
- **Horas ahorradas estimadas:** páginas escaneadas × `MANUAL_MINUTES_PER_PAGE`, menos el tiempo real del escaneo, expresado en horas.

Recomendación: medir el tiempo manual con una muestra, validar texto/imágenes/PDF/video y hacer parte de la revisión manual a ciegas. Documenta falsos positivos, falsos negativos, bloqueos HTTP y páginas no cubiertas.

## Limitaciones y resultados esperados

El piloto debe demostrar visibilidad centralizada por región/tipo/estado, detección priorizable, calidad frente a revisión manual, ahorro de tiempo y evidencia accionable. **No se publican cifras de precisión, recall o ROI hasta completar y documentar esa validación.**

Limitaciones conocidas:

- Sitios con login de formulario, SSO o MFA no están soportados por el flujo HTTP Basic.
- Algunos servidores pueden devolver `403` al descargar imágenes o PDFs; una página visitada no garantiza que todos sus recursos se analizaran.
- El video no se transcribe ni se inspecciona cuadro a cuadro; Kaltura aporta metadatos solo si la API está habilitada.
- Los patrones de Approval ID, etiquetas y campos Veeva deben calibrarse por mercado/Vault.
- SQLite está pensado para el piloto local; un despliegue multiusuario requiere base de datos compartida, autenticación y política de retención.
- No hay alertas, ejecución programada ni botón de purga de detalle en la versión actual.

## Relación con Website Scan App / AtomAZer

Según la captura compartida, **Website Scan App** parece aceptar solicitudes de escaneo y mostrar/descargar un informe por sitio. Las capacidades que no sean visibles en esa captura quedan **por confirmar** con sus responsables.

Este proyecto se centra específicamente en expiración de contenido, Approval IDs, OCR/VLM de imágenes, metadatos de Kaltura, enriquecimiento Veeva y validación de métricas. Podrían complementarse; el posible solapamiento debe confirmarse con los propietarios de ambas herramientas.

## API local

- `GET /api/scan/markets`: regiones y sitios configurados.
- `POST /api/scan/start/{region}`: iniciar escaneo; el body opcional admite URL, país, categoría y confirmación de alcance externo.
- `GET /api/scan/runs`: historial de ejecuciones.
- `GET /api/assets` y `GET /api/assets/summary`: detalle y agregados filtrables.
- `GET /api/assets/export.csv`: exportación.
- `POST /api/assets/{id}/validate` y `POST /api/assets/missed`: captura de validación manual.
- `GET /api/metrics/pilot`: métricas del piloto.
- `GET /api/metrics/pilot/export.csv`: exportación CSV de métricas.

## Pruebas

```bash
pytest
```
## Seguridad

La herramienta rastrea sitios, puede iniciar sesión con credenciales de servicio y guarda capturas, por lo que está endurecida por defecto.

| Protección | Qué hace | Ajuste |
| --- | --- | --- |
| Acceso local por defecto | Sin `DASHBOARD_PASSWORD`, solo acepta conexiones desde la propia máquina y rechaza peticiones con cabeceras de proxy (`X-Forwarded-For`, etc.) | `DASHBOARD_PASSWORD`, `DASHBOARD_USER` |
| Autenticación | HTTP Basic sobre dashboard y API, comparación en tiempo constante y bloqueo tras 10 intentos fallidos (5 min) | `DASHBOARD_PASSWORD` |
| Host permitido | Rechaza cabeceras `Host` desconocidas (anti DNS-rebinding) | `ALLOWED_HOSTS` |
| Anti-CSRF | Las peticiones que modifican datos deben ser del mismo origen | — |
| Anti-SSRF | Las URLs externas se **resuelven por DNS** y todas las IPs deben ser públicas; sin `usuario:clave@`, solo puertos 80/443; cada redirección se revalida; en escaneos externos el navegador revalida la IP de cada petición | `ALLOW_PRIVATE_NETWORKS` |
| Credenciales acotadas | El login del crawler solo se escribe en `CRAWLER_LOGIN_URL`/`CRAWLER_LOGIN_HOSTS`, una vez por origen, y nunca en escaneos de URLs externas | `CRAWLER_LOGIN_HOSTS` |
| Revisor no suplantable | Con autenticación, `reviewed_by` es el usuario autenticado | — |
| Cabeceras | CSP, `nosniff`, anti-clickjacking, `no-store` en la API | — |

**Cambios de comportamiento a tener en cuenta**

- Ya no se puede abrir el dashboard desde otra máquina sin definir `DASHBOARD_PASSWORD`. Para publicarlo, usa además un proxy inverso con **HTTPS**: HTTP Basic envía la contraseña sin cifrar si no hay TLS. Añade tu dominio a `ALLOWED_HOSTS`.
- El login genérico del crawler **ya no se prueba en todas las páginas**: si usas `CRAWLER_LOGIN_*`, define también `CRAWLER_LOGIN_URL` (o `CRAWLER_LOGIN_HOSTS`).
- Los sitios que resuelven a redes privadas (VPN, intranet) se rechazan salvo que pongas `ALLOW_PRIVATE_NETWORKS=true`.

**Limitaciones conocidas.** Entre la comprobación DNS y la conexión real el DNS podría cambiar (DNS rebinding); para cerrarlo del todo, añade una regla de salida en el firewall o proxy del servidor que impida conectar a redes privadas. Guarda `.env` y `data/auth_state.json` como secretos (el segundo se crea con permisos 600).

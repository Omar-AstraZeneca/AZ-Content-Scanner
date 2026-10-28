const $ = (id) => document.getElementById(id);
const api = (path, options) => fetch(path, options).then(async (response) => {
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const error = new Error(body.detail || response.statusText);
        error.status = response.status;
        throw error;
    }
    return response;
});
const json = (path, options) => api(path, options).then((response) => response.json());

// Estados que viajan desde el servidor
const DISPLAY_STATUSES = ["VENCIDO", "PROXIMO_A_VENCER", "REVISAR", "SIN_DATO", "VIGENTE"];
const STATUS_LABELS = {
    VENCIDO: "Vencido",
    PROXIMO_A_VENCER: "Próximo a vencer",
    REVISAR: "Sin fecha fiable",
    SIN_DATO: "Sin fecha fiable",
    VIGENTE: "Vigente",
};
const RUN_STATUS_LABELS = {
    RUNNING: "En curso",
    DONE: "Completado",
    ERROR: "Error",
    CANCELLED: "Cancelado",
};

// «Por revisar» y «Sin dato» significan lo mismo para quien audita: no se puede afirmar nada
// y hay que mirarlo a mano. Se muestran juntos como «Sin fecha fiable» (el servidor sigue
// distinguiéndolos por si se quisiera recuperar la separación).
const UNRELIABLE_STATUSES = ["SIN_DATO", "REVISAR"];
const isUnreliable = (status) => UNRELIABLE_STATUSES.includes(status);

// Fallos consecutivos de conexión tolerados durante un escaneo antes de darlo por perdido
const MAX_POLL_FAILURES = 3;

// Intervalo de actualización mientras hay un escaneo en curso.
// 8 s es suficiente para ver el avance y reduce a la mitad la carga sobre el servidor.
const POLL_INTERVAL_MS = 8000;

// Regla de estados. Debe coincidir con WARNING_DAYS de extractors.py.
//   días < 0                  -> VENCIDO
//   0 <= días <= WARNING_DAYS -> PROXIMO_A_VENCER
//   días > WARNING_DAYS       -> VIGENTE
//   sin fecha                 -> SIN_DATO
//   REVISAR                   -> fecha poco fiable: lo decide el servidor (según la confianza de
//                                la detección, no según el calendario), por eso se conserva.
// Si la página solo indica el año, se asume vencimiento el 31 de diciembre de ese año.
const WARNING_DAYS = 60;
const YEAR_END_MONTH = 12;
const YEAR_END_DAY = 31;

// Peticiones simultáneas máximas al calcular el historial
const HISTORY_BATCH_SIZE = 4;

let pollTimer = null;
let scanElapsedTimer = null;
let scanFinishTimer = null;
let scanStartedAt = 0;
let pollFailures = 0;
let connectionAlertShown = false;
let markets = [];
let sites = [];
let currentRunDetail = null; // detalle del escaneo abierto en el pop-up del historial (para exportar)

/* ------------------------------------------------------------------
   ESCANEO ACTIVO
   Es el único origen de datos de los cuadros de color, del detalle de
   activos y de la revisión manual. Todo lo anterior vive en el historial.
   Se rellena al pulsar «Escanear sitio» y se descarta con «Limpiar filtros».
------------------------------------------------------------------ */
let activeRunId = null;
let activeRun = null;      // datos del run (estado, páginas, etc.)
let activeAssets = [];     // activos del run activo, con el estado ya recalculado
let activeMarketCode = "";

// Caché de recuentos del historial: runId -> { counts, day }
// Un escaneo terminado no cambia, pero la clasificación depende de la fecha de hoy,
// por eso la entrada caduca al cambiar el día.
const runCountsCache = new Map();
let historyRenderToken = 0; // evita que una respuesta lenta pise a una más reciente

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}[character]));

function statusLabel(status) {
    return STATUS_LABELS[status] || status;
}

function siteOrigin(value) {
    try {
        const url = new URL(value.trim());
        return ["http:", "https:"].includes(url.protocol) ? url.origin : "";
    } catch {
        return "";
    }
}

function safeWebUrl(value) {
    try {
        const url = new URL(value);
        return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch {
        return "";
    }
}

function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

/* ==========================================================================
   Clasificación de estados (única fuente de verdad en el navegador)
   Todos los números del dashboard (cuadros de color, historial, pop-up,
   CSV y PDF) salen de estas funciones, así nunca difieren entre sí.
   ========================================================================== */
const MS_PER_DAY = 86400000;

function startOfDay(date) {
    return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function parseIsoDate(value) {
    if (!value) return null;
    const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value));
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return Number.isNaN(date.getTime()) ? null : date;
}

// Fecha de vencimiento efectiva: la exacta si existe; si solo hay año, el 31/12 de ese año
function effectiveExpiry(asset) {
    const exact = parseIsoDate(asset.expiry_date);
    if (exact) return { date: exact, approximate: false };
    const year = Number(asset.expiry_year);
    if (Number.isInteger(year) && year > 1900) {
        return { date: new Date(year, YEAR_END_MONTH - 1, YEAR_END_DAY), approximate: true };
    }
    return null;
}

function daysUntil(date) {
    return Math.round((startOfDay(date) - startOfDay(new Date())) / MS_PER_DAY);
}

function classifyAsset(asset) {
    if (asset.status === "REVISAR") return "REVISAR";
    const expiry = effectiveExpiry(asset);
    if (!expiry) return "SIN_DATO";
    const days = daysUntil(expiry.date);
    if (days < 0) return "VENCIDO";
    if (days <= WARNING_DAYS) return "PROXIMO_A_VENCER";
    return "VIGENTE";
}

// Devuelve una copia del activo con el estado recalculado y los días restantes
function withStatus(asset) {
    const expiry = effectiveExpiry(asset);
    return {
        ...asset,
        status: classifyAsset(asset),
        days_left: expiry ? daysUntil(expiry.date) : null,
        approximate: expiry ? expiry.approximate : false,
    };
}

function daysText(days) {
    if (days === null || days === undefined) return "";
    if (days === 0) return "vence hoy";
    if (days > 0) return `en ${days} día${days === 1 ? "" : "s"}`;
    const past = Math.abs(days);
    return `hace ${past} día${past === 1 ? "" : "s"}`;
}

// Celda de vencimiento: fecha o año, días restantes y aviso de fecha aproximada
function expiryCell(asset) {
    const base = asset.expiry_date || asset.expiry_year;
    if (!base) return "—";
    const days = asset.days_left !== null && asset.days_left !== undefined
        ? `<span class="due ${asset.status}">${escapeHtml(daysText(asset.days_left))}</span>` : "";
    const approx = asset.approximate
        ? ' <span class="approx" title="Solo se detectó el año: se asume vencimiento el 31 de diciembre">(≈ 31/12)</span>' : "";
    return `${escapeHtml(base)}${approx}${days}`;
}

// Recuento por los cinco estados del servidor
function countStatuses(assets) {
    const counts = Object.fromEntries(DISPLAY_STATUSES.map((status) => [status, 0]));
    assets.forEach((asset) => { if (asset.status in counts) counts[asset.status] += 1; });
    return counts;
}

// Recuento tal como se muestra: «Sin fecha fiable» agrupa SIN_DATO y REVISAR
function displayCounts(counts) {
    return {
        VENCIDO: counts.VENCIDO || 0,
        PROXIMO_A_VENCER: counts.PROXIMO_A_VENCER || 0,
        VIGENTE: counts.VIGENTE || 0,
        UNRELIABLE: (counts.SIN_DATO || 0) + (counts.REVISAR || 0),
    };
}

function totalOf(counts) {
    return DISPLAY_STATUSES.reduce((sum, status) => sum + (counts[status] || 0), 0);
}

/* ------------------------------------------------------------------ */

function showAppDialog({
    title,
    message,
    fields = [],
    confirmLabel = "Aceptar",
    cancelLabel = "",
    actions = null,
}) {
    const dialog = $("feedbackDialog");
    const form = $("feedbackForm");
    const fieldsContainer = $("feedbackFields");
    const actionsContainer = $("feedbackActions");
    const closeButton = $("feedbackClose");

    $("feedbackTitle").textContent = title;
    $("feedbackMessage").textContent = message;
    fieldsContainer.replaceChildren();
    actionsContainer.replaceChildren();

    fields.forEach((field, index) => {
        const id = `feedback-field-${index}`;
        const wrapper = document.createElement("div");
        wrapper.className = "feedback-field";
        const label = document.createElement("label");
        label.htmlFor = id;
        label.textContent = field.label;
        const input = field.multiline ? document.createElement("textarea") : document.createElement("input");
        input.id = id;
        input.name = field.name;
        if (!field.multiline) input.type = field.type || "text";
        input.value = field.value || "";
        input.placeholder = field.placeholder || "";
        if (field.inputMode) input.inputMode = field.inputMode;
        wrapper.append(label, input);
        fieldsContainer.append(wrapper);
    });

    const submitButton = document.createElement("button");
    submitButton.className = "btn primary";
    submitButton.type = "submit";
    submitButton.textContent = confirmLabel;

    let finish = null;
    const customActions = Array.isArray(actions) && actions.length ? actions : null;
    if (customActions) {
        customActions.forEach((action) => {
            const button = document.createElement("button");
            button.className = `btn${action.variant ? ` ${action.variant}` : ""}`;
            button.type = "button";
            button.textContent = action.label;
            button.addEventListener("click", () => {
                if (finish) finish(action.value);
            }, { once: true });
            actionsContainer.append(button);
        });
    } else {
        actionsContainer.append(submitButton);
    }

    return new Promise((resolve) => {
        let settled = false;
        const onSubmit = (event) => {
            event.preventDefault();
            if (customActions) {
                finish(customActions[0].value);
                return;
            }
            const values = Object.fromEntries(fields.map((field, index) => [
                field.name,
                fieldsContainer.querySelector(`#feedback-field-${index}`).value.trim(),
            ]));
            finish(values);
        };
        const onCancel = (event) => {
            event.preventDefault();
            finish(null);
        };
        const onBackdrop = (event) => {
            if (event.target === dialog) finish(null);
        };
        const onKeydown = (event) => {
            if (event.key === "Escape" && dialog.open) {
                event.preventDefault();
                finish(null);
            }
        };
        finish = (value) => {
            if (settled) return;
            settled = true;
            form.removeEventListener("submit", onSubmit);
            dialog.removeEventListener("cancel", onCancel);
            dialog.removeEventListener("click", onBackdrop);
            document.removeEventListener("keydown", onKeydown);
            if (dialog.open) dialog.close();
            resolve(value);
        };

        closeButton.onclick = () => finish(null);
        if (cancelLabel) {
            const cancelButton = document.createElement("button");
            cancelButton.className = "btn";
            cancelButton.type = "button";
            cancelButton.textContent = cancelLabel;
            cancelButton.addEventListener("click", () => finish(null), { once: true });
            actionsContainer.prepend(cancelButton);
        }
        form.addEventListener("submit", onSubmit);
        dialog.addEventListener("cancel", onCancel);
        dialog.addEventListener("click", onBackdrop);
        document.addEventListener("keydown", onKeydown);
        dialog.showModal();
        (fieldsContainer.querySelector("input, textarea")
            || actionsContainer.querySelector("button")
            || submitButton).focus();
    });
}

window.showAppDialog = showAppDialog;

/* ------------------------------------------------------------------
   Detección de pérdida de conexión con el servidor
------------------------------------------------------------------ */
function isConnectionError(error) {
    return error instanceof TypeError || /failed to fetch|networkerror|load failed/i.test(String(error?.message || ""));
}

async function reportConnectionLost() {
    if (connectionAlertShown) return;
    connectionAlertShown = true;
    await showAppDialog({
        title: "Sin conexión con el servidor",
        message: "No se pudo contactar con el servidor local (127.0.0.1:8000). Verifica que uvicorn siga en ejecución, reinícialo si es necesario y recarga la página.",
        confirmLabel: "Entendido",
    });
    connectionAlertShown = false;
}

/* ==========================================================================
   Exportación a CSV y PDF (se genera en el navegador, sin dependencias)
   ========================================================================== */
const EXPORT_COLUMNS = [
    ["Estado", (asset) => statusLabel(asset.status)],
    ["Región", (asset) => asset.market],
    ["País", (asset) => asset.site_country],
    ["Veeva ID", (asset) => asset.veeva_id || "ID no detectado"],
    ["Vencimiento", (asset) => asset.expiry_date || asset.expiry_year || ""],
    ["Días restantes", (asset) => (asset.days_left ?? "")],
    ["Fecha aproximada (solo año)", (asset) => (asset.approximate ? "Sí" : "No")],
    ["URL de la página", (asset) => asset.page_url],
    ["URL del activo", (asset) => (asset.asset_url && asset.asset_url !== asset.page_url ? asset.asset_url : "")],
    ["Apariciones (páginas)", (asset) => asset.occurrences ?? 1],
];

// Protege contra inyección de fórmulas al abrir el CSV en Excel/Sheets
function csvCell(value) {
    let text = String(value ?? "");
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g, '""')}"`;
}

function buildCsv(assets) {
    const header = EXPORT_COLUMNS.map(([name]) => csvCell(name)).join(",");
    const lines = assets.map((asset) => EXPORT_COLUMNS.map(([, getter]) => csvCell(getter(asset))).join(","));
    return [header, ...lines].join("\r\n");
}

function fileStamp() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}`;
}

function downloadCsv(assets, baseName) {
    // BOM UTF-8 para que Excel muestre bien tildes y eñes
    const blob = new Blob(["\ufeff", buildCsv(assets)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${baseName}_${fileStamp()}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Abre una vista imprimible; desde allí se elige "Guardar como PDF"
function openPdfReport({ title, subtitle, assets }) {
    const counts = displayCounts(countStatuses(assets));
    const rows = assets.map((asset) => `
        <tr>
          <td><span class="b ${escapeHtml(asset.status)}">${escapeHtml(statusLabel(asset.status))}</span></td>
          <td>${escapeHtml(asset.market)}</td>
          <td>${escapeHtml(asset.site_country || "—")}</td>
          <td>${escapeHtml(asset.veeva_id || "ID no detectado")}</td>
          <td>${escapeHtml(asset.expiry_date || asset.expiry_year || "—")}${asset.approximate ? " (≈ 31/12)" : ""}
              ${asset.days_left !== null && asset.days_left !== undefined ? `<br><span class="s">${escapeHtml(daysText(asset.days_left))}</span>` : ""}</td>
          <td class="u">${escapeHtml(asset.page_url)}${asset.asset_url && asset.asset_url !== asset.page_url
            ? `<br><span class="s">${escapeHtml(asset.asset_url)}</span>` : ""}</td>
        </tr>`).join("");

    const html = `<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><title>${escapeHtml(title)}</title>
<style>
  *{box-sizing:border-box}
  body{font-family:"Segoe UI",system-ui,-apple-system,Roboto,sans-serif;color:#1f2430;margin:24px;font-size:11px}
  h1{font-size:18px;margin:0 0 4px;color:#830051}
  .m{color:#6b7280;margin:0 0 14px}
  .k{display:flex;gap:8px;margin:0 0 16px;flex-wrap:wrap}
  .k div{border:1px solid #e5e7eb;border-left:4px solid #667085;border-radius:6px;padding:6px 10px;min-width:96px}
  .k b{display:block;font-size:16px}
  .k span{color:#6b7280;font-size:10px;text-transform:uppercase}
  .k .VENCIDO{border-left-color:#d92d20}.k .PROXIMO_A_VENCER{border-left-color:#f79009}
  .k .VIGENTE{border-left-color:#12b76a}
  table{width:100%;border-collapse:collapse}
  th{background:#f9fafb;text-align:left;font-size:9px;text-transform:uppercase;color:#6b7280;padding:6px;border-bottom:1px solid #e5e7eb}
  td{padding:6px;border-bottom:1px solid #e5e7eb;vertical-align:top}
  tr{break-inside:avoid}
  .u{word-break:break-all;max-width:260px}.s{color:#6b7280;font-size:9px}
  .b{display:inline-block;padding:2px 8px;border-radius:999px;font-weight:700;font-size:9px;background:#f2f4f7;color:#667085}
  .b.VENCIDO{background:#fef3f2;color:#d92d20}.b.PROXIMO_A_VENCER{background:#fffaeb;color:#b54708}
  .b.VIGENTE{background:#ecfdf3;color:#027a48}
  .f{margin-top:14px;color:#6b7280;font-size:9px}
  @page{size:A4 landscape;margin:12mm}
</style></head><body>
<h1>${escapeHtml(title)}</h1>
<p class="m">${escapeHtml(subtitle)} · Generado el ${escapeHtml(new Date().toLocaleString())}</p>
<div class="k">
  <div class="VENCIDO"><b>${counts.VENCIDO}</b><span>Vencidos</span></div>
  <div class="PROXIMO_A_VENCER"><b>${counts.PROXIMO_A_VENCER}</b><span>Próximos a vencer</span></div>
  <div class="VIGENTE"><b>${counts.VIGENTE}</b><span>Vigentes</span></div>
  <div><b>${counts.UNRELIABLE}</b><span>Sin fecha fiable</span></div>
  <div><b>${assets.length}</b><span>Total IDs</span></div>
</div>
<table><thead><tr><th>Estado</th><th>Región</th><th>País</th><th>Veeva ID</th><th>Vencimiento</th><th>URL</th></tr></thead>
<tbody>${rows}</tbody></table>
<p class="f">Content Expiry Scanner · Piloto. Los resultados requieren validación humana y no sustituyen la decisión de Compliance.</p>
</body></html>`;

    const reportWindow = window.open("", "_blank");
    if (!reportWindow) {
        showAppDialog({
            title: "El navegador bloqueó la ventana",
            message: "Permite las ventanas emergentes para este sitio y vuelve a pulsar «Descargar PDF». En la ventana de impresión elige «Guardar como PDF».",
        });
        return;
    }
    reportWindow.document.open();
    reportWindow.document.write(html);
    reportWindow.document.close();
    // La CSP no permite scripts dentro del informe: la impresión se lanza desde aquí
    setTimeout(() => { try { reportWindow.focus(); reportWindow.print(); } catch (_) { /* cerrada */ } }, 500);
}

/* ------------------------------------------------------------------
   Datos visibles del escaneo activo (aplican el filtro de estado y el
   buscador, SOLO sobre los activos del run activo)
------------------------------------------------------------------ */
function visibleActiveAssets() {
    const status = $("fStatus").value;
    const query = $("fSearch").value.trim().toLowerCase();
    return activeAssets.filter((asset) => {
        if (status) {
            // «Sin fecha fiable» agrupa los estados SIN_DATO y REVISAR
            const matches = status === "SIN_DATO" ? isUnreliable(asset.status) : asset.status === status;
            if (!matches) return false;
        }
        if (query) {
            const haystack = `${asset.veeva_id || ""} ${asset.page_url || ""} ${asset.asset_url || ""}`.toLowerCase();
            if (!haystack.includes(query)) return false;
        }
        return true;
    });
}

function updateExportButtons() {
    const disabled = visibleActiveAssets().length === 0;
    $("btnExportCsv").disabled = disabled;
    $("btnExportPdf").disabled = disabled;
}

function exportContext() {
    return [
        activeRunId ? `Escaneo #${activeRunId}` : "",
        activeMarketCode,
        activeRun?.site_country,
        activeRun?.site_url,
        $("fStatus").value && statusLabel($("fStatus").value),
    ].filter(Boolean).join(" · ") || "Escaneo actual";
}

function exportTableCsv() {
    const rows = visibleActiveAssets();
    if (!rows.length) return;
    downloadCsv(rows, activeRunId ? `escaneo_${activeRunId}_detalle` : "detalle_activos");
}

function exportTablePdf() {
    const rows = visibleActiveAssets();
    if (!rows.length) return;
    openPdfReport({ title: "Detalle de activos", subtitle: exportContext(), assets: rows });
}

function exportRunCsv() {
    if (!currentRunDetail || !(currentRunDetail.assets || []).length) return;
    downloadCsv(currentRunDetail.assets, `escaneo_${currentRunDetail.id}`);
}

function exportRunPdf() {
    if (!currentRunDetail || !(currentRunDetail.assets || []).length) return;
    const meta = [currentRunDetail.market, currentRunDetail.site_country, currentRunDetail.site_url, currentRunDetail.site_category]
        .filter(Boolean).join(" · ");
    openPdfReport({
        title: `Análisis del escaneo #${currentRunDetail.id}`,
        subtitle: meta,
        assets: currentRunDetail.assets,
    });
}

/* ------------------------------------------------------------------ */

async function loadMarkets() {
    markets = await json("/api/scan/markets");
    sites = markets.flatMap((market) => market.sites.map((site) => ({ ...site, market })));
    const select = $("fMarket");
    markets.forEach((market) => {
        select.insertAdjacentHTML("beforeend", `<option value="${market.code}">${market.code} · ${escapeHtml(market.name)}</option>`);
    });
    renderCountries();
    renderSites();
    updateExternalScope();
    renderReviewMarkets();
}

function renderCountries() {
    const marketCode = $("fMarket").value;
    const selected = $("fCountry").value;
    const countries = marketCode
        ? markets.find((market) => market.code === marketCode)?.countries || []
        : [...new Set(markets.flatMap((market) => market.countries))].sort((left, right) => left.localeCompare(right));
    $("fCountry").innerHTML = `<option value="">Todos</option>${countries.map((country) =>
        `<option value="${escapeHtml(country)}">${escapeHtml(country)}</option>`
    ).join("")}`;
    $("fCountry").value = countries.includes(selected) ? selected : "";
}

function renderSites() {
    const marketCode = $("fMarket").value;
    const country = $("fCountry").value;
    const query = $("fSiteSearch").value.trim().toLowerCase();
    const selected = $("fSite").value;
    let visible = sites.filter((site) => !marketCode || site.market.code === marketCode);
    if (country) visible = visible.filter((site) => site.country === country);
    if (query) {
        visible = visible.filter((site) =>
            `${site.market.name} ${site.market.code} ${site.country} ${site.category} ${site.host} ${site.url}`.toLowerCase().includes(query)
        );
    }
    const selectedSite = sites.find((site) => site.url === selected);
    if (selectedSite && !visible.some((site) => site.url === selected)) visible.unshift(selectedSite);
    $("fSite").innerHTML = `<option value="">Todos los sitios</option>${visible.map((site) =>
        `<option value="${escapeHtml(site.url)}">${site.market.code} · ${escapeHtml(site.country)} · ${escapeHtml(site.category)} · ${escapeHtml(site.host)}</option>`
    ).join("")}`;
    $("fSite").value = selected;
}

function updateExternalScope() {
    $("confirmExternalScope").checked = false;
    const market = markets.find((item) => item.code === $("fMarket").value);
    const url = siteOrigin($("fSiteUrl").value);
    if (!market || !url) {
        $("externalScopeWrap").classList.add("hidden");
        return;
    }
    const host = new URL(url).hostname.toLowerCase();
    const isCatalogued = market.domain_allowlist.some((domain) =>
        host === domain.toLowerCase() || host.endsWith(`.${domain.toLowerCase()}`)
    );
    $("externalScopeWrap").classList.toggle("hidden", isCatalogued);
}

function resetKpis() {
    ["kVencido", "kProximo", "kSinDato", "kVigente", "kTotal"].forEach((id) => { $(id).textContent = "0"; });
}

/* ==========================================================================
   Pintado del ESCANEO ACTIVO: cuadros de color, detalle y revisión
   Todo sale de activeAssets, nunca del histórico.
   ========================================================================== */
function renderKpis() {
    const counts = displayCounts(countStatuses(activeAssets));
    $("kVencido").textContent = counts.VENCIDO;
    $("kProximo").textContent = counts.PROXIMO_A_VENCER;
    $("kSinDato").textContent = counts.UNRELIABLE;
    $("kVigente").textContent = counts.VIGENTE;
    $("kTotal").textContent = activeAssets.length;
}

function renderDetailTable() {
    const rows = visibleActiveAssets();
    window.__rows = Object.fromEntries(rows.map((row) => [row.id, row]));

    if (!activeRunId) {
        $("rowCount").textContent = "Inicia un escaneo para ver resultados";
    } else if (!activeAssets.length) {
        $("rowCount").textContent = activeRun?.status === "RUNNING"
            ? "Buscando IDs…"
            : "Este escaneo no encontró IDs";
    } else {
        $("rowCount").textContent = rows.length === activeAssets.length
            ? `${rows.length} ID(s) en este escaneo`
            : `${rows.length} de ${activeAssets.length} ID(s) en este escaneo`;
    }

    // La tabla solo se pinta si el detalle está abierto (evita trabajo innecesario)
    if (!$("assetsDetails").open) {
        updateExportButtons();
        return;
    }

    $("tbl").querySelector("tbody").innerHTML = rows.length
        ? rows.map((asset) => `
        <tr>
          <td data-label="Estado"><span class="badge ${asset.status}">${statusLabel(asset.status)}</span></td>
          <td data-label="Región">${escapeHtml(asset.market)}</td>
          <td data-label="País">${escapeHtml(asset.site_country || "—")}</td>
          <td data-label="Veeva ID">${escapeHtml(asset.veeva_id) || "ID no detectado"}</td>
          <td data-label="Vencimiento">${expiryCell(asset)}</td>
          <td data-label="URL del contenido">
            <span class="url" title="${escapeHtml(asset.page_url)}">${escapeHtml(asset.page_url)}</span>
            ${asset.asset_url && asset.asset_url !== asset.page_url
                ? `<span class="url sub" title="${escapeHtml(asset.asset_url)}">${escapeHtml(asset.asset_url)}</span>`
                : ""}
            ${Number(asset.occurrences) > 1
                ? `<span class="approx" title="El mismo contenido aparece en varias páginas">Aparece en ${Number(asset.occurrences)} páginas</span>`
                : ""}
          </td>
          <td data-label="Acciones"><button class="link-btn" data-id="${asset.id}">Ver</button></td>
        </tr>`).join("")
        : '<tr><td colspan="7" class="muted">No hay IDs que coincidan con el filtro.</td></tr>';
    updateExportButtons();
}

function renderActiveRun() {
    renderKpis();
    renderDetailTable();
    renderReview();
}

// Descarta el escaneo activo y deja la vista vacía
function clearActiveRun() {
    activeRunId = null;
    activeRun = null;
    activeAssets = [];
    activeMarketCode = "";
    resetKpis();
    $("tbl").querySelector("tbody").replaceChildren();
    window.__rows = {};
    updateExportButtons();
    $("rowCount").textContent = "Inicia un escaneo para ver resultados";
    hideReview();
}

// Lee el detalle del run activo: es la ÚNICA fuente de los cuadros, el detalle y la revisión
async function loadActiveRun() {
    if (!activeRunId) return null;
    const detail = await json(`/api/scan/runs/${activeRunId}`);
    activeRun = detail;
    activeAssets = (detail.assets || [])
        .map(withStatus)
        .filter((asset) => DISPLAY_STATUSES.includes(asset.status));
    renderActiveRun();
    return detail;
}

/* ==========================================================================
   Historial: todos los escaneos anteriores.
   Sus recuentos se calculan desde los activos de cada escaneo, con la misma
   función que usan los cuadros de color, así nunca difieren.
   ========================================================================== */
const todayKey = () => new Date().toDateString();

function getCachedCounts(run) {
    // Un escaneo en curso nunca se cachea: sus activos cambian en cada ciclo
    if (run.status === "RUNNING") return null;
    const entry = runCountsCache.get(run.id);
    if (!entry || entry.day !== todayKey()) return null;
    return entry.counts;
}

// Pide los activos de un escaneo y calcula sus recuentos con la regla vigente
async function computeRunCounts(run) {
    const detail = await json(`/api/scan/runs/${run.id}`);
    const counts = countStatuses((detail.assets || []).map(withStatus));
    if (run.status !== "RUNNING") {
        runCountsCache.set(run.id, { counts, day: todayKey() });
    }
    return counts;
}

function runRowHtml(run, counts, { fallback = false } = {}) {
    // counts === null -> aún calculando
    const shown = counts === null ? null : displayCounts(counts);
    const cell = (key) => (shown === null ? "…" : `${shown[key] || 0}${fallback ? "*" : ""}`);
    const total = counts === null ? "…" : `${totalOf(counts)}${fallback ? "*" : ""}`;
    const pages = run.pages_scanned ?? run.pages ?? "—";
    const isActive = run.id === activeRunId;
    return `
        <tr data-run-row="${run.id}">
          <td data-label="#">${run.id}${isActive ? " ●" : ""}</td>
          <td data-label="Región">${escapeHtml(run.market)}</td>
          <td data-label="Sitio">${escapeHtml([run.site_country, run.site_url || "Todos los sitios", run.site_category].filter(Boolean).join(" · "))}</td>
          <td data-label="Estado"><span class="chip ${escapeHtml(run.status)}">${escapeHtml(RUN_STATUS_LABELS[run.status] || run.status)}</span></td>
          <td data-label="Inicio">${formatDate(run.started_at)}</td>
          <td data-label="Páginas">${escapeHtml(pages)}${run.pages_failed ? ` <span class="approx" title="Páginas que no se pudieron cargar">(${run.pages_failed} con error)</span>` : ""}</td>
          <td data-label="Imágenes">${escapeHtml(run.images_analyzed ?? "—")}${run.images_skipped ? ` <span class="approx" title="Imágenes que no se pudieron leer">(${run.images_skipped} omitidas)</span>` : ""}</td>
          <td data-label="Vencidos">${cell("VENCIDO")}</td>
          <td data-label="Próximos">${cell("PROXIMO_A_VENCER")}</td>
          <td data-label="Sin fecha fiable">${cell("UNRELIABLE")}</td>
          <td data-label="Vigentes">${cell("VIGENTE")}</td>
          <td data-label="Total IDs">${total}</td>
          <td data-label="Acción"><button class="link-btn" data-run-id="${run.id}">Ver análisis</button></td>
        </tr>`;
}

// Rellena una fila concreta cuando llegan sus recuentos
function fillRunRow(run, counts, options) {
    const row = $("tblRuns").querySelector(`tr[data-run-row="${run.id}"]`);
    if (!row) return;
    row.outerHTML = runRowHtml(run, counts, options);
}

/* El historial muestra TODOS los escaneos, sin depender de los filtros del formulario.
   `light: true` (usado durante un sondeo) repinta la lista pero NO recalcula los recuentos
   de los escaneos ya terminados: solo se pide el detalle de los que aún no tienen caché.
   Así un escaneo largo no genera decenas de peticiones pesadas por minuto. */
async function loadRuns({ light = false } = {}) {
    const runs = await json("/api/scan/runs");
    window.__runRows = Object.fromEntries(runs.map((run) => [run.id, run]));

    const token = ++historyRenderToken;
    const tbody = $("tblRuns").querySelector("tbody");

    if (!runs.length) {
        tbody.innerHTML = '<tr><td colspan="13" class="muted">Sin escaneos registrados todavía.</td></tr>';
        return runs;
    }

    // 1) Pintado inmediato: recuentos en caché o "…" mientras se calculan
    tbody.innerHTML = runs.map((run) => runRowHtml(run, getCachedCounts(run))).join("");

    // 2) Escaneos que faltan por calcular. Durante un sondeo se omite el escaneo en curso:
    //    su detalle ya lo pide loadActiveRun() y repetirlo duplicaría el trabajo.
    const pending = runs.filter((run) => {
        if (getCachedCounts(run) !== null) return false;
        if (light && run.status === "RUNNING") return false;
        return true;
    });

    // 3) Cálculo en lotes pequeños; cada fila se rellena en cuanto llega su resultado
    for (let index = 0; index < pending.length; index += HISTORY_BATCH_SIZE) {
        if (token !== historyRenderToken) break; // llegó un repintado más reciente
        const batch = pending.slice(index, index + HISTORY_BATCH_SIZE);
        await Promise.all(batch.map(async (run) => {
            try {
                const counts = await computeRunCounts(run);
                if (token === historyRenderToken) fillRunRow(run, counts);
            } catch (error) {
                // Respaldo: recuentos del servidor, marcados con asterisco
                if (token === historyRenderToken) {
                    fillRunRow(run, run.status_counts || {}, { fallback: true });
                }
                if (isConnectionError(error)) throw error;
            }
        }));
    }

    // 4) El escaneo en curso no se cachea: se muestran sus recuentos del activo actual
    if (light && activeRunId) {
        const running = runs.find((run) => run.id === activeRunId && run.status === "RUNNING");
        if (running && token === historyRenderToken) {
            fillRunRow(running, countStatuses(activeAssets));
        }
    }
    return runs;
}

function openModal(id) {
    const asset = window.__rows[id];
    if (!asset) return;
    currentRunDetail = null;
    const pageUrl = safeWebUrl(asset.page_url);
    const assetUrl = safeWebUrl(asset.asset_url);
    $("modalTitle").textContent = "Detalle del ID";
    $("modalBody").innerHTML = `
        <dl class="detail-grid">
          <dt>Estado</dt><dd><span class="badge ${asset.status}">${statusLabel(asset.status)}</span></dd>
          <dt>Veeva Approval ID</dt><dd>${escapeHtml(asset.veeva_id) || "ID no detectado"}</dd>
          <dt>Vencimiento</dt><dd>${expiryCell(asset)}</dd>
          <dt>Región</dt><dd>${escapeHtml(asset.market)}</dd>
          <dt>País</dt><dd>${escapeHtml(asset.site_country || "—")}</dd>
          <dt>URL de página</dt><dd>${pageUrl ? `<a href="${escapeHtml(pageUrl)}" target="_blank" rel="noopener">${escapeHtml(asset.page_url)}</a>` : escapeHtml(asset.page_url)}</dd>
          ${assetUrl && asset.asset_url !== asset.page_url ? `<dt>URL del activo</dt><dd><a href="${escapeHtml(assetUrl)}" target="_blank" rel="noopener">${escapeHtml(asset.asset_url)}</a></dd>` : ""}
          ${Number(asset.occurrences) > 1 ? `<dt>Aparece en</dt><dd id="occurrenceList">Cargando ${Number(asset.occurrences)} páginas…</dd>` : ""}
        </dl>
        ${asset.evidence ? `<h3>Evidencia</h3><img class="evidence-img" src="/api/assets/${asset.id}/evidence" alt="Captura del contenido" />` : ""}`;
    $("modal").classList.remove("hidden");
    if (Number(asset.occurrences) > 1) loadOccurrences(asset.id);
}

// Lista las páginas donde aparece el mismo activo (para saber dónde retirarlo)
async function loadOccurrences(assetId) {
    const box = $("occurrenceList");
    try {
        const data = await json(`/api/assets/${assetId}/occurrences`);
        if (!$("occurrenceList")) return;
        const shown = data.pages.slice(0, 50);
        box.innerHTML = `<ol class="occurrence-list">${shown.map((page) => {
            const safe = safeWebUrl(page);
            return `<li>${safe
                ? `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener">${escapeHtml(page)}</a>`
                : escapeHtml(page)}</li>`;
        }).join("")}</ol>${data.pages.length > shown.length
            ? `<span class="approx">… y ${data.pages.length - shown.length} más (están en el CSV)</span>` : ""}`;
    } catch (error) {
        if ($("occurrenceList")) box.textContent = "No se pudo cargar la lista de páginas.";
    }
}

// «Ver análisis» del historial: abre un pop-up con ese escaneo, sin tocar los cuadros de color
async function openRunModal(id) {
    try {
        const detail = await json(`/api/scan/runs/${id}`);
        // Se recalcula el estado de cada ID con la misma regla que los cuadros de color
        detail.assets = (detail.assets || []).map(withStatus);
        const rawCounts = countStatuses(detail.assets);
        detail.status_counts = rawCounts;
        // Aprovecha para refrescar la caché del historial con el mismo cálculo
        const knownRun = window.__runRows?.[id];
        if (knownRun && knownRun.status !== "RUNNING") {
            runCountsCache.set(id, { counts: rawCounts, day: todayKey() });
        }
        const shown = displayCounts(rawCounts);
        const counts = { ...shown, TOTAL: detail.assets.length };
        const labels = [
            ["VENCIDO", "Vencidos", "danger"],
            ["PROXIMO_A_VENCER", "Próximos a vencer", "warning"],
            ["VIGENTE", "Vigentes", "success"],
            ["UNRELIABLE", "Sin fecha fiable", "neutral"],
            ["TOTAL", "Total IDs", "info"],
        ];
        const assets = detail.assets;
        currentRunDetail = detail; // se conserva para exportar a CSV/PDF
        const rows = assets.map((asset) => {
            const pageUrl = safeWebUrl(asset.page_url);
            const assetUrl = safeWebUrl(asset.asset_url);
            return `
                <article class="run-url-row">
                    <div class="run-url-heading">
                        <span class="badge ${asset.status}">${statusLabel(asset.status)}</span>
                        <strong>${escapeHtml(asset.veeva_id) || "ID no detectado"}</strong>
                    </div>
                    <p>Vencimiento: ${expiryCell(asset)}</p>
                    <a class="run-url-link" href="${escapeHtml(pageUrl)}" target="_blank" rel="noopener">${escapeHtml(asset.page_url)}</a>
                    ${assetUrl && asset.asset_url !== asset.page_url
                        ? `<a class="run-url-link sub" href="${escapeHtml(assetUrl)}" target="_blank" rel="noopener">Activo: ${escapeHtml(asset.asset_url)}</a>`
                        : ""}
                </article>`;
        }).join("");

        $("modalTitle").textContent = `Análisis del escaneo #${detail.id}`;
        $("modalBody").innerHTML = `
            <div class="run-analysis">
                <p class="run-analysis-meta">${escapeHtml(detail.market)} · ${escapeHtml([detail.site_country, detail.site_url, detail.site_category].filter(Boolean).join(" · "))}</p>

                <div class="export-bar in-modal" role="group" aria-label="Exportar análisis del escaneo">
                    <span class="export-bar-label">Exportar análisis</span>
                    <div class="export-bar-actions">
                        <button type="button" class="btn" id="btnRunCsv" ${assets.length ? "" : "disabled"}>⬇ Descargar CSV</button>
                        <button type="button" class="btn" id="btnRunPdf" ${assets.length ? "" : "disabled"}>⬇ Descargar PDF</button>
                    </div>
                </div>

                <div class="run-status-grid">
                    ${labels.map(([status, label, color]) => `
                    <div class="run-status-card ${color}">
                        <span>${label}</span>
                        <strong>${counts[status] || 0}</strong>
                    </div>`).join("")}
                </div>
                <p class="run-analysis-date">${formatDate(detail.started_at)} · ${escapeHtml(RUN_STATUS_LABELS[detail.status] || detail.status)}
                · ${detail.pages_scanned ?? 0} página(s) · ${detail.images_analyzed ?? 0} imagen(es) analizadas${detail.images_skipped ? ` · ${detail.images_skipped} omitidas` : ""}
                ${detail.pages_failed ? ` · <strong>${detail.pages_failed} página(s) con error</strong>` : ""}</p>
                ${detail.summary && detail.status === "ERROR" ? `<p class="run-analysis-date"><strong>Motivo del error:</strong> ${escapeHtml(detail.summary)}</p>` : ""}
                ${detail.summary && detail.summary.includes("COBERTURA PARCIAL") ? `<p class="run-analysis-date"><strong>⚠ Cobertura parcial:</strong> ${escapeHtml(detail.summary.slice(detail.summary.indexOf("COBERTURA PARCIAL")))}</p>` : ""}
                ${detail.summary && detail.summary.includes("ESCANEO DETENIDO") ? `<p class="run-analysis-date"><strong>⚠ Escaneo detenido:</strong> ${escapeHtml(detail.summary.slice(detail.summary.indexOf("ESCANEO DETENIDO")))}</p>` : ""}
                <h3>URLs con IDs detectados (${assets.length})</h3>
                <div class="run-url-list">
                    ${rows || '<p class="run-empty">No se detectaron IDs en esta ejecución.</p>'}
                </div>
            </div>`;
        $("btnRunCsv").addEventListener("click", exportRunCsv);
        $("btnRunPdf").addEventListener("click", exportRunPdf);
        $("modal").classList.remove("hidden");
    } catch (error) {
        if (isConnectionError(error)) reportConnectionLost();
        else await showAppDialog({ title: "No se pudo cargar el análisis", message: error.message });
    }
}

/* ------------------------------------------------------------------
   Actualización de la pantalla.
   - Historial: siempre.
   - Escaneo activo (cuadros, detalle, revisión): solo si existe uno.
   No lanza excepciones sin gestionar; con propagateErrors = true
   propaga el error para contar fallos de conexión.
------------------------------------------------------------------ */
async function refreshAll({ propagateErrors = false } = {}) {
    try {
        const [runs] = await Promise.all([loadRuns(), loadActiveRun()]);
        $("btnRefresh").title = `Actualizado ${new Date().toLocaleTimeString()}`;
        return runs;
    } catch (error) {
        if (propagateErrors) throw error;
        if (isConnectionError(error)) reportConnectionLost();
        else console.error(error);
        return null;
    }
}

function clearView() {
    stopScanTimers();
    $("scanBanner").classList.add("hidden");
    ["fMarket", "fCountry", "fSite", "fSiteSearch", "fSiteUrl", "fSiteCategory", "fStatus", "fSearch"]
        .forEach((id) => { $(id).value = ""; });
    $("confirmExternalScope").checked = false;
    $("assetsDetails").open = false;
    $("advancedFilters").open = false;
    renderCountries();
    renderSites();
    updateExternalScope();
    setScanButtons({ running: false });
    clearActiveRun(); // descarta el escaneo activo: cuadros, detalle y revisión quedan vacíos
    refreshAll();     // el historial se conserva y se vuelve a pintar
}

function formatElapsed(milliseconds) {
    const seconds = Math.floor(milliseconds / 1000);
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function stopScanTimers() {
    clearInterval(pollTimer);
    clearInterval(scanElapsedTimer);
    clearTimeout(scanFinishTimer);
    pollTimer = null;
    scanElapsedTimer = null;
}

// Alterna los botones según haya o no un escaneo en curso
function setScanButtons({ running, cancelling = false }) {
    $("btnScan").classList.toggle("hidden", running);
    $("btnScan").disabled = running;
    $("btnCancel").classList.toggle("hidden", !running);
    $("btnCancel").disabled = cancelling;
    $("btnCancel").textContent = cancelling ? "Cancelando…" : "■ Cancelar escaneo";
}

/* ------------------------------------------------------------------
   Banner de progreso. NO muestra recuentos por estado: solo páginas
   analizadas e IDs encontrados. Los recuentos por estado viven
   únicamente en los cuadros de color.
------------------------------------------------------------------ */
function renderScanProgress({ market, run, phase = "running" }) {
    const banner = $("scanBanner");
    const track = $("scanProgressTrack");
    const indicator = $("scanSpinner");
    const cancelled = run?.status === "CANCELLED";
    const failed = run?.status === "ERROR" || phase === "lost";
    const done = Boolean(run && run.status !== "RUNNING");
    const finished = done || phase === "lost";
    const pages = run?.pages_scanned ?? 0;
    const found = activeAssets.length;
    const progress = `${pages} página(s) analizadas · ${found} ID(s) encontrados`;

    banner.classList.toggle("is-complete", done && !failed && !cancelled);
    banner.classList.toggle("is-error", failed);
    banner.classList.toggle("is-cancelled", cancelled);
    indicator.classList.toggle("is-complete", done && !failed && !cancelled);
    indicator.classList.toggle("is-error", failed);
    indicator.classList.toggle("is-cancelled", cancelled);
    indicator.textContent = finished ? (failed ? "!" : cancelled ? "■" : "✓") : "";
    $("scanElapsed").textContent = formatElapsed(Date.now() - scanStartedAt);

    if (phase === "lost") {
        $("scanBannerTitle").textContent = "Se perdió la conexión con el servidor";
        $("scanProgressText").textContent = "El servidor dejó de responder y el escaneo se interrumpió. Reinicia uvicorn y recarga la página.";
    } else if (phase === "starting") {
        $("scanBannerTitle").textContent = "Preparando escaneo";
        $("scanProgressText").textContent = "Conectando con el crawler y preparando los sitios.";
    } else if (phase === "cancelling") {
        $("scanBannerTitle").textContent = "Cancelando escaneo";
        $("scanProgressText").textContent = `Esperando a que termine la página en curso. ${progress}.`;
    } else if (phase === "waiting") {
        $("scanBannerTitle").textContent = `Escaneando ${market}`;
        $("scanProgressText").textContent = "El crawler está iniciando la revisión de IDs.";
    } else if (cancelled) {
        $("scanBannerTitle").textContent = "Escaneo cancelado";
        $("scanProgressText").textContent = `${progress}. Lo detectado hasta ahora quedó guardado en el historial.`;
    } else if (done && failed) {
        // Muestra el motivo real que registró el servidor (red caída, sin sesión, etc.)
        $("scanBannerTitle").textContent = "El escaneo no se pudo completar";
        $("scanProgressText").textContent = run?.summary
            ? run.summary
            : "El crawler encontró un error. Revisa el estado en el historial.";
    } else if (done) {
        $("scanBannerTitle").textContent = "Escaneo completado";
        $("scanProgressText").textContent = `${progress}. Los resultados están en los cuadros de color.`;
    } else {
        $("scanBannerTitle").textContent = `Escaneando ${market}`;
        $("scanProgressText").textContent = `En curso · ${progress}.`;
    }

    if (finished) {
        track.setAttribute("aria-valuenow", "100");
        track.setAttribute("aria-valuetext", failed ? "Escaneo finalizado con errores" : cancelled ? "Escaneo cancelado" : "Escaneo completado");
    } else {
        track.removeAttribute("aria-valuenow");
        track.setAttribute("aria-valuetext", `${progress}; progreso indeterminado`);
    }
}

function hideScanProgress() {
    stopScanTimers();
    $("scanBanner").classList.add("hidden");
    setScanButtons({ running: false });
}

// Un escaneo terminó (completado, con error o cancelado): se cierra el sondeo y se muestran sus resultados
async function finishScan(run, marketCode) {
    stopScanTimers();
    setScanButtons({ running: false });
    renderScanProgress({ market: marketCode, run });
    // Un error se deja visible hasta que el usuario lo descarte; el resto se oculta solo
    if (run.status !== "ERROR") {
        scanFinishTimer = setTimeout(() => $("scanBanner").classList.add("hidden"), 8000);
    }
    runCountsCache.delete(run.id); // recalcular con los datos finales
    await refreshAll();
}

async function startScan() {
    const startUrl = $("fSiteUrl").value.trim();
    const selectedMarket = $("fMarket").value;
    let parsedUrl = null;
    if (startUrl) {
        try {
            parsedUrl = new URL(startUrl);
        } catch {
            return showAppDialog({ title: "URL no válida", message: "Escribe una URL válida, incluyendo https://." });
        }
        if (!["http:", "https:"].includes(parsedUrl.protocol)) {
            return showAppDialog({ title: "Protocolo no permitido", message: "Solo se permiten direcciones http o https." });
        }
    }
    if (!startUrl && !selectedMarket) {
        return showAppDialog({
            title: "Selecciona qué escanear",
            message: "Elige una región para escanear todos sus sitios, o selecciona/escribe un sitio concreto.",
        });
    }

    let market = null;
    let isExternal = false;
    if (startUrl) {
        const domainMarket = markets.find((candidate) =>
            (!selectedMarket || candidate.code === selectedMarket) &&
            candidate.domain_allowlist.some((domain) =>
                parsedUrl.hostname.toLowerCase() === domain.toLowerCase() ||
                parsedUrl.hostname.toLowerCase().endsWith(`.${domain.toLowerCase()}`)
            )
        );
        market = selectedMarket ? markets.find((candidate) => candidate.code === selectedMarket) : domainMarket;
        if (!market) return showAppDialog({ title: "Selecciona una región", message: "Elige una región para asignar este sitio antes de escanear." });
        isExternal = !market.domain_allowlist.some((domain) =>
            parsedUrl.hostname.toLowerCase() === domain.toLowerCase() ||
            parsedUrl.hostname.toLowerCase().endsWith(`.${domain.toLowerCase()}`)
        );
        if (isExternal && !$("confirmExternalScope").checked) {
            return showAppDialog({ title: "Confirma el alcance", message: "Confirma en el formulario que tienes autorización para escanear este dominio externo." });
        }
    } else {
        // Sin URL: se escanean todos los sitios configurados de la región elegida.
        market = markets.find((candidate) => candidate.code === selectedMarket);
        if (!market) return showAppDialog({ title: "Región no válida", message: "La región seleccionada no está en el catálogo." });
    }

    // Si el objetivo incluye portales post-login y la sesión no cubre sus
    // plataformas, se pregunta antes de gastar tiempo rastreando a ciegas.
    let useLogin = true;
    const target = scanTargetInfo(market, startUrl);
    if (target.requiresLogin) {
        const info = await fetchSessionStatus({ force: true });
        if (!sessionCovers(info, target)) {
            const platformNames = target.platforms.length
                ? target.platforms.map((key) => PLATFORM_LABELS[key]).join(", ")
                : "portal protegido";
            const choice = await showAppDialog({
                title: "Los sitios post-login requieren iniciar sesión",
                message: `El sitio incluye secciones protegidas (${platformNames}). Con sesión se auditan también esas páginas; sin sesión solo se recorre el contenido público y el resto se marca como protegido.`,
                actions: [
                    { label: "Iniciar sesión", value: "login", variant: "primary" },
                    { label: "Continuar sin logueo", value: "no-login" },
                ],
                cancelLabel: "Cancelar",
            });
            if (choice === null) return;
            if (choice === "no-login") {
                useLogin = false;
            } else if (choice === "login") {
                const result = await openSessionDialog({
                    requiredPlatforms: target.platforms,
                    autoStart: true,
                });
                if (!result || result.action === "cancel") return;
                if (result.action === "no-login") useLogin = false;
            }
        }
    }

    // Nuevo escaneo: se descarta el anterior y se parte de cero
    stopScanTimers();
    clearActiveRun();
    $("assetsDetails").open = false;
    setScanButtons({ running: true });
    pollFailures = 0;
    scanStartedAt = Date.now();
    activeMarketCode = market.code;
    renderScanProgress({ market: market.code, phase: "starting" });
    $("scanBanner").classList.remove("hidden", "is-complete", "is-error", "is-cancelled");
    scanElapsedTimer = setInterval(() => {
        $("scanElapsed").textContent = formatElapsed(Date.now() - scanStartedAt);
    }, 1000);

    let response;
    try {
        response = await json(`/api/scan/start/${market.code}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                start_url: startUrl || null,
                site_country: $("fCountry").value || null,
                site_category: $("fSiteCategory").value.trim() || null,
                confirm_external_scope: isExternal && $("confirmExternalScope").checked,
                use_saved_session: useLogin,
                use_login: useLogin,
            }),
        });
    } catch (error) {
        hideScanProgress();
        return showAppDialog({
            title: "No se pudo iniciar el escaneo",
            message: isConnectionError(error)
                ? "No hay conexión con el servidor local. Verifica que uvicorn esté en ejecución y recarga la página."
                : error.message,
        });
    }

    const siteUrl = response.site_url;
    // El servidor devuelve el id del run; si no, se localiza por sitio y hora de inicio
    if (response.run_id) activeRunId = response.run_id;

    pollTimer = setInterval(async () => {
        try {
            // Modo ligero: no se recalculan los recuentos de escaneos antiguos en cada ciclo
            const runs = await loadRuns({ light: true });
            pollFailures = 0;

            // Localiza el run de ESTE escaneo (nunca un escaneo anterior del mismo sitio)
            if (!activeRunId) {
                const candidate = (runs || []).find((run) =>
                    run.market === market.code &&
                    (siteUrl ? run.site_url === siteUrl : !run.site_url) &&
                    new Date(`${run.started_at}Z`).getTime() >= scanStartedAt - 1000
                );
                if (candidate) activeRunId = candidate.id;
            }
            if (!activeRunId) {
                renderScanProgress({ market: market.code, phase: "waiting" });
                return;
            }

            const detail = await loadActiveRun();
            if (!detail) return;
            if (detail.status === "RUNNING") {
                renderScanProgress({
                    market: market.code,
                    run: detail,
                    phase: $("btnCancel").disabled ? "cancelling" : "running",
                });
                return;
            }
            await finishScan(detail, market.code);
        } catch (error) {
            if (isConnectionError(error)) {
                pollFailures += 1;
                if (pollFailures >= MAX_POLL_FAILURES) {
                    stopScanTimers();
                    setScanButtons({ running: false });
                    renderScanProgress({ market: market.code, phase: "lost" });
                    return;
                }
            } else {
                console.error(error);
            }
            renderScanProgress({ market: market.code, phase: "waiting" });
        }
    }, POLL_INTERVAL_MS);
}

/* ------------------------------------------------------------------
   Cancelar el escaneo en curso.
   Requiere el endpoint POST /api/scan/runs/{id}/cancel del backend.
   Si aún no existe (404), se avisa con claridad en vez de fingir.
------------------------------------------------------------------ */
async function cancelScan() {
    if (!activeRunId) {
        return showAppDialog({
            title: "Aún no se puede cancelar",
            message: "El escaneo todavía se está iniciando. Inténtalo de nuevo en unos segundos.",
        });
    }

    const answer = await showAppDialog({
        title: "Cancelar escaneo",
        message: "Se detendrá cuando termine la página que se está analizando. Lo detectado hasta ahora se conserva en el historial.",
        confirmLabel: "Sí, cancelar",
        cancelLabel: "Seguir escaneando",
    });
    if (answer === null) return;

    setScanButtons({ running: true, cancelling: true });
    renderScanProgress({ market: activeMarketCode, run: activeRun, phase: "cancelling" });

    try {
        await json(`/api/scan/runs/${activeRunId}/cancel`, { method: "POST" });
        // El sondeo detectará el estado CANCELLED y cerrará el escaneo
    } catch (error) {
        setScanButtons({ running: true, cancelling: false });
        renderScanProgress({ market: activeMarketCode, run: activeRun, phase: "running" });
        if (error.status === 404) {
            return showAppDialog({
                title: "El servidor no admite cancelar",
                message: "Falta el endpoint de cancelación en el backend (POST /api/scan/runs/{id}/cancel). Aplica el cambio de scan.py y pipeline.py y reinicia el servidor.",
            });
        }
        if (error.status === 409) {
            return showAppDialog({ title: "El escaneo ya terminó", message: error.message });
        }
        return showAppDialog({
            title: "No se pudo cancelar",
            message: isConnectionError(error)
                ? "No hay conexión con el servidor local."
                : error.message,
        });
    }
}

/* ==========================================================================
   Revisión manual de IDs
   Solo muestra los IDs del ESCANEO ACTIVO. Aparece cuando hay resultados y
   desaparece al limpiar los filtros. Lo anterior vive en el historial.

   Endpoints usados (verifica que existan en tu backend):
     POST /api/assets/{id}/validate    (validar)
     POST /api/assets/missed           (registrar ID no detectado)
   ========================================================================== */
const REVIEWER_KEY = "az_scanner_reviewer";

function showReview() {
    $("review").classList.remove("hidden");
    $("navReview").classList.remove("hidden");
}

function hideReview() {
    $("review").classList.add("hidden");
    $("navReview").classList.add("hidden");
    $("tblVal").querySelector("tbody").replaceChildren();
    // Si el usuario estaba en el ancla de la sección, volver al inicio de la página
    if (window.location.hash === "#review") history.replaceState(null, "", "#scanner");
    updateActiveSection();
}

function renderReviewMarkets() {
    const select = $("mMarketIn");
    if (!select) return;
    const previous = select.value;
    select.innerHTML = markets
        .map((market) => `<option value="${escapeHtml(market.code)}">${escapeHtml(market.code)} · ${escapeHtml(market.name)}</option>`)
        .join("");
    // La región del formulario sigue a la del escaneo activo
    const current = activeMarketCode || $("fMarket").value;
    if (current && markets.some((market) => market.code === current)) select.value = current;
    else if (previous && markets.some((market) => market.code === previous)) select.value = previous;
}

function renderReviewTable() {
    const tbody = $("tblVal").querySelector("tbody");
    const onlyPending = $("vShow").value === "pending";
    const visible = onlyPending ? activeAssets.filter((row) => !row.validated) : activeAssets;

    if (!visible.length) {
        const message = activeAssets.length
            ? "No hay IDs pendientes de revisión."
            : "No hay IDs para revisar en este escaneo.";
        tbody.innerHTML = `<tr><td colspan="6" class="muted">${message}</td></tr>`;
        return;
    }

    tbody.innerHTML = visible.map((asset) => {
        const pageUrl = safeWebUrl(asset.page_url);
        const review = asset.validated
            ? `<span class="badge ${asset.is_correct ? "VIGENTE" : "VENCIDO"}">${asset.is_correct ? "✔ Correcto" : "✘ Incorrecto"}</span>`
            : `<div class="actions">
                <button type="button" class="btn ok" data-review="ok" aria-label="Marcar como correcto">✔</button>
                <button type="button" class="btn bad" data-review="bad" aria-label="Marcar como incorrecto">✘</button>
               </div>`;
        return `
        <tr data-review-id="${asset.id}">
          <td data-label="Estado"><span class="badge ${asset.status}">${statusLabel(asset.status)}</span></td>
          <td data-label="Región">${escapeHtml(asset.market)}</td>
          <td data-label="Veeva ID">${escapeHtml(asset.veeva_id) || "ID no detectado"}</td>
          <td data-label="Vencimiento">${expiryCell(asset)}</td>
          <td data-label="URL">
            <span class="url" title="${escapeHtml(asset.page_url)}">${escapeHtml(asset.page_url)}</span>
            ${pageUrl ? `<a class="link-btn" href="${escapeHtml(pageUrl)}" target="_blank" rel="noopener">Abrir</a>` : ""}
            ${asset.evidence ? `<a class="link-btn" href="/api/assets/${asset.id}/evidence" target="_blank" rel="noopener">Captura</a>` : ""}
          </td>
          <td data-label="Revisión">${review}</td>
        </tr>`;
    }).join("");
}

// La revisión se muestra únicamente si hay un escaneo activo con resultados
function renderReview() {
    if (!activeRunId || !activeAssets.length) {
        hideReview();
        return;
    }
    renderReviewMarkets();
    showReview();
    renderReviewTable();
}

async function reviewValidate(id, isCorrect) {
    const payload = {
        is_correct: isCorrect,
        corrected_year: null,
        corrected_veeva_id: null,
        notes: null,
        reviewed_by: $("vReviewer").value.trim() || null,
    };

    if (!isCorrect) {
        const values = await showAppDialog({
            title: "Corregir detección",
            message: "Indica los valores correctos. Puedes dejar vacío lo que no aplique.",
            fields: [
                { name: "year", label: "Año de vencimiento correcto", type: "number", inputMode: "numeric", placeholder: "Ej. 2026" },
                { name: "veeva", label: "Veeva ID correcto", placeholder: "Ej. CC-123456" },
                { name: "notes", label: "Notas (opcional)", multiline: true },
            ],
            confirmLabel: "Guardar corrección",
            cancelLabel: "Cancelar",
        });
        if (values === null) return;

        const year = parseInt(values.year, 10);
        payload.corrected_year = Number.isFinite(year) ? year : null;
        payload.corrected_veeva_id = values.veeva || null;
        payload.notes = values.notes || null;
    }

    try {
        await json(`/api/assets/${id}/validate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const item = activeAssets.find((row) => row.id === id);
        if (item) {
            item.validated = true;
            item.is_correct = isCorrect;
        }
        renderReviewTable();
    } catch (error) {
        await showAppDialog({ title: "No se pudo guardar la revisión", message: error.message });
    }
}

async function reviewAddMissed() {
    const body = {
        market: $("mMarketIn").value,
        page_url: $("mUrl").value.trim(),
        asset_type: $("mType").value,
        description: $("mDesc").value.trim() || null,
        reviewed_by: $("vReviewer").value.trim() || null,
    };

    if (!body.market) {
        return showAppDialog({ title: "Falta la región", message: "Selecciona la región donde encontraste el ID." });
    }
    if (!body.page_url) {
        return showAppDialog({ title: "Falta la URL", message: "Indica la URL de la página donde lo encontraste." });
    }
    if (!safeWebUrl(body.page_url)) {
        return showAppDialog({ title: "URL no válida", message: "Escribe una URL completa, por ejemplo https://www.ejemplo.com/pagina" });
    }

    const button = $("btnMissed");
    button.disabled = true;
    try {
        await json("/api/assets/missed", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        $("mUrl").value = "";
        $("mDesc").value = "";
        await showAppDialog({
            title: "ID registrado",
            message: "Quedó guardado como no detectado y se incluirá en las métricas del piloto.",
            confirmLabel: "Entendido",
        });
    } catch (error) {
        await showAppDialog({ title: "No se pudo registrar el ID", message: error.message });
    } finally {
        button.disabled = false;
    }
}

/* ------------------------------ Eventos ------------------------------ */

/* ------------------------------------------------------------------
   Sesión para contenido protegido (AzMed / Veeva / Kaltura)

   El dashboard NO puede leer las cookies de azmed.cr, veevavault.com ni
   kaltura.com: la política de mismo origen lo impide. Por eso el login se
   hace en una ventana controlada por Playwright (Chrome, Edge o Chromium),
   que exporta storage_state a data/auth_state.json y el token KS de KMC a
   data/kaltura_session.json. Aquí solo se muestra el progreso, se abre/cierra
   esa ventana y se decide si el escaneo usa la sesión.
------------------------------------------------------------------ */
const PLATFORM_LABELS = {
    azmed: "AzMed / Identity",
    veeva: "Veeva Vault",
    kaltura: "Kaltura",
};

let sessionInfoCache = null;
let sessionStatusPromise = null;
let sessionGateTimer = null;
let sessionGateStarting = false;
let sessionGateRanLogin = false;
let sessionGateError = "";
let sessionGateDismissed = false;
let activeSessionDialog = null;

function platformForHost(host) {
    const value = String(host || "").toLowerCase();
    if (!value) return null;
    if (/azmed|azmedical|idaz|identity\.astrazeneca|login\.astrazeneca|sso\.astrazeneca/.test(value)) return "azmed";
    if (/veevavault|veeva/.test(value)) return "veeva";
    if (/kaltura/.test(value)) return "kaltura";
    return null;
}

function sessionSummary(info) {
    if (!info) return "No se pudo comprobar la sesión.";
    const platforms = info.platforms || {};
    const parts = Object.entries(PLATFORM_LABELS).map(([key, label]) =>
        `${platforms[key] ? "✓" : "✗"} ${label}`
    );
    const stored = [];
    if (info.exists && info.cookies) stored.push(`${info.cookies} cookies guardadas`);
    if (info.kaltura_ks) stored.push("token KS de Kaltura");
    if (!stored.length) stored.push("sin sesión guardada");
    return `${parts.join(" · ")} · ${stored.join(" y ")}`;
}

function selectedSessionBrowser() {
    return $("sessionBrowser")?.value || "auto";
}

function setSessionGateVisible(visible) {
    const gate = $("sessionGate");
    if (!gate) return;
    gate.classList.toggle("hidden", !visible);
    document.body.classList.toggle("session-gate-open", visible);
    // Evita que el teclado navegue por la app que queda detrás de la pantalla.
    document.querySelectorAll("header.topbar, main.container").forEach((element) => {
        element.inert = visible;
    });
    if (visible) sessionGateDismissed = false;
}

function renderSessionGateSteps(process) {
    const list = $("sessionGateSteps");
    if (!list) return;
    const steps = Array.isArray(process?.steps) ? process.steps : [];
    list.replaceChildren();
    if (!steps.length) {
        list.classList.add("hidden");
        return;
    }
    list.classList.remove("hidden");
    steps.forEach((step) => {
        const item = document.createElement("li");
        item.className = `session-step ${step.status || "pending"}`;
        const icon = document.createElement("span");
        icon.className = "session-step-status";
        icon.textContent = step.status === "done" ? "✓"
            : step.status === "active" ? "●"
            : step.status === "skipped" ? "–"
            : step.status === "error" ? "!" : "○";
        const text = document.createElement("span");
        text.className = "session-step-text";
        const label = document.createElement("span");
        label.className = "session-step-label";
        label.textContent = step.label || step.id;
        const detail = document.createElement("span");
        detail.className = "session-step-detail";
        detail.textContent = step.detail
            || (step.status === "active" ? "Esperando inicio de sesión…" : "");
        text.append(label, detail);
        item.append(icon, text);
        list.append(item);
    });
}

function gateButton(label, { variant = "", disabled = false, onClick } = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `btn${variant ? ` ${variant}` : ""}`;
    button.textContent = label;
    button.disabled = Boolean(disabled);
    if (onClick) button.addEventListener("click", onClick);
    return button;
}

function renderSessionGate() {
    const gate = $("sessionGate");
    if (!gate || sessionGateDismissed) return;

    const info = sessionInfoCache;
    const process = info?.process || {};
    const running = Boolean(process.running || info?.running);
    // `finished` persiste entre reinicios de la app: solo se muestra como
    // «Sesión finalizada» si el login se lanzó desde esta pantalla.
    const finished = sessionGateRanLogin && Boolean(process.finished);
    const platforms = info?.platforms || {};
    const covered = Object.keys(PLATFORM_LABELS).filter((key) => platforms[key]);
    const complete = covered.length === Object.keys(PLATFORM_LABELS).length;

    const title = $("sessionGateTitle");
    const message = $("sessionGateMessage");
    const browserBox = $("sessionGateBrowserBox");
    const actions = $("sessionGateActions");

    title.textContent = running
        ? "Iniciando sesión"
        : finished
            ? "Sesión finalizada"
            : complete
                ? "Sesión lista"
                : "Inicia sesión para auditar contenido protegido";

    if (sessionGateError) {
        message.textContent = sessionGateError;
    } else if (running) {
        message.textContent = process.message
            || "Inicia sesión en cada plataforma. La ventana se cerrará sola al detectar el acceso.";
    } else if (finished) {
        message.textContent = process.message || "Sesión finalizada.";
    } else if (info && info.exists) {
        message.textContent = `${sessionSummary(info)}. Puedes continuar con esta sesión o volver a iniciarla.`;
    } else {
        message.textContent = "El escáner puede auditar el contenido público sin sesión. Para auditar AzMed, Veeva y Kaltura, inicia sesión una vez; se abrirá una ventana aparte y podrás usar MFA.";
    }

    const useProcessSteps = (running || sessionGateRanLogin)
        && Array.isArray(process.steps)
        && process.steps.length > 0;
    const steps = useProcessSteps
        ? process.steps
        : Object.entries(PLATFORM_LABELS).map(([id, label]) => ({
            id,
            label,
            status: platforms[id] ? "done" : "pending",
            detail: platforms[id] ? "Sesión detectada" : "Sin sesión",
        }));
    renderSessionGateSteps({ steps });
    browserBox.classList.toggle("hidden", running || sessionGateStarting);
    actions.replaceChildren();

    if (sessionGateStarting && !running) {
        actions.append(gateButton("Abriendo navegador…", { disabled: true }));
        return;
    }

    if (running) {
        if (process.current) {
            actions.append(gateButton("Omitir paso actual", {
                onClick: skipSessionGateStep,
            }));
        }
        actions.append(gateButton("Detener", {
            variant: "danger",
            onClick: stopSessionGateLogin,
        }));
        return;
    }

    if (complete) {
        actions.append(
            gateButton("Continuar al escáner", {
                variant: "primary",
                onClick: () => dismissSessionGate(),
            }),
            gateButton("Volver a iniciar sesión", { onClick: startSessionGateLogin }),
            gateButton("Continuar sin logueo", { onClick: () => dismissSessionGate() }),
        );
        return;
    }

    if (finished && info?.exists) {
        actions.append(
            gateButton("Continuar de todos modos", {
                variant: "primary",
                onClick: () => dismissSessionGate(),
            }),
            gateButton("Volver a iniciar sesión", { onClick: startSessionGateLogin }),
            gateButton("Continuar sin logueo", { onClick: () => dismissSessionGate() }),
        );
        return;
    }

    actions.append(
        gateButton("Iniciar sesión", {
            variant: "primary",
            onClick: startSessionGateLogin,
        }),
        gateButton("Continuar sin logueo", { onClick: () => dismissSessionGate() }),
    );
    if (info?.exists) {
        actions.insertBefore(
            gateButton("Continuar con la sesión guardada", { onClick: () => dismissSessionGate() }),
            actions.lastChild,
        );
    }
}

async function fetchSessionStatus({ force = false } = {}) {
    if (!force && sessionInfoCache && !sessionInfoCache.running) return sessionInfoCache;
    if (sessionStatusPromise) return sessionStatusPromise;
    sessionStatusPromise = (async () => {
        try {
            const info = await json("/api/auth/session-status");
            sessionInfoCache = info;
            if (!sessionGateDismissed) renderSessionGate();
            return info;
        } catch (error) {
            sessionInfoCache = null;
            if (!sessionGateDismissed) renderSessionGate();
            return null;
        } finally {
            sessionStatusPromise = null;
        }
    })();
    return sessionStatusPromise;
}

async function loadSessionBrowsers() {
    const select = $("sessionBrowser");
    const hint = $("sessionBrowserHint");
    if (!select) return;
    try {
        const data = await json("/api/auth/browsers");
        const options = Array.isArray(data?.browsers) ? data.browsers : [];
        select.replaceChildren();
        options.forEach((option) => {
            const item = document.createElement("option");
            item.value = option.id;
            item.textContent = option.available === false
                ? `${option.label} (no instalado)`
                : option.label;
            item.disabled = option.available === false;
            select.append(item);
        });
        select.value = "auto";
        const installed = options.some((option) =>
            (option.id === "chrome" || option.id === "edge") && option.available
        );
        hint.textContent = installed
            ? "Se usará Chrome o Edge si están instalados; si no, el Chromium incluido. Safari no se puede automatizar."
            : "No se detectó Chrome ni Edge: se usará el Chromium incluido. En Mac, instala Chrome o Edge para usar tu navegador habitual.";
    } catch (error) {
        select.replaceChildren();
        const item = document.createElement("option");
        item.value = "auto";
        item.textContent = "Automático (Chromium incluido)";
        select.append(item);
        hint.textContent = "No se pudieron detectar los navegadores instalados; se usará el Chromium incluido.";
    }
}

async function refreshSessionGate() {
    const info = await fetchSessionStatus({ force: true });
    const process = info?.process || {};
    const running = Boolean(process.running || info?.running);
    if (!running) {
        if (sessionGateTimer) {
            clearInterval(sessionGateTimer);
            sessionGateTimer = null;
        }
        if (process.finished || !sessionGateStarting) {
            sessionGateStarting = false;
        }
    }
    renderSessionGate();
    return info;
}

async function startSessionGateLogin() {
    sessionGateStarting = true;
    sessionGateRanLogin = true;
    sessionGateError = "";
    renderSessionGate();
    try {
        const data = await json("/api/auth/prepare-session", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                mode: "guided",
                browser: selectedSessionBrowser(),
            }),
        });
        if (data && data.ok === false) {
            sessionGateError = data.error || "No se pudo abrir el navegador.";
            sessionGateStarting = false;
        }
    } catch (error) {
        sessionGateError = isConnectionError(error)
            ? "No hay conexión con el servidor local."
            : error.message;
        sessionGateStarting = false;
    }
    await refreshSessionGate();
    if (!sessionGateTimer && !sessionGateDismissed) {
        sessionGateTimer = setInterval(refreshSessionGate, 2000);
    }
}

async function skipSessionGateStep() {
    try {
        await json("/api/auth/skip-step", { method: "POST" });
    } catch (error) {
        /* el proceso puede haber terminado */
    }
    await refreshSessionGate();
}

async function stopSessionGateLogin() {
    try {
        await json("/api/auth/session-stop", { method: "POST" });
    } catch (error) {
        /* el proceso puede haber terminado */
    }
    await refreshSessionGate();
}

function dismissSessionGate() {
    sessionGateDismissed = true;
    if (sessionGateTimer) {
        clearInterval(sessionGateTimer);
        sessionGateTimer = null;
    }
    setSessionGateVisible(false);
}

async function initSessionGate() {
    setSessionGateVisible(true);
    renderSessionGate();
    await Promise.allSettled([loadSessionBrowsers(), refreshSessionGate()]);
    const process = sessionInfoCache?.process || {};
    if (process.running && !sessionGateTimer && !sessionGateDismissed) {
        sessionGateTimer = setInterval(refreshSessionGate, 2000);
    }
}

function scanTargetInfo(market, startUrl) {
    const platforms = new Set();
    let requiresLogin = false;

    const considerSite = (site) => {
        if (!site || !site.requires_login) return;
        requiresLogin = true;
        const platform = platformForHost(site.host || new URL(site.url).hostname);
        if (platform) platforms.add(platform);
    };

    if (startUrl) {
        const host = new URL(startUrl).hostname;
        const catalogSite = sites.find((site) => site.url === startUrl);
        if (catalogSite) {
            considerSite(catalogSite);
        } else {
            const platform = platformForHost(host);
            if (platform) {
                requiresLogin = true;
                platforms.add(platform);
            }
        }
    } else if (market) {
        (market.sites || []).forEach(considerSite);
    }

    return { requiresLogin, platforms: [...platforms] };
}

function sessionCovers(info, target) {
    if (!target || !target.requiresLogin) return true;
    if (!info || !info.exists) return false;
    // Sin plataforma identificada no podemos afirmar que la sesión sirva para
    // ese portal: solo se evita el aviso si las tres plataformas están cubiertas.
    if (!target.platforms.length) return Boolean(info.complete);
    return target.platforms.every((key) => Boolean(info.platforms?.[key]));
}

function openSessionDialog({ requiredPlatforms = [], autoStart = true } = {}) {
    if (activeSessionDialog) return activeSessionDialog.promise;

    const dialog = $("sessionDialog");
    const closeButton = $("sessionDialogClose");
    const title = $("sessionTitle");
    const message = $("sessionMessage");
    const stepsList = $("sessionSteps");
    const actionsBox = $("sessionActions");

    let settled = false;
    let timer = null;
    let starting = false;
    let startError = "";
    let currentInfo = null;

    const promise = new Promise((resolve) => {
        const onDialogCancel = (event) => {
            event.preventDefault();
            close({ action: "cancel" });
        };

        function close(result) {
            if (settled) return;
            settled = true;
            if (timer) clearInterval(timer);
            timer = null;
            closeButton.onclick = null;
            dialog.removeEventListener("cancel", onDialogCancel);
            if (dialog.open) dialog.close();
            activeSessionDialog = null;
            resolve(result);
        }

        function setActions(buttons) {
            actionsBox.replaceChildren();
            buttons.forEach((spec) => {
                const button = document.createElement("button");
                button.type = "button";
                button.className = `btn${spec.variant ? ` ${spec.variant}` : ""}`;
                button.textContent = spec.label;
                button.disabled = Boolean(spec.disabled);
                button.addEventListener("click", () => spec.onClick());
                actionsBox.append(button);
            });
        }

        function stepIcon(status) {
            if (status === "done") return "✓";
            if (status === "active") return "●";
            if (status === "skipped") return "–";
            if (status === "error") return "!";
            return "○";
        }

        function renderSteps(process) {
            const steps = Array.isArray(process?.steps) ? process.steps : [];
            stepsList.replaceChildren();
            if (!steps.length) {
                stepsList.classList.add("hidden");
                return;
            }
            stepsList.classList.remove("hidden");
            steps.forEach((step) => {
                const item = document.createElement("li");
                item.className = `session-step ${step.status || "pending"}`;
                const icon = document.createElement("span");
                icon.className = "session-step-status";
                icon.textContent = stepIcon(step.status);
                const text = document.createElement("span");
                text.className = "session-step-text";
                const label = document.createElement("span");
                label.className = "session-step-label";
                label.textContent = step.label || step.id;
                const detail = document.createElement("span");
                detail.className = "session-step-detail";
                detail.textContent = step.detail
                    || (step.status === "active" ? "Esperando inicio de sesión…" : "");
                text.append(label, detail);
                item.append(icon, text);
                stepsList.append(item);
            });
        }

        function render() {
            const info = currentInfo;
            const process = info?.process || {};
            const running = Boolean(process.running || info?.running);
            const finished = Boolean(process.finished);
            const covered = requiredPlatforms.length
                ? requiredPlatforms.every((key) => Boolean(info?.platforms?.[key]))
                : Boolean(info?.complete);
            const missing = requiredPlatforms.filter((key) => !info?.platforms?.[key]);

            title.textContent = running
                ? "Iniciando sesión"
                : (finished ? "Sesión finalizada" : "Sesión para contenido protegido");

            if (startError) {
                message.textContent = startError;
            } else if (running) {
                message.textContent = process.message
                    || "Se abrió el navegador. Inicia sesión en cada plataforma; la ventana se cerrará sola al detectar el acceso.";
            } else if (finished) {
                message.textContent = process.message || "Sesión finalizada.";
            } else {
                message.textContent = `${sessionSummary(info)}. Inicia sesión para auditar también el contenido detrás del login.`;
            }
            renderSteps(process);

            if (starting && !running && !finished) {
                setActions([{ label: "Abriendo navegador…", disabled: true }]);
                return;
            }

            if (running) {
                const buttons = [];
                if (process.current) {
                    buttons.push({
                        label: "Omitir paso actual",
                        onClick: async () => {
                            try {
                                await json("/api/auth/skip-step", { method: "POST" });
                            } catch (error) {
                                /* el proceso puede haber terminado */
                            }
                        },
                    });
                }
                buttons.push({
                    label: "Detener",
                    variant: "danger",
                    onClick: async () => {
                        try {
                            await json("/api/auth/session-stop", { method: "POST" });
                        } catch (error) {
                            /* el proceso puede haber terminado */
                        }
                        await refresh();
                    },
                });
                setActions(buttons);
                return;
            }

            if (finished) {
                if (missing.length) {
                    message.textContent = `${process.message || "Sesión parcial."} Sin sesión detectada en: ${missing.map((key) => PLATFORM_LABELS[key]).join(", ")}.`;
                }
                setActions([
                    {
                        label: covered ? "Continuar escaneo" : "Continuar de todos modos",
                        variant: "primary",
                        onClick: () => close({ action: "continue", useLogin: true }),
                    },
                    {
                        label: "Continuar sin logueo",
                        onClick: () => close({ action: "no-login", useLogin: false }),
                    },
                ]);
                return;
            }

            setActions([
                { label: "Iniciar sesión", variant: "primary", onClick: startLogin },
                { label: "Cerrar", onClick: () => close({ action: "cancel" }) },
            ]);
        }

        async function refresh() {
            currentInfo = await fetchSessionStatus({ force: true });
            const process = currentInfo?.process || {};
            const running = Boolean(process.running || currentInfo?.running);
            if (!running) {
                starting = false;
                if (timer) {
                    clearInterval(timer);
                    timer = null;
                }
            }
            render();
        }

        async function startLogin() {
            starting = true;
            startError = "";
            render();
            try {
                const data = await json("/api/auth/prepare-session", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        mode: "guided",
                        browser: selectedSessionBrowser(),
                    }),
                });
                if (data && data.ok === false) {
                    startError = data.error || "No se pudo abrir el navegador.";
                    starting = false;
                }
            } catch (error) {
                startError = isConnectionError(error)
                    ? "No hay conexión con el servidor local."
                    : error.message;
                starting = false;
            }
            await refresh();
            if (!timer && !settled) {
                timer = setInterval(refresh, 2000);
            }
        }

        closeButton.onclick = () => close({ action: "cancel" });
        dialog.addEventListener("cancel", onDialogCancel);

        render();
        dialog.showModal();

        if (autoStart) {
            startLogin();
        } else {
            refresh().then(() => {
                if (currentInfo?.running && !timer && !settled) {
                    timer = setInterval(refresh, 2000);
                }
            });
        }
    });

    activeSessionDialog = { promise };
    return promise;
}

initSessionGate();

// Los filtros del formulario solo sirven para elegir QUÉ escanear.
// Estado y búsqueda sí filtran el escaneo activo, pero solo en pantalla.
$("fStatus").addEventListener("change", renderActiveRun);
let searchTimer;
$("fSearch").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderActiveRun, 250);
});

$("fMarket").addEventListener("change", () => {
    const nextMarket = markets.find((market) => market.code === $("fMarket").value);
    const selectedSite = sites.find((site) => site.url === $("fSite").value);
    const host = siteOrigin($("fSiteUrl").value)
        ? new URL(siteOrigin($("fSiteUrl").value)).hostname.toLowerCase()
        : "";
    const hostAllowed = nextMarket && nextMarket.domain_allowlist.some((domain) =>
        host === domain.toLowerCase() || host.endsWith(`.${domain.toLowerCase()}`)
    );
    if ((selectedSite && selectedSite.market.code !== $("fMarket").value) || (host && !hostAllowed)) {
        $("fSite").value = "";
        $("fSiteUrl").value = "";
        $("fSiteCategory").value = "";
    }
    renderCountries();
    renderSites();
    updateExternalScope();
});
$("fCountry").addEventListener("change", () => {
    const selectedSite = sites.find((site) => site.url === $("fSite").value);
    if (selectedSite && selectedSite.country !== $("fCountry").value) {
        $("fSite").value = "";
        $("fSiteUrl").value = "";
    }
    renderSites();
    updateExternalScope();
});
$("fSiteSearch").addEventListener("input", renderSites);
$("fSite").addEventListener("change", () => {
    const site = sites.find((candidate) => candidate.url === $("fSite").value);
    if (site) {
        $("fMarket").value = site.market.code;
        renderCountries();
        $("fCountry").value = site.country;
        $("fSiteUrl").value = site.url;
        $("fSiteCategory").value = site.category;
        renderSites();
    } else {
        // «Todos los sitios»: se escanearán todos los sitios de la región elegida
        $("fSiteUrl").value = "";
        $("fSiteCategory").value = "";
    }
    updateExternalScope();
});
$("fSiteUrl").addEventListener("input", updateExternalScope);

$("assetsDetails").addEventListener("toggle", () => {
    if ($("assetsDetails").open) {
        renderDetailTable();
        return;
    }
    $("tbl").querySelector("tbody").replaceChildren();
    updateExportButtons();
});

$("btnScan").addEventListener("click", startScan);
$("btnCancel").addEventListener("click", cancelScan);
$("btnRefresh").addEventListener("click", clearView);

$("tbl").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-id]");
    if (button) openModal(+button.dataset.id);
});
$("tblRuns").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-run-id]");
    if (button) openRunModal(+button.dataset.runId);
});
$("modalClose").addEventListener("click", () => $("modal").classList.add("hidden"));
$("modal").addEventListener("click", (event) => {
    if (event.target.id === "modal") $("modal").classList.add("hidden");
});

// Exportación del detalle de activos
$("btnExportCsv").addEventListener("click", exportTableCsv);
$("btnExportPdf").addEventListener("click", exportTablePdf);

// Cerrar el modal de detalle con Escape
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("modal").classList.contains("hidden")) {
        $("modal").classList.add("hidden");
    }
});

// KPIs operables con teclado (Enter / Espacio): filtran el escaneo activo
document.querySelectorAll(".kpi[data-status]").forEach((kpi) => {
    kpi.setAttribute("role", "button");
    kpi.setAttribute("tabindex", "0");
    const activate = () => {
        if (!activeRunId) return; // sin escaneo activo no hay nada que filtrar
        $("fStatus").value = kpi.dataset.status;
        $("advancedFilters").open = true;
        $("assetsDetails").open = true;
        renderActiveRun();
    };
    kpi.addEventListener("click", activate);
    kpi.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            activate();
        }
    });
});

// Eventos de la revisión manual
$("tblVal").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-review]");
    if (!button) return;
    const id = Number(button.closest("tr").dataset.reviewId);
    reviewValidate(id, button.dataset.review === "ok");
});
$("vShow").addEventListener("change", renderReviewTable);
$("btnReload").addEventListener("click", () => loadActiveRun().catch((error) => {
    if (isConnectionError(error)) reportConnectionLost();
    else console.error(error);
}));
$("btnMissed").addEventListener("click", reviewAddMissed);
$("vReviewer").value = localStorage.getItem(REVIEWER_KEY) || "";
$("vReviewer").addEventListener("change", () => {
    localStorage.setItem(REVIEWER_KEY, $("vReviewer").value.trim());
});

const sectionLinks = [...document.querySelectorAll('.topbar nav a[href^="#"]')];
function updateActiveSection() {
    const currentHash = window.location.hash || "#scanner";
    sectionLinks.forEach((link) => {
        const isCurrent = link.hash === currentHash;
        link.classList.toggle("active", isCurrent);
        if (isCurrent) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
    });
}
window.addEventListener("hashchange", updateActiveSection);
updateActiveSection();

let foregroundRefreshTimer;
function refreshOnReturn() {
    if (document.visibilityState !== "visible") return;
    // No refrescar mientras hay un escaneo en curso: ya lo hace el sondeo
    if (pollTimer) return;
    clearTimeout(foregroundRefreshTimer);
    foregroundRefreshTimer = setTimeout(() => refreshAll(), 200);
}

document.addEventListener("visibilitychange", refreshOnReturn);
window.addEventListener("focus", refreshOnReturn);

(async () => {
    setScanButtons({ running: false });
    try {
        await loadMarkets();
    } catch (error) {
        if (isConnectionError(error)) await reportConnectionLost();
        else await showAppDialog({ title: "No se pudieron cargar los mercados", message: error.message });
        return;
    }
    // Al abrir la página: cuadros y revisión vacíos; solo se carga el historial
    clearActiveRun();
    await refreshAll();
})();
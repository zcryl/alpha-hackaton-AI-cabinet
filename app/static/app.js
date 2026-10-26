// Лёгкий скелет интерфейса без сборки. Решения принимает сервер, здесь только показ и отправка.
// Где править: FIELDS (поля формы), renderClient / renderOfficer (экраны), renderDetail (карточка заявки).

const API = "/api/v1";
const AUTOSAVE_MS = 2000;

const FIELDS = [
  { key: "company_name", label: "Название ИП", type: "text", full: true },
  { key: "inn", label: "ИНН (12 цифр)", type: "text" },
  { key: "industry", label: "Отрасль", type: "select", options: { trade: "Торговля", food: "Общепит", construction: "Строительство", transport: "Транспорт" } },
  { key: "business_age_months", label: "Срок работы бизнеса, мес.", type: "number", int: true },
  { key: "monthly_revenue", label: "Выручка в месяц (среднее за 6 мес.), ₽", type: "number" },
  { key: "monthly_expenses", label: "Расходы в месяц, ₽", type: "number" },
  { key: "existing_loans_monthly", label: "Текущие платежи по кредитам в месяц, ₽", type: "number" },
  { key: "requested_amount", label: "Сумма кредита, ₽", type: "number" },
  { key: "requested_term_months", label: "Срок кредита, мес.", type: "number", int: true },
  { key: "purpose", label: "Цель или комментарий (необязательно)", type: "text", full: true },
];
const LABEL = Object.fromEntries(FIELDS.map(f => [f.key, f.label]));
const STATUS_RU = { DRAFT: "Черновик", SUBMITTED: "Отправлена", CHECKS: "Проверки", SCORING: "Оценка", ACTION_REQUIRED: "Нужны данные",
  MANUAL_REVIEW: "Ручное рассмотрение", APPROVED: "Одобрено", REJECTED: "Отказ" };
const ZONES = { red: "красная зона", gray: "серая зона", green: "зелёная зона" };

const state = {
  token: sessionStorage.getItem("token"), role: sessionStorage.getItem("role"), name: sessionStorage.getItem("name"),
  apps: [], selected: null, detail: null, products: [], news: [], message: null,
};
const root = document.getElementById("app");
let saveTimer = null, saveChain = Promise.resolve(), dirty = false, selSeq = 0;

// ---------- утилиты ----------
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = n => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n) + " ₽";
const money2 = n => new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n) + " ₽";
const when = iso => new Date(iso).toLocaleString("ru-RU");
const cls = status => String(status).toLowerCase();
const $ = sel => root.querySelector(sel);

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(API + path, {
    method,
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(state.token ? { Authorization: "Bearer " + state.token } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && state.token) { logout(); throw new Error("Сессия истекла, войдите снова"); }
  if (!res.ok) throw new Error(errorText(data.detail) || "Ошибка " + res.status);
  return data;
}

function errorText(d) {
  if (!d) return "";
  if (typeof d === "string") return d;
  if (d.missing) return d.message + ": " + d.missing.map(k => LABEL[k] || k).join(", ");
  if (d.errors) return d.message + ": " + d.errors.join("; ");
  return d.message || JSON.stringify(d);
}

function flash(text, kind = "bad") { state.message = { text, kind }; }
function logout() {
  clearTimeout(saveTimer);
  sessionStorage.clear(); dirty = false;
  Object.assign(state, { token: null, role: null, name: null, apps: [], selected: null, detail: null });
  render();
}

// ---------- вход ----------
function renderLogin() {
  root.innerHTML = `
    <div class="card login">
      <img src="/assets/logo.svg" alt="" width="48" height="48">
      <h1>Умный кредитный кабинет</h1>
      <p class="muted">Учебный прототип. Демо-логины: client (Иванов), client2 (Смирнов), client3 (Петров), officer (сотрудник банка).</p>
      <form id="login">
        <label>Логин</label><input name="username" value="client" autocomplete="username">
        <label>Пароль</label><input name="password" type="password" value="demo123" autocomplete="current-password">
        ${banner()}
        <div class="actions"><button class="primary">Войти</button></div>
      </form>
    </div>`;
  $("#login").onsubmit = async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api("/auth/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
      Object.assign(state, { token: r.token, role: r.role, name: r.full_name });
      sessionStorage.setItem("token", r.token); sessionStorage.setItem("role", r.role); sessionStorage.setItem("name", r.full_name);
      state.message = null;
      await boot();
    } catch (err) { flash(err.message); renderLogin(); }
  };
}

function banner() {
  if (!state.message) return "";
  const m = state.message; state.message = null;
  return `<div class="notice ${m.kind}">${esc(m.text)}</div>`;
}

const MENU = ["Лента операций","Выписка","Счета","Карты","Справки","Импорт реестров","Инкассация","Эквайринг и касса","Пульс рынка","Зарплатный проект","Документооборот","Депозиты"];
function topbar() { return ""; }
function counters() {
  const n = st => state.apps.filter(a => a.status === st).length;
  const rows = [["Черновики", n("DRAFT")], ["Нужны данные", n("ACTION_REQUIRED")], ["На рассмотрении", n("MANUAL_REVIEW")], ["Одобрено", n("APPROVED")], ["Отказ", n("REJECTED")]];
  return `<div class="card works"><h3>Дела в работе</h3>${rows.map(([t, c]) =>
    `<div class="wrow ${c ? "" : "zero"}"><span>${t}</span><span class="cnt ${c && (t === "Нужны данные" || t === "Отказ") ? "hot" : ""}">${c}</span></div>`).join("")}</div>`;
}
function shell(inner) {
  const officer = state.role === "officer";
  const menu = MENU.map(m => `<a class="mi dis" title="Вне прототипа">${m}</a>`).join("");
  return `<div class="shell"><aside class="side">
      <div class="logo"><img src="/assets/logo.svg" alt="" width="36" height="36"><b>Альфа-Бизнес</b></div>
      <a class="mi dis">Все сервисы</a><hr>${menu}
      <a class="mi on">${officer ? "Кредитные заявки" : "Кредитные продукты"}</a>
    </aside>
    <div class="body"><header class="head"><span class="muted small">${officer ? "Рабочее место сотрудника банка" : "Учебный прототип · данные синтетические"}</span><span class="spacer"></span>
      <span class="who"><b>${esc(state.name)}</b><br><span class="muted small">${officer ? "сотрудник банка" : "предприниматель"}</span></span>
      ${officer ? '<button id="reset" class="small" title="Только демо: удаляет заявки всех клиентов">Сбросить демо</button>' : ""}<button id="logout">Выйти</button></header>
      <div class="content">${inner}</div></div></div>`;
}
function bindTopbar() {
  $("#logout").onclick = async () => { await flushDraft(); logout(); };
  if ($("#reset")) $("#reset").onclick = async () => {
    if (!confirm("Удалить все заявки и вернуть демо в начальное состояние?")) return;
    try { await api("/demo/reset", { method: "POST" }); } catch (e) { flash(e.message); }
    logout();
  };
}

// ---------- загрузка ----------
async function boot() {
  try {
    state.apps = await api("/applications");
    if (state.role === "client") state.products = await api("/products");
    if (state.selected) await select(state.selected, false);
  } catch (e) { flash(e.message); }
  render();
}

async function select(id, rerender = true) {
  await flushDraft();                       // несохранённые правки текущей заявки не теряем
  const seq = ++selSeq;
  state.selected = id;
  try {
    const detail = await api("/applications/" + id);
    let news = [];
    if (state.role === "client") {
      const d = detail.data || {};
      const q = new URLSearchParams({ product_type: detail.product_type, status: detail.status, ...(d.industry ? { industry: d.industry } : {}) });
      news = await api("/news?" + q);
    }
    if (seq !== selSeq) return;             // за это время открыли другую заявку: ответ устарел
    state.detail = detail; state.news = news;
  } catch (e) { if (seq !== selSeq) return; flash(e.message); state.detail = null; state.selected = null; }
  if (rerender) render();
}

async function refreshAll() {
  await flushDraft();
  try { await refreshList(); if (state.selected) await select(state.selected, false); } catch (e) { flash(e.message); }
  render();
}
async function refreshList() { state.apps = await api("/applications"); }

// ---------- общий каркас ----------
function render() {
  if (!state.token) return renderLogin();
  state.role === "officer" ? renderOfficer() : renderClient();
}

function appItem(a) {
  const who = a.data?.company_name ? esc(a.data.company_name) : "Без названия";
  return `<div class="list-item ${a.id === state.selected ? "active" : ""}" data-id="${a.id}">
    <div class="top"><strong>№${a.id} · ${esc(a.product_name)}</strong><span class="badge ${cls(a.status)}">${esc(a.status_label)}</span></div>
    <div class="muted small">${who}${a.data?.requested_amount ? " · " + money(a.data.requested_amount) : ""}</div></div>`;
}
function bindList() { root.querySelectorAll(".list-item").forEach(el => el.onclick = () => select(Number(el.dataset.id))); }

// ---------- клиент ----------
function renderClient() {
  root.innerHTML = shell(`<h1 class="pg">Кредитные продукты</h1><div class="layout"><aside>
      ${counters()}
      <div class="card"><h2>Новая заявка</h2>
        ${state.products.map(p => `<div class="product"><strong>${esc(p.name)}</strong>
          <div class="muted small">до ${money(p.max_amount)}, срок ${p.min_term_months}–${p.max_term_months} мес., от ${(p.annual_rate * 100).toFixed(0)}% годовых (учебные условия)</div>
          <div class="actions"><button class="primary" data-product="${p.code}">Оформить</button></div></div>`).join("")}
      </div>
      <div class="card"><h2>Мои заявки</h2>${state.apps.length ? state.apps.map(appItem).join("") : '<p class="muted">Заявок пока нет</p>'}
        <div class="actions"><button id="refresh">Обновить</button></div></div>
    </aside><main>${banner()}${state.detail ? renderDetail(state.detail) : '<div class="card muted">Выберите продукт или заявку слева</div>'}</main></div>`);
  bindTopbar(); bindList(); bindDetail();
  $("#refresh").onclick = refreshAll;
  root.querySelectorAll("[data-product]").forEach(b => b.onclick = async () => {
    try { await flushDraft(); const a = await api("/applications", { method: "POST", body: { product_type: b.dataset.product } }); await refreshList(); await select(a.id); }
    catch (e) { flash(e.message); render(); }
  });
}

// ---------- сотрудник ----------
function renderOfficer() {
  root.innerHTML = shell(`<h1 class="pg">Кредитные заявки</h1><div class="layout"><aside>
      <div class="card"><h2>Очередь заявок</h2>${state.apps.length ? state.apps.map(appItem).join("") : '<p class="muted">Отправленных заявок нет</p>'}
      <div class="actions"><button id="refresh">Обновить</button></div></div>
    </aside><main>${banner()}${state.detail ? renderDetail(state.detail) : '<div class="card muted">Выберите заявку в очереди</div>'}</main></div>`);
  bindTopbar(); bindList(); bindDetail();
  $("#refresh").onclick = refreshAll;
}

// ---------- карточка заявки ----------
function renderDetail(a) {
  const editable = state.role === "client" && (a.status === "DRAFT" || a.status === "ACTION_REQUIRED");
  const d = a.decision, c = a.clarification;
  const tone = !d ? "" : d.outcome === "approved" ? "ok" : d.outcome === "rejected" ? "bad" : "warn";
  return `<div class="card">
    <div style="display:flex;justify-content:space-between;gap:1rem">
      <h2>Заявка №${a.id}: ${esc(a.product_name)}</h2><span class="badge ${cls(a.status)}">${esc(a.status_label)}</span></div>
    ${a.status === "ACTION_REQUIRED" && c ? `<div class="notice warn"><strong>Банк просит уточнить:</strong> ${esc(c.message)}</div>` : ""}
    ${d && a.status !== "ACTION_REQUIRED" ? `<div class="notice ${tone}">${d.client_message ? `<strong>${esc(d.client_message)}</strong><br>` : ""}
        ${d.review_reason && a.status === "MANUAL_REVIEW" ? `<span>${esc(d.review_reason)}</span><br>` : ""}
        ${d.next_step ? "<strong>Что дальше:</strong> " + esc(d.next_step) : ""}</div>` : ""}
    ${c?.reply && a.status !== "ACTION_REQUIRED" ? `<div class="notice"><strong>Ваш ответ банку:</strong> ${esc(c.reply)}</div>` : ""}
    ${editable ? renderForm(a) : renderData(a.data)}
    ${state.role === "client" && a.status === "MANUAL_REVIEW" ? `<div class="actions"><button id="edit">Изменить заявку</button></div>
      <p class="muted small">Заявка вернётся в черновик. После правок её нужно отправить заново, проверки и оценка пройдут повторно.</p>` : ""}
    ${state.role === "client" && a.status === "REJECTED" ? `<div class="actions"><button id="repeat" class="primary">Подать повторно в 1 клик</button></div>` : ""}
    ${state.role === "client" && a.status === "APPROVED" ? `<div class="actions"><button id="repeat">Создать новую заявку на основе этой</button></div>` : ""}
  </div>
  ${d ? renderResult(a) : ""}
  ${state.role === "officer" && a.status === "MANUAL_REVIEW" ? renderOfficerActions(a) : ""}
  ${a.events?.length ? `<div class="card"><h3>История</h3><ul class="timeline">${collapseEvents(a.events).map(e =>
      `<li><strong>${esc(eventText(e))}</strong> <span class="muted small">${esc(e.actor)} · ${when(e.at)}</span></li>`).join("")}</ul></div>` : ""}
  ${state.role === "client" && state.news.length ? `<div class="card"><h3>Новости для вас</h3>${state.news.map(n =>
      `<p><strong>${esc(n.title)}</strong><br><span class="muted small">${esc(n.body)}</span></p>`).join("")}</div>` : ""}`;
}

// Подряд идущие «Данные сохранены» одного автора показываем одной строкой (для старых записей в базе).
function collapseEvents(events) {
  return events.filter((e, i) => !(e.kind === "data_saved" && i > 0 && events[i - 1].kind === "data_saved" && events[i - 1].actor === e.actor));
}

function eventText(e) {
  const map = { created: "Заявка создана", data_saved: "Данные сохранены", submit: "Заявка отправлена", status: "Статус: " + (STATUS_RU[e.to_status] || e.to_status),
    decision: "Автоматическое решение", clarification_requested: "Запрошено уточнение", clarification_answered: "Клиент ответил на запрос",
    officer_decision: "Решение сотрудника", reopened_for_edit: "Клиент вернул заявку на правку", check_failed: "Проверка недоступна, ручное рассмотрение", scoring_failed: "Ошибка оценки, ручное рассмотрение" };
  return map[e.kind] || e.kind;
}

function renderForm(a) {
  const d = a.data || {}, c = a.status === "ACTION_REQUIRED" ? a.clarification : null;
  const fields = FIELDS.map(f => {
    const v = d[f.key] ?? "";
    const input = f.type === "select"
      ? `<select name="${f.key}"><option value="">Выберите</option>${Object.entries(f.options).map(([k, t]) => `<option value="${k}" ${v === k ? "selected" : ""}>${t}</option>`).join("")}</select>`
      : `<input name="${f.key}" type="${f.type}" value="${esc(v)}" ${f.type === "number" ? `min="0" step="${f.int ? 1 : "any"}"${f.int ? ' inputmode="numeric"' : ""}` : ""}>`;
    const flagged = c && c.field === f.key;
    return `<div class="${flagged ? "flagged" : ""}" style="${f.full ? "grid-column:1/-1" : ""}">
      ${flagged ? `<div class="flag"><span class="badge needs">Банк просит уточнить</span> ${esc(c.message)}</div>` : ""}
      <label>${f.label}</label>${input}</div>`;
  }).join("");
  return `<form id="appform"><div class="row">${fields}</div>
    ${c ? `<label>Ваш ответ банку</label><textarea id="reply" rows="3" placeholder="Опишите запрошенные сведения. Загрузка файлов в прототипе не реализована."></textarea>` : ""}
    <div id="formerr" class="notice bad" role="alert" hidden></div>
    <p id="savestate" class="muted small">Изменения сохраняются автоматически каждые 2 секунды</p>
    <div class="actions"><button type="submit" class="primary" id="send">${c ? "Отправить уточнение" : "Отправить заявку"}</button></div></form>`;
}

function renderData(d = {}) {
  const rows = FIELDS.filter(f => d[f.key] !== undefined && d[f.key] !== "").map(f => {
    let v = d[f.key];
    if (f.type === "select") v = f.options[v] || v;
    else if (/₽/.test(f.label)) v = money(v);
    return `<dt>${f.label.replace(/, ₽| \(.*\)/, "")}</dt><dd>${esc(v)}</dd>`;
  });
  return `<dl class="kv">${rows.join("")}</dl>`;
}

function renderResult(a) {
  const s = a.score, d = a.decision, o = d.offer;
  const pos = s ? Math.max(0, Math.min(100, (s.score - 300) / 550 * 100)) : 0;
  return `<div class="card"><h3>Результат</h3>
    ${s ? `<div><span class="score">${s.score}</span> <span class="muted">из 850 · ${ZONES[d.zone] || ""} · ${esc(s.version)}</span></div>
      <div class="zonebar" title="Красная до 520, серая 520–679, зелёная от 680"><span style="left:${pos}%"></span></div>
      <div class="zonelabels small muted"><span>300</span><span>520</span><span>680</span><span>850</span></div>
      <p class="muted small">Балл — только оценка. Решение принимают отдельные правила банка с учётом продукта, суммы и стоп-факторов.
        Долговая нагрузка (DTI): ${s.features.dti === null ? "нет свободных средств" : (s.features.dti * 100).toFixed(0) + "% свободных средств"}.
        Баллы модели: ${s.model_points} из 100, итоговый балл = 300 + 5,5 × баллы модели.</p>
      <ul class="reasons">${s.reasons.map(r => `<li><span class="pts ${r.direction}">${r.points > 0 ? "+" : ""}${r.points}</span><span>${esc(r.text)}</span></li>`).join("")}</ul>` : ""}
    ${d.stop_factors?.length ? `<div class="notice bad"><strong>Причины отказа:</strong><ul>${d.stop_factors.map(x => `<li>${esc(x.text)}</li>`).join("")}</ul></div>` : ""}
    ${d.reason ? `<p><strong>Причина решения сотрудника:</strong> ${esc(d.reason)}</p>` : ""}
    ${o ? `<div class="notice ok"><strong>Условия:</strong> ${money(o.amount)} на ${o.term_months} мес., ${(o.annual_rate * 100).toFixed(1)}% годовых,
        платёж ${money2(o.monthly_payment)} в месяц.<br>
        <span class="small">Всего выплат ${money2(o.total_payments)}, переплата ${money2(o.overpayment)}. Ставка: ${(o.base_rate * 100).toFixed(0)}% по продукту${o.annual_rate > o.base_rate ? ` + ${((o.annual_rate - o.base_rate) * 100).toFixed(0)} п.п. за уровень риска` : ""}.
        ${o.payment_load == null ? "" : `Нагрузка по итоговому платежу: ${(o.payment_load * 100).toFixed(0)}% свободных средств${o.preliminary_load != null && o.preliminary_load !== o.payment_load ? ` (в оценке по базовой ставке: ${(o.preliminary_load * 100).toFixed(0)}%)` : ""}.`}
        <br>${esc(o.note)}</span></div>
      <details><summary>График платежей</summary><table class="small"><tr><th>Мес.</th><th>Платёж</th><th>Проценты</th><th>Основной долг</th><th>Остаток</th></tr>
        ${o.schedule.map(r => `<tr><td>${r.month}</td><td>${money2(r.payment)}</td><td>${money2(r.interest)}</td><td>${money2(r.principal)}</td><td>${money2(r.balance)}</td></tr>`).join("")}</table></details>` : ""}
    ${a.checks ? `<details><summary class="small muted">Результаты проверок (имитации)</summary><pre class="small">${esc(JSON.stringify(a.checks, null, 2))}</pre></details>` : ""}
  </div>`;
}

function renderOfficerActions(a) {
  const d = a.decision || {};
  return `<div class="card"><h3>Действия сотрудника</h3>
    ${d.review_hint ? `<div class="notice warn"><strong>Рекомендация системы:</strong> ${esc(d.review_hint)}</div>` : ""}
    <label>Комментарий</label>
    <textarea id="note" rows="3" placeholder="Для отказа и запроса данных комментарий обязателен: клиент увидит его в кабинете. Для одобрения можно оставить пустым"></textarea>
    <label>Подсветить клиенту поле (необязательно)</label>
    <select id="field"><option value="">Без подсветки</option>${FIELDS.map(f => `<option value="${f.key}">${esc(f.label)}</option>`).join("")}</select>
    <div id="formerr" class="notice bad" role="alert" hidden></div>
    <div class="actions">${d.clarify_template ? '<button id="template">Подставить шаблон запроса</button>' : ""}
      <button id="clarify">Запросить данные</button><button id="approve" class="primary">Одобрить</button><button id="reject">Отказать</button></div></div>`;
}

// ---------- действия в карточке ----------
function readForm(includeEmpty = false) {
  const out = {};
  new FormData($("#appform")).forEach((v, k) => {
    if (v === "" && !includeEmpty) return;
    out[k] = v === "" ? "" : FIELDS.find(f => f.key === k)?.type === "number" ? Number(v) : v;
  });
  return out;
}

// Сохранения идут строго по очереди; значения формы читаются в момент вызова, ответ применяется только к «своей» заявке.
function saveDraft() {
  const a = state.detail;
  if (!a || !$("#appform")) return saveChain;
  const id = a.id, body = { data: readForm(true) };
  dirty = false;
  const el = $("#savestate"); if (el) el.textContent = "Сохраняется…";
  saveChain = saveChain.then(async () => {
    try {
      const r = await api(`/applications/${id}/draft`, { method: "PATCH", body });
      if (state.detail?.id === id) {
        state.detail = { ...state.detail, data: r.data };
        const e2 = $("#savestate"); if (e2) e2.textContent = "Черновик сохранён в " + new Date().toLocaleTimeString("ru-RU");
      }
    } catch (e) {
      if (state.detail?.id === id) { dirty = true; const e2 = $("#savestate"); if (e2) e2.textContent = "Не удалось сохранить: " + e.message; }
    }
  });
  return saveChain;
}
async function flushDraft() {
  clearTimeout(saveTimer);
  if (dirty && $("#appform")) saveDraft();
  await saveChain;
}

function bindDetail() {
  const a = state.detail; if (!a) return;
  const run = async fn => {
    const y = window.scrollY, box = $("#formerr");
    if (box) box.hidden = true;
    try { await fn(); await refreshList(); await select(a.id, false); }
    catch (e) {
      if (box) {   // ошибка рядом с кнопками: форму не перерисовываем, введённый текст остаётся
        box.textContent = e.message; box.hidden = false; box.scrollIntoView({ block: "nearest" });
        $("#send")?.removeAttribute("disabled");
        return;
      }
      flash(e.message);
    }
    render(); window.scrollTo(0, y);
  };
  if ($("#appform")) {
    $("#appform").oninput = () => { dirty = true; clearTimeout(saveTimer); const el = $("#savestate"); if (el) el.textContent = "Есть несохранённые изменения…"; saveTimer = setTimeout(saveDraft, AUTOSAVE_MS); };
    $("#appform").onsubmit = e => {
      e.preventDefault(); clearTimeout(saveTimer); dirty = false; $("#send").disabled = true;   // защита от двойного клика
      run(async () => {
        await api(`/applications/${a.id}/draft`, { method: "PATCH", body: { data: readForm(true) } });
        await api(`/applications/${a.id}/submit`, { method: "POST", body: { reply: $("#reply")?.value || null } });
      });
    };
  }
  const act = (id, path, body) => $(id) && ($(id).onclick = () => run(() => api(`/applications/${a.id}/${path}`, { method: "POST", body: body() })));
  act("#clarify", "clarify", () => ({ message: $("#note").value, field: $("#field").value || null }));
  act("#approve", "decision", () => ({ action: "approve", reason: $("#note").value }));
  act("#reject", "decision", () => ({ action: "reject", reason: $("#note").value }));
  if ($("#template")) $("#template").onclick = () => { $("#note").value = a.decision.clarify_template; };
  if ($("#edit")) $("#edit").onclick = () => run(() => api(`/applications/${a.id}/edit`, { method: "POST" }));
  if ($("#repeat")) $("#repeat").onclick = async () => {
    try { const n = await api(`/applications/${a.id}/repeat`, { method: "POST" }); await refreshList(); await select(n.id); }
    catch (e) { flash(e.message); render(); }
  };
}

// ---------- старт ----------
state.token ? boot() : render();

"use strict";
const $ = (selector) => document.querySelector(selector);
let loggedIn = false, currentStatus = null, poll = null;
const mediaLabels = {photo: "Фото", video: "Видео", animation: "GIF / анимации", video_note: "Видеокружки"};
const skipLabels = {
  text: "Текст без вложений", service: "Служебные сообщения", audio: "Аудио / голосовые",
  sticker: "Стикеры / эмодзи", unsupported_file: "Другие файлы или неизвестный формат",
  protected: "Запрещено сохранение", self_destructing: "Исчезающие вложения",
  unavailable: "Недоступные вложения", pending_video: "Видео ещё обрабатывается",
  link_preview: "Превью ссылок", paid_media: "Платные вложения", story: "Истории / ссылки на истории",
  other: "Другие сообщения (опросы, контакты и т. п.)",
};

function notify(message, error = false) {
  $("#notice").textContent = message;
  $("#notice").classList.toggle("error", error);
  $("#notice").hidden = false;
}
async function api(path, body, method = "POST") {
  const response = await fetch(`/api/${path}`, {
    method, credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-Requested-With": "telegram-photos"},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  const data = await response.json();
  if (response.status === 401) showLogin();
  if (!response.ok) throw new Error(data.detail || "Не удалось выполнить запрос.");
  return data;
}
function showLogin() {
  loggedIn = false;
  clearInterval(poll);
  $("#workspace").hidden = true;
  $("#logout").hidden = true;
  $("#login-panel").hidden = false;
}
function tab(name) {
  for (const el of document.querySelectorAll("[data-tab]")) {
    el.classList.toggle("active", el.dataset.tab === name);
  }
  $("#tab-import").hidden = name !== "import";
  $("#tab-settings").hidden = name !== "settings";
}
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function formatDate(value) {
  return new Date(value).toLocaleString("ru-RU", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    timeZone: "Europe/Moscow",
  });
}
function renderMode() {
  const mode = $("#run-mode").value;
  const labels = {preview: "Проверить без скачивания", download: "Скачать в локальную папку", import: "Скачать и отправить в Immich"};
  const hints = {
    preview: "Файлы не скачиваются. История импорта не меняется. Запросов в Immich не будет.",
    download: "Фото и видео будут скачаны в папку приложения. Immich не используется.",
    import: "Фото и видео будут скачаны, затем отправлены в аккаунт Immich из .env.",
  };
  $("#sync").textContent = labels[mode];
  $("#mode-help").textContent = hints[mode];
  $("#sync").disabled = !currentStatus || currentStatus.busy || !currentStatus.telegram_connected ||
    (mode === "import" && !currentStatus.immich_configured);
}
function renderReport(report) {
  $("#preview-panel").hidden = !report;
  if (!report) return;
  $("#preview-state").textContent = report.complete ? "Проверка завершена. Файлы не скачивались." :
    (currentStatus.busy && currentStatus.mode === "preview" ? "Проверяем чаты… Пока показан частичный результат." : "Проверка завершилась не полностью. Показан частичный результат.");
  $("#preview-results").replaceChildren();
  for (const chat of report.chats) {
    const row = node("div", undefined, "preview-row");
    row.append(node("strong", chat.title), node("span", `Новых: ${chat.new} · Уже скачано: ${chat.known} · Проверено сообщений: ${chat.scanned}`));
    const size = (chat.bytes >= 1e9 ? `${(chat.bytes / 1e9).toFixed(1)} ГБ` : `${(chat.bytes / 1e6).toFixed(1)} МБ`) +
      (chat.unknown_sizes ? ` + ${chat.unknown_sizes} файлов без данных о размере` : "");
    row.append(node("small", `Оценка скачивания: ${size}`, "muted"));
    if (chat.types) row.append(node("span", Object.entries(chat.types)
      .map(([kind, count]) => `${mediaLabels[kind] || kind}: ${count}`).join(" · ") || "Фото и видео не найдены."));
    if (chat.albums) row.append(node("small", `Галереи: ${chat.albums} · вложений в них: ${chat.album_items}`, "muted"));
    if (Object.keys(chat.skipped || {}).length) {
      const details = node("details"), summary = node("summary", "Пропущенные сообщения");
      details.append(summary);
      for (const [kind, count] of Object.entries(chat.skipped)) {
        details.append(node("div", `${skipLabels[kind] || kind}: ${count}`));
      }
      row.append(details);
    }
    if (chat.error) row.append(node("p", chat.error, "error"));
    $("#preview-results").append(row);
  }
}
async function loadStatus(initial = false) {
  const state = await api("status", undefined, "GET");
  currentStatus = state;
  $("#downloaded").textContent = state.stats.downloaded;
  $("#uploaded").textContent = state.stats.uploaded;
  $("#pending").textContent = state.stats.downloaded - state.stats.uploaded;
  $("#phase").textContent = state.phase;
  $("#job-error").textContent = state.error || "";
  $("#job-error").hidden = !state.error;
  $("#last-finished").textContent = state.last_finished ? `Последний запуск завершён: ${formatDate(state.last_finished)}` : "";
  $("#run-mode").disabled = state.busy;
  $("#stop").hidden = !state.busy;
  $("#refresh-chats").disabled = state.busy || !state.telegram_connected;
  $("#save-chats").disabled = state.busy;
  $("#telegram-state").textContent = state.telegram_connected ? "Подключён · сессия сохранена на сервере" : "Ожидает входа";
  $("#telegram-env-state").textContent = state.telegram_configured ? "Данные Telegram заданы в окружении." : "Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.";
  $("#phone-form button").disabled = !state.telegram_configured || state.busy;
  $("#phone-form").hidden = state.telegram_connected;
  $("#disconnect-telegram").hidden = !state.telegram_connected;
  $("#immich-state").textContent = state.immich_account ? `Аккаунт для загрузки: ${state.immich_account}` :
    (state.immich_configured ? "Данные заданы в окружении. Подключение ещё не проверено." : "Не настроен · локальный режим доступен");
  $("#immich-address").textContent = state.immich_url || "";
  $("#check-immich").disabled = !state.immich_configured || state.busy;
  $("#reload-config").disabled = state.busy;
  $("#photos-path").textContent = `Папка для скачивания: ${state.photos_path}`;
  $("#open-immich").href = state.immich_url.replace(/\/api\/?$/, "");
  $("#open-immich").hidden = !state.immich_configured;
  if (initial) {
    $("#schedule-form [name=enabled]").checked = state.auto_sync;
    $("#schedule-form [name=interval]").value = state.interval;
    $("#schedule-form [name=mode]").value = state.schedule_mode;
    $("#run-mode").value = state.default_mode;
  }
  renderMode();
  renderReport(state.report);
  if (state.telegram_connected) {
    $("#code-form").hidden = true;
    $("#password-form").hidden = true;
  }
}
async function loadChats() {
  const chats = await api("chats", undefined, "GET");
  $("#chats").replaceChildren();
  if (!chats.length) $("#chats").append(node("p", "Подключите Telegram и нажмите «Обновить список».", "empty"));
  for (const chat of chats) {
    const row = node("div", undefined, "chat");
    row.dataset.id = chat.id;
    row.dataset.title = [chat.title, ...chat.topics.map((topic) => topic.title)].join(" ").toLocaleLowerCase();
    const title = node("div", undefined, "chat-title");
    const check = node("label", undefined, "check");
    const input = node("input");
    input.type = "checkbox"; input.name = "selected"; input.checked = Boolean(chat.selected);
    check.append(input, node("span", chat.title));
    title.append(check, node("small", `${chat.photos} файлов · ${chat.id}`));
    if (chat.error) title.append(node("p", chat.error, "error"));
    row.append(title);
    for (const [name, label, value] of [["since", "С", chat.since], ["until", "По", chat.until_date]]) {
      const field = node("label", label), date = node("input");
      date.type = "date"; date.name = name; date.value = value || ""; date.required = name === "since";
      field.append(date); row.append(field);
    }
    if (chat.is_forum || chat.topic_ids !== null) {
      const details = node("details", undefined, "topics");
      const summary = node("summary");
      const allLabel = node("label", undefined, "check"), all = node("input");
      all.type = "checkbox"; all.name = "all-topics"; all.checked = chat.topic_ids === null;
      allLabel.append(all, node("span", "Все темы, включая новые"));
      const list = node("div", undefined, "topic-list");
      const topics = [...chat.topics];
      for (const id of chat.topic_ids || []) {
        if (!topics.some((topic) => topic.id === id)) topics.push({id, title: `Тема #${id} (недоступна)`});
      }
      for (const topic of topics) {
        const label = node("label", undefined, "check"), choice = node("input");
        choice.type = "checkbox"; choice.name = "topic"; choice.value = topic.id;
        choice.checked = all.checked || (chat.topic_ids || []).includes(topic.id);
        const suffix = topic.closed ? " · закрыта" : topic.hidden ? " · скрыта" : "";
        label.append(choice, node("span", topic.title + suffix)); list.append(label);
        choice.addEventListener("change", () => {
          all.checked = false;
          input.checked = [...list.querySelectorAll("input")].some((item) => item.checked);
          describe();
        });
      }
      function describe() {
        const count = list.querySelectorAll("input:checked").length;
        summary.textContent = `Темы (${chat.topics.length}) · ${all.checked ? "все темы" : `выбрано: ${count}`}`;
      }
      all.addEventListener("change", () => {
        list.querySelectorAll("input").forEach((choice) => { choice.checked = all.checked; });
        input.checked = all.checked;
        describe();
      });
      details.append(summary, allLabel, node("p", "Период группы применяется ко всем выбранным темам.", "muted"), list);
      if (chat.topics_error) details.append(node("p", `Не удалось обновить темы: ${chat.topics_error}`, "error"));
      if (!topics.length) details.append(node("p", "Нажмите «Обновить список», чтобы получить темы.", "muted"));
      describe();
      row.append(details);
    }
    $("#chats").append(row);
  }
  filterChats();
}
function filterChats() {
  const query = $("#chat-search").value.toLocaleLowerCase();
  for (const row of document.querySelectorAll(".chat")) row.hidden = !row.dataset.title.includes(query);
}
async function loadRecent() {
  const photos = await api("recent", undefined, "GET");
  $("#recent").replaceChildren();
  if (!photos.length) $("#recent").append(node("p", "Здесь появятся фото и видео после первой загрузки.", "empty"));
  for (const photo of photos) {
    const row = node("div", undefined, "recent-row");
    const description = node("span", photo.title);
    description.append(node("small", `${mediaLabels[photo.kind] || "Фото"}${photo.grouped_id ? " · элемент галереи" : ""}`));
    row.append(description, node("small", formatDate(photo.sent_at)));
    if (photo.error) row.append(node("span", photo.error, "error"));
    else if (photo.asset_id && currentStatus.immich_configured) {
      const link = node("a", "В Immich ↗", "badge");
      link.href = currentStatus.immich_url.replace(/\/api\/?$/, "") + "/photos/" + encodeURIComponent(photo.asset_id);
      link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link);
    } else {
      const link = node("a", "Открыть файл ↗", "badge");
      link.href = `/api/media/${encodeURIComponent(photo.media_id)}`;
      link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link);
    }
    $("#recent").append(row);
  }
}
async function enter() {
  await loadStatus(true);
  loggedIn = true;
  $("#login-panel").hidden = true;
  $("#workspace").hidden = false;
  $("#logout").hidden = false;
  await Promise.all([loadChats(), loadRecent()]);
  clearInterval(poll);
  poll = setInterval(async () => {
    if (!loggedIn || document.hidden) return;
    try { await loadStatus(); await loadRecent(); } catch (e) { notify(e.message, true); }
  }, 5000);
}
function action(selector, event, handler) {
  $(selector).addEventListener(event, async (ev) => {
    ev.preventDefault();
    const target = ev.currentTarget;
    const buttons = target.tagName === "FORM" ? [...target.querySelectorAll("button")] : [target];
    buttons.forEach((b) => { b.disabled = true; });
    try { await handler(target); } catch (e) { notify(e.message, true); }
    finally {
      buttons.forEach((b) => { b.disabled = false; });
      if (loggedIn) loadStatus().catch(() => {});
    }
  });
}
for (const el of document.querySelectorAll("[data-tab]")) el.addEventListener("click", () => tab(el.dataset.tab));
$("#chat-search").addEventListener("input", filterChats);
$("#run-mode").addEventListener("change", renderMode);
action("#login-form", "submit", async (form) => {
  await api("login", {password: form.elements.password.value});
  form.reset(); $("#notice").hidden = true; await enter();
});
action("#logout", "click", async () => { await api("logout", {}); showLogin(); });
action("#reload-config", "click", async () => {
  await api("config/reload", {});
  await loadStatus(true);
  notify("Настройки перечитаны из .env. Расписание выключено.");
});
action("#check-immich", "click", async () => {
  const result = await api("immich/check", {});
  notify(`Immich доступен. Аккаунт: «${result.account}».`);
});
action("#phone-form", "submit", async (form) => {
  const result = await api("telegram/send-code", {phone: form.elements.phone.value});
  $("#code-form").hidden = result.status !== "code";
  notify(result.status === "connected" ? "Telegram уже подключён." : "Код отправлен. Проверьте Telegram.");
});
action("#code-form", "submit", async (form) => {
  const result = await api("telegram/code", {code: form.elements.code.value});
  form.reset(); $("#password-form").hidden = result.status !== "password";
  notify(result.status === "password" ? "Введите пароль двухэтапной аутентификации." : "Telegram подключён. Теперь выберите чаты.");
});
action("#password-form", "submit", async (form) => {
  const password = form.elements.password.value; form.reset();
  await api("telegram/password", {password}); notify("Telegram подключён. Теперь выберите чаты.");
});
action("#disconnect-telegram", "click", async () => {
  await api("telegram/disconnect", {}); notify("Сессия Telegram завершена. Автозагрузка выключена."); await loadStatus(true);
});
action("#refresh-chats", "click", async () => {
  await api("chats/refresh", {}); await loadChats(); notify("Список чатов и тем обновлён.");
});
action("#chats-form", "submit", async () => {
  const chats = [...document.querySelectorAll(".chat")].map((row) => ({
    id: Number(row.dataset.id), selected: row.querySelector("[name=selected]").checked,
    since: row.querySelector("[name=since]").value, until: row.querySelector("[name=until]").value || null,
    topic_ids: !row.querySelector("[name=all-topics]") || row.querySelector("[name=all-topics]").checked ? null :
      [...row.querySelectorAll("[name=topic]:checked")].map((choice) => Number(choice.value)),
  }));
  await api("chats", chats, "PUT"); notify("Чаты, темы и периоды сохранены.");
});
action("#schedule-form", "submit", async (form) => {
  await api("schedule", {enabled: form.elements.enabled.checked, interval: Number(form.elements.interval.value), mode: form.elements.mode.value});
  notify(form.elements.enabled.checked ? "Расписание сохранено для выбранного режима." : "Расписание выключено.");
});
action("#sync", "click", async () => {
  const mode = $("#run-mode").value;
  await api("sync", {mode});
  notify(mode === "preview" ? "Началась проверка без скачивания." : "Скачивание началось. Эту страницу можно закрыть.");
});
action("#stop", "click", async () => {
  await api("stop", {}); notify("Загрузка остановится после текущего файла. Автозагрузка выключена.");
  $("#schedule-form [name=enabled]").checked = false;
});
enter().catch((error) => { showLogin(); if (!error.message.includes("Войдите")) notify(error.message, true); });

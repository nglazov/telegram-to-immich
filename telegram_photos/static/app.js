"use strict";
const $ = (selector) => document.querySelector(selector);
let loggedIn = false, currentStatus = null, poll = null;
let filesOffset = 0, filesTotal = 0, filesRequest = 0, filesQuery = "", filesStatus = "all";
const filesLimit = 50;
const fileStatusLabels = {
  pending: "Ожидает отправки", uploaded: "В Immich и альбоме",
  album_pending: "В Immich · ожидает альбом", error: "Ошибка отправки",
};
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
  for (const section of ["import", "files", "settings"]) $(`#tab-${section}`).hidden = section !== name;
  history.replaceState(null, "", `#${name}`);
  if (name === "files" && loggedIn) loadFiles().catch((e) => notify(e.message, true));
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
    row.append(node("strong", chat.title), node("span", `Новых: ${chat.new} · Уже скачано: ${chat.known} · Отложено: ${chat.deferred || 0} · Проверено сообщений: ${chat.scanned}`));
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
  $("#telegram-state").textContent = state.telegram_connected ?
    `Подключён${state.telegram_premium ? " · Premium подтверждён" : ""} · сессия сохранена на сервере` : "Ожидает входа";
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
  $("#open-immich").href = state.immich_public_url;
  $("#open-immich").hidden = !state.immich_configured;
  if (initial) {
    $("#schedule-form [name=enabled]").checked = state.auto_sync;
    $("#schedule-form [name=interval]").value = state.interval;
    $("#schedule-form [name=mode]").value = state.schedule_mode;
    $("#run-mode").value = state.default_mode;
  }
  if (state.busy) $("#run-mode").value = state.mode;
  renderMode();
  renderReport(state.report);
  renderTransfer(state);
  await loadCalendar();
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
      link.href = currentStatus.immich_public_url + "/photos/" + encodeURIComponent(photo.asset_id);
      link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link);
    } else {
      const link = node("a", "Открыть файл ↗", "badge");
      link.href = `/api/media/${encodeURIComponent(photo.media_id)}`;
      link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link);
    }
    $("#recent").append(row);
  }
}
function formatBytes(value) {
  if (value === null) return "Неизвестен";
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} ГБ`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} МБ`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(1)} КБ`;
  return `${value} Б`;
}
async function loadFiles() {
  const request = ++filesRequest;
  const params = new URLSearchParams({offset: filesOffset, limit: filesLimit, q: filesQuery, status: filesStatus});
  const [data, deferred] = await Promise.all([
    api(`files?${params}`, undefined, "GET"), api("deferred", undefined, "GET"),
  ]);
  if (request !== filesRequest || !loggedIn) return;
  renderDeferred(deferred);
  filesTotal = data.total;
  if (filesOffset && filesOffset >= filesTotal) {
    filesOffset = Math.max(0, Math.floor((filesTotal - 1) / filesLimit) * filesLimit);
    return loadFiles();
  }
  const s = data.summary;
  $("#files-summary").textContent = `${s.total} уникальных файлов · ${s.sources} вложений в сообщениях · ${formatBytes(s.bytes)}` +
    `${s.unknown_sizes ? ` + ${s.unknown_sizes} без размера` : ""}. ` +
    `Ожидают отправки: ${s.pending} · В Immich: ${s.uploaded + s.album_pending} · Ожидают альбом: ${s.album_pending} · Ошибок: ${s.errors}.`;
  const open = new Set([...$("#files-list").querySelectorAll("details[open]")].map((el) => el.dataset.id));
  $("#files-list").replaceChildren();
  for (const file of data.items) {
    const row = node("tr"), info = node("td"), dates = node("td"), state = node("td");
    const kinds = [...new Set(file.sources.map((source) => mediaLabels[source.kind] || source.kind))].join(", ");
    const shortName = file.filename.length > 28 ? `${file.filename.slice(0, 12)}…${file.filename.slice(-8)}` : file.filename;
    const link = node("a", `${kinds || "Файл"} · ${shortName}`, "file-name");
    link.title = file.filename;
    link.href = `/api/media/${encodeURIComponent(file.id)}`; link.target = "_blank"; link.rel = "noopener noreferrer";
    info.append(link);
    const sources = node("details");
    sources.dataset.id = file.id; sources.open = open.has(file.id);
    sources.append(node("summary", `${file.sources[0]?.title || "Источники"} · сообщений: ${file.sources.length}`));
    for (const source of file.sources) {
      sources.append(node("div", `${source.title} · сообщение ${source.message_id}` +
        `${source.part === "live_video" ? " · видео Live Photo" : ""}` +
        `${source.grouped_id ? " · галерея" : ""} · ${formatDate(source.sent_at)}`));
    }
    info.append(sources);
    dates.append(node("div", `Скачан: ${file.downloaded_at ? formatDate(file.downloaded_at) : "дата неизвестна"}`));
    dates.append(node("div", `Отправлен: ${file.uploaded_at ? formatDate(file.uploaded_at) : file.asset_id ? "дата неизвестна" : "ещё нет"}`));
    state.append(node("span", fileStatusLabels[file.status], file.status === "error" ? "error" : ""));
    if (file.error) state.append(node("p", file.error, "error"));
    if (file.asset_id && currentStatus.immich_configured) {
      const remote = node("a", "Открыть в Immich ↗");
      remote.href = currentStatus.immich_public_url + "/photos/" + encodeURIComponent(file.asset_id);
      remote.target = "_blank"; remote.rel = "noopener noreferrer"; state.append(remote);
    }
    row.append(info, node("td", formatBytes(file.size_bytes)), dates, state);
    $("#files-list").append(row);
  }
  $("#files-empty").hidden = Boolean(data.items.length);
  $("#files-page").textContent = filesTotal ? `${filesOffset + 1}–${filesOffset + data.items.length} из ${filesTotal}` : "0 файлов";
  $("#files-prev").disabled = filesOffset === 0;
  $("#files-next").disabled = filesOffset + filesLimit >= filesTotal;
}
function renderDeferred(data) {
  $("#deferred-panel").hidden = !data.total;
  $("#deferred-summary").textContent = `Ожидают повтора: ${data.total}.` +
    (data.total > data.items.length ? ` Показаны первые ${data.items.length}.` : "");
  $("#deferred-list").replaceChildren();
  for (const file of data.items) {
    const row = node("div", undefined, "preview-row");
    row.append(node("strong", `${mediaLabels[file.kind] || file.kind} · сообщение ${file.message_id} · ${formatBytes(file.size_bytes)}`));
    row.append(node("span", `${file.title} · ${file.day}`), node("p", file.reason));
    row.append(node("small", `Повторных попыток: ${file.attempts}` +
      (file.last_attempt_at ? ` · последняя: ${formatDate(file.last_attempt_at)}` : ""), "muted"));
    const modes = currentStatus.immich_configured ? ["download", "import"] : ["download"];
    for (const mode of modes) {
      const button = node("button", mode === "import" ? "Повторить и отправить в Immich" : "Повторить скачивание", "secondary");
      button.type = "button";
      button.disabled = currentStatus.busy || !currentStatus.telegram_connected;
      button.onclick = async () => {
        button.disabled = true;
        try {
          await api(`deferred/${file.id}/retry`, {mode});
          await loadStatus(); await loadFiles();
          notify("Запущена отдельная попытка. Действующая пауза Telegram будет соблюдена.");
        } catch (e) { notify(e.message, true); await loadStatus(); await loadFiles(); }
      };
      row.append(button);
    }
    $("#deferred-list").append(row);
  }
}
async function enter() {
  await loadStatus(true);
  loggedIn = true;
  $("#login-panel").hidden = true;
  $("#workspace").hidden = false;
  $("#logout").hidden = false;
  await Promise.all([loadChats(), loadRecent()]);
  tab(["files", "settings"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "import");
  clearInterval(poll);
  poll = setInterval(async () => {
    if (!loggedIn || document.hidden) return;
    try {
      await loadStatus();
      if (!$("#tab-files").hidden) await loadFiles();
      else if (!$("#tab-import").hidden) await loadRecent();
    } catch (e) { notify(e.message, true); }
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
action("#files-filter", "submit", async (form) => {
  filesQuery = form.elements.q.value.trim(); filesStatus = form.elements.status.value; filesOffset = 0;
  await loadFiles();
});
for (const [selector, direction] of [["#files-prev", -1], ["#files-next", 1]]) {
  $(selector).addEventListener("click", () => {
    filesOffset = Math.max(0, filesOffset + direction * filesLimit);
    loadFiles().catch((e) => notify(e.message, true));
  });
}
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


const dayLabels = {
  active: "Обрабатывается", complete: "День проверен, файлы в Immich и альбоме",
  pending: "Есть файлы, ожидающие скачивания, отправки или добавления в альбом", saved: "Есть сохранённые файлы, полнота дня неизвестна",
  empty: "День проверен, подходящих вложений нет", unknown: "Полнота дня ещё не проверена", error: "Ошибка отправки",
};
let calendarData = null, calendarFingerprint = "", calendarSelected = null;
function duration(seconds) {
  seconds = Math.max(0, Math.ceil(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  return `${h ? `${h} ч ` : ""}${m ? `${m} мин ` : ""}${s} с`;
}
function renderTransfer(state) {
  const wait = state.telegram_wait;
  const remaining = state.telegram_retry_after - Date.now() / 1000;
  const waiting = remaining > 0;
  $("#transfer-panel").classList.toggle("waiting", waiting);
  $("#transfer-title").textContent = waiting
    ? "FloodWait — Telegram ограничил запросы"
    : state.error ? "Требуется внимание" : state.phase;
  $("#transfer-wait").textContent = waiting
    ? `До следующей попытки: ${duration(remaining)} · ${formatDate(state.telegram_retry_after * 1000)}. ` +
      (state.busy ? "Продолжим автоматически." : "Задание остановлено. После паузы запустите загрузку.") +
      (wait ? ` ${wait.kind} · попытка ${wait.attempt} · ${wait.request}.` : "")
    : state.error || (wait ? `Последняя пауза: ${wait.kind}, ${formatDate(wait.caught_at)}. Срок ожидания истёк.` : "Активной паузы обычного API нет.");
  const exportRemaining = state.takeout_retry_after - Date.now() / 1000;
  $("#transfer-export").textContent = state.telegram_takeout && exportRemaining > 0
    ? `Takeout отложен до ${formatDate(state.takeout_retry_after * 1000)} (ещё ${duration(exportRemaining)}). ` +
      "Пока используется обычный API; ожидание экспорта не блокирует загрузку."
    : state.takeout_error && !state.takeout_active
      ? `Takeout: ${state.takeout_error.kind}. Для загрузки выбран обычный API.` : "";
  const file = state.current_transfer;
  $("#transfer-file").textContent = file
    ? `${file.chat_title} · ${file.day} · сообщение ${file.message_id}` +
      (file.kind ? ` · ${mediaLabels[file.kind] || file.kind} · ${formatBytes(file.bytes)}${file.total ? ` из ${formatBytes(file.total)}` : ""}` : " · просмотр сообщений")
    : "";
  $("#transfer-bytes").hidden = !file?.total;
  $("#transfer-bytes").value = file?.total ? Math.min(100, file.bytes / file.total * 100) : 0;
  $("#transfer-total").textContent = `Скачано: ${state.stats.downloaded} · В Immich: ${state.stats.uploaded}` +
    ` · Отложено: ${state.deferred_count || 0} · Пауза между файлами: ${state.telegram_download_delay} с` +
    (state.takeout_active ? " · Медиа через Takeout" : " · Обычный Telegram API") +
    (state.telegram_premium ? " · Premium подтверждён" : "");
  $("#progress-stop").hidden = !state.busy;
}
$("#progress-stop").onclick = () => $("#stop").click();
setInterval(() => { if (loggedIn && currentStatus) renderTransfer(currentStatus); }, 1000);

async function loadCalendar() {
  const data = await api("calendar", undefined, "GET");
  const fingerprint = JSON.stringify(data);
  if (fingerprint === calendarFingerprint) return;
  calendarFingerprint = fingerprint;
  calendarData = data;
  const previous = $("#calendar-chat").value;
  $("#calendar-chat").replaceChildren();
  for (const chat of data.chats) {
    const option = node("option", chat.title); option.value = chat.id;
    $("#calendar-chat").append(option);
  }
  if (data.chats.some((c) => String(c.id) === previous)) $("#calendar-chat").value = previous;
  renderCalendar();
}
function renderCalendar() {
  const chat = calendarData?.chats.find((c) => String(c.id) === $("#calendar-chat").value);
  const focusDay = document.activeElement?.dataset.day;
  $("#calendar-months").replaceChildren();
  if (!chat) { $("#calendar-detail").textContent = "Выберите чат и период на вкладке «Загрузка»."; return; }
  if (!chat.days.some((d) => d.day === calendarSelected)) {
    calendarSelected = chat.days.find((d) => d.state === "active")?.day || chat.days.at(-1)?.day;
  }
  const months = new Map();
  for (const day of chat.days) {
    const month = day.day.slice(0, 7);
    if (!months.has(month)) months.set(month, []);
    months.get(month).push(day);
  }
  for (const [month, days] of months) {
    const section = node("section", undefined, "calendar-month");
    section.append(node("h4", new Date(`${month}-01T12:00:00Z`).toLocaleDateString("ru-RU", {month: "long", year: "numeric", timeZone: "UTC"})));
    const grid = node("div", undefined, "calendar-grid");
    for (const name of ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]) grid.append(node("span", name, "weekday"));
    const offset = (new Date(`${days[0].day}T12:00:00Z`).getUTCDay() + 6) % 7;
    for (let i = 0; i < offset; i++) grid.append(node("span"));
    for (const day of days) {
      const button = node("button", String(Number(day.day.slice(-2))), `calendar-day ${day.state}`);
      button.type = "button"; button.dataset.day = day.day;
      button.setAttribute("aria-label", `${day.day}: ${dayLabels[day.state]}; скачано ${day.downloaded}, в Immich ${day.uploaded}, отложено ${day.deferred}`);
      button.title = button.getAttribute("aria-label");
      button.setAttribute("aria-pressed", String(day.day === calendarSelected));
      button.onclick = () => { calendarSelected = day.day; renderCalendar(); };
      grid.append(button);
    }
    section.append(grid); $("#calendar-months").append(section);
  }
  const selected = chat.days.find((d) => d.day === calendarSelected);
  $("#calendar-detail").textContent = selected
    ? `${selected.day} · ${dayLabels[selected.state]}. Вложений скачано: ${selected.downloaded} · В Immich: ${selected.uploaded} · В альбоме: ${selected.in_album} · Отложено: ${selected.deferred}. ` +
      (!selected.covered ? "Полнота выгрузки дня пока не подтверждена." : "История выбранных тем за день просмотрена.")
    : "В выбранном периоде пока нет прошедших дней.";
  if (focusDay) document.querySelector(`[data-day="${focusDay}"]`)?.focus({preventScroll: true});
}
$("#calendar-chat").onchange = () => { calendarSelected = null; renderCalendar(); };

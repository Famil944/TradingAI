"use strict";
(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  const node = (id) => document.getElementById(id);
  const element = (tag, klass, value) => {
    const n = document.createElement(tag);
    if (klass) n.className = klass;
    if (value !== undefined && value !== null) n.textContent = String(value);
    return n;
  };
  const show = (parent, child) => parent.appendChild(child);
  const pct = (value) => value === null ? "—" : (100 * value).toFixed(1) + "%";
  const empty = (id) => { node(id).replaceChildren(); return node(id); };
  function render(data) {
    node("status").textContent = data.running ? "Поток · " + data.phase : "Не запущен";
    node("monitored").textContent = data.monitored;
    node("age").textContent = data.quote_age_s === null ? "—" : data.quote_age_s + " с";
    let total = 0;
    const cards = empty("strategies");
    for (const [label, m] of Object.entries(data.strategies)) {
      total += m.total;
      const c = element("article", "card");
      const top = element("div", "top");
      show(top, element("span", "label", label));
      show(top, element("span", "muted", "Виртуально · 7 дней"));
      show(c, top);
      const s = element("div", "stats");
      for (const [name, value] of [["Сигналов", m.total], ["WIN / LOSS", m.wins + " / " + m.losses], ["Винрейт", pct(m.win_rate)]]) {
        const item = element("div");
        show(item, element("strong", "", value));
        show(item, element("small", "", name));
        show(s, item);
      }
      show(c, s);
      show(cards, c);
    }
    node("total").textContent = total;
    const recent = empty("signals");
    if (!data.recent.length) show(recent, element("p", "muted", "Сигналов пока нет"));
    for (const s of data.recent) {
      const c = element("article", "card");
      const title = element("div", "top");
      show(title, element("span", "label", s.symbol + " · M" + s.minutes));
      show(title, element("strong", s.direction === "UP" ? "up" : "down", s.direction === "UP" ? "↑ ВВЕРХ" : "↓ ВНИЗ"));
      show(c, title);
      show(c, element("div", "muted", s.source + " · " + new Date(s.entry_at * 1000).toLocaleString("ru-RU", { timeZone: "Europe/Moscow", hour12: false }) + " МСК"));
      show(c, element("div", "muted", "Исход: " + s.outcome + " · Метод: " + (s.result_source || "неизвестен") + " · Индикаторы: " + (s.confirmations === null ? "—" : s.confirmations) + " · Рынок: " + (s.regime || "не определён")));
      show(recent, c);
    }
  }
  async function refresh() {
    if (!tg || !tg.initData) {
      node("status").textContent = "Открой через Telegram";
      return;
    }
    node("status").textContent = "Обновление…";
    try {
      const response = await fetch("/api/dashboard", {
        headers: { "Authorization": "tma " + tg.initData },
        cache: "no-store"
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      render(await response.json());
    } catch (err) {
      node("status").textContent = "Нет доступа или связи";
    }
  }
  if (tg) { tg.ready(); tg.expand(); }
  node("refresh").addEventListener("click", refresh);
  refresh();
})();

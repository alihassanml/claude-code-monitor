/* Chart.js wiring, theme switching, and the live WebSocket feed. */

(function () {
  "use strict";

  const charts = {};
  let currentDays = 30;
  let C = {};

  /* Colors live in CSS custom properties, so they're re-read after a theme
     switch rather than baked in at load. */
  function readColors() {
    const s = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (s.getPropertyValue(name) || fallback).trim();
    C = {
      text: v("--text", "#e9ecf1"),
      text2: v("--text-2", "#aeb6c4"),
      muted: v("--muted", "#737d8d"),
      line: v("--line", "#222933"),
      surface: v("--surface", "#171716"),
      accent: v("--accent", "#6fc3ac"),
      info: v("--info", "#6fc3ac"),
      good: v("--good", "#6fc3ac"),
      bad: v("--bad", "#e2685f"),
      warn: v("--warn", "#d6a05a"),
      neutral: v("--neutral-bar", "#34342f"),
      // Sequential ramp — the token chart is one quantity split four ways.
      ramp: [v("--ramp-1", "#34342f"), v("--ramp-2", "#4c5a53"),
             v("--ramp-3", "#6d8b80"), v("--ramp-4", "#6fc3ac")],
    };
    // Cost-by-model is genuinely categorical, so it keeps distinct hues —
    // but muted ones, anchored on the accent.
    C.series = [C.accent, "#7f9fc4", C.warn, "#b08ab5", C.bad,
                "#6d8b80", "#c98f6a", "#8fa6c9", "#5d9c8b", "#a9a08c"];
    return C;
  }

  const withAlpha = (hex, a) => {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
    if (!m) return hex;
    return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${a})`;
  };

  const shortDay = (d) => d.slice(5);
  const compact = (n) => {
    const a = Math.abs(n);
    if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(n);
  };
  const money = (v) => (v >= 100 ? "$" + Math.round(v) : "$" + Number(v).toFixed(2));

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function axes(opts) {
    const { stacked = false, yFmt = compact, xTicks = true } = opts || {};
    return {
      x: {
        stacked,
        ticks: xTicks
          ? { color: C.muted, font: { size: 10 }, maxRotation: 0, autoSkipPadding: 18 }
          : { display: false },
        grid: { display: false, drawBorder: false },
        border: { color: C.line },
      },
      y: {
        stacked, beginAtZero: true,
        ticks: { color: C.muted, font: { size: 10 }, padding: 6, callback: yFmt },
        grid: { color: C.line, drawBorder: false, tickLength: 0 },
        border: { display: false },
      },
    };
  }

  function baseOptions(extra) {
    return Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 320 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: {
            color: C.text2, boxWidth: 8, boxHeight: 8,
            usePointStyle: true, pointStyle: "circle",
            padding: 14, font: { size: 11 },
          },
        },
        tooltip: {
          backgroundColor: C.surface,
          borderColor: C.line,
          borderWidth: 1,
          titleColor: C.text,
          bodyColor: C.text2,
          padding: 11,
          cornerRadius: 8,
          boxPadding: 5,
          usePointStyle: true,
        },
      },
      scales: axes(),
    }, extra || {});
  }

  function render(id, config) {
    const el = document.getElementById(id);
    if (!el || typeof Chart === "undefined") return null;
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(el, config);
    return charts[id];
  }

  const barStyle = { borderRadius: 3, borderSkipped: false, maxBarThickness: 34 };

  /* ---------------------------------------------------------------- */
  /* dashboard                                                        */
  /* ---------------------------------------------------------------- */

  async function loadDashboard(days) {
    if (!document.getElementById("tokensChart")) return;
    currentDays = days;
    const { series } = await (await fetch(`/api/usage/daily?days=${days}`)).json();
    const labels = series.map((r) => shortDay(r.day));

    render("tokensChart", {
      type: "bar",
      data: {
        labels,
        datasets: [
          { label: "Cache read", data: series.map((r) => r.cache_read_tokens), backgroundColor: C.ramp[0], stack: "t", ...barStyle },
          { label: "Cache write", data: series.map((r) => r.cache_creation_tokens), backgroundColor: C.ramp[1], stack: "t", ...barStyle },
          { label: "Input", data: series.map((r) => r.input_tokens), backgroundColor: C.ramp[2], stack: "t", ...barStyle },
          { label: "Output", data: series.map((r) => r.output_tokens), backgroundColor: C.ramp[3], stack: "t", ...barStyle },
        ],
      },
      options: baseOptions({
        plugins: {
          legend: { labels: { color: C.muted, boxWidth: 8, boxHeight: 8, usePointStyle: true, pointStyle: "circle", padding: 16, font: { size: 10.5 } } },
          tooltip: { backgroundColor: C.surface, borderColor: C.line, borderWidth: 1, titleColor: C.text, bodyColor: C.text2, padding: 11, cornerRadius: 8, usePointStyle: true,
                     callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${compact(ctx.parsed.y)}` } },
        },
        scales: axes({ stacked: true }),
      }),
    });

    render("costChart", {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Cost",
          data: series.map((r) => r.cost_usd),
          borderColor: C.accent,
          backgroundColor: withAlpha(C.accent, 0.10),
          fill: true, tension: .34, borderWidth: 1.75,
          pointRadius: 0, pointHoverRadius: 4,
          pointBackgroundColor: C.accent, pointBorderColor: C.surface, pointBorderWidth: 2,
        }],
      },
      options: baseOptions({
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: C.surface, borderColor: C.line, borderWidth: 1, titleColor: C.text, bodyColor: C.text2, padding: 11, cornerRadius: 8, usePointStyle: true,
                     callbacks: { label: (ctx) => " $" + Number(ctx.parsed.y).toFixed(2) } },
        },
        scales: axes({ yFmt: money }),
      }),
    });
  }

  /* ---------------------------------------------------------------- */
  /* analytics                                                        */
  /* ---------------------------------------------------------------- */

  async function loadAnalytics() {
    if (!document.getElementById("stackedCostChart")) return;
    const days = Number(new URLSearchParams(location.search).get("days") || 30);
    const cost = await (await fetch(`/api/usage/cost?days=${days}`)).json();
    const bdm = cost.by_day_and_model;

    render("stackedCostChart", {
      type: "bar",
      data: {
        labels: bdm.days.map(shortDay),
        datasets: bdm.series.map((s, i) => ({
          label: s.model, data: s.cost,
          backgroundColor: C.series[i % C.series.length],
          stack: "cost", ...barStyle,
        })),
      },
      options: baseOptions({
        plugins: {
          legend: { labels: { color: C.text2, boxWidth: 8, boxHeight: 8, usePointStyle: true, pointStyle: "circle", padding: 14, font: { size: 11 } } },
          tooltip: { backgroundColor: C.surface, borderColor: C.line, borderWidth: 1, titleColor: C.text, bodyColor: C.text2, padding: 11, cornerRadius: 8, usePointStyle: true,
                     callbacks: { label: (ctx) => ` ${ctx.dataset.label}: $${Number(ctx.parsed.y).toFixed(2)}` } },
        },
        scales: axes({ stacked: true, yFmt: money }),
      }),
    });

    const t = cost.by_model.reduce((a, m) => {
      a.input += m.input_tokens; a.output += m.output_tokens;
      a.read += m.cache_read_tokens; a.write += m.cache_creation_tokens;
      return a;
    }, { input: 0, output: 0, read: 0, write: 0 });
    const totalTokens = t.input + t.output + t.read + t.write || 1;

    render("tokenMixChart", {
      type: "doughnut",
      data: {
        labels: ["Cache read", "Cache write", "Input", "Output"],
        datasets: [{
          data: [t.read, t.write, t.input, t.output],
          backgroundColor: C.ramp,
          borderColor: C.surface, borderWidth: 3, hoverOffset: 6,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: "64%",
        plugins: {
          legend: { position: "right", labels: { color: C.text2, boxWidth: 8, boxHeight: 8, usePointStyle: true, pointStyle: "circle", padding: 12, font: { size: 11 } } },
          tooltip: {
            backgroundColor: C.surface, borderColor: C.line, borderWidth: 1,
            titleColor: C.text, bodyColor: C.text2, padding: 11, cornerRadius: 8, usePointStyle: true,
            callbacks: {
              label: (ctx) => ` ${ctx.label}: ${compact(ctx.parsed)} (${(ctx.parsed / totalTokens * 100).toFixed(1)}%)`,
            },
          },
        },
      },
    });

    const hourly = await (await fetch(`/api/usage/hourly?days=${days}`)).json();
    const peak = Math.max(...hourly.buckets);
    render("hourlyChart", {
      type: "bar",
      data: {
        labels: hourly.buckets.map((_, h) => String(h).padStart(2, "0")),
        datasets: [{
          label: "API calls",
          data: hourly.buckets,
          backgroundColor: hourly.buckets.map((n) => (n === peak ? C.accent : C.ramp[1])),
          ...barStyle,
        }],
      },
      options: baseOptions({
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: C.surface, borderColor: C.line, borderWidth: 1, titleColor: C.text, bodyColor: C.text2, padding: 11, cornerRadius: 8,
                     callbacks: { title: (i) => i[0].label + ":00", label: (ctx) => " " + ctx.parsed.y + " API calls" } },
        },
        scales: axes({ yFmt: (v) => v }),
      }),
    });
  }

  /* ---------------------------------------------------------------- */
  /* failures                                                         */
  /* ---------------------------------------------------------------- */

  async function loadFailures() {
    if (!document.getElementById("failuresChart")) return;
    const { failures } = await (await fetch("/api/tools")).json();
    const top = failures.slice(0, 12);
    render("failuresChart", {
      type: "bar",
      data: {
        labels: top.map((t) => t.name || "unknown"),
        datasets: [{
          label: "Failures", data: top.map((t) => t.failures),
          backgroundColor: withAlpha(C.bad, 0.82),
          borderRadius: 3, borderSkipped: false, maxBarThickness: 18,
        }],
      },
      options: baseOptions({
        indexAxis: "y",
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: C.surface, borderColor: C.line, borderWidth: 1, titleColor: C.text, bodyColor: C.text2, padding: 11, cornerRadius: 8,
                     callbacks: { label: (ctx) => {
                       const row = top[ctx.dataIndex];
                       return ` ${row.failures} of ${row.total} calls`;
                     } } },
        },
        scales: {
          x: { beginAtZero: true, ticks: { color: C.muted, font: { size: 10 } }, grid: { color: C.line, drawBorder: false }, border: { display: false } },
          y: { ticks: { color: C.text2, font: { size: 11 } }, grid: { display: false }, border: { color: C.line } },
        },
      }),
    });
  }

  function reloadCharts() {
    readColors();
    loadDashboard(currentDays);
    loadAnalytics();
    loadFailures();
  }

  /* ---------------------------------------------------------------- */
  /* live feed                                                        */
  /* ---------------------------------------------------------------- */

  /* The socket feeds the Live activity panel; there is no connection indicator,
     so a drop is handled silently by reconnecting with backoff. */
  function connectLive() {
    const list = document.getElementById("live-feed");
    let ws, retry = 1000, keepalive;

    const open = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);

      ws.onopen = () => {
        retry = 1000;
        keepalive = setInterval(() => ws.readyState === 1 && ws.send("ping"), 25000);
      };

      ws.onmessage = (evt) => {
        let msg;
        try { msg = JSON.parse(evt.data); } catch { return; }
        if (msg.type === "hook" && list) prependEvent(list, msg.event);
      };

      ws.onclose = () => {
        clearInterval(keepalive);
        setTimeout(open, retry);
        retry = Math.min(retry * 2, 20000);
      };

      ws.onerror = () => ws.close();
    };

    open();
  }

  function prependEvent(list, e) {
    const empty = list.querySelector(".empty");
    if (empty) empty.remove();
    const li = document.createElement("li");
    li.className = "fresh" + (e.is_error ? " err" : "");
    li.innerHTML = `<span class="feed-ts"></span><span class="tag"></span><span class="feed-text"></span>`;
    li.children[0].textContent = new Date(e.ts).toLocaleTimeString();
    li.children[1].textContent = e.event_type || "event";
    li.children[2].textContent = e.summary || "";
    list.prepend(li);
    while (list.children.length > 60) list.lastElementChild.remove();
  }

  /* ---------------------------------------------------------------- */
  /* controls                                                         */
  /* ---------------------------------------------------------------- */

  function wireControls() {
    const seg = document.getElementById("range-seg");
    if (seg) {
      seg.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-days]");
        if (!btn) return;
        seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
        loadDashboard(Number(btn.dataset.days));
      });
    }

    const filter = document.getElementById("timeline-filter");
    if (filter) {
      filter.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-kind]");
        if (!btn) return;
        filter.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === btn));
        const kind = btn.dataset.kind;
        document.querySelectorAll(".timeline .tl").forEach((li) => {
          li.classList.toggle("hide", kind !== "all" && li.dataset.kind !== kind);
        });
      });
    }

    // Failure log rows expand in place instead of widening the table.
    document.querySelectorAll(".fail-row").forEach((row) => {
      row.addEventListener("click", () => row.classList.toggle("open"));
    });

    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.addEventListener("click", () => {
        const root = document.documentElement;
        const next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try { localStorage.setItem("theme", next); } catch (e) {}
        reloadCharts();
      });
    }

  }

  ready(() => {
    readColors();
    wireControls();
    connectLive();
    setTimeout(() => {          // Chart.js is deferred; let the global define first
      loadDashboard(30);
      loadAnalytics();
      loadFailures();
    }, 0);
  });
})();

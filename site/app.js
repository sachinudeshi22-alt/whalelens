/* WhaleLens front end: overview table + per-token page, hand-built SVG charts.
   All data-derived text goes in via textContent (labels are untrusted). */
(function () {
  "use strict";

  const SVGNS = "http://www.w3.org/2000/svg";
  const WINDOWS = [7, 30, 90];
  const app = document.getElementById("app");

  // ---------- storage (per-viewer conveniences only) ----------
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  // ---------- theme ----------
  const themeBtn = document.getElementById("theme-btn");
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }
  applyTheme(store.get("theme", ""));
  themeBtn.addEventListener("click", () => {
    const dark = getComputedStyle(document.documentElement).colorScheme === "dark";
    const next = dark ? "light" : "dark";
    store.set("theme", next);
    applyTheme(next);
    route();
  });

  // ---------- DOM helpers ----------
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  function svg(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------- formatting ----------
  function pct(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    const d = digits !== undefined ? digits : (Math.abs(v) < 0.001 && v !== 0 ? 2 : 1);
    return (v * 100).toFixed(d) + "%";
  }
  function signedPct(v) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    return (v > 0 ? "+" : v < 0 ? "−" : "") + pct(Math.abs(v));
  }
  function compact(v) {
    if (v === null || v === undefined) return "—";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(a < 10 ? 2 : 0);
  }
  const shortAddr = (a) => a.slice(0, 6) + "…" + a.slice(-4);
  const fmtDate = (iso) => new Date(iso + "T00:00:00Z").toLocaleDateString(undefined,
    { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
  const fmtMonth = (iso) => new Date(iso + "T00:00:00Z").toLocaleDateString(undefined,
    { month: "short", timeZone: "UTC" });

  function deltaCell(v) {
    if (v === "new") return el("span", { class: "tag", title: "Held none 30 days ago", text: "new" });
    if (v === null || v === undefined || !isFinite(v)) return el("span", { class: "na", text: "—" });
    const glyph = v > 0.0005 ? "▲" : v < -0.0005 ? "▼" : "•";
    return el("span", { class: "delta" }, el("span", { class: "glyph", "aria-hidden": "true", text: glyph }), signedPct(v));
  }
  function breadthText(c) {
    return c ? `${c.up} up · ${c.down} down` : "—";
  }

  // ---------- data ----------
  const cache = {};
  async function load(path) {
    if (!cache[path]) {
      cache[path] = fetch(path, { cache: "no-cache" }).then((r) => {
        if (!r.ok) throw new Error(`${path}: ${r.status}`);
        return r.json();
      });
    }
    return cache[path];
  }

  // ---------- window filter ----------
  function getWindow() {
    const w = parseInt(store.get("window", "30"), 10);
    return WINDOWS.includes(w) ? w : 30;
  }
  function windowFilter(onChange) {
    const cur = getWindow();
    const seg = el("div", { class: "seg", role: "group", "aria-label": "Time window" },
      WINDOWS.map((w) => el("button", {
        type: "button", "aria-pressed": String(w === cur), text: `${w}d`,
        onclick: () => { store.set("window", String(w)); onChange(); },
      })));
    return el("div", { class: "filters" }, el("label", { text: "Window" }), seg);
  }

  // ---------- tooltip ----------
  function tooltip(container) {
    const t = el("div", { class: "tooltip", role: "status" });
    container.append(t);
    return {
      show(x, y, build) {
        t.replaceChildren(...build());
        t.style.display = "block";
        const cw = container.clientWidth;
        const tw = t.offsetWidth;
        let left = x + 14;
        if (left + tw > cw) left = Math.max(0, x - tw - 14);
        t.style.left = left + "px";
        t.style.top = Math.max(0, y - 10) + "px";
      },
      hide() { t.style.display = "none"; },
    };
  }
  function tRow(color, value, name) {
    return el("div", { class: "t-row" },
      el("span", { class: "t-key", style: `background:${color}` }),
      el("strong", { text: value }),
      el("span", { class: "t-name", text: name }));
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  // ---------- line chart: share of supply over time ----------
  function lineChart(dates, series) {
    const box = el("div", { class: "chart" });
    const render = () => {
      box.querySelectorAll("svg").forEach((n) => n.remove());
      const W = Math.max(300, box.clientWidth), H = 260;
      const m = { l: 44, r: W < 500 ? 12 : 96, t: 10, b: 28 };
      const iw = W - m.l - m.r, ih = H - m.t - m.b;
      const all = series.flatMap((s) => s.values).filter((v) => v !== null);
      const yMax = niceMax(Math.max(...all) * 1.08);
      const x = (i) => m.l + (dates.length < 2 ? iw / 2 : (i / (dates.length - 1)) * iw);
      const y = (v) => m.t + ih - (v / yMax) * ih;
      const s = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
        "aria-label": "Share of total supply held over time" });

      for (let k = 0; k <= 4; k++) {
        const v = (yMax / 4) * k, yy = y(v);
        s.append(svg("line", { class: k === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: yy, y2: yy }));
        const t = svg("text", { class: "tick", x: m.l - 8, y: yy + 4, "text-anchor": "end" });
        t.textContent = pct(v, v < 0.1 && k ? 1 : 0);
        s.append(t);
      }
      let lastMonth = "";
      dates.forEach((d, i) => {
        const mo = d.slice(0, 7);
        if (mo !== lastMonth && d.slice(8) <= "07") {
          lastMonth = mo;
          if (x(i) > m.l + 14 && x(i) < W - m.r - 14 && (W > 520 || parseInt(d.slice(5, 7), 10) % 2 === 1)) {
            const t = svg("text", { class: "tick", x: x(i), y: H - 8, "text-anchor": "middle" });
            t.textContent = fmtMonth(d);
            s.append(t);
          }
        }
      });

      const ends = [];
      series.forEach((ser) => {
        const color = cssVar(ser.color);
        let d = "", pen = false;
        ser.values.forEach((v, i) => {
          if (v === null) { pen = false; return; }
          d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1);
          pen = true;
        });
        s.append(svg("path", { d, fill: "none", stroke: color, "stroke-width": 2,
          "stroke-linejoin": "round", "stroke-linecap": "round" }));
        const li = ser.values.length - 1;
        if (ser.values[li] !== null) ends.push({ ser, color, yy: y(ser.values[li]), v: ser.values[li] });
      });
      // End labels only where they don't collide; the legend always carries identity
      const room = m.r > 40;
      ends.forEach((e) => {
        s.append(svg("circle", { cx: x(dates.length - 1), cy: e.yy, r: 4, fill: e.color,
          stroke: cssVar("--surface"), "stroke-width": 2 }));
        const clash = ends.some((o) => o !== e && Math.abs(o.yy - e.yy) < 16);
        if (room && !clash) {
          const t = svg("text", { class: "endlabel", x: x(dates.length - 1) + 10, y: e.yy + 4 });
          t.textContent = `${pct(e.v)} ${e.ser.short}`;
          s.append(t);
        }
      });

      // Crosshair + tooltip
      const cross = svg("line", { class: "crosshair", y1: m.t, y2: m.t + ih, visibility: "hidden" });
      const dots = series.map((ser) => svg("circle", { r: 4, fill: cssVar(ser.color),
        stroke: cssVar("--surface"), "stroke-width": 2, visibility: "hidden" }));
      s.append(cross, ...dots);
      const hit = svg("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent", tabindex: 0,
        "aria-label": "Chart: use arrow keys to step through dates" });
      s.append(hit);
      let idx = dates.length - 1;
      const show = (i) => {
        idx = Math.max(0, Math.min(dates.length - 1, i));
        const xx = x(idx);
        cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.setAttribute("visibility", "visible");
        series.forEach((ser, k) => {
          const v = ser.values[idx];
          dots[k].setAttribute("visibility", v === null ? "hidden" : "visible");
          if (v !== null) { dots[k].setAttribute("cx", xx); dots[k].setAttribute("cy", y(v)); }
        });
        tip.show(xx, m.t, () => [el("div", { class: "t-date", text: fmtDate(dates[idx]) }),
          ...series.map((ser) => tRow(cssVar(ser.color), pct(ser.values[idx]), ser.name))]);
      };
      const hide = () => { cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); tip.hide(); };
      hit.addEventListener("pointermove", (ev) => {
        const r = s.getBoundingClientRect();
        const px = (ev.clientX - r.left) * (W / r.width);
        show(Math.round(((px - m.l) / iw) * (dates.length - 1)));
      });
      hit.addEventListener("pointerleave", hide);
      hit.addEventListener("focus", () => show(idx));
      hit.addEventListener("blur", hide);
      hit.addEventListener("keydown", (ev) => {
        if (ev.key === "ArrowLeft") { show(idx - 1); ev.preventDefault(); }
        if (ev.key === "ArrowRight") { show(idx + 1); ev.preventDefault(); }
      });
      box.prepend(s);
    };
    const tip = tooltip(box);
    new ResizeObserver(() => render()).observe(box);
    return box;
  }

  // ---------- bar chart: weekly net flow (diverging) ----------
  function flowChart(flows, symbol) {
    const box = el("div", { class: "chart" });
    const render = () => {
      box.querySelectorAll("svg").forEach((n) => n.remove());
      const W = Math.max(300, box.clientWidth), H = 220;
      const m = { l: 52, r: 12, t: 10, b: 28 };
      const iw = W - m.l - m.r, ih = H - m.t - m.b;
      const ext = niceMax(Math.max(...flows.map((f) => Math.abs(f.pct_supply)), 1e-6) * 1.1);
      const y = (v) => m.t + ih / 2 - (v / ext) * (ih / 2);
      const band = iw / flows.length;
      const bw = Math.max(2, Math.min(24, band - 2));
      const s = svg("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
        "aria-label": "Weekly net change in whale holdings, as share of supply" });
      [-ext, -ext / 2, 0, ext / 2, ext].forEach((v) => {
        s.append(svg("line", { class: v === 0 ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
        const t = svg("text", { class: "tick", x: m.l - 8, y: y(v) + 4, "text-anchor": "end" });
        t.textContent = (v > 0 ? "+" : v < 0 ? "−" : "") + pct(Math.abs(v), Math.abs(v) < 0.01 ? 2 : 1);
        s.append(t);
      });
      let lastMonth = "";
      flows.forEach((f, i) => {
        const mo = f.week_end.slice(0, 7);
        if (mo !== lastMonth) {
          lastMonth = mo;
          const cx = m.l + band * i + band / 2;
          if (cx > m.l + 10 && cx < W - m.r - 10 && (W > 520 || parseInt(mo.slice(5), 10) % 2 === 1)) {
            const t = svg("text", { class: "tick", x: cx, y: H - 8, "text-anchor": "middle" });
            t.textContent = fmtMonth(f.week_end);
            s.append(t);
          }
        }
      });
      const pos = cssVar("--pos"), neg = cssVar("--neg");
      flows.forEach((f, i) => {
        const cx = m.l + band * i + band / 2, x0 = cx - bw / 2;
        const y0 = y(0), y1 = y(f.pct_supply);
        const h = Math.abs(y1 - y0);
        const r = Math.min(4, h, bw / 2);
        let d;
        if (h < 0.5) d = `M${x0} ${y0}h${bw}`;
        else if (f.pct_supply >= 0)  // rounded data-end on top, square at baseline
          d = `M${x0} ${y0}V${y1 + r}Q${x0} ${y1} ${x0 + r} ${y1}H${x0 + bw - r}Q${x0 + bw} ${y1} ${x0 + bw} ${y1 + r}V${y0}Z`;
        else
          d = `M${x0} ${y0}V${y1 - r}Q${x0} ${y1} ${x0 + r} ${y1}H${x0 + bw - r}Q${x0 + bw} ${y1} ${x0 + bw} ${y1 - r}V${y0}Z`;
        const bar = svg("path", { d, fill: f.pct_supply >= 0 ? pos : neg,
          stroke: h < 0.5 ? cssVar("--axis") : "none" });
        s.append(bar);
        const hit = svg("rect", { x: m.l + band * i, y: m.t, width: band, height: ih, fill: "transparent", tabindex: 0,
          "aria-label": `Week ending ${f.week_end}: ${signedPct(f.pct_supply)} of supply` });
        const showTip = () => {
          bar.setAttribute("opacity", "0.75");
          tip.show(cx, m.t, () => [el("div", { class: "t-date", text: `Week ending ${fmtDate(f.week_end)}` }),
            tRow(f.pct_supply >= 0 ? pos : neg, signedPct(f.pct_supply), `of ${symbol} supply`),
            tRow(cssVar("--axis"), signedPct(f.pct), "change in their holdings"),
            el("div", { class: "t-name", text: `${f.up} whales up · ${f.down} down` })]);
        };
        const hideTip = () => { bar.removeAttribute("opacity"); tip.hide(); };
        hit.addEventListener("pointerenter", showTip);
        hit.addEventListener("pointerleave", hideTip);
        hit.addEventListener("focus", showTip);
        hit.addEventListener("blur", hideTip);
        s.append(hit);
      });
      box.prepend(s);
    };
    const tip = tooltip(box);
    new ResizeObserver(() => render()).observe(box);
    return box;
  }

  function sparkline(values) {
    const W = 96, H = 26, v = values.filter((x) => x !== null);
    const s = svg("svg", { class: "spark", width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
    if (v.length < 2) return s;
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || hi || 1;
    let d = "";
    values.forEach((x, i) => {
      if (x === null) return;
      d += (d ? "L" : "M") + ((i / (values.length - 1)) * (W - 6) + 1).toFixed(1) + " " +
        (H - 3 - ((x - lo) / span) * (H - 6)).toFixed(1);
    });
    s.append(svg("path", { d, fill: "none", stroke: cssVar("--whale"), "stroke-width": 1.5,
      "stroke-linejoin": "round", "stroke-linecap": "round" }));
    return s;
  }

  // ---------- card with chart/table toggle ----------
  function chartCard(title, sub, chartNode, legendNode, tableBuilder) {
    let showingTable = false;
    const body = el("div", {}, legendNode, chartNode);
    const btn = el("button", { class: "link-btn", type: "button", text: "Show table" });
    btn.addEventListener("click", () => {
      showingTable = !showingTable;
      btn.textContent = showingTable ? "Show chart" : "Show table";
      body.replaceChildren(...(showingTable ? [tableBuilder()] : [legendNode, chartNode].filter(Boolean)));
    });
    return el("section", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("h2", { text: title }), el("p", { class: "sub", text: sub })), btn),
      body);
  }

  // ---------- pages ----------
  async function overview() {
    document.getElementById("nav-overview").setAttribute("aria-current", "page");
    const data = await load("data/overview.json");
    const w = getWindow();
    const asOf = data.tokens.length ? data.tokens[0].as_of : null;
    const rows = data.tokens.map((t) => {
      const wc = t.stats[`whales_${w}d`], ic = t.stats[`insiders_${w}d`];
      const go = () => { location.hash = `#/t/${t.symbol}`; };
      return el("tr", { class: "row-link", onclick: go },
        el("td", {}, el("a", { class: "sym", href: `#/t/${t.symbol}`, text: t.symbol }),
          t.notes ? el("span", { class: "tag", title: "This token has a data note", text: "note" }) : null),
        el("td", { class: "num" }, pct(t.stats.whale_share)),
        el("td", { class: "hide-sm" }, sparkline(t.spark)),
        el("td", { class: "num" }, deltaCell(wc && wc.pct)),
        el("td", { class: "breadth hide-sm", text: breadthText(wc) }),
        el("td", { class: "num" }, t.stats.insider_share ? pct(t.stats.insider_share) : el("span", { class: "na", text: "none found" })),
        el("td", { class: "num" }, ic ? deltaCell(ic.pct) : el("span", { class: "na", text: "—" })));
    });
    app.replaceChildren(
      el("h1", { text: "What the biggest holders are doing" }),
      el("p", { class: "lede", text:
        "Whales are each day's 50 largest independent wallets, with staked tokens counted and exchanges removed. " +
        "Insiders (team, investor and treasury wallets) are tracked separately. Changes follow the wallets that " +
        "were whales at the start of the window, so a wallet that sold out still counts against the group." }),
      windowFilter(route),
      el("section", { class: "card" },
        el("div", { class: "table-scroll" }, el("table", {},
          el("thead", {}, el("tr", {},
            el("th", { text: "Token" }), el("th", { class: "num", text: "Whales hold" }),
            el("th", { class: "hide-sm", text: "90-day trend" }),
            el("th", { class: "num", text: `Whale change, ${w}d` }), el("th", { class: "hide-sm", text: "Breadth" }),
            el("th", { class: "num", text: "Insiders hold" }), el("th", { class: "num", text: `Insider change, ${w}d` }))),
          el("tbody", {}, rows)))),
      asOf ? el("p", { class: "sub", text: `Data as of ${fmtDate(asOf)} (UTC). "Hold" is share of total supply.` }) : null,
      data.pending.length ? el("p", { class: "sub", text: `Still being processed: ${data.pending.join(", ")}.` }) : null);
  }

  async function tokenPage(symbol) {
    const d = await load(`data/${encodeURIComponent(symbol)}.json`);
    const w = getWindow();
    const st = d.stats, wc = st[`whales_${w}d`], ic = st[`insiders_${w}d`];
    const hasInsiders = d.series.some((p) => p.insider_share);
    const dates = d.series.map((p) => p.date);
    const series = [{ name: "Top 50 whales", short: "whales", color: "--whale", values: d.series.map((p) => p.whale_share) }];
    if (hasInsiders) series.push({ name: "Insiders", short: "insiders", color: "--insider", values: d.series.map((p) => p.insider_share) });

    const legend = hasInsiders ? el("div", { class: "legend" },
      el("span", {}, el("span", { class: "key", style: `background:${cssVar("--whale")}` }), "Top 50 whales"),
      el("span", {}, el("span", { class: "key", style: `background:${cssVar("--insider")}` }), "Insiders")) : null;
    const flowLegend = el("div", { class: "legend" },
      el("span", {}, el("span", { class: "swatch", style: `background:${cssVar("--pos")}` }), "Whales added"),
      el("span", {}, el("span", { class: "swatch", style: `background:${cssVar("--neg")}` }), "Whales reduced"));

    const shareTable = () => el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Date" }), el("th", { class: "num", text: "Whales" }),
        hasInsiders ? el("th", { class: "num", text: "Insiders" }) : null)),
      el("tbody", {}, d.series.slice().reverse().map((p) => el("tr", {},
        el("td", { text: p.date }), el("td", { class: "num", text: pct(p.whale_share, 2) }),
        hasInsiders ? el("td", { class: "num", text: pct(p.insider_share, 2) }) : null)))));
    const flowTable = () => el("div", { class: "table-scroll" }, el("table", {},
      el("thead", {}, el("tr", {}, el("th", { text: "Week ending" }), el("th", { class: "num", text: "Share of supply" }),
        el("th", { class: "num", text: "Change in holdings" }), el("th", { text: "Breadth" }))),
      el("tbody", {}, d.flows.slice().reverse().map((f) => el("tr", {},
        el("td", { text: f.week_end }), el("td", { class: "num", text: signedPct(f.pct_supply) }),
        el("td", { class: "num", text: signedPct(f.pct) }), el("td", { class: "breadth", text: `${f.up} up · ${f.down} down` }))))));

    const explorer = (a) => `https://eth.blockscout.com/address/${a}`;
    const whaleRows = d.whales.map((h) => el("tr", {},
      el("td", { class: "num", text: String(h.rank) }),
      el("td", {}, el("a", { class: "addr", href: explorer(h.address), rel: "noopener", target: "_blank", text: shortAddr(h.address) }),
        h.type === "safe" ? el("span", { class: "tag", text: "Safe" }) : null,
        h.entity ? el("span", { class: "tag", title: "Shares signers with other Safes in this list", text: "grouped" }) : null,
        h.labels.length ? el("span", { class: "tag", text: h.labels[0] }) : null),
      el("td", { class: "num", text: compact(h.total) }),
      el("td", { class: "num", text: pct(h.share, 2) }),
      el("td", { class: "num hide-sm", text: h.staked > 0 ? compact(h.staked) : "—" }),
      el("td", { class: "num" }, deltaCell(h.d30))));
    const insiderRows = d.insiders.map((h) => el("tr", {},
      el("td", {}, el("a", { class: "addr", href: explorer(h.address), rel: "noopener", target: "_blank", text: shortAddr(h.address) })),
      el("td", { class: "num", text: compact(h.total) }),
      el("td", { class: "num", text: pct(h.share, 2) }),
      el("td", { class: "reason", text: h.reason }),
      el("td", { class: "num" }, deltaCell(h.d30))));

    const notes = [...d.notes];
    if (d.quality.proven < d.quality.days)
      notes.push(`Completeness is proven for ${d.quality.proven} of ${d.quality.days} days; on the rest, a wallet outside our candidate list could in principle have ranked in the top 50.`);

    app.replaceChildren(
      el("p", { class: "sub" }, el("a", { href: "#/", text: "← All tokens" })),
      el("h1", { text: symbol }),
      el("div", { class: "hero" },
        el("div", { class: "value", text: pct(st.whale_share) }),
        el("div", { class: "label", text: `of ${symbol} supply is held by the top 50 independent wallets` })),
      windowFilter(route),
      el("div", { class: "tiles" },
        el("div", { class: "tile" }, el("div", { class: "label", text: `Whale holdings, ${w}d` }),
          el("div", { class: "value" }, deltaCell(wc && wc.pct)),
          el("div", { class: "foot", text: wc ? `${wc.up} added · ${wc.down} reduced` : "" })),
        el("div", { class: "tile" }, el("div", { class: "label", text: "Insiders hold" }),
          el("div", { class: "value", text: hasInsiders ? pct(st.insider_share) : "None found" }),
          el("div", { class: "foot", text: hasInsiders ? `${d.insiders.length} wallets` : "No wallets met the insider rules" })),
        hasInsiders ? el("div", { class: "tile" }, el("div", { class: "label", text: `Insider holdings, ${w}d` }),
          el("div", { class: "value" }, deltaCell(ic && ic.pct)),
          el("div", { class: "foot", text: ic ? `${ic.up} added · ${ic.down} reduced` : "" })) : null,
        el("div", { class: "tile" }, el("div", { class: "label", text: "Total supply" }),
          el("div", { class: "value", text: compact(st.supply) }),
          el("div", { class: "foot", text: `as of ${fmtDate(d.as_of)}` }))),
      notes.length ? el("div", { class: "note", role: "note" }, notes.map((n) => el("p", { text: n }))) : null,
      chartCard("Share of supply held", "Each day's top 50 whales, ranked that day. Insiders are every wallet classed as insider.",
        lineChart(dates, series), legend, shareTable),
      chartCard("Weekly whale flow", "Change in holdings of the wallets that were whales at the start of each week, as a share of supply.",
        flowChart(d.flows, symbol), flowLegend, flowTable),
      el("section", { class: "card" },
        el("h2", { text: "Top 50 whales today" }),
        el("p", { class: "sub", text: "Holdings include staked tokens where we track them. Labels are public tags, not our claims." }),
        el("div", { class: "table-scroll" }, el("table", {},
          el("thead", {}, el("tr", {}, el("th", { class: "num", text: "#" }), el("th", { text: "Wallet" }),
            el("th", { class: "num", text: "Holdings" }), el("th", { class: "num", text: "% supply" }),
            el("th", { class: "num hide-sm", text: "Staked" }), el("th", { class: "num", text: "30d" }))),
          el("tbody", {}, whaleRows)))),
      hasInsiders ? el("section", { class: "card" },
        el("h2", { text: "Insiders" }),
        el("p", { class: "sub", text: "Wallets holding project-allocated or project-controlled supply, with the evidence for each." }),
        el("div", { class: "table-scroll" }, el("table", {},
          el("thead", {}, el("tr", {}, el("th", { text: "Wallet" }), el("th", { class: "num", text: "Holdings" }),
            el("th", { class: "num", text: "% supply" }), el("th", { text: "Why it's classed insider" }),
            el("th", { class: "num", text: "30d" }))),
          el("tbody", {}, insiderRows)))) : null);
    document.title = `${symbol} · WhaleLens`;
  }

  // ---------- router ----------
  async function route() {
    document.querySelectorAll("nav.main a").forEach((a) => a.removeAttribute("aria-current"));
    document.title = "WhaleLens";
    const m = location.hash.match(/^#\/t\/([A-Za-z0-9]+)$/);
    try {
      if (m) await tokenPage(m[1].toUpperCase());
      else await overview();
    } catch (e) {
      app.replaceChildren(el("h1", { text: "Couldn't load data" }), el("p", { class: "lede", text: String(e.message || e) }));
    }
  }
  window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });
  route();
})();

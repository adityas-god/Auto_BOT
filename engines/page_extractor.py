"""
Browser DOM Data Extraction Engine for GreyOrange OpsBot.
Executes client-side inspection across Grafana tables, stat panels, SVG elements, and status badges.
"""


def extract_page_data(page):
    try:
        return page.evaluate("""() => {
            const result = {
                tables: [],
                stats: [],
                primary_metrics: [],
                badges: [],
                colors_detected: [],
                raw_text: (document.body ? document.body.innerText : "") || ""
            };

            const clean = s => (s || "").replace(/\\s+/g, " ").trim();

            const checkColor = (fg, bg, stroke, fill, cls) => {
                const combined = `${fg} ${bg} ${stroke} ${fill} ${cls}`.toLowerCase();
                if (/red|critical|danger|error|failed|#e02f44|#c4162a|#f2495c|rgb\\(2[0-5][0-9],\\s*[0-7]?[0-9],\\s*[0-7]?[0-9]\\)/i.test(combined)) {
                    if (!result.colors_detected.includes("red")) result.colors_detected.push("red");
                    return "red";
                }
                if (/yellow|warn|orange|pending|delayed|#ff9900|#faad14|#eab308|rgb\\(2[0-5][0-9],\\s*1[0-9][0-9],\\s*[0-9]+\\)/i.test(combined)) {
                    if (!result.colors_detected.includes("yellow")) result.colors_detected.push("yellow");
                    return "yellow";
                }
                if (/green|success|normal|ok|healthy|#73bf69|#52c41a|rgb\\([0-9]+,\\s*2[0-5][0-9],\\s*[0-9]+\\)/i.test(combined)) {
                    if (!result.colors_detected.includes("green")) result.colors_detected.push("green");
                    return "green";
                }
                return "normal";
            };

            // 1. Table Extraction
            const tables = document.querySelectorAll("table, [role='table'], .table-panel");
            tables.forEach((tbl) => {
                const tblData = { title: "", headers: [], rows: [] };
                const panel = tbl.closest(".panel-container, [data-testid*='panel'], .react-grid-item, .dashboard-row");
                if (panel) {
                    const titleEl = panel.querySelector(".panel-title, [data-testid*='panel-header'], h2, h3, h4");
                    if (titleEl) tblData.title = titleEl.innerText.trim();
                }

                const ths = tbl.querySelectorAll("th, [role='columnheader']");
                ths.forEach(th => {
                    const t = th.innerText.trim();
                    if (t) tblData.headers.push(t);
                });

                const trs = tbl.querySelectorAll("tbody tr, [role='row']");
                trs.forEach(tr => {
                    if (tr.querySelector("[role='columnheader']")) return;
                    const rowCells = [];
                    const tds = tr.querySelectorAll("td, [role='cell']");
                    tds.forEach(td => {
                        const text = td.innerText.trim();
                        let color = "normal";
                        const style = window.getComputedStyle(td);
                        const fg = style.color || "";
                        const bg = style.backgroundColor || "";
                        const cls = (td.className || "") + " " + (td.getAttribute("data-status") || "");

                        color = checkColor(fg, bg, "", "", cls);
                        rowCells.push({ text: text, color: color });
                    });
                    if (rowCells.length > 0) tblData.rows.push(rowCells);
                });

                tblData.row_count = tblData.rows.length;
                if (tblData.rows.length > 0 || tblData.headers.length > 0) {
                    result.tables.push(tblData);
                }
            });

            // 2. Grafana Gauges, Stat Panels & Big Numbers Deep Scan
            const panels = document.querySelectorAll(".panel-container, [data-testid*='panel'], .react-grid-item, div[class*='panel-container'], .view-panel, div[class*='panel-wrapper']");
            panels.forEach(p => {
                let title = "";
                const titleEl = p.querySelector(".panel-title, [data-testid*='panel-header'], [class*='panel-title'], h1, h2, h3, h4, header");
                if (titleEl) title = clean(titleEl.innerText);

                const svgs = p.querySelectorAll("svg");
                svgs.forEach(svg => {
                    const shapes = svg.querySelectorAll("path, circle, rect, text, tspan");
                    shapes.forEach(el => {
                        const style = window.getComputedStyle(el);
                        checkColor(style.color, style.backgroundColor, style.stroke, style.fill || el.getAttribute("fill") || "", el.getAttribute("class") || "");
                        if (el.tagName.toLowerCase() === "text" || el.tagName.toLowerCase() === "tspan") {
                            const txt = clean(el.textContent);
                            if (txt && /^[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units)?$/i.test(txt)) {
                                const pair = title ? `${title}: ${txt}` : txt;
                                if (!result.stats.includes(pair)) result.stats.push(pair);
                            }
                        }
                    });
                });

                const content = p.querySelector(".panel-content, div[class*='panel-content'], div[class*='panel-body']") || p;
                const els = content.querySelectorAll("div, span, p, h1, h2, h3, text, b, strong");
                els.forEach(el => {
                    if (el.children.length > 2) return;
                    const txt = clean(el.innerText || el.textContent);
                    if (!txt || txt.length > 40) return;

                    if (/^[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units|items|rpm|k|m|g)?$/i.test(txt)) {
                        const style = window.getComputedStyle(el);
                        checkColor(style.color, style.backgroundColor, style.stroke, style.fill, el.className);
                        const pair = title ? `${title}: ${txt}` : txt;
                        if (!result.stats.includes(pair)) result.stats.push(pair);
                        if (!result.stats.includes(txt)) result.stats.push(txt);
                        if (!result.primary_metrics.includes(pair)) result.primary_metrics.push(pair);
                    }
                });
            });

            // 3. Fallback: Parse visible body text lines
            const lines = (document.body ? document.body.innerText : "").split("\\n");
            let prevLine = "";
            for (let i = 0; i < lines.length; i++) {
                const line = clean(lines[i]);
                if (!line) continue;

                if (/^[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units)?$/i.test(line)) {
                    if (prevLine && prevLine.length < 50 && !/^(timeinterval|ppsid|bin_tags|all|last|greymatter|dashboard)/i.test(prevLine)) {
                        const combined = `${prevLine}: ${line}`;
                        if (!result.stats.includes(combined)) result.stats.push(combined);
                        if (!result.primary_metrics.includes(combined)) result.primary_metrics.push(combined);
                    }
                    if (!result.stats.includes(line)) result.stats.push(line);
                }

                const inlineM = line.match(/([A-Za-z0-9_\\-\\s]{3,35}[:=]\\s*[-+]?\\d+(?:\\.\\d+)?\\s*(?:s|ms|sec|seconds|%|orders|totes|units)?)/i);
                if (inlineM) {
                    const found = clean(inlineM[1]);
                    if (!result.stats.includes(found)) result.stats.push(found);
                    if (!result.primary_metrics.includes(found)) result.primary_metrics.push(found);
                }
                prevLine = line;
            }

            // 4. Badges, Status Pills & Tags
            const pills = document.querySelectorAll(".badge, [class*='status'], [class*='state'], [class*='pill'], [data-testid*='badge']");
            pills.forEach(p => {
                const t = clean(p.innerText);
                if (t && t.length < 40 && !result.badges.includes(t)) result.badges.push(t);
            });

            return result;
        }""")
    except Exception as e:
        return {"tables": [], "stats": [], "primary_metrics": [], "badges": [], "colors_detected": [], "raw_text": ""}

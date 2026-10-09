"""
Anomaly & Sudden Spike Threshold Evaluation Engine for GreyOrange OpsBot.
Evaluates extracted metrics against multi-layer anomaly criteria:
1. Table row counts
2. Absolute numeric boundaries (Gauges, Stat Panels, Image OCR)
3. Sudden spike deltas, percentage jumps, and graph min-max ranges
4. Alert keyword detection
5. Visual status warning colors (Red / Orange / Yellow)
"""

import re


def evaluate_threshold(extraction_data, threshold_cfg=None):
    cfg = threshold_cfg or {}
    if not cfg.get("enabled", True):
        return {
            "breached": False,
            "status": "DISABLED",
            "summary": "Threshold monitoring disabled",
            "reasons": [],
            "total_rows": 0,
            "detected_colors": [],
            "ocr_detected": 0
        }

    metric_type = cfg.get("metric_type", "row_count") or "row_count"
    op = cfg.get("operator", ">") or ">"
    val_str = str(cfg.get("value", "0")).strip()
    raw_kw = cfg.get("keywords", "") or ""
    keywords = [k.strip().lower() for k in raw_kw.split(",") if k.strip()]
    raw_cl = cfg.get("colors", "red, orange, yellow") or "red, orange, yellow"
    colors = [c.strip().lower() for c in raw_cl.split(",") if c.strip()]

    tables = extraction_data.get("tables", [])
    raw_text = extraction_data.get("raw_text", "").lower()
    detected_colors = list(set(extraction_data.get("colors_detected", [])))
    stats = extraction_data.get("stats", [])
    primary_metrics = extraction_data.get("primary_metrics", [])
    ocr_data = extraction_data.get("ocr", {})
    ocr_raw_text = ocr_data.get("raw_text", "").lower()
    ocr_lines = ocr_data.get("lines", [])
    ocr_pixel_colors = ocr_data.get("pixel_colors", [])
    for pc in ocr_pixel_colors:
        if pc not in detected_colors:
            detected_colors.append(pc)

    total_rows = sum(t.get("row_count", 0) for t in tables)
    reasons = []
    breached = False

    all_text = f"{raw_text}\n{ocr_raw_text}".lower()

    # Check 1: Table row count
    if metric_type in ("row_count", "any"):
        try:
            target_num = float(val_str) if val_str else 0.0
            if op == ">" and total_rows > target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) > {target_num}")
            elif op == ">=" and total_rows >= target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) >= {target_num}")
            elif op == "<" and total_rows < target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) < {target_num}")
            elif op == "<=" and total_rows <= target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) <= {target_num}")
            elif op == "==" and total_rows == target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) == {target_num}")
            elif op == "!=" and total_rows != target_num:
                breached = True
                reasons.append(f"Table row count ({total_rows}) != {target_num}")
        except ValueError:
            pass

    # Check 2: Numeric values in stats/KPIs and Image OCR
    if metric_type in ("number_val", "any"):
        try:
            target_num = float(val_str) if val_str else 0.0
            candidate_items = []
            seen = set()
            for item in (primary_metrics + stats + ocr_lines):
                c = str(item).strip()
                if c and c not in seen:
                    candidate_items.append(c)
                    seen.add(c)

            evaluated_numbers = []
            for item in candidate_items:
                found_nums = re.findall(r"[-+]?\d+(?:\.\d+)?", item)
                for fn in found_nums:
                    try:
                        n_val = float(fn)
                        evaluated_numbers.append((n_val, item))
                    except ValueError:
                        pass

            for num_val, source_line in evaluated_numbers:
                match_condition = False
                if op == ">" and num_val > target_num: match_condition = True
                elif op == ">=" and num_val >= target_num: match_condition = True
                elif op == "<" and num_val < target_num: match_condition = True
                elif op == "<=" and num_val <= target_num: match_condition = True
                elif op == "==" and num_val == target_num: match_condition = True
                elif op == "!=" and num_val != target_num: match_condition = True

                if match_condition:
                    breached = True
                    reasons.append(f"Metric value ({num_val}) {op} {target_num} in '{source_line}'")
                    break
        except Exception:
            pass

    # Check 3: Sudden Spike & Anomaly Detection (Delta & % Jump & Range)
    prev_reading = cfg.get("prev_reading")
    extracted_nums = []
    for item in (primary_metrics + stats + ocr_lines):
        found_nums = re.findall(r"[-+]?\d+(?:\.\d+)?", str(item))
        for fn in found_nums:
            try:
                extracted_nums.append(float(fn))
            except ValueError:
                pass

    current_primary_val = extracted_nums[0] if extracted_nums else None

    # Within-panel range spike: max - min in table or chart
    min_val = None
    max_val = None
    for tbl in tables:
        h_lower = [str(h).lower() for h in tbl.get("headers", [])]
        if "min" in h_lower and "max" in h_lower:
            min_idx = h_lower.index("min")
            max_idx = h_lower.index("max")
            for r in tbl.get("rows", []):
                if len(r) > max(min_idx, max_idx):
                    try:
                        m_val = float(re.findall(r"[-+]?\d+(?:\.\d+)?", r[min_idx].get("text", ""))[0])
                        mx_val = float(re.findall(r"[-+]?\d+(?:\.\d+)?", r[max_idx].get("text", ""))[0])
                        if min_val is None or m_val < min_val: min_val = m_val
                        if max_val is None or mx_val > max_val: max_val = mx_val
                    except Exception:
                        pass

    if metric_type in ("spike_jump", "any"):
        spike_threshold = float(val_str) if val_str else 20.0

        # Also detect max and min directly from text/OCR (e.g. "Max: 95.2", "Min: 12.0", "Peak: 140")
        if min_val is None or max_val is None:
            txt_max = re.findall(r"(?:max|peak|high)[:=\s]+([-+]?\d+(?:\.\d+)?)", all_text)
            txt_min = re.findall(r"(?:min|low|trough)[:=\s]+([-+]?\d+(?:\.\d+)?)", all_text)
            if txt_max and txt_min:
                try:
                    mx = float(txt_max[0])
                    mn = float(txt_min[0])
                    if max_val is None or mx > max_val: max_val = mx
                    if min_val is None or mn < min_val: min_val = mn
                except ValueError:
                    pass

        # Condition A: Graph range anomaly (max - min)
        if min_val is not None and max_val is not None:
            range_delta = max_val - min_val
            if range_delta >= spike_threshold:
                breached = True
                reasons.append(f"Graph range spike detected: max ({max_val}) - min ({min_val}) = {range_delta:.1f} >= {spike_threshold}")

        # Condition B: Current vs Average surge from panel/legend (e.g. "Current: 140, Avg: 50")
        txt_avg = re.findall(r"(?:avg|average|mean)[:=\s]+([-+]?\d+(?:\.\d+)?)", all_text)
        txt_curr = re.findall(r"(?:current|now|latest)[:=\s]+([-+]?\d+(?:\.\d+)?)", all_text)
        if txt_avg and txt_curr:
            try:
                curr_v = float(txt_curr[0])
                avg_v = float(txt_avg[0])
                if avg_v > 0:
                    surge_pct = ((curr_v - avg_v) / avg_v) * 100.0
                    if surge_pct >= spike_threshold:
                        breached = True
                        reasons.append(f"Spike above average: current ({curr_v}) is +{surge_pct:.1f}% above avg ({avg_v})")
            except ValueError:
                pass

        # Condition C: Cycle-over-cycle spike jump & percentage surge
        if prev_reading is not None and current_primary_val is not None:
            delta = current_primary_val - float(prev_reading)
            if op == ">" and delta >= spike_threshold:
                breached = True
                reasons.append(f"Sudden surge detected: jumped by +{delta:.1f} (from {prev_reading} to {current_primary_val})")
            elif op == ">=" and delta >= spike_threshold:
                breached = True
                reasons.append(f"Sudden surge detected: jumped by +{delta:.1f} (from {prev_reading} to {current_primary_val})")
            elif op == "<=" and delta <= -spike_threshold:
                breached = True
                reasons.append(f"Sudden drop detected: fell by {delta:.1f} (from {prev_reading} to {current_primary_val})")
            # Percentage spike
            if float(prev_reading) > 0:
                pct_change = (delta / float(prev_reading)) * 100.0
                if pct_change >= spike_threshold and f"jumped by +{delta:.1f}" not in str(reasons):
                    breached = True
                    reasons.append(f"Sudden percentage spike: +{pct_change:.1f}% surge (from {prev_reading} to {current_primary_val})")

    # Check 4: Alert Keywords
    if metric_type in ("keyword", "any"):
        for kw in keywords:
            if kw in all_text:
                breached = True
                reasons.append(f"Alert keyword '{kw}' found in dashboard")

    # Check 5: Alert Colors
    if metric_type in ("color_status", "any"):
        matched_c = [c for c in colors if c in detected_colors]
        if matched_c:
            breached = True
            reasons.append(f"Detected alert status color(s): {', '.join(matched_c)}")

        for tbl in tables:
            for row in tbl.get("rows", []):
                for cell in row:
                    c_text = cell.get("text", "")
                    c_color = cell.get("color", "normal")
                    if c_color in ("red", "yellow"):
                        if not breached:
                            breached = True
                            reasons.append(f"Warning cell '{c_text}' ({c_color})")

    summary = " | ".join(reasons) if reasons else f"All clear (Rows: {total_rows})"
    return {
        "breached": breached,
        "status": "BREACHED" if breached else "NORMAL",
        "reasons": reasons,
        "summary": summary,
        "total_rows": total_rows,
        "primary_val": current_primary_val,
        "detected_colors": detected_colors,
        "ocr_detected": len(ocr_lines)
    }

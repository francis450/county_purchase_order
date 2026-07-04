# Copyright (c) 2026, ERP Kenya and contributors
# For license information, please see license.txt

import bisect
import json
import statistics
from collections import defaultdict
from datetime import timedelta

import frappe
from frappe.utils import add_months, escape_html, flt, get_first_day, getdate, now_datetime

REORDER_DOCTYPE = "Reorder Recommendation"
ALERT_DOCTYPE = "Reorder Alert Log"
URGENT_STATUSES = ("Critical", "Reorder Now")


def recalculate_reorder_recommendations():
	"""Nightly job: recompute median demand, reorder level, and suggested order
	qty for every stock item, based on trailing Sales Invoice history."""
	settings = frappe.get_cached_doc("Reorder Automation Settings")
	warehouses = [row.warehouse for row in settings.included_warehouses]
	if not warehouses:
		frappe.log_error(
			title="Reorder Automation",
			message="No warehouses configured in Reorder Automation Settings.included_warehouses - skipping run.",
		)
		return

	trailing_months = int(settings.trailing_months)
	window_start, window_end, month_keys = _get_trailing_window(trailing_months)

	items = frappe.get_all(
		"Item",
		filters={"is_stock_item": 1, "disabled": 0},
		fields=["item_code", "lead_time_days"],
	)
	item_codes = [d.item_code for d in items]
	if not item_codes:
		return

	monthly_sales = _get_monthly_sales(item_codes, window_start, window_end)
	stockout_months = _get_stockout_months(item_codes, warehouses, window_start, window_end, month_keys)
	sle_qty_by_item = _get_sle_outgoing_qty(item_codes, window_start, window_end)
	bin_data = _get_bin_data(item_codes, warehouses)
	open_po_by_item = _get_open_po_dates(item_codes)
	rates_by_item = _get_last_rates(item_codes)
	last_sale_dates = _get_last_sale_dates(item_codes)
	last_receipt_dates = _get_last_receipt_dates(item_codes, warehouses)
	default_suppliers = _get_default_suppliers(item_codes)

	empty_bin = {"actual_qty": 0.0, "projected_qty": 0.0, "valuation_rate": 0.0}
	for item in items:
		_recalculate_one_item(
			item=item,
			month_keys=month_keys,
			trailing_months=trailing_months,
			monthly_sales=monthly_sales.get(item.item_code, {}),
			excluded_months=stockout_months.get(item.item_code, set()),
			sle_qty=sle_qty_by_item.get(item.item_code, 0),
			bin_data=bin_data.get(item.item_code, empty_bin),
			open_po_dates=open_po_by_item.get(item.item_code, []),
			rates=rates_by_item.get(item.item_code, {}),
			last_sale_date=last_sale_dates.get(item.item_code),
			last_receipt_date=last_receipt_dates.get(item.item_code),
			default_supplier=default_suppliers.get(item.item_code),
			settings=settings,
		)

	frappe.db.commit()


def check_low_stock_and_notify():
	"""Nightly job (run after recalculate_reorder_recommendations): notify once
	per transition into Critical/Reorder Now, not on every standing alert."""
	settings = frappe.get_cached_doc("Reorder Automation Settings")
	if not settings.notify_role:
		return

	urgent_items = frappe.get_all(
		REORDER_DOCTYPE,
		filters={"status": ["in", URGENT_STATUSES]},
		fields=["name", "item_code", "item_name", "status", "reorder_level", "projected_qty", "suggested_order_qty"],
	)
	urgent_codes = {d.item_code for d in urgent_items}

	# Resolve alerts for items that are no longer urgent.
	active_alerts = frappe.get_all(ALERT_DOCTYPE, filters={"active": 1}, fields=["name", "item_code"])
	for alert in active_alerts:
		if alert.item_code not in urgent_codes:
			frappe.db.set_value(ALERT_DOCTYPE, alert.name, "active", 0)

	# Raise new alerts only for items with no currently-active log entry.
	already_active = {
		d.item_code
		for d in frappe.get_all(ALERT_DOCTYPE, filters={"active": 1}, fields=["item_code"])
	}
	new_alerts = [item for item in urgent_items if item.item_code not in already_active]
	if not new_alerts:
		return

	for item in new_alerts:
		frappe.get_doc(
			{
				"doctype": ALERT_DOCTYPE,
				"item_code": item.item_code,
				"status": item.status,
				"raised_on": now_datetime(),
				"active": 1,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()

	try:
		_send_digest(settings.notify_role, new_alerts)
	except Exception:
		# Alert log rows above are already committed - a notification delivery
		# failure (e.g. no outgoing Email Account configured) must not cause
		# these newly-raised alerts to be silently re-attempted or lost.
		frappe.log_error(title="Reorder Automation: notification delivery failed")


def get_summary_numbers():
	"""Shared by the report's summary cards and the alert digest email."""
	critical_count = frappe.db.count(REORDER_DOCTYPE, {"status": "Critical"})
	reorder_now_count = frappe.db.count(REORDER_DOCTYPE, {"status": "Reorder Now"})
	total_suggested_value = flt(
		frappe.db.sql(
			"""select sum(suggested_order_value) from `tabReorder Recommendation`
			where status in ('Critical', 'Reorder Now', 'Watch')"""
		)[0][0]
	)
	total_revenue_at_risk = flt(
		frappe.db.sql(
			"""select sum(monthly_revenue_at_risk) from `tabReorder Recommendation`
			where status in ('Critical', 'Reorder Now')"""
		)[0][0]
	)
	total_dormant_capital = flt(
		frappe.db.sql(
			"""select sum(locked_capital) from `tabReorder Recommendation` where status='Dormant Stock'"""
		)[0][0]
	)
	return {
		"critical_count": critical_count,
		"reorder_now_count": reorder_now_count,
		"total_suggested_value": total_suggested_value,
		"total_revenue_at_risk": total_revenue_at_risk,
		"total_dormant_capital": total_dormant_capital,
	}


def _get_trailing_window(trailing_months):
	current_month_start = get_first_day(getdate())
	window_start = add_months(current_month_start, -trailing_months)
	window_end = current_month_start  # exclusive - current partial month excluded
	month_keys = [
		add_months(window_start, i).strftime("%Y-%m") for i in range(trailing_months)
	]
	return window_start, window_end, month_keys


def _get_monthly_sales(item_codes, window_start, window_end):
	rows = frappe.db.sql(
		"""
		select sii.item_code,
			date_format(si.posting_date, '%%Y-%%m') as ym,
			sum(sii.qty) as qty
		from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent and si.docstatus = 1
		where si.posting_date >= %(start)s and si.posting_date < %(end)s
			and sii.item_code in %(items)s
		group by sii.item_code, ym
		""",
		{"start": window_start, "end": window_end, "items": item_codes},
		as_dict=True,
	)
	result = defaultdict(dict)
	for row in rows:
		result[row.item_code][row.ym] = flt(row.qty)
	return result


def _get_sle_outgoing_qty(item_codes, window_start, window_end):
	"""Sales-Invoice-voucher outgoing qty per item over the window, for the
	invoice-vs-ledger corroboration check."""
	rows = frappe.db.sql(
		"""
		select item_code, sum(abs(actual_qty)) as qty
		from `tabStock Ledger Entry`
		where voucher_type = 'Sales Invoice'
			and posting_date >= %(start)s and posting_date < %(end)s
			and item_code in %(items)s
		group by item_code
		""",
		{"start": window_start, "end": window_end, "items": item_codes},
		as_dict=True,
	)
	return {row.item_code: flt(row.qty) for row in rows}


def _get_stockout_months(item_codes, warehouses, window_start, window_end, month_keys):
	"""For each item, determine which trailing calendar months it was out of
	stock (summed across included warehouses) for more than half the days."""
	rows = frappe.db.sql(
		"""
		select item_code, warehouse, posting_date, posting_time, qty_after_transaction, creation
		from `tabStock Ledger Entry`
		where warehouse in %(warehouses)s
			and item_code in %(items)s
			and posting_date < %(end)s
		order by item_code, warehouse, posting_date, posting_time, creation
		""",
		{"warehouses": warehouses, "items": item_codes, "end": window_end},
		as_dict=True,
	)

	# checkpoints[(item_code, warehouse)] = sorted list of (date, balance)
	checkpoints = defaultdict(list)
	for row in rows:
		checkpoints[(row.item_code, row.warehouse)].append((row.posting_date, flt(row.qty_after_transaction)))

	all_days = []
	d = window_start
	while d < window_end:
		all_days.append(d)
		d = d + timedelta(days=1)

	excluded = defaultdict(set)
	for item_code in item_codes:
		# Daily total balance across included warehouses.
		daily_totals = [0.0] * len(all_days)
		for wh in warehouses:
			series = checkpoints.get((item_code, wh))
			if not series:
				continue
			dates = [s[0] for s in series]
			balances = [s[1] for s in series]
			for i, day in enumerate(all_days):
				idx = bisect.bisect_right(dates, day) - 1
				if idx >= 0:
					daily_totals[i] += balances[idx]

		zero_days_by_month = defaultdict(int)
		total_days_by_month = defaultdict(int)
		for day, total in zip(all_days, daily_totals):
			ym = day.strftime("%Y-%m")
			total_days_by_month[ym] += 1
			if total <= 0:
				zero_days_by_month[ym] += 1

		for ym in month_keys:
			total = total_days_by_month.get(ym, 0)
			if total and zero_days_by_month.get(ym, 0) > total / 2:
				excluded[item_code].add(ym)

	return excluded


def _get_bin_data(item_codes, warehouses):
	"""Actual/projected qty summed across included warehouses, plus a
	qty-weighted average valuation rate (used for the locked-capital column)."""
	rows = frappe.db.sql(
		"""
		select item_code, actual_qty, projected_qty, valuation_rate
		from `tabBin`
		where warehouse in %(warehouses)s and item_code in %(items)s
		""",
		{"warehouses": warehouses, "items": item_codes},
		as_dict=True,
	)
	agg = defaultdict(lambda: {"actual_qty": 0.0, "projected_qty": 0.0, "qty_x_rate": 0.0})
	for row in rows:
		a = agg[row.item_code]
		a["actual_qty"] += flt(row.actual_qty)
		a["projected_qty"] += flt(row.projected_qty)
		a["qty_x_rate"] += flt(row.actual_qty) * flt(row.valuation_rate)

	result = {}
	for item_code, a in agg.items():
		valuation_rate = (a["qty_x_rate"] / a["actual_qty"]) if a["actual_qty"] > 0 else 0.0
		result[item_code] = {
			"actual_qty": a["actual_qty"],
			"projected_qty": a["projected_qty"],
			"valuation_rate": valuation_rate,
		}
	return result


def _get_open_po_dates(item_codes):
	"""Transaction dates of still-open PO lines per item (qty > received_qty,
	parent not Completed/Closed/Cancelled) - used for the stale-open-PO flag.
	Keyed off PO status/per_received since this site never uses Purchase Receipt."""
	rows = frappe.db.sql(
		"""
		select poi.item_code, po.transaction_date
		from `tabPurchase Order Item` poi
		inner join `tabPurchase Order` po on po.name = poi.parent
		where po.docstatus = 1
			and po.status not in ('Completed', 'Closed', 'Cancelled')
			and poi.qty > poi.received_qty
			and poi.item_code in %(items)s
		""",
		{"items": item_codes},
		as_dict=True,
	)
	result = defaultdict(list)
	for row in rows:
		result[row.item_code].append(row.transaction_date)
	return result


def _get_last_rates(item_codes):
	"""Last buying/selling rate per item: most recent Purchase/Sales Invoice
	rate, falling back to Item Price when no invoice history exists."""
	buying_rate = {}
	for row in frappe.db.sql(
		"""
		select pii.item_code, pii.rate
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent and pi.docstatus = 1
		where pii.item_code in %(items)s and pii.rate > 0
		order by pi.posting_date desc, pi.creation desc
		""",
		{"items": item_codes},
		as_dict=True,
	):
		buying_rate.setdefault(row.item_code, flt(row.rate))

	selling_rate = {}
	for row in frappe.db.sql(
		"""
		select sii.item_code, sii.rate
		from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent and si.docstatus = 1
		where sii.item_code in %(items)s and sii.rate > 0
		order by si.posting_date desc, si.creation desc
		""",
		{"items": item_codes},
		as_dict=True,
	):
		selling_rate.setdefault(row.item_code, flt(row.rate))

	missing_buy = [c for c in item_codes if c not in buying_rate]
	missing_sell = [c for c in item_codes if c not in selling_rate]

	if missing_buy:
		for row in frappe.db.sql(
			"""
			select item_code, price_list_rate from `tabItem Price`
			where buying = 1 and item_code in %(items)s and price_list_rate > 0
			order by valid_from desc
			""",
			{"items": missing_buy},
			as_dict=True,
		):
			buying_rate.setdefault(row.item_code, flt(row.price_list_rate))

	if missing_sell:
		for row in frappe.db.sql(
			"""
			select item_code, price_list_rate from `tabItem Price`
			where selling = 1 and item_code in %(items)s and price_list_rate > 0
			order by valid_from desc
			""",
			{"items": missing_sell},
			as_dict=True,
		):
			selling_rate.setdefault(row.item_code, flt(row.price_list_rate))

	return {
		item_code: {"buying_rate": buying_rate.get(item_code), "selling_rate": selling_rate.get(item_code)}
		for item_code in item_codes
	}


def _get_last_sale_dates(item_codes):
	rows = frappe.db.sql(
		"""
		select sii.item_code, max(si.posting_date) as last_sale
		from `tabSales Invoice Item` sii
		inner join `tabSales Invoice` si on si.name = sii.parent and si.docstatus = 1
		where sii.item_code in %(items)s
		group by sii.item_code
		""",
		{"items": item_codes},
		as_dict=True,
	)
	return {row.item_code: row.last_sale for row in rows}


def _get_last_receipt_dates(item_codes, warehouses):
	"""No Purchase Receipts exist on this site, so the latest positive stock
	movement into an included warehouse is used as a receipt-date proxy."""
	rows = frappe.db.sql(
		"""
		select item_code, max(posting_date) as last_receipt
		from `tabStock Ledger Entry`
		where warehouse in %(warehouses)s and item_code in %(items)s and actual_qty > 0
		group by item_code
		""",
		{"warehouses": warehouses, "items": item_codes},
		as_dict=True,
	)
	return {row.item_code: row.last_receipt for row in rows}


def _get_default_suppliers(item_codes):
	rows = frappe.db.sql(
		"""
		select parent as item_code, supplier
		from `tabItem Supplier`
		where parent in %(items)s
		order by idx asc
		""",
		{"items": item_codes},
		as_dict=True,
	)
	result = {}
	for row in rows:
		result.setdefault(row.item_code, row.supplier)
	return result


def _build_breakdown(month_keys, monthly_sales, excluded_months, raw_series, clipped_series):
	"""JSON-able list of {month, qty, excluded, clipped} for every trailing
	month (not just usable ones) - drives the report's auditable bucket columns."""
	breakdown = []
	usable_idx = 0
	for ym in month_keys:
		entry = {"month": ym, "qty": flt(monthly_sales.get(ym, 0.0)), "excluded": ym in excluded_months, "clipped": False}
		if ym not in excluded_months:
			if usable_idx < len(clipped_series) and clipped_series[usable_idx] != raw_series[usable_idx]:
				entry["clipped"] = True
			usable_idx += 1
		breakdown.append(entry)
	return breakdown


def _compute_trend(clipped_series, trailing_months):
	"""Mean of the 2 most recent usable months vs the 2 prior, +/-20% threshold.
	Only meaningful when every trailing month was usable (no exclusions) - a
	gap from an excluded month would make "2 most recent" ambiguous."""
	if trailing_months < 4 or len(clipped_series) != trailing_months:
		return ""
	prior_avg = statistics.mean(clipped_series[:-2])
	recent_avg = statistics.mean(clipped_series[-2:])
	if prior_avg == 0:
		return ""
	if recent_avg > prior_avg * 1.2:
		return "Up"
	if recent_avg < prior_avg * 0.8:
		return "Down"
	return "Flat"


def _recalculate_one_item(
	item,
	month_keys,
	trailing_months,
	monthly_sales,
	excluded_months,
	sle_qty,
	bin_data,
	open_po_dates,
	rates,
	last_sale_date,
	last_receipt_date,
	default_supplier,
	settings,
):
	usable_months = [ym for ym in month_keys if ym not in excluded_months]
	raw_series = [monthly_sales.get(ym, 0.0) for ym in usable_months]
	total_usable_sales = sum(raw_series)
	has_enough_history = len(usable_months) >= settings.min_history_months

	rec = _get_or_create_recommendation(item.item_code)
	rec.excluded_stockout_months = ", ".join(sorted(excluded_months)) or ""
	rec.last_calculated = now_datetime()
	rec.actual_qty = bin_data["actual_qty"]
	rec.projected_qty = bin_data["projected_qty"]
	rec.default_supplier = default_supplier
	rec.last_sale_date = last_sale_date
	rec.last_receipt_date = last_receipt_date

	# (a) raw median, (b) clip months > anomaly_multiplier x raw median, (c) recompute median once.
	median_demand = 0.0
	anomaly_flag = 0
	clipped_series = list(raw_series)
	if has_enough_history and raw_series:
		raw_median = statistics.median(raw_series)
		if raw_median > 0:
			cap = settings.anomaly_multiplier * raw_median
			clipped_series = []
			for v in raw_series:
				if v > cap:
					anomaly_flag = 1
					clipped_series.append(cap)
				else:
					clipped_series.append(v)
		median_demand = statistics.median(clipped_series)

	rec.monthly_demand_breakdown = json.dumps(
		_build_breakdown(month_keys, monthly_sales, excluded_months, raw_series, clipped_series)
	)
	rec.anomaly_flag = anomaly_flag
	rec.median_monthly_demand = median_demand
	rec.trend = _compute_trend(clipped_series, trailing_months) if has_enough_history else ""

	# --- Classification (B1): three distinct "no reliable number" buckets ---
	# - Insufficient History: not enough usable months yet, but some real sales
	#   exist in the ones we do have (recently active / growing item).
	# - Dormant Stock: real stock on hand, zero demand signal - an overstock
	#   signal, not a data problem.
	# - Dead SKU: zero stock and zero demand signal - a disable candidate.
	no_math_status = None
	if not has_enough_history and total_usable_sales > 0:
		no_math_status = "Insufficient History"
	elif median_demand == 0:
		no_math_status = "Dormant Stock" if rec.actual_qty > 0 else "Dead SKU"

	if no_math_status:
		rec.insufficient_history_flag = 1
		rec.status = no_math_status
		rec.reorder_level = 0
		rec.suggested_order_qty = 0
		rec.large_order_flag = 0
		rec.months_of_stock_remaining = None
		rec.corroboration_flag = 0
		rec.lead_time_months_used = 0
		rec.lead_time_source = ""
		rec.stale_open_po_flag = 0
		rec.projected_stockout_date = None
		rec.order_by_date = None
		rec.suggested_order_value = None
		rec.monthly_revenue_at_risk = None

		rate_missing = False
		if no_math_status == "Dormant Stock":
			valuation_rate = bin_data.get("valuation_rate") or 0
			if valuation_rate > 0:
				rec.locked_capital = rec.actual_qty * valuation_rate
			else:
				rec.locked_capital = None
				rate_missing = True
		else:
			rec.locked_capital = None
		rec.rate_missing_flag = 1 if rate_missing else 0
		rec.save(ignore_permissions=True)
		return

	# --- Full reorder math (enough history and real median demand) ---
	rec.insufficient_history_flag = 0

	if item.lead_time_days:
		lead_time_months = flt(item.lead_time_days) / 30.0
		lead_time_source = "Item"
	else:
		lead_time_months = flt(settings.lead_time_months)
		lead_time_source = "Default"

	reorder_level = median_demand * (lead_time_months + 0.5)

	raw_suggested = median_demand * (lead_time_months + 4) - rec.projected_qty
	suggested_qty = raw_suggested if raw_suggested > 0.01 else 0
	large_order_flag = 1 if suggested_qty > median_demand * settings.max_suggested_months else 0

	months_remaining = rec.actual_qty / median_demand

	corroboration_flag = 0
	if total_usable_sales > 0 and sle_qty:
		deviation_pct = abs(sle_qty - total_usable_sales) / total_usable_sales * 100
		corroboration_flag = 1 if deviation_pct > settings.corroboration_threshold_pct else 0

	stale_open_po_flag = 0
	if open_po_dates:
		stale_cutoff_days = int(lead_time_months * 30) + 14
		today = getdate()
		for txn_date in open_po_dates:
			if (today - getdate(txn_date)).days > stale_cutoff_days:
				stale_open_po_flag = 1
				break

	if rec.projected_qty <= 0:
		status = "Critical"
	elif rec.projected_qty <= reorder_level:
		status = "Reorder Now"
	elif rec.projected_qty <= reorder_level * 1.25:
		status = "Watch"
	else:
		status = "OK"

	projected_stockout_date = getdate() + timedelta(days=round(months_remaining * 30))
	order_by_date = projected_stockout_date - timedelta(days=round(lead_time_months * 30))

	rate_missing = False
	buying_rate = (rates or {}).get("buying_rate")
	suggested_order_value = suggested_qty * buying_rate if buying_rate else None
	if not buying_rate:
		rate_missing = True

	monthly_revenue_at_risk = None
	if status in URGENT_STATUSES:
		selling_rate = (rates or {}).get("selling_rate")
		if selling_rate:
			monthly_revenue_at_risk = median_demand * selling_rate
		else:
			rate_missing = True

	rec.lead_time_months_used = lead_time_months
	rec.lead_time_source = lead_time_source
	rec.reorder_level = reorder_level
	rec.suggested_order_qty = suggested_qty
	rec.large_order_flag = large_order_flag
	rec.months_of_stock_remaining = months_remaining
	rec.corroboration_flag = corroboration_flag
	rec.stale_open_po_flag = stale_open_po_flag
	rec.status = status
	rec.projected_stockout_date = projected_stockout_date
	rec.order_by_date = order_by_date
	rec.suggested_order_value = suggested_order_value
	rec.monthly_revenue_at_risk = monthly_revenue_at_risk
	rec.locked_capital = None
	rec.rate_missing_flag = 1 if rate_missing else 0
	rec.save(ignore_permissions=True)


def _get_or_create_recommendation(item_code):
	if frappe.db.exists(REORDER_DOCTYPE, item_code):
		return frappe.get_doc(REORDER_DOCTYPE, item_code)
	doc = frappe.new_doc(REORDER_DOCTYPE)
	doc.item_code = item_code
	return doc


def _send_digest(role, new_alerts):
	recipients = frappe.get_all(
		"Has Role", filters={"role": role, "parenttype": "User"}, fields=["parent"]
	)
	emails = [r.parent for r in recipients if frappe.db.get_value("User", r.parent, "enabled")]
	if not emails:
		return

	summary = get_summary_numbers()
	top10 = frappe.get_all(
		REORDER_DOCTYPE,
		filters={"status": ["in", URGENT_STATUSES]},
		fields=["item_code", "item_name", "status", "order_by_date", "suggested_order_qty"],
		order_by="order_by_date asc",
		limit_page_length=10,
	)

	top_rows = "".join(
		f"<tr><td>{escape_html(d.item_name or d.item_code)}</td><td>{escape_html(d.status)}</td>"
		f"<td>{d.order_by_date or ''}</td><td>{flt(d.suggested_order_qty)}</td></tr>"
		for d in top10
	)
	message = f"""
		<p>{len(new_alerts)} item(s) newly crossed into a reorder-triggering status.</p>
		<ul>
			<li>Critical: {summary['critical_count']}</li>
			<li>Reorder Now: {summary['reorder_now_count']}</li>
			<li>Total suggested order value: {summary['total_suggested_value']:,.0f}</li>
			<li>Total monthly revenue at risk: {summary['total_revenue_at_risk']:,.0f}</li>
			<li>Total dormant capital: {summary['total_dormant_capital']:,.0f}</li>
		</ul>
		<p>Top 10 by order-by date:</p>
		<table border="1" cellpadding="4" cellspacing="0">
			<tr><th>Item</th><th>Status</th><th>Order By</th><th>Suggested Qty</th></tr>
			{top_rows}
		</table>
		<p><a href="/app/query-report/Reorder Recommendations">Open the full Reorder Recommendations report</a></p>
	"""
	frappe.sendmail(
		recipients=emails,
		subject=f"Reorder Alert: {len(new_alerts)} item(s) need attention",
		message=message,
	)
	for d in new_alerts:
		frappe.publish_realtime(
			event="reorder_alert",
			message={"item_code": d.item_code, "status": d.status},
			user=None,
		)

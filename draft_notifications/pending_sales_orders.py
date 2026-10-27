"""Read-only pending financial-review digest for CRM Sales Order group notifications."""

import json
import re
from html import escape
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model import get_permitted_fields
from frappe.utils import get_url
from pypika import Order
from pypika.analytics import Count


SUMMARY_LIMIT = 10
# Stable CRM process-state code, defined by crm_integration.finance_release.PENDING.
PENDING_FINANCE_STATE = "Pending Deposit Confirmation"
REQUIRED_FIELDS = {"name", "company", "creation", "docstatus", "status", "custom_crm_order_no", "custom_process_status"}


def is_applicable(doc, rule):
	return (
		doc.doctype == "Sales Order" and doc.docstatus == 1 and bool(doc.get("custom_crm_order_no"))
		and bool(doc.get("company")) and bool(doc.get("creation"))
		and (rule.get("trigger_type") or "Document Event") == "Document Event"
		and rule.get("trigger_event") == "On Submit"
	)


def pending_filters(doc):
	return {
		"company": doc.company, "docstatus": 1,
		"custom_process_status": PENDING_FINANCE_STATE,
		"custom_crm_order_no": ["is", "set"],
		"status": ["not in", ["Closed", "Cancelled", "Stopped", "On Hold"]],
		"name": ["!=", doc.name], "creation": ["<", doc.creation],
	}


def get_pending_summary(doc, receivers):
	"""Intersect native recipient permissions before counting or limiting results."""
	if not receivers or any(not row.get("user") or not row.get("enabled") for row in receivers):
		return None
	users = sorted({row["user"] for row in receivers})
	meta = frappe.get_meta("Sales Order")
	masked_fields = [df for df in meta.fields if df.fieldname in REQUIRED_FIELDS and df.get("mask")]
	for user in users:
		if (
			not frappe.has_permission("Sales Order", "read", user=user)
			or not REQUIRED_FIELDS <= set(get_permitted_fields("Sales Order", user=user, permission_type="read"))
		):
			return None
		# Read access does not grant access to a masked field's original value.
		# Check the recipient, never the worker/session user's masking policy.
		if masked_fields and user != "Administrator":
			mask_levels = set(meta.get_permlevel_access(permission_type="mask", user=user))
			if any((df.permlevel or 0) not in mask_levels for df in masked_fields):
				return None

	filters = pending_filters(doc)
	order = frappe.qb.DocType("Sales Order")
	query = frappe.qb.from_(order)
	for user in users:
		# Native read/share/owner/User Permission and query hooks apply for each user.
		visible = frappe.get_list(
			"Sales Order", fields=["name"], filters=filters, user=user,
			limit=0, order_by="", run=False,
		)
		query = query.where(order.name.isin(visible))
	# Window count and page share one snapshot; no unbounded result or per-order reads.
	rows = (
		query.select(order.name, order.custom_crm_order_no, Count(order.name).over().as_("pending_total"))
		.orderby(order.creation, order=Order.asc).orderby(order.name, order=Order.asc)
		.limit(SUMMARY_LIMIT).run(as_dict=True)
	)
	return {
		"rows": [{"name": row.name, "custom_crm_order_no": row.custom_crm_order_no} for row in rows],
		"total": int(rows[0].pending_total) if rows else 0,
	}


def get_summary_receivers(doc, rule, receivers):
	"""Retain previous group recipients when retries or rule changes narrow the mentions."""
	previous_users = frappe.get_all(
		"Draft Notification Log", filters={
			"rule": rule.name, "reference_doctype": doc.doctype, "reference_name": doc.name,
			"dingtalk_config": rule.dingtalk_config, "notification_channel": "DingTalk Group",
		}, pluck="user", distinct=True, limit=0,
	)
	if not previous_users:
		return receivers
	# Legacy aggregate logs cannot establish a safe recipient permission boundary.
	if any(not user for user in previous_users):
		return []
	users = frappe.get_all("User", filters={"name": ["in", previous_users]}, fields=["name", "enabled"], limit=0)
	enabled = {row.name: row.enabled for row in users}
	return [*receivers, *({"user": user, "enabled": enabled.get(user, 0)} for user in previous_users)]


def pending_list_url(doc):
	filters = {key: json.dumps(value, default=str) if isinstance(value, list) else str(value)
		for key, value in pending_filters(doc).items()}
	return get_url("/desk/sales-order?" + urlencode(filters))


def _identifier(value):
	# Identifiers originate outside ERP; prevent line breaks, HTML and Markdown injection.
	value = escape(" ".join(str(value or "").split()))
	return re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", value)


def compose_group_message(doc, rule, content, language, receivers):
	if not is_applicable(doc, rule):
		return content
	language = language or "en"
	message = "### " + _("New sales order", lang=language) + "\n\n" + content
	try:
		summary = get_pending_summary(doc, get_summary_receivers(doc, rule, receivers))
		return message + render_pending_section(doc, summary, language)
	except frappe.PermissionError:
		return message
	except Exception:
		# Keep the primary notification usable even if the database error also prevents logging.
		traceback = frappe.get_traceback()
		try:
			frappe.log_error(title=f"Sales order pending summary failed: {doc.name}", message=traceback)
		except Exception:
			frappe.logger("draft_notifications", allow_site=True).error(traceback)
		return message + "\n\n" + _("Historical pending orders could not be loaded; check the sales order list.", lang=language)


def render_pending_section(doc, summary, language):
	if not summary or not summary["rows"]:
		return ""

	message = "\n\n---\n\n### " + _("Historical orders pending financial review ({0})", lang=language).format(summary["total"])
	for index, row in enumerate(summary["rows"], start=1):
		message += f"\n\n{index}. ERP: {_identifier(row['name'])} | CRM: {_identifier(row['custom_crm_order_no'])}"
	remaining = max(0, summary["total"] - len(summary["rows"]))
	if remaining:
		message += "\n\n" + _("{0} more pending orders.", lang=language).format(remaining)
	message += "\n\n[" + _("View all historical pending orders", lang=language) + "](" + pending_list_url(doc) + ")"
	return message

import json

import frappe
from frappe import _
from frappe.utils import add_days, getdate, get_url_to_form, nowdate


RULE_DOCTYPE = "Draft Notification Rule"
LOG_DOCTYPE = "Draft Notification Log"
DESK_NOTIFICATION_DOCTYPE = "Notification Log"
ALLOWED_METHOD_PREFIXES_CONF = "draft_notification_allowed_method_prefixes"


def handle_after_insert(doc, method=None):
	if doc.doctype == "Item":
		return
	handle_document_event(doc, "After Insert")


def handle_item_after_insert(doc, method=None):
	"""Queue Item insert notifications through an explicit DocType hook."""
	handle_document_event(doc, "After Insert")


def handle_on_update(doc, method=None):
	handle_document_event(doc, "On Update", doc.get_doc_before_save())


def handle_on_submit(doc, method=None):
	handle_document_event(doc, "On Submit", doc.get_doc_before_save())


def handle_on_cancel(doc, method=None):
	handle_document_event(doc, "On Cancel", doc.get_doc_before_save())


def handle_purchase_receipt_submit(doc, method=None):
	_enqueue_purchase_order_notifications(doc, "On Submit")


def handle_purchase_receipt_cancel(doc, method=None):
	_enqueue_purchase_order_notifications(doc, "On Cancel")


def _enqueue_purchase_order_notifications(receipt, trigger_event):
	for purchase_order in unique(row.purchase_order for row in receipt.get("items") or [] if row.purchase_order):
		if not frappe.db.exists("Purchase Order", purchase_order):
			continue
		po = frappe.get_doc("Purchase Order", purchase_order)
		if not has_enabled_rules("Purchase Order", trigger_event):
			continue
		frappe.enqueue(
			"draft_notifications.draft_notifications.draft_notification.send_draft_notifications",
			queue="short",
			enqueue_after_commit=True,
			job_id=f"draft-notification::{trigger_event}::Purchase Order::{purchase_order}",
			deduplicate=True,
			doctype="Purchase Order",
			name=purchase_order,
			trigger_event=trigger_event,
			company=po.get("company"),
			previous_values={"per_received": 0},
		)


def handle_document_event(doc, trigger_event, previous_doc=None):
	if not frappe.db.exists("DocType", RULE_DOCTYPE):
		return

	if not has_enabled_rules(doc.doctype, trigger_event):
		return

	frappe.enqueue(
		"draft_notifications.draft_notifications.draft_notification.send_draft_notifications",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"draft-notification::{trigger_event}::{doc.doctype}::{doc.name}",
		deduplicate=True,
		at_front=True,
		doctype=doc.doctype,
		name=doc.name,
		trigger_event=trigger_event,
		company=doc.get("company") if hasattr(doc, "get") else None,
		previous_values=get_previous_values(previous_doc),
	)


def send_draft_notifications(doctype, name, trigger_event="After Insert", company=None, previous_values=None):
	if not frappe.db.exists(doctype, name) or not frappe.db.exists("DocType", RULE_DOCTYPE):
		return

	doc = frappe.get_doc(doctype, name)

	for rule in get_enabled_rules(doctype, trigger_event, company):
		try:
			if rule_matches_trigger(doc, rule, previous_values):
				send_for_rule(doc, rule)
		except Exception:
			frappe.log_error(
				title=f"Draft notification rule failed: {rule.name}",
				message=frappe.get_traceback(),
			)


def get_previous_values(previous_doc):
	if not previous_doc:
		return None
	return {
		field.fieldname: previous_doc.get(field.fieldname)
		for field in frappe.get_meta(previous_doc.doctype).fields
		if field.fieldname
	}


def rule_matches_trigger(doc, rule, previous_values=None):
	trigger_type = rule.get("trigger_type") or "Document Event"
	if trigger_type == "Document Event":
		return True
	if trigger_type == "Purchase Order Received Complete":
		return doc.doctype == "Purchase Order" and float(doc.get("per_received") or 0) >= 100 and float((previous_values or {}).get("per_received") or 0) < 100
	if trigger_type == "Status Change":
		fieldname = rule.get("status_field") or "status"
		previous = (previous_values or {}).get(fieldname)
		return previous != rule.get("target_status") and doc.get(fieldname) == rule.get("target_status")
	return False


def send_for_rule(doc, rule):
	channel = get_notification_channel(rule)
	candidates = []

	if channel_uses_frappe_users(channel) or channel_uses_dingtalk(channel):
		try:
			candidates = get_candidate_users(doc, rule)
		except Exception:
			frappe.log_error(
				title=f"Draft notification recipient error: {rule.name}",
				message=frappe.get_traceback(),
			)
			return

	if channel_uses_email(channel) or channel_uses_desk(channel):
		for user in unique(candidates):
			try:
				send_to_user(
					doc,
					rule,
					user,
					send_email=channel_uses_email(channel),
					send_desk=channel_uses_desk(channel),
					notification_channel=channel,
				)
			except Exception:
				traceback = frappe.get_traceback()
				create_log(doc, rule, user=user, status="Failed", reason=traceback, notification_channel=channel)
				frappe.log_error(title=f"Draft notification failed for user {user}", message=traceback)

	if channel_uses_dingtalk(channel):
		send_dingtalk_private_chat_for_rule(doc, rule, receiver_users=candidates)


def send_to_user(
	doc, rule, user, ignore_deduplicate=False, send_email=True, send_desk=True,
	notification_channel=None, retry_of=None,
):
	if not frappe.db.has_column(LOG_DOCTYPE, "delivery_channel"):
		frappe.throw(_("Notification log schema is outdated. Please migrate the site before sending notifications."))
	# Each transport owns its outcome. A Desk failure must not prevent email queueing.
	results = []
	for channel, enabled in (("Desk", send_desk), ("Email", send_email)):
		if enabled:
			results.append(send_user_channel(
				doc, rule, user, channel, notification_channel or get_notification_channel(rule),
				ignore_deduplicate=ignore_deduplicate, retry_of=retry_of,
			))
	return results[-1] if results else None


def send_user_channel(doc, rule, user, channel, notification_channel, ignore_deduplicate=False, retry_of=None):
	if retry_of:
		previous = get_retry_attempt(retry_of, channel, user=user)
		if previous:
			return previous
	user_data = get_enabled_user_data(user)
	log_values = {
		"user": user, "notification_channel": notification_channel,
		"delivery_channel": channel, "retry_of": retry_of,
	}
	reason = get_recipient_skip_reason(doc, rule, user, user_data, send_email=channel == "Email")
	if reason:
		return create_log(doc, rule, status="Skipped", reason=reason, **log_values)
	log_values["email"] = user_data.email
	if not ignore_deduplicate and rule.deduplicate and already_sent(doc, rule, user, channel):
		return create_log(doc, rule, status="Skipped", reason=_("Already sent"), **log_values)

	try:
		language = get_user_language(user)
		subject = render_subject(doc, rule, language)
		message = render_message(doc, rule, language)
		if channel == "Desk":
			create_desk_notification(doc, rule, user, user_data.email, subject, message)
			return create_log(doc, rule, status="Sent", **log_values)
		email_queue = frappe.sendmail(
			recipients=[user_data.email], subject=subject, message=message,
			reference_doctype=doc.doctype, reference_name=doc.name, delayed=True,
		)
		log = create_log(doc, rule, status="Queued", email_queue=email_queue.name if email_queue else None, **log_values)
		queue_log_sync_after_commit(log)
		return log
	except Exception:
		traceback = frappe.get_traceback()
		frappe.log_error(title=f"Draft notification {channel} send failed: {doc.doctype} {doc.name}", message=traceback)
		return create_log(doc, rule, status="Failed", reason=traceback, **log_values)


def send_dingtalk_private_chat_for_rule(doc, rule, receiver_users=None, target_id=None, retry_of=None):
	from draft_notifications.dingtalk_robot import get_dingtalk_receivers, send_private_chat_notification

	logs = {}
	delivery_channel = "DingTalk Group" if channel_uses_group(get_notification_channel(rule)) else "DingTalk Private Chat"
	if delivery_channel == "DingTalk Group":
		from draft_notifications.dingtalk_group import get_group_receivers as get_dingtalk_receivers
	try:
		if not all(frappe.db.has_column(LOG_DOCTYPE, field) for field in ("dingtalk_user_id", "dingtalk_config", "retry_of", "delivery_channel")):
			frappe.throw(_("Notification log schema is outdated. Please migrate the site before sending DingTalk notifications."))
		# Serialize eligibility checks and sends for this rule, including manual retries.
		frappe.db.get_value(RULE_DOCTYPE, rule.name, "name", for_update=True)
		if receiver_users is None:
			receiver_users = get_candidate_users(doc, rule)
		receivers = get_dingtalk_receivers(rule.dingtalk_config, receiver_users=receiver_users)
		# Preserve the complete group audience before targeting, permission skips or deduplication.
		summary_receivers = receivers
		if target_id:
			receivers = [receiver for receiver in receivers if receiver["user_id"] == target_id]
		if not receivers:
			return create_log(doc, rule, status="Skipped", reason=_("No eligible DingTalk recipients found."),
				notification_channel=delivery_channel, retry_of=retry_of)

		eligible = []
		for receiver in receivers:
			user = receiver.get("user")
			reason = None
			if receiver.get("unbound"):
				reason = _("Group mentions require a DingTalk UserId or mobile number.")
			elif receiver.get("ambiguous"):
				reason = _("DingTalk UserId is linked to multiple ERP users.")
			elif user:
				reason = get_recipient_skip_reason(doc, rule, user, receiver if receiver.get("enabled") else None)
			elif rule.respect_permissions:
				reason = _("No read permission")
			if not reason and (rule.deduplicate or retry_of) and already_sent_dingtalk(doc, rule, receiver["user_id"], delivery_channel):
				reason = _("Already sent")
			log = create_log(
				doc, rule, user=user, status="Skipped" if reason else "Queued", reason=reason,
				notification_channel=delivery_channel, dingtalk_user_id=receiver["user_id"],
				dingtalk_config=rule.dingtalk_config, retry_of=retry_of,
			)
			if not log:
				raise RuntimeError("Failed to persist DingTalk recipient log; no message sent")
			if not reason:
				logs[receiver["user_id"]] = log
				eligible.append(receiver)

		def record_results(outcomes):
			for outcome in outcomes:
				log = logs[outcome["user_id"]]
				log.status = outcome["status"]
				frappe.db.set_value(LOG_DOCTYPE, log.name, {
					"status": outcome["status"], "dingtalk_status": outcome["status"],
					"reason": outcome["reason"], "dingtalk_error": outcome["reason"],
				})

		if eligible:
			kwargs = {"receivers": eligible, "on_result": record_results}
			if delivery_channel == "DingTalk Group":
				kwargs["summary_receivers"] = summary_receivers
			send_private_chat_notification(doc, rule, **kwargs)
		# Return a failed recipient when any batch failed; successful targets retain their own logs.
		return next((log for log in logs.values() if log.status == "Failed"), next(iter(logs.values()), log))
	except Exception:
		traceback = frappe.get_traceback()
		for pending in logs.values():
			if pending.status == "Queued":
				frappe.db.set_value(LOG_DOCTYPE, pending.name, {
					"status": "Failed", "dingtalk_status": "Failed", "reason": traceback, "dingtalk_error": traceback,
				})
		log = create_log(
			doc, rule, status="Failed", reason=traceback, notification_channel=delivery_channel,
			dingtalk_status="Failed", dingtalk_error=traceback, dingtalk_user_id=target_id,
			dingtalk_config=rule.get("dingtalk_config"), retry_of=retry_of,
		)
		frappe.log_error(title=f"DingTalk private chat notification failed: {rule.name}", message=traceback)
		return log


def already_sent_dingtalk(doc, rule, user_id, delivery_channel="DingTalk Private Chat"):
	filters = {
		"rule": rule.name, "reference_doctype": doc.doctype, "reference_name": doc.name,
		"notification_channel": delivery_channel, "dingtalk_user_id": user_id,
		"dingtalk_config": rule.dingtalk_config, "status": ["in", ["Queued", "Sent"]],
	}
	if rule.get("trigger_type") == "Date Condition":
		filters["notification_period"] = rule.get("_notification_period")
	if frappe.db.exists(LOG_DOCTYPE, filters):
		return True
	if delivery_channel == "DingTalk Group":
		return False
	# Older successful DingTalk logs represented the whole recipient set.
	legacy_filters = {
		**filters, "dingtalk_user_id": ["is", "not set"], "dingtalk_config": ["is", "not set"],
		"status": "Sent", "notification_channel": ["in", ["DingTalk Private Chat", "DingTalk DING"]],
	}
	return bool(frappe.db.exists(LOG_DOCTYPE, legacy_filters))


def get_notification_channel(rule):
	return rule.get("notification_channel") or "Email"


def channel_uses_email(channel):
	return channel in ("Email", "Email + DingTalk Private Chat", "Email + DingTalk DING", "Email + DingTalk Group")


def channel_uses_desk(channel):
	return channel in (
		"Email",
		"Desk",
		"Email + DingTalk Private Chat",
		"Desk + DingTalk Private Chat",
		"Email + DingTalk DING",
		"Desk + DingTalk DING",
		"Email + DingTalk Group",
		"Desk + DingTalk Group",
	)


def channel_uses_dingtalk_private_chat(channel):
	return channel in (
		"DingTalk Private Chat",
		"Email + DingTalk Private Chat",
		"Desk + DingTalk Private Chat",
		"DingTalk DING",
		"Email + DingTalk DING",
		"Desk + DingTalk DING",
	)


def channel_uses_group(channel):
	return channel in ("DingTalk Group", "Email + DingTalk Group", "Desk + DingTalk Group")


def channel_uses_dingtalk(channel):
	return channel_uses_dingtalk_private_chat(channel) or channel_uses_group(channel)


def channel_uses_frappe_users(channel):
	return channel_uses_email(channel) or channel_uses_desk(channel)


def has_enabled_rules(doctype, trigger_event="After Insert"):
	if not frappe.db.has_column(RULE_DOCTYPE, "trigger_type"):
		return bool(
			frappe.db.exists(
				RULE_DOCTYPE,
				{"enabled": 1, "document_type": doctype, "trigger_event": trigger_event},
			)
		)

	rules = frappe.get_all(
		RULE_DOCTYPE,
		filters={"enabled": 1, "document_type": doctype},
		fields=["trigger_event", "trigger_type"],
		limit=100,
	)
	return any(
		(r.get("trigger_type") or "Document Event") in ("Status Change", "Purchase Order Received Complete")
		or r.trigger_event == trigger_event
		for r in rules
	)


def get_enabled_rules(doctype, trigger_event="After Insert", company=None):
	advanced_rules = frappe.db.has_column(RULE_DOCTYPE, "trigger_type")
	filters = {"enabled": 1, "document_type": doctype}
	if not advanced_rules:
		filters["trigger_event"] = trigger_event

	fields = [
		"name",
		"trigger_event",
		"recipient_type",
		"role",
		"user_field",
		"custom_method",
		"respect_permissions",
		"include_owner",
		"deduplicate",
		"subject",
		"message",
		"subject_zh",
		"message_zh",
		"subject_en",
		"message_en",
		"subject_es",
		"message_es",
		"notification_channel",
		"dingtalk_config",
		"dingtalk_message_template",
		"dingtalk_message_zh",
		"dingtalk_message_en",
		"dingtalk_message_es",
		"company",
	]
	if advanced_rules:
		fields[2:2] = [
			"trigger_type",
			"status_field",
			"target_status",
			"date_field",
			"date_offset_days",
			"repeat_interval_days",
			"condition_scope",
		]

	rules = frappe.get_all(
		RULE_DOCTYPE,
		filters=filters,
		fields=fields,
	)
	return [
		r
		for r in rules
		if (not r.company or r.company == company)
		and (
			(r.get("trigger_type") or "Document Event") in ("Status Change", "Purchase Order Received Complete")
			or r.trigger_event == trigger_event
		)
	]


def get_candidate_users(doc, rule):
	users = []
	explicit_administrator = False

	if rule.recipient_type == "Fixed Users":
		users.extend(get_fixed_users(rule.name))
		explicit_administrator = "Administrator" in users
	elif rule.recipient_type == "Users With Role":
		users.extend(get_users_with_role(rule.role))
	elif rule.recipient_type == "User Field":
		users.append(doc.get(rule.user_field))
	elif rule.recipient_type == "Owner":
		users.append(doc.owner)
	elif rule.recipient_type == "Custom Method":
		validate_custom_method(rule.custom_method)
		users.extend(as_list(frappe.get_attr(rule.custom_method)(doc)))

	if rule.include_owner:
		users.append(doc.owner)

	return [
		user
		for user in users
		if user and user != "Guest" and (user != "Administrator" or explicit_administrator)
	]


def get_fixed_users(rule_name):
	rule = frappe.get_doc(RULE_DOCTYPE, rule_name)
	return [row.user for row in rule.fixed_recipients if row.user]


def get_users_with_role(role):
	if not role:
		return []

	return frappe.get_all(
		"Has Role",
		filters={
			"role": role,
			"parenttype": "User",
		},
		pluck="parent",
	)


def get_enabled_user_email(user):
	user_data = get_enabled_user_data(user)
	return user_data.email if user_data and user_data.email else None


def get_enabled_user_data(user):
	if not user:
		return None

	user_data = frappe.db.get_value(
		"User",
		user,
		["enabled", "email"],
		as_dict=True,
	)
	if not user_data or not user_data.enabled:
		return None

	return user_data


def get_user_language(user):
	return frappe.db.get_value("User", user, "language") or frappe.local.lang or "en"


def get_recipient_skip_reason(doc, rule, user, user_data, send_email=False):
	if not user_data:
		return _("User is disabled or missing")
	if send_email and not user_data.email:
		return _("User has no email")
	if rule.respect_permissions and not can_read_doc(doc, user):
		return _("No read permission")
	return None


def can_read_doc(doc, user):
	return frappe.has_permission(
		doc.doctype,
		"read",
		doc=doc,
		user=user,
	)


def already_sent(doc, rule, user, notification_channel=None):
	if not frappe.db.exists("DocType", LOG_DOCTYPE):
		return False
	channel = notification_channel or get_notification_channel(rule)
	filters = {
		"rule": rule.name, "reference_doctype": doc.doctype, "reference_name": doc.name,
		"user": user, "status": ["in", ["Queued", "Sent"]],
	}
	if rule.get("trigger_type") == "Date Condition" and frappe.db.has_column(LOG_DOCTYPE, "notification_period"):
		filters["notification_period"] = rule.get("_notification_period")
	if frappe.db.exists(LOG_DOCTYPE, {**filters, "delivery_channel": channel}):
		return True
	legacy_channels = [name for name in (
		"Email", "Desk", "Email + DingTalk Private Chat", "Desk + DingTalk Private Chat",
		"Email + DingTalk DING", "Desk + DingTalk DING",
	) if (channel_uses_email(name) if channel == "Email" else channel_uses_desk(name))]
	return bool(frappe.db.exists(LOG_DOCTYPE, {
		**filters, "delivery_channel": ["is", "not set"], "notification_channel": ["in", legacy_channels],
	}))


def create_desk_notification(doc, rule, user, user_email, subject, message):
	if not frappe.db.exists("DocType", DESK_NOTIFICATION_DOCTYPE):
		return

	if already_notified(doc, rule, user, subject):
		return

	try:
		frappe.get_doc(
			{
				"doctype": DESK_NOTIFICATION_DOCTYPE,
				"type": "Alert",
				"for_user": user,
				"from_user": doc.owner,
				"document_type": doc.doctype,
				"document_name": doc.name,
				"subject": subject,
				"email_content": message,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"Draft notification desk alert failed for user {user_email}",
			message=frappe.get_traceback(),
		)
		raise


def already_notified(doc, rule, user, subject):
	if not rule.deduplicate:
		return False

	return bool(
		frappe.db.exists(
			DESK_NOTIFICATION_DOCTYPE,
			{
				"type": "Alert",
				"for_user": user,
				"document_type": doc.doctype,
				"document_name": doc.name,
				"subject": subject,
			},
		)
	)


def render_subject(doc, rule, language=None):
	template = get_localized_template(rule, "subject", language) or "New draft {{ doc.doctype }} {{ doc.name }}"
	return frappe.render_template(template, get_template_context(doc))


def render_message(doc, rule, language=None):
	template = get_localized_template(rule, "message", language) or (
		"<p>A new draft {{ doc.doctype }} has been created: "
		'<a href="{{ doc_url }}">{{ doc.name }}</a></p>'
	)
	return frappe.render_template(template, get_template_context(doc))


def get_localized_template(rule, fieldname, language=None):
	language_key = get_language_key(language)
	localized_value = rule.get(f"{fieldname}_{language_key}") if language_key else None
	return localized_value or rule.get(fieldname)


def get_language_key(language):
	if not language:
		return None

	language = language.replace("_", "-").lower()
	if language.startswith("zh"):
		return "zh"
	if language.startswith("es"):
		return "es"
	if language.startswith("en"):
		return "en"
	return None


def get_template_context(doc):
	return {
		"doc": doc,
		"doc_url": get_url_to_form(doc.doctype, doc.name),
	}


def create_log(
	doc,
	rule,
	status,
	user=None,
	email=None,
	reason=None,
	email_queue=None,
	notification_channel=None,
	dingtalk_status=None,
	dingtalk_open_ding_id=None,
	dingtalk_error=None,
	notification_period=None,
	dingtalk_user_id=None,
	dingtalk_config=None,
	retry_of=None,
	delivery_channel=None,
):
	if not frappe.db.exists("DocType", LOG_DOCTYPE):
		return

	try:
		log_data = {
			"doctype": LOG_DOCTYPE,
			"rule": rule.name,
			"notification_channel": notification_channel or get_notification_channel(rule),
			"status": status,
			"user": user,
			"email": email,
			"email_queue": email_queue,
			"reference_doctype": doc.doctype,
			"reference_name": doc.name,
			"dingtalk_status": dingtalk_status,
			"dingtalk_open_ding_id": dingtalk_open_ding_id,
			"dingtalk_error": dingtalk_error,
			"reason": reason,
		}
		if not delivery_channel and notification_channel in ("DingTalk Private Chat", "DingTalk DING", "DingTalk Group"):
			delivery_channel = "DingTalk Group" if notification_channel == "DingTalk Group" else "DingTalk Private Chat"
		for field, value in (
			("dingtalk_user_id", dingtalk_user_id), ("dingtalk_config", dingtalk_config),
			("retry_of", retry_of), ("delivery_channel", delivery_channel),
		):
			if value is not None and frappe.db.has_column(LOG_DOCTYPE, field):
				log_data[field] = value
		if frappe.db.has_column(LOG_DOCTYPE, "notification_period"):
			log_data["notification_period"] = notification_period or rule.get("_notification_period")
		return frappe.get_doc(
			log_data
		).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(title="Failed to create Draft Notification Log", message=frappe.get_traceback())
		return None


def queue_log_sync_after_commit(log):
	if not log or not log.email_queue:
		return

	frappe.db.after_commit.add(lambda: send_email_queue_and_sync_log(log.email_queue, log.name))


def send_email_queue_and_sync_log(email_queue_name, log_name):
	try:
		if frappe.db.exists("Email Queue", email_queue_name):
			frappe.get_doc("Email Queue", email_queue_name).send()
	except Exception:
		frappe.log_error(
			title=f"Draft notification email send failed: {email_queue_name}",
			message=frappe.get_traceback(),
		)
	finally:
		log = frappe.db.get_value(LOG_DOCTYPE, log_name, ["name", "email", "email_queue"], as_dict=True)
		if log:
			sync_log_from_email_queue(log)
			frappe.db.commit()


def sync_queued_logs():
	if not frappe.db.exists("DocType", LOG_DOCTYPE):
		return

	logs = frappe.get_all(
		LOG_DOCTYPE,
		filters={
			"status": "Queued",
			"email_queue": ["is", "set"],
		},
		fields=["name", "email", "email_queue"],
		limit=200,
	)

	for log in logs:
		sync_log_from_email_queue(log)


def process_date_condition_notifications():
	"""Send due date-condition notifications once per configured period."""
	if not frappe.db.exists("DocType", RULE_DOCTYPE) or not frappe.db.has_column(RULE_DOCTYPE, "trigger_type"):
		return

	rules = frappe.get_all(
		RULE_DOCTYPE,
		filters={"enabled": 1, "trigger_type": "Date Condition"},
		fields="*",
	)
	today = getdate(nowdate())
	for rule in rules:
		if not rule.date_field or not frappe.db.exists("DocType", rule.document_type):
			continue
		for name in frappe.get_all(rule.document_type, pluck="name"):
			try:
				doc = frappe.get_doc(rule.document_type, name)
				if should_skip_date_condition(doc, rule):
					continue
				due_date = get_rule_date(doc, rule)
				if not due_date:
					continue
				target_date = add_days(due_date, int(rule.date_offset_days or 0))
				if today < getdate(target_date):
					continue
				interval = int(rule.repeat_interval_days or 0)
				period = "once" if interval <= 0 else str((today - getdate(target_date)).days // interval)
				rule._notification_period = period
				send_for_rule(doc, rule)
			except Exception:
				frappe.log_error(
					title=f"Draft date notification failed: {rule.name} {name}",
					message=frappe.get_traceback(),
				)


def get_rule_date(doc, rule):
	if rule.condition_scope == "Child Table" or (
		doc.doctype == "Purchase Order" and rule.date_field == "schedule_date"
	):
		values = [row.get(rule.date_field) for row in doc.get("items") or [] if row.get(rule.date_field)]
		return max(values) if values else None
	return doc.get(rule.date_field)


def should_skip_date_condition(doc, rule):
	return doc.doctype == "Purchase Order" and (
		float(doc.get("per_received") or 0) >= 100 or doc.get("status") in ("Cancelled", "Closed")
	)


def get_retry_root(log):
	"""Resolve old multi-level chains; all new attempts point directly to their root."""
	seen = set()
	while True:
		if log.name in seen:
			frappe.throw(_("Notification retry chain is invalid."))
		seen.add(log.name)
		if not log.get("retry_of"):
			return log.name
		log = frappe.get_doc(LOG_DOCTYPE, log.retry_of)
		log.check_permission("write")


def get_retry_attempt(root, channel, user=None, user_id=None, config=None):
	"""Find queued/successful attempts anywhere in a historical retry tree."""
	frontier = [root]
	seen = {root}
	while frontier:
		# Locking reads see committed attempts after waiting for the root lock,
		# including on databases configured with repeatable-read isolation.
		rows = frappe.db.get_values(
			LOG_DOCTYPE, filters={"retry_of": ["in", frontier]},
			fieldname=["name", "status", "notification_channel", "delivery_channel", "user", "dingtalk_user_id", "dingtalk_config"],
			as_dict=True, for_update=True,
		)
		frontier = []
		for row in rows:
			if row.name in seen:
				continue
			seen.add(row.name)
			frontier.append(row.name)
			if row.status not in ("Queued", "Sent"):
				continue
			actual = row.get("delivery_channel")
			if actual:
				matches = actual == channel
			elif channel == "Email":
				matches = channel_uses_email(row.notification_channel)
			elif channel == "Desk":
				matches = channel_uses_desk(row.notification_channel)
			else:
				matches = row.notification_channel in ("DingTalk Private Chat", "DingTalk DING")
			if not matches or (user and row.user != user):
				continue
			if user_id and (row.dingtalk_user_id != user_id or (config and row.dingtalk_config != config)):
				continue
			return row
	return None


@frappe.whitelist()
def retry_failed_log(log_name):
	if not frappe.has_permission(LOG_DOCTYPE, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	log = frappe.get_doc(LOG_DOCTYPE, log_name)
	log.check_permission("write")
	if log.status != "Failed":
		frappe.throw(_("Only failed draft notification logs can be retried."))

	if not log.rule or not frappe.db.exists(RULE_DOCTYPE, log.rule):
		frappe.throw(_("Draft notification rule is missing."))

	if not log.reference_doctype or not log.reference_name:
		frappe.throw(_("Reference document is missing."))

	if not frappe.db.exists(log.reference_doctype, log.reference_name):
		frappe.throw(_("Reference document no longer exists."))

	doc = frappe.get_doc(log.reference_doctype, log.reference_name)
	rule = frappe.get_doc(RULE_DOCTYPE, log.rule)

	if not rule.enabled:
		frappe.throw(_("Disabled notification rules cannot be retried."))
	if not all(frappe.db.has_column(LOG_DOCTYPE, field) for field in ("retry_of", "delivery_channel")):
		frappe.throw(_("Notification log schema is outdated. Please migrate the site before retrying."))
	root = get_retry_root(log)
	frappe.db.get_value(LOG_DOCTYPE, root, "name", for_update=True)
	channel = log.get("delivery_channel") or log.notification_channel or get_notification_channel(rule)
	# Legacy aggregate DingTalk logs can have both successful and failed retry children.
	if channel in ("DingTalk Private Chat", "DingTalk DING", "DingTalk Group") and log.get("dingtalk_user_id"):
		previous_retry = get_retry_attempt(root, "DingTalk Group" if channel == "DingTalk Group" else "DingTalk Private Chat", user_id=log.dingtalk_user_id,
			config=log.get("dingtalk_config"))
		if previous_retry:
			return {"status": previous_retry.status, "log": previous_retry.name}
	# Preserve the source period rather than interpreting a retry as today's reminder.
	rule._notification_period = log.get("notification_period")
	if channel in ("DingTalk Private Chat", "DingTalk DING", "DingTalk Group"):
		rule.notification_channel = channel
		if log.get("dingtalk_config"):
			rule.dingtalk_config = log.dingtalk_config
		retry_log = send_dingtalk_private_chat_for_rule(
			doc, rule, target_id=log.get("dingtalk_user_id"), retry_of=root,
		)
	else:
		retry_log = send_to_user(
			doc, rule, log.user, ignore_deduplicate=True,
			send_email=channel_uses_email(channel),
			send_desk=(channel == "Desk" if log.get("delivery_channel") else channel_uses_desk(channel)),
			notification_channel=log.notification_channel or channel, retry_of=root,
		)

	return {"status": retry_log.status if retry_log else None, "log": retry_log.name if retry_log else None}


@frappe.whitelist()
def retry_failed_logs(limit=100):
	if not frappe.has_permission(LOG_DOCTYPE, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	log_names = frappe.get_all(
		LOG_DOCTYPE,
		filters={"status": "Failed"},
		pluck="name",
		limit=int(limit or 100),
	)

	retried = []
	for log_name in log_names:
		try:
			result = retry_failed_log(log_name)
			retried.append({"source_log": log_name, **result})
		except Exception:
			frappe.log_error(title=f"Draft notification retry failed: {log_name}", message=frappe.get_traceback())

	return {"retried": retried}


def sync_log_from_email_queue(log):
	if not frappe.db.exists("Email Queue", log.email_queue):
		frappe.db.set_value(LOG_DOCTYPE, log.name, {"status": "Failed", "reason": "Email Queue record not found"})
		return

	queue = frappe.db.get_value("Email Queue", log.email_queue, ["status", "error"], as_dict=True)
	if not queue:
		return

	if queue.status == "Sent":
		frappe.db.set_value(LOG_DOCTYPE, log.name, {"status": "Sent", "reason": None})
	elif queue.status == "Error":
		frappe.db.set_value(
			LOG_DOCTYPE,
			log.name,
			{
				"status": "Failed",
				"reason": queue.error or "Email Queue status is Error",
			},
		)
	elif queue.status == "Partially Sent":
		sync_partially_sent_log(log, queue)


def sync_partially_sent_log(log, queue):
	recipient = frappe.db.get_value(
		"Email Queue Recipient",
		{
			"parent": log.email_queue,
			"recipient": log.email,
		},
		["status", "error"],
		as_dict=True,
	)

	if not recipient:
		frappe.db.set_value(
			LOG_DOCTYPE,
			log.name,
			{"status": "Failed", "reason": "Recipient not found in Email Queue"},
		)
	elif recipient.status == "Sent":
		frappe.db.set_value(LOG_DOCTYPE, log.name, {"status": "Sent", "reason": None})
	else:
		frappe.db.set_value(
			LOG_DOCTYPE,
			log.name,
			{
				"status": "Failed",
				"reason": recipient.error or queue.error or "Email Queue was partially sent",
			},
		)


def validate_custom_method(method_path):
	if not method_path:
		frappe.throw(_("Custom Method is required."))

	if method_path.startswith("_") or "._" in method_path:
		frappe.throw(_("Custom Method cannot point to private modules or methods."))

	allowed_prefixes = get_allowed_custom_method_prefixes()
	if not any(method_path.startswith(prefix) for prefix in allowed_prefixes):
		frappe.throw(
			_("Custom Method must start with one of these prefixes: {0}").format(", ".join(allowed_prefixes))
		)


def get_allowed_custom_method_prefixes():
	configured_prefixes = frappe.conf.get(ALLOWED_METHOD_PREFIXES_CONF)
	if configured_prefixes:
		if isinstance(configured_prefixes, str):
			configured_prefixes = configured_prefixes.split(",")
		return [prefix.strip() for prefix in configured_prefixes if prefix and prefix.strip()]

	return [f"{app}." for app in frappe.get_installed_apps() if app != "frappe"]


def as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		return [value]
	return list(value)


def unique(values):
	seen = set()
	result = []
	for value in values:
		if value in seen:
			continue
		seen.add(value)
		result.append(value)
	return result

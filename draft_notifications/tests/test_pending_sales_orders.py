"""Pending digest regressions: mocked SQL/sends; no site writes or real notifications."""

import csv
import json
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import frappe

from draft_notifications import dingtalk_group as group
from draft_notifications import dingtalk_robot as robot
from draft_notifications import pending_sales_orders as pending
from draft_notifications.tests.test_delivery import make_rule


def make_doc(**values):
	return frappe._dict({
		"doctype": "Sales Order", "name": "NEW-SO", "docstatus": 1, "company": "TEST COMPANY",
		"custom_crm_order_no": "CRM-NEW", "creation": datetime(2026, 10, 27, 9, 30), **values,
	})


def recipients(*users):
	return [{"user": user, "user_id": f"id-{user}", "enabled": 1, "language": "en"} for user in users]


class TestPendingQuery(TestCase):
	def setUp(self):
		self.doc = make_doc()
		self.db = MagicMock()
		self.db.sql.return_value = [
			frappe._dict(name="OLD-SO", custom_crm_order_no="CRM-OLD", pending_total=13),
		]
		self.meta = MagicMock(fields=[])
		for item in (
			patch.object(frappe.local, "db", self.db, create=True),
			patch.object(frappe, "get_meta", return_value=self.meta),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(pending, "get_permitted_fields", return_value=list(pending.REQUIRED_FIELDS)),
		):
			item.start()
			self.addCleanup(item.stop)

	def test_native_recipient_queries_intersect_before_page_and_count(self):
		order = frappe.qb.DocType("Sales Order")
		with patch.object(frappe, "get_list", side_effect=lambda *args, user, **kwargs:
			frappe.qb.from_(order).select(order.name).where(order.owner == user)) as get_list:
			result = pending.get_pending_summary(self.doc, recipients("A", "B", "A"))
		self.assertEqual(result, {"rows": [{"name": "OLD-SO", "custom_crm_order_no": "CRM-OLD"}], "total": 13})
		self.assertEqual([call.kwargs["user"] for call in get_list.call_args_list], ["A", "B"])
		for call in get_list.call_args_list:
			self.assertEqual(call.kwargs["fields"], ["name"])
			self.assertEqual(call.kwargs["limit"], 0)
			self.assertFalse(call.kwargs["run"])
			self.assertNotIn("ignore_permissions", call.kwargs)
			filters = call.kwargs["filters"]
			self.assertEqual(filters["company"], "TEST COMPANY")
			self.assertEqual(filters["custom_process_status"], "Pending Deposit Confirmation")
			self.assertEqual(filters["docstatus"], 1)
			self.assertEqual(filters["custom_crm_order_no"], ["is", "set"])
			self.assertEqual(filters["creation"], ["<", self.doc.creation])
			self.assertEqual(filters["name"], ["!=", "NEW-SO"])
			self.assertEqual(filters["status"], ["not in", ["Closed", "Cancelled", "Stopped", "On Hold"]])
		self.db.sql.assert_called_once()
		sql = self.db.sql.call_args.args[0]
		self.assertEqual(sql.count(" IN (SELECT "), 2)
		self.assertIn(" AND ", sql)
		self.assertIn("COUNT(`name`) OVER()", sql)
		self.assertTrue(sql.endswith("ORDER BY `creation` ASC,`name` ASC LIMIT 10"), sql)

	def test_empty_permission_intersection_returns_zero(self):
		self.db.sql.return_value = []
		with patch.object(frappe, "get_list", return_value=frappe.qb.from_("tabSales Order").select("name")):
			self.assertEqual(pending.get_pending_summary(self.doc, recipients("A")), {"rows": [], "total": 0})

	def test_unbound_disabled_and_empty_receivers_do_not_query(self):
		with patch.object(frappe, "get_list") as query:
			for rows in ([], [{"user_id": "unbound"}], [{"user": "A", "enabled": 0}],
				recipients("A") + [{"user_id": "unbound"}]):
				with self.subTest(rows=rows):
					self.assertIsNone(pending.get_pending_summary(self.doc, rows))
		query.assert_not_called()
		self.db.sql.assert_not_called()

	def test_mask_permission_is_checked_for_every_recipient_not_worker(self):
		self.meta.fields = [frappe._dict(fieldname="custom_crm_order_no", mask=1, permlevel=0)]
		self.meta.get_permlevel_access.side_effect = lambda *, permission_type, user: [0] if user == "A" else []
		with patch.object(frappe, "session", frappe._dict(user="Administrator")), patch.object(frappe, "get_list") as query:
			self.assertIsNone(pending.get_pending_summary(self.doc, recipients("A", "B")))
		query.assert_not_called()
		self.assertEqual([call.kwargs["user"] for call in self.meta.get_permlevel_access.call_args_list], ["A", "B"])
		self.assertTrue(all(call.kwargs["permission_type"] == "mask" for call in self.meta.get_permlevel_access.call_args_list))
		self.db.sql.assert_not_called()

	def test_masked_filter_field_also_hides_counts(self):
		self.meta.fields = [frappe._dict(fieldname="company", mask=1, permlevel=1)]
		self.meta.get_permlevel_access.return_value = [0]
		with patch.object(frappe, "get_list") as query:
			self.assertIsNone(pending.get_pending_summary(self.doc, recipients("A")))
		query.assert_not_called()

	def test_recipients_with_mask_permission_or_administrator_can_read_original_identifiers(self):
		self.meta.fields = [frappe._dict(fieldname="custom_crm_order_no", mask=1, permlevel=1)]
		self.meta.get_permlevel_access.return_value = [1]
		visible = frappe.qb.from_("tabSales Order").select("name")
		for user in ("A", "Administrator"):
			with self.subTest(user=user), patch.object(frappe, "get_list", return_value=visible):
				self.assertEqual(pending.get_pending_summary(self.doc, recipients(user))["rows"][0]["custom_crm_order_no"], "CRM-OLD")
		# Native masking exempts Administrator even when the worker is a different user.
		self.meta.get_permlevel_access.assert_called_once_with(permission_type="mask", user="A")

	def test_unrelated_masked_field_does_not_suppress_identifiers(self):
		self.meta.fields = [frappe._dict(fieldname="customer_name", mask=1, permlevel=0)]
		with patch.object(frappe, "get_list", return_value=frappe.qb.from_("tabSales Order").select("name")):
			self.assertEqual(pending.get_pending_summary(self.doc, recipients("A"))["total"], 13)
		self.meta.get_permlevel_access.assert_not_called()

	def test_any_recipient_without_read_or_identifier_field_hides_digest(self):
		for denied_field in (None, "custom_crm_order_no", "custom_process_status", "company"):
			with (
				self.subTest(field=denied_field),
				patch.object(frappe, "has_permission", side_effect=lambda *a, user, **kw: user != "B" or denied_field is not None),
				patch.object(pending, "get_permitted_fields", side_effect=lambda *a, user, **kw:
					pending.REQUIRED_FIELDS - ({denied_field} if user == "B" else set())),
				patch.object(frappe, "get_list") as query,
			):
				self.assertIsNone(pending.get_pending_summary(self.doc, recipients("A", "B")))
				query.assert_not_called()
		self.db.sql.assert_not_called()


class TestPendingMessage(TestCase):
	def setUp(self):
		self.doc = make_doc()
		self.rule = make_rule(trigger_type="Document Event", trigger_event="On Submit", notification_channel="DingTalk Group")
		self.summary = {"rows": [{"name": f"OLD-{i}", "custom_crm_order_no": f"CRM-{i}"} for i in range(10)], "total": 13}
		self.translations = {}
		for language in ("zh", "en", "es"):
			path = Path(pending.__file__).parent / "translations" / f"{language}.csv"
			with path.open(newline="") as handle:
				self.translations[language] = {row[0]: row[1] for row in csv.reader(handle) if len(row) >= 2}
		for item in (
			patch.object(pending, "get_summary_receivers", side_effect=lambda doc, rule, rows: rows),
			patch.object(pending, "_", side_effect=lambda text, lang: self.translations[lang][text]),
			patch.object(pending, "get_url", side_effect=lambda path: "https://erp.invalid" + path),
		):
			item.start()
			self.addCleanup(item.stop)

	def compose(self, **kwargs):
		return pending.compose_group_message(self.doc, self.rule, "NEW-ORDER-DETAILS", kwargs.get("lang", "en"), recipients("A"))

	def test_only_submitted_crm_order_document_submit_events_append_digest(self):
		with patch.object(pending, "get_pending_summary") as query:
			for changes in (
				{"docstatus": 0}, {"docstatus": 2}, {"doctype": "Purchase Order"},
				{"custom_crm_order_no": ""}, {"company": ""}, {"creation": None},
			):
				with self.subTest(changes=changes):
					self.assertEqual(pending.compose_group_message(make_doc(**changes), self.rule, "original", "en", recipients("A")), "original")
			for changes in (
				{"trigger_event": "After Insert"}, {"trigger_event": "On Update"}, {"trigger_event": "On Cancel"},
				{"trigger_type": "Date Condition"}, {"trigger_type": "Status Change"},
			):
				with self.subTest(changes=changes):
					self.assertEqual(pending.compose_group_message(self.doc, frappe._dict({**self.rule, **changes}), "original", "en", recipients("A")), "original")
		query.assert_not_called()

	def test_ten_old_identifiers_total_remainder_and_three_languages(self):
		for language, heading in (("zh", "新增销售订单"), ("en", "New sales order"), ("es", "Nuevo pedido de venta")):
			with self.subTest(language=language), patch.object(pending, "get_pending_summary", return_value=self.summary):
				message = self.compose(lang=language)
				self.assertTrue(message.startswith(f"### {heading}\n\nNEW-ORDER-DETAILS"))
				self.assertIn(self.translations[language]["Historical orders pending financial review ({0})"].format(13), message)
				self.assertIn(self.translations[language]["{0} more pending orders."].format(3), message)
				self.assertEqual(message.count(" | CRM: "), 10)
				self.assertIn(r"10. ERP: OLD\-9 | CRM: CRM\-9", message)
				self.assertIn(self.translations[language]["View all historical pending orders"], message)

	def test_zero_or_invisible_history_omits_empty_section(self):
		for summary in (None, {"rows": [], "total": 0}):
			with self.subTest(summary=summary), patch.object(pending, "get_pending_summary", return_value=summary):
				self.assertEqual(self.compose(), "### New sales order\n\nNEW-ORDER-DETAILS")

	def test_datetime_and_company_special_characters_roundtrip_as_route_filters(self):
		self.doc.company = "Test & Company + México"
		url = pending.pending_list_url(self.doc)
		query = parse_qs(urlsplit(url).query)
		self.assertEqual(query["company"], [self.doc.company])
		self.assertEqual(json.loads(query["creation"][0]), ["<", "2026-10-27 09:30:00"])
		self.assertEqual(json.loads(query["status"][0]), ["not in", ["Closed", "Cancelled", "Stopped", "On Hold"]])
		self.assertEqual(query["custom_process_status"], ["Pending Deposit Confirmation"])
		self.assertEqual(json.loads(query["name"][0]), ["!=", "NEW-SO"])

	def test_external_identifier_cannot_inject_markup_or_new_lines(self):
		malicious = "<script>\n[link](https://evil.invalid) *bold*"
		self.summary["rows"][0]["custom_crm_order_no"] = malicious
		with patch.object(pending, "get_pending_summary", return_value=self.summary):
			message = self.compose()
		self.assertNotIn("<script>", message)
		self.assertNotIn("[link](https://evil.invalid)", message)
		self.assertIn(r"&lt;script&gt; \[link\]\(https://evil\.invalid\) \*bold\*", message)
		self.assertEqual(self.summary["rows"][0]["custom_crm_order_no"], malicious)

	def test_query_or_render_failure_keeps_original_notification_with_warning(self):
		for failing_method in ("get_summary_receivers", "get_pending_summary", "pending_list_url"):
			with (
				self.subTest(method=failing_method),
				patch.object(pending, "get_pending_summary", return_value=self.summary),
				patch.object(pending, failing_method, side_effect=RuntimeError("test failure")),
				patch.object(frappe, "get_traceback", return_value="test traceback"),
				patch.object(frappe, "log_error") as logger,
			):
				message = self.compose()
				self.assertIn("NEW-ORDER-DETAILS", message)
				self.assertIn("Historical pending orders could not be loaded", message)
				self.assertNotIn("OLD", message)
				logger.assert_called_once()

	def test_permission_error_reveals_no_count_or_warning(self):
		with patch.object(pending, "get_pending_summary", side_effect=frappe.PermissionError):
			self.assertEqual(self.compose(), "### New sales order\n\nNEW-ORDER-DETAILS")

	def test_database_logging_failure_uses_file_log_and_preserves_new_order(self):
		with (
			patch.object(pending, "get_pending_summary", side_effect=RuntimeError("test")),
			patch.object(frappe, "get_traceback", return_value="original traceback"),
			patch.object(frappe, "log_error", side_effect=RuntimeError("log db unavailable")),
			patch.object(frappe, "logger") as logger,
		):
			self.assertIn("NEW-ORDER-DETAILS", self.compose())
		logger.return_value.error.assert_called_once_with("original traceback")

	def test_group_integration_keeps_full_audience_across_languages_and_twenty_person_batches(self):
		rows = recipients(*(f"user-{index}" for index in range(21)))
		rows += [{**recipients("spanish")[0], "language": "es"}]
		with (
			patch.object(robot, "get_dingtalk_config", return_value=frappe._dict(robot_type="Group Webhook")),
			patch.object(robot, "render_dingtalk_message", side_effect=lambda doc, rule, language: language),
			patch.object(pending, "compose_group_message", side_effect=lambda doc, rule, message, language, receivers: message + " digest") as compose,
			patch.object(group, "send_group_message", return_value={"accepted": True}) as send,
		):
			results = robot.send_private_chat_notification(self.doc, self.rule, receivers=rows)
		self.assertTrue(all(row["status"] == "Sent" for row in results))
		self.assertEqual([len(call.args[4]) for call in compose.call_args_list], [22, 22, 22])
		self.assertEqual([call.args[3] for call in compose.call_args_list], ["en", "en", "es"])
		self.assertEqual([call.args[2] for call in send.call_args_list], ["en digest", "en digest", "es digest"])
		self.assertTrue(all(call.args[4] == rows for call in compose.call_args_list))

	def test_history_not_readable_by_every_language_batch_never_enters_public_messages(self):
		rows = recipients("A", "B")
		rows[0]["language"], rows[1]["language"] = "zh", "es"
		def summary(doc, receivers):
			return self.summary if {row["user"] for row in receivers} == {"A"} else {"rows": [], "total": 0}
		with (
			patch.object(robot, "get_dingtalk_config", return_value=frappe._dict(robot_type="Group Webhook")),
			patch.object(robot, "render_dingtalk_message", return_value="NEW ORDER READABLE BY BOTH"),
			patch.object(pending, "get_pending_summary", side_effect=summary),
			patch.object(group, "send_group_message", return_value={"accepted": True}) as send,
		):
			result = robot.send_private_chat_notification(self.doc, self.rule, receivers=rows)
		self.assertTrue(all(row["status"] == "Sent" for row in result))
		self.assertEqual(send.call_count, 2)
		self.assertTrue(all(" | CRM: " not in call.args[2] for call in send.call_args_list))

	def test_retry_only_mentions_failed_user_but_keeps_full_summary_scope(self):
		rows = recipients("A", "B")
		with (
			patch.object(robot, "get_dingtalk_config", return_value=frappe._dict(robot_type="Group Webhook")),
			patch.object(robot, "render_dingtalk_message", return_value="NEW ORDER DETAILS"),
			patch.object(pending, "get_pending_summary", return_value={"rows": [], "total": 0}) as query,
			patch.object(group, "send_group_message", return_value={"accepted": True}) as send,
		):
			robot.send_private_chat_notification(self.doc, self.rule, receivers=rows[:1], summary_receivers=rows)
		self.assertEqual(query.call_args.args[1], rows)
		self.assertEqual(send.call_args.args[1], ["id-A"])


class TestHistoricalAudience(TestCase):
	def setUp(self):
		self.doc = make_doc()
		self.rule = make_rule(notification_channel="DingTalk Group")

	def test_previous_users_survive_rule_changes_and_deleted_users_fail_closed(self):
		with patch.object(frappe, "get_all", side_effect=[
			["A", "B", "deleted"], [frappe._dict(name="A", enabled=1), frappe._dict(name="B", enabled=0)],
		]) as query:
			scope = pending.get_summary_receivers(self.doc, self.rule, recipients("A"))
		self.assertEqual(scope[-2:], [{"user": "B", "enabled": 0}, {"user": "deleted", "enabled": 0}])
		filters = query.call_args_list[0].kwargs["filters"]
		self.assertEqual(filters, {"rule": self.rule.name, "reference_doctype": "Sales Order", "reference_name": "NEW-SO",
			"dingtalk_config": "TEST-ROBOT", "notification_channel": "DingTalk Group"})
		self.assertTrue(query.call_args_list[0].kwargs["distinct"])
		self.assertIsNone(pending.get_pending_summary(self.doc, scope))

	def test_legacy_unmapped_audience_omits_history(self):
		with patch.object(frappe, "get_all", return_value=["A", None]) as query:
			self.assertEqual(pending.get_summary_receivers(self.doc, self.rule, recipients("A")), [])
		query.assert_called_once()

	def test_first_send_uses_full_rule_scope_without_extra_user_lookup(self):
		rows = recipients("A", "B")
		with patch.object(frappe, "get_all", return_value=[]) as query:
			self.assertEqual(pending.get_summary_receivers(self.doc, self.rule, rows), rows)
		query.assert_called_once()

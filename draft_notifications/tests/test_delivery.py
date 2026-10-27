"""Isolated delivery regressions: no site writes or outbound requests."""

import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from draft_notifications import dingtalk_robot as robot
from draft_notifications.draft_notifications import draft_notification as notifications


def make_rule(**values):
	return frappe._dict({
		"name": "TEST-RULE", "enabled": 1, "deduplicate": 1, "respect_permissions": 1,
		"dingtalk_config": "TEST-ROBOT", "notification_channel": "Email + DingTalk Private Chat",
		**values,
	})


class TestDingTalkBatches(TestCase):
	def send(self, receivers, send_mock, callback=None):
		with (
			patch.object(robot, "get_dingtalk_config", return_value=frappe._dict(name="TEST-ROBOT")),
			patch.object(robot, "send_private_chat_message", send_mock),
			patch.object(robot, "render_dingtalk_message", side_effect=lambda doc, rule, language: language or "default"),
		):
			return robot.send_private_chat_notification(
				frappe._dict(doctype="Sales Order", name="TEST-SO"), make_rule(),
				receivers=receivers, on_result=callback,
			)

	def test_language_groups_are_split_into_batches_of_twenty(self):
		send = MagicMock(return_value={"processQueryKey": "receipt"})
		receivers = [{"user_id": str(i), "language": "zh"} for i in range(45)]
		receivers.append({"user_id": "es-user", "language": "es"})
		results = self.send(receivers, send)
		self.assertEqual([len(call.kwargs["receiver_user_ids"]) for call in send.call_args_list], [20, 20, 5, 1])
		self.assertEqual([call.kwargs["content"] for call in send.call_args_list], ["zh", "zh", "zh", "es"])
		self.assertEqual(len(results), 46)
		self.assertTrue(all(result["status"] == "Sent" for result in results))

	def test_rejected_throttled_and_filtered_recipients_are_not_marked_sent(self):
		send = MagicMock(return_value={
			"processQueryKey": "receipt", "invalidStaffIdList": ["invalid"],
			"flowControlledStaffIdList": ["throttled"], "filteredStaffIdList": ["filtered"],
		})
		results = self.send([{"user_id": user} for user in ["ok", "invalid", "throttled", "filtered"]], send)
		self.assertEqual([result["status"] for result in results], ["Sent", "Failed", "Failed", "Failed"])

	def test_later_batch_error_preserves_successes_and_continues(self):
		send = MagicMock(side_effect=[{"processQueryKey": "first"}, robot.DingTalkAPIError("timeout"), {"processQueryKey": "last"}])
		callback = MagicMock()
		results = self.send([{"user_id": str(i)} for i in range(41)], send, callback)
		self.assertEqual([result["status"] for result in results], ["Sent"] * 20 + ["Failed"] * 20 + ["Sent"])
		self.assertEqual(callback.call_count, 3)
		self.assertEqual(results[20]["reason"], "timeout")

	def test_empty_provider_response_is_a_failure(self):
		with (
			patch.object(robot, "get_dingtalk_config", return_value=frappe._dict(robot_code="TEST")),
			patch.object(robot, "get_dingtalk_access_token", return_value="test-only"),
			patch.object(robot, "post_dingtalk_json", return_value={}),
		):
			with self.assertRaises(robot.DingTalkAPIError):
				robot.send_private_chat_message("TEST-ROBOT", ["user-id"], "test")

	def test_fixed_and_dynamic_ids_resolve_to_users_and_keep_disabled_state(self):
		config = frappe._dict(recipients=[frappe._dict(user_id="fixed"), frappe._dict(user_id="disabled")])
		with (
			patch.object(robot, "user_has_dingtalk_user_id_field", return_value=True),
			patch.object(frappe, "get_all", side_effect=[
				[frappe._dict(dingtalk_user_id="dynamic"), frappe._dict(dingtalk_user_id="fixed")],
				[frappe._dict(name="fixed-user", dingtalk_user_id="fixed", enabled=1, language="zh-CN"),
				 frappe._dict(name="disabled-user", dingtalk_user_id="disabled", enabled=0, language="en"),
				 frappe._dict(name="dynamic-user", dingtalk_user_id="dynamic", enabled=1, language="es")],
			]),
		):
			receivers = robot.get_dingtalk_receivers("TEST-ROBOT", ["dynamic-user"], _config=config)
		self.assertEqual([receiver["user_id"] for receiver in receivers], ["fixed", "disabled", "dynamic"])
		self.assertEqual(receivers[0]["user"], "fixed-user")
		self.assertEqual(receivers[1]["enabled"], 0)
		self.assertEqual(receivers[2]["language"], "es")


class TestNotificationDelivery(TestCase):
	def setUp(self):
		self.doc = frappe._dict(doctype="Sales Order", name="TEST-SO")
		self.rule = make_rule()
		self.db = MagicMock()
		self.db.exists.return_value = False
		self.db.has_column.return_value = True
		self.db.get_value.return_value = None
		self.patches = [
			patch.object(frappe, "db", self.db),
			patch.object(frappe, "log_error"),
			patch.object(frappe, "get_traceback", return_value="test traceback"),
			patch.object(notifications, "get_candidate_users", return_value=["dynamic-user"]),
			patch.object(notifications, "can_read_doc", side_effect=lambda doc, user: user != "denied"),
			patch.object(notifications, "create_log", side_effect=self.log),
		]
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)
		self.logs = []

	def log(self, doc, rule, **values):
		log = frappe._dict(name=f"LOG-{len(self.logs)}", **values)
		self.logs.append(log)
		return log

	def deliver(self, receivers, **kwargs):
		def send(doc, rule, *, receivers, on_result):
			on_result([{"user_id": row["user_id"], "status": "Sent", "reason": None} for row in receivers])
		with (
			patch.object(robot, "get_dingtalk_receivers", return_value=receivers) as resolve,
			patch.object(robot, "send_private_chat_notification", side_effect=send) as sender,
		):
			result = notifications.send_dingtalk_private_chat_for_rule(self.doc, self.rule, **kwargs)
			return result, resolve, sender

	def test_permissions_disabled_and_unbound_ids_are_filtered(self):
		_, _, sender = self.deliver([
			{"user_id": "ok-id", "user": "ok", "enabled": 1},
			{"user_id": "disabled-id", "user": "disabled", "enabled": 0},
			{"user_id": "denied-id", "user": "denied", "enabled": 1},
			{"user_id": "unbound"}, {"user_id": "ambiguous", "ambiguous": True},
		])
		self.assertEqual([row["user_id"] for row in sender.call_args.kwargs["receivers"]], ["ok-id"])
		self.assertEqual([log.status for log in self.logs], ["Sent", "Skipped", "Skipped", "Skipped", "Skipped"])

	def test_explicit_permission_opt_out_keeps_unbound_fixed_ids(self):
		self.rule.respect_permissions = 0
		_, _, sender = self.deliver([{"user_id": "fixed"}, {"user_id": "disabled", "user": "disabled", "enabled": 0}])
		self.assertEqual([row["user_id"] for row in sender.call_args.kwargs["receivers"]], ["fixed"])

	def test_dingtalk_dedup_is_per_target_channel_config_and_date_period(self):
		self.rule.trigger_type = "Date Condition"
		self.rule._notification_period = "3"
		self.db.exists.side_effect = lambda doctype, filters: filters.get("dingtalk_user_id") == "done"
		_, _, sender = self.deliver([{"user_id": "done", "user": "ok", "enabled": 1}, {"user_id": "new", "user": "ok", "enabled": 1}])
		self.assertEqual([row["user_id"] for row in sender.call_args.kwargs["receivers"]], ["new"])
		filters = self.db.exists.call_args_list[0].args[1]
		self.assertEqual(filters["notification_channel"], "DingTalk Private Chat")
		self.assertEqual(filters["notification_period"], "3")
		self.assertEqual(filters["dingtalk_config"], "TEST-ROBOT")
		self.assertEqual(filters["status"], ["in", ["Queued", "Sent"]])

	def test_legacy_successful_aggregate_log_still_deduplicates(self):
		self.db.exists.side_effect = [False, True]
		self.assertTrue(notifications.already_sent_dingtalk(self.doc, self.rule, "legacy-recipient"))
		self.assertEqual(self.db.exists.call_args.args[1]["dingtalk_user_id"], ["is", "not set"])

	def test_partial_recipient_results_are_persisted_separately(self):
		def send(doc, rule, *, receivers, on_result):
			on_result([{"user_id": "ok", "status": "Sent", "reason": None}])
			on_result([{"user_id": "fail", "status": "Failed", "reason": "throttled"}])
		with (
			patch.object(robot, "get_dingtalk_receivers", return_value=[
				{"user_id": "ok", "user": "ok", "enabled": 1}, {"user_id": "fail", "user": "ok", "enabled": 1},
			]),
			patch.object(robot, "send_private_chat_notification", side_effect=send),
		):
			result = notifications.send_dingtalk_private_chat_for_rule(self.doc, self.rule)
		self.assertEqual([log.status for log in self.logs], ["Sent", "Failed"])
		self.assertEqual(result.dingtalk_user_id, "fail")

	def test_email_dedup_does_not_match_dingtalk_logs(self):
		notifications.already_sent(self.doc, self.rule, "ok", "Email")
		# DocType existence is checked first.
		self.db.exists.return_value = True
		notifications.already_sent(self.doc, self.rule, "ok", "Email")
		self.assertEqual(self.db.exists.call_args.args[1]["delivery_channel"], "Email")

	def test_legacy_retry_re_resolves_dynamic_users_and_skips_successes_even_when_dedup_off(self):
		self.rule.deduplicate = 0
		self.db.exists.side_effect = lambda doctype, filters: filters.get("dingtalk_user_id") == "done"
		_, resolve, sender = self.deliver([
			{"user_id": "done", "user": "ok", "enabled": 1},
			{"user_id": "retry", "user": "ok", "enabled": 1},
		], retry_of="OLD-AGGREGATE")
		self.assertEqual(resolve.call_args.kwargs["receiver_users"], ["dynamic-user"])
		self.assertEqual([row["user_id"] for row in sender.call_args.kwargs["receivers"]], ["retry"])

	def test_schema_must_be_migrated_before_any_dingtalk_send(self):
		self.db.has_column.return_value = False
		with patch.object(robot, "send_private_chat_notification") as sender:
			result = notifications.send_dingtalk_private_chat_for_rule(self.doc, self.rule)
		sender.assert_not_called()
		self.assertEqual(result.status, "Failed")

	def test_cannot_send_without_persisting_recipient_log(self):
		with patch.object(notifications, "create_log", return_value=None):
			_, _, sender = self.deliver([{"user_id": "ok-id", "user": "ok", "enabled": 1}])
		sender.assert_not_called()

	def test_no_recipients_has_visible_skipped_log(self):
		result, _, sender = self.deliver([])
		self.assertEqual(result.status, "Skipped")
		sender.assert_not_called()

	def test_target_retry_does_not_send_to_newly_added_receivers(self):
		_, _, sender = self.deliver([
			{"user_id": "original", "user": "ok", "enabled": 1},
			{"user_id": "added", "user": "ok", "enabled": 1},
		], target_id="original", retry_of="OLD-LOG")
		self.assertEqual([row["user_id"] for row in sender.call_args.kwargs["receivers"]], ["original"])

	def test_setup_failure_does_not_leave_reserved_targets_queued(self):
		with (
			patch.object(robot, "get_dingtalk_receivers", return_value=[{"user_id": "ok-id", "user": "ok", "enabled": 1}]),
			patch.object(robot, "send_private_chat_notification", side_effect=ValueError("config disabled")),
		):
			result = notifications.send_dingtalk_private_chat_for_rule(self.doc, self.rule)
		self.assertEqual(result.status, "Failed")
		self.assertEqual(self.db.set_value.call_args.args[2]["status"], "Failed")


class TestNotificationRetry(TestCase):
	def setUp(self):
		self.log = frappe._dict(
			name="OLD-LOG", status="Failed", rule="TEST-RULE", user="recipient",
			reference_doctype="Sales Order", reference_name="TEST-SO", notification_period="2",
			notification_channel="Desk", check_permission=MagicMock(),
		)
		self.rule = make_rule()
		self.db = MagicMock()
		self.db.get_value.return_value = None
		self.db.has_column.return_value = True
		self.patches = [patch.object(frappe, "db", self.db), patch.object(frappe, "has_permission", return_value=True),
			patch.object(notifications, "get_retry_attempt", return_value=None),
			patch.object(frappe, "get_doc", side_effect=lambda doctype, name: {
				notifications.LOG_DOCTYPE: self.log, notifications.RULE_DOCTYPE: self.rule,
				"Sales Order": frappe._dict(doctype="Sales Order", name="TEST-SO"),
			}[doctype])]
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)

	def test_retry_preserves_each_original_email_and_desk_channel(self):
		for channel, email, desk in [("Desk", False, True), ("Email", True, True),
			("Email + DingTalk Private Chat", True, True), ("Desk + DingTalk Private Chat", False, True)]:
			with self.subTest(channel=channel), patch.object(notifications, "send_to_user", return_value=frappe._dict(status="Sent", name="NEW")) as sender:
				self.log.notification_channel = channel
				notifications.retry_failed_log("OLD-LOG")
				self.assertEqual(sender.call_args.kwargs["send_email"], email)
				self.assertEqual(sender.call_args.kwargs["send_desk"], desk)
				self.assertEqual(sender.call_args.kwargs["retry_of"], "OLD-LOG")

	def test_dingtalk_retry_preserves_target_config_and_period(self):
		self.log.notification_channel = "DingTalk Private Chat"
		self.log.dingtalk_user_id = "original-id"
		self.log.dingtalk_config = "ORIGINAL-ROBOT"
		with patch.object(notifications, "send_dingtalk_private_chat_for_rule", return_value=frappe._dict(status="Sent", name="NEW")) as sender:
			notifications.retry_failed_log("OLD-LOG")
		self.assertEqual(sender.call_args.kwargs["target_id"], "original-id")
		self.assertEqual(sender.call_args.args[1].dingtalk_config, "ORIGINAL-ROBOT")
		self.assertEqual(sender.call_args.args[1]._notification_period, "2")

	def test_repeated_retry_reuses_existing_queued_or_sent_attempt(self):
		existing = frappe._dict(name="ALREADY-RETRIED", status="Queued")
		with (
			patch.object(notifications, "get_retry_attempt", return_value=existing),
			patch.object(frappe, "sendmail") as sender,
		):
			result = notifications.retry_failed_log("OLD-LOG")
		self.assertEqual(result, {"status": "Queued", "log": "ALREADY-RETRIED"})
		sender.assert_not_called()

	def test_legacy_aggregate_retry_does_not_stop_after_one_successful_child(self):
		self.log.notification_channel = "DingTalk DING"
		self.db.get_value.return_value = frappe._dict(name="SUCCESSFUL-CHILD", status="Sent")
		with patch.object(notifications, "send_dingtalk_private_chat_for_rule", return_value=frappe._dict(status="Failed", name="REMAINING")) as sender:
			result = notifications.retry_failed_log("OLD-LOG")
		sender.assert_called_once()
		self.assertEqual(result["status"], "Failed")

	def test_retry_requires_log_write_permission_and_enabled_rule(self):
		with patch.object(frappe, "has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				notifications.retry_failed_log("OLD-LOG")
		self.rule.enabled = 0
		with patch.object(notifications, "send_to_user") as sender:
			with self.assertRaises(frappe.ValidationError):
				notifications.retry_failed_log("OLD-LOG")
		sender.assert_not_called()

	def test_user_send_desk_only_never_creates_email_queue(self):
		with (
			patch.object(notifications, "get_enabled_user_data", return_value=frappe._dict(email="")),
			patch.object(notifications, "can_read_doc", return_value=True),
			patch.object(notifications, "get_user_language", return_value="en"),
			patch.object(notifications, "render_subject", return_value="subject"),
			patch.object(notifications, "render_message", return_value="message"),
			patch.object(notifications, "create_desk_notification") as desk,
			patch.object(notifications, "create_log", return_value=frappe._dict(name="NEW", status="Sent")) as log,
			patch.object(frappe, "sendmail") as mail,
		):
			notifications.send_to_user(frappe._dict(), self.rule, "recipient", ignore_deduplicate=True,
				send_email=False, send_desk=True, notification_channel="Desk", retry_of="OLD-LOG")
		mail.assert_not_called()
		desk.assert_called_once()
		self.assertEqual(log.call_args.kwargs["retry_of"], "OLD-LOG")

	def test_desk_insert_failure_propagates_to_delivery_failure(self):
		with (
			patch.object(notifications, "already_notified", return_value=False),
			patch.object(frappe, "get_doc", side_effect=RuntimeError("insert failed")),
			patch.object(frappe, "log_error"),
		):
			with self.assertRaisesRegex(RuntimeError, "insert failed"):
				notifications.create_desk_notification(frappe._dict(owner="owner", doctype="Sales Order", name="TEST-SO"),
					self.rule, "recipient", "test@example.invalid", "subject", "message")

	def test_default_system_manager_permissions_include_retry_write(self):
		path = Path(notifications.__file__).parent / "doctype/draft_notification_log/draft_notification_log.json"
		meta = json.loads(path.read_text())
		manager = next(row for row in meta["permissions"] if row["role"] == "System Manager")
		self.assertEqual(manager["write"], 1)
		for fieldname in ("dingtalk_user_id", "dingtalk_config", "retry_of", "delivery_channel"):
			self.assertTrue(next(row for row in meta["fields"] if row["fieldname"] == fieldname)["read_only"])


class TestRetryChainAndIndependentChannels(TestCase):
	def setUp(self):
		self.doc = frappe._dict(doctype="Sales Order", name="TEST-SO", owner="owner")
		self.rule = make_rule()
		self.rows = {}
		self.db = MagicMock()
		self.db.has_column.return_value = True
		self.db.exists.return_value = True
		self.db.get_value.return_value = None
		self.db.get_values.side_effect = lambda doctype, filters, fieldname, as_dict, for_update: self.get_all(doctype, filters, fieldname)
		self.logs = []
		self.patches = [
			patch.object(frappe, "db", self.db),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_doc", side_effect=self.get_doc),
			patch.object(frappe, "get_all", side_effect=self.get_all),
			patch.object(frappe, "log_error"),
			patch.object(frappe, "get_traceback", return_value="isolated failure"),
			patch.object(notifications, "get_enabled_user_data", return_value=frappe._dict(email="test@example.invalid")),
			patch.object(notifications, "can_read_doc", return_value=True),
			patch.object(notifications, "get_user_language", return_value="en"),
			patch.object(notifications, "render_subject", return_value="subject"),
			patch.object(notifications, "render_message", return_value="message"),
			patch.object(notifications, "already_sent", return_value=False),
			patch.object(notifications, "create_log", side_effect=self.create_log),
			patch.object(notifications, "queue_log_sync_after_commit"),
		]
		for item in self.patches:
			item.start()
			self.addCleanup(item.stop)

	def row(self, name, **values):
		row = frappe._dict(
			name=name, status="Failed", user="recipient", rule="TEST-RULE",
			reference_doctype="Sales Order", reference_name="TEST-SO", notification_channel="Email",
			delivery_channel="Email", check_permission=MagicMock(),
		)
		row.update(values)
		self.rows[name] = row
		return row

	def get_doc(self, doctype, name):
		if doctype == notifications.LOG_DOCTYPE:
			return self.rows[name]
		return self.rule if doctype == notifications.RULE_DOCTYPE else self.doc

	def get_all(self, doctype, filters, fields):
		return [row for row in self.rows.values() if row.get("retry_of") in filters["retry_of"][1]]

	def create_log(self, doc, rule, **values):
		row = self.row(f"NEW-{len(self.logs)}", **values)
		self.logs.append(row)
		return row

	def test_successful_grandchild_prevents_resend_from_every_failed_ancestor(self):
		self.row("A")
		self.row("B", retry_of="A")
		self.row("C", retry_of="B", status="Sent")
		with patch.object(frappe, "sendmail") as mail, patch.object(notifications, "create_desk_notification") as desk:
			for name in ("A", "B"):
				self.assertEqual(notifications.retry_failed_log(name), {"status": "Sent", "log": "C"})
		mail.assert_not_called()
		desk.assert_not_called()
		locks = [call.args[1] for call in self.db.get_value.call_args_list if call.kwargs.get("for_update")]
		self.assertEqual(locks, ["A", "A"])
		self.assertTrue(all(call.kwargs["for_update"] for call in self.db.get_values.call_args_list))

	def test_new_attempts_from_a_failed_grandchild_point_to_the_root(self):
		self.row("A")
		self.row("B", retry_of="A")
		self.row("C", retry_of="B")
		with patch.object(frappe, "sendmail", return_value=frappe._dict(name="QUEUE")):
			notifications.retry_failed_log("C")
		self.assertEqual(self.logs[0].retry_of, "A")
		self.assertEqual(self.logs[0].delivery_channel, "Email")

	def test_queued_grandchild_prevents_another_attempt(self):
		self.row("A")
		self.row("B", retry_of="A")
		self.row("C", retry_of="B", status="Queued")
		with patch.object(frappe, "sendmail") as mail:
			self.assertEqual(notifications.retry_failed_log("A")["log"], "C")
		mail.assert_not_called()

	def test_desk_failure_is_recorded_but_email_is_still_queued(self):
		with (
			patch.object(notifications, "create_desk_notification", side_effect=RuntimeError("desk failed")),
			patch.object(frappe, "sendmail", return_value=frappe._dict(name="QUEUE")) as mail,
		):
			result = notifications.send_to_user(self.doc, self.rule, "recipient", notification_channel="Email")
		mail.assert_called_once()
		self.assertEqual([(row.delivery_channel, row.status) for row in self.logs], [("Desk", "Failed"), ("Email", "Queued")])
		self.assertEqual(result.email_queue, "QUEUE")
		self.assertEqual(self.logs[0].reason, "isolated failure")

	def test_only_the_failed_desk_channel_is_retried_after_email_queueing(self):
		with (
			patch.object(notifications, "create_desk_notification", side_effect=RuntimeError("desk failed")),
			patch.object(frappe, "sendmail", return_value=frappe._dict(name="QUEUE")),
		):
			notifications.send_to_user(self.doc, self.rule, "recipient", notification_channel="Email")
		with patch.object(notifications, "create_desk_notification") as desk, patch.object(frappe, "sendmail") as mail:
			result = notifications.retry_failed_log(self.logs[0].name)
		self.assertEqual(result["status"], "Sent")
		desk.assert_called_once()
		mail.assert_not_called()

	def test_email_failure_does_not_repeat_the_successful_desk_notification(self):
		with (
			patch.object(notifications, "create_desk_notification"),
			patch.object(frappe, "sendmail", side_effect=RuntimeError("smtp failed")),
		):
			notifications.send_to_user(self.doc, self.rule, "recipient", notification_channel="Email")
		self.assertEqual([(row.delivery_channel, row.status) for row in self.logs], [("Desk", "Sent"), ("Email", "Failed")])
		with (
			patch.object(notifications, "create_desk_notification") as desk,
			patch.object(frappe, "sendmail", return_value=frappe._dict(name="QUEUE")) as mail,
		):
			notifications.retry_failed_log(self.logs[1].name)
		desk.assert_not_called()
		mail.assert_called_once()

	def test_legacy_email_retry_reuses_email_success_but_recovers_failed_desk(self):
		self.row("A", delivery_channel=None)
		self.row("B", retry_of="A", delivery_channel="Desk")
		self.row("C", retry_of="A", delivery_channel="Email", status="Sent")
		with patch.object(notifications, "create_desk_notification") as desk, patch.object(frappe, "sendmail") as mail:
			result = notifications.retry_failed_log("A")
		desk.assert_called_once()
		mail.assert_not_called()
		self.assertEqual(result["log"], "C")
		self.assertEqual(self.logs[0].delivery_channel, "Desk")

	def test_legacy_successful_grandchild_is_recognized_without_delivery_channel(self):
		self.row("A", delivery_channel=None)
		self.row("B", retry_of="A", delivery_channel=None)
		self.row("C", retry_of="B", status="Sent", delivery_channel=None)
		with patch.object(frappe, "sendmail") as mail, patch.object(notifications, "create_desk_notification") as desk:
			result = notifications.retry_failed_log("A")
		self.assertEqual(result["log"], "C")
		mail.assert_not_called()
		desk.assert_not_called()

	def test_cyclic_retry_chain_is_rejected_before_sending(self):
		self.row("A", retry_of="B")
		self.row("B", retry_of="A")
		with patch.object(frappe, "sendmail") as mail:
			with self.assertRaises(frappe.ValidationError):
				notifications.retry_failed_log("A")
		mail.assert_not_called()

	def test_bulk_retry_reuses_the_same_successful_descendant_for_both_ancestors(self):
		self.row("A")
		self.row("B", retry_of="A")
		self.row("C", retry_of="B", status="Sent")
		def get_all(doctype, filters, fields=None, pluck=None, limit=None):
			if filters.get("status") == "Failed":
				return ["B", "A"]
			return self.get_all(doctype, filters, fields)
		with patch.object(frappe, "get_all", side_effect=get_all), patch.object(frappe, "sendmail") as mail:
			result = notifications.retry_failed_logs()
		self.assertEqual([row["log"] for row in result["retried"]], ["C", "C"])
		mail.assert_not_called()

"""Group Webhook tests use dummy credentials and mocked network/database access."""

import base64
import hashlib
import hmac
import socket
import ssl
import traceback
from unittest import TestCase
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import frappe
import requests

from draft_notifications import dingtalk_group as group
from draft_notifications import dingtalk_robot as robot
from draft_notifications.draft_notifications import draft_notification as notifications
from draft_notifications.tests import test_delivery as delivery_tests

make_rule = delivery_tests.make_rule


class TestGroupWebhook(TestCase):
	def setUp(self):
		self.webhook = "https://oapi.dingtalk.com/robot/send?access_token=test-only-token"
		self.config = frappe._dict(robot_type="Group Webhook", group_keyword="ERP")
		self.config.get_password = MagicMock(side_effect=lambda field, **kwargs: self.webhook if field == "group_webhook" else "")

	def test_url_rejects_other_hosts_ports_credentials_and_query_parameters(self):
		for url in (
			"http://oapi.dingtalk.com/robot/send?access_token=x",
			"https://example.invalid/robot/send?access_token=x",
			"https://oapi.dingtalk.com:8443/robot/send?access_token=x",
			"https://user:pass@oapi.dingtalk.com/robot/send?access_token=x",
			self.webhook + "&timestamp=123", self.webhook + "&access_token=second",
			"https://oapi.dingtalk.com/robot/send?access_token=",
		):
			with self.subTest(url=url), self.assertRaises(robot.DingTalkAPIError):
				group.validate_webhook_url(url)

	def test_signing_preserves_token_and_encodes_signature_once(self):
		signed = group.build_signed_webhook(self.webhook, "test-secret", timestamp=1234567890000)
		query = parse_qs(urlsplit(signed).query)
		expected = base64.b64encode(hmac.new(b"test-secret", b"1234567890000\ntest-secret", hashlib.sha256).digest()).decode()
		self.assertEqual(query, {"access_token": ["test-only-token"], "timestamp": ["1234567890000"], "sign": [expected]})
		self.assertEqual(group.build_signed_webhook(self.webhook, ""), self.webhook)

	def test_payload_mentions_users_and_mobiles_without_at_all(self):
		response = MagicMock(status_code=200)
		response.json.return_value = {"errcode": 0, "errmsg": "ok"}
		with patch.object(group.requests, "post", return_value=response) as post:
			self.assertEqual(group.send_group_message(self.config, ["staff-id", "mobile:13900000000"], "message", "title"), {"accepted": True})
		payload = post.call_args.kwargs["json"]
		self.assertEqual(payload["at"], {"atUserIds": ["staff-id"], "atMobiles": ["13900000000"], "isAtAll": False})
		self.assertEqual(payload["markdown"]["text"], "ERP\nmessage\n\n@staff-id @13900000000")
		self.assertFalse(post.call_args.kwargs["allow_redirects"])
		self.assertEqual(post.call_args.kwargs["timeout"], 20)

	def test_network_error_does_not_expose_webhook_in_traceback(self):
		with patch.object(group.requests, "post", side_effect=requests.RequestException(self.webhook)):
			try:
				group.send_group_message(self.config, ["staff-id"], "message", "title")
			except robot.DingTalkAPIError as exc:
				formatted = "".join(traceback.format_exception(exc))
				self.assertNotIn("test-only-token", formatted)
				frames = []
				tb = exc.__traceback__
				while tb:
					frames.append(tb.tb_frame.f_code.co_name)
					tb = tb.tb_next
				self.assertNotIn("_post_group_message", frames)
			else:
				self.fail("Expected sanitized network error")

	def test_network_failure_log_contains_allowlisted_exception_types(self):
		for error_type in (
			requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout,
			requests.exceptions.SSLError, requests.exceptions.ProxyError,
			requests.exceptions.ConnectionError, RuntimeError,
		):
			with self.subTest(kind=error_type.__name__), patch.object(group.requests, "post", side_effect=error_type(self.webhook)):
				with self.assertRaises(robot.DingTalkAPIError) as error:
					group.send_group_message(self.config, ["staff-id"], "message", "title")
				self.assertIn(error_type.__name__, str(error.exception))
				self.assertNotIn("test-only-token", str(error.exception))

	def test_tls_and_dns_root_types_survive_wrappers_without_messages(self):
		cases = [
			(requests.exceptions.SSLError(group.urllib3_errors.MaxRetryError(None, self.webhook,
				reason=ssl.SSLEOFError(self.webhook))), ["SSLError", "MaxRetryError", "SSLEOFError"]),
			(requests.exceptions.ConnectionError(socket.gaierror(8, self.webhook)), ["ConnectionError", "gaierror"]),
		]
		for error, expected in cases:
			with self.subTest(expected=expected), patch.object(group.requests, "post", side_effect=error):
				result = group._post_group_message(self.config, {})
				self.assertEqual(result, {"kind": "network", "exception_type": " -> ".join(expected)})
				self.assertNotIn("test-only-token", str(result))

	def test_unknown_class_names_and_exception_strings_are_not_recorded(self):
		custom = type("test_only_token_do_not_log", (Exception,), {})
		error = custom(self.webhook)
		error.__cause__ = error
		self.assertEqual(group.sanitized_exception_types(error), "Exception")

	def test_exception_chain_is_bounded(self):
		root = RuntimeError(self.webhook)
		current = root
		for _ in range(30):
			current.__cause__ = RuntimeError(self.webhook)
			current = current.__cause__
		self.assertEqual(group.sanitized_exception_types(root), "RuntimeError")

	def test_recipient_failure_persists_the_sanitized_type(self):
		case = delivery_tests.TestNotificationDelivery()
		case.setUp()
		try:
			case.rule.notification_channel = "DingTalk Group"
			with (
				patch.object(group, "get_group_receivers", return_value=[{"user_id": "id", "user": "ok", "enabled": 1}]),
				patch.object(robot, "get_dingtalk_config", return_value=self.config),
				patch.object(robot, "render_dingtalk_message", return_value="message"),
				patch.object(group.requests, "post", side_effect=requests.exceptions.ReadTimeout(self.webhook)),
			):
				result = notifications.send_dingtalk_private_chat_for_rule(case.doc, case.rule)
			self.assertEqual(result.status, "Failed")
			values = case.db.set_value.call_args.args[2]
			self.assertIn("ReadTimeout", values["reason"])
			self.assertEqual(values["reason"], values["dingtalk_error"])
			self.assertNotIn("test-only-token", str(values))
		finally:
			case.doCleanups()

	def test_provider_errors_and_redirects_are_not_success_or_secret_echoes(self):
		for status, result in ((301, {}), (200, {"errcode": 310000, "errmsg": self.webhook}), (200, {}), (200, {"errcode": "0"})):
			with self.subTest(status=status, result=result):
				response = MagicMock(status_code=status)
				response.json.return_value = result
				with patch.object(group.requests, "post", return_value=response), self.assertRaises(robot.DingTalkAPIError) as error:
					group.send_group_message(self.config, ["staff-id"], "message", "title")
				self.assertNotIn("test-only-token", str(error.exception))

	def test_missing_target_or_wrong_type_fails_before_network(self):
		with patch.object(group.requests, "post") as post:
			for ids in ([], ["unbound:user"], ["bad user"]):
				with self.assertRaises(robot.DingTalkAPIError):
					group.send_group_message(self.config, ids, "message", "title")
			self.config.robot_type = "Private Chat"
			with self.assertRaises(robot.DingTalkAPIError):
				group.send_group_message(self.config, ["id"], "message", "title")
		post.assert_not_called()

	def test_rule_users_resolve_to_user_ids_or_mobile_and_missing_bindings_are_visible(self):
		users = [
			frappe._dict(name="A", enabled=1, language="zh", dingtalk_user_id="staff-a", mobile_no="13900000001"),
			frappe._dict(name="B", enabled=1, language="es", dingtalk_user_id="", mobile_no="13900000002"),
			frappe._dict(name="C", enabled=0, language="en", dingtalk_user_id="", mobile_no=""),
		]
		with patch.object(frappe, "get_meta") as meta, patch.object(frappe, "get_all", side_effect=[
			users, [frappe._dict(dingtalk_user_id="staff-a")], [frappe._dict(mobile_no="13900000002")],
		]) as get_all:
			meta.return_value.has_field.return_value = True
			receivers = group.get_group_receivers("group-config", ["A", "B", "C"])
		self.assertEqual([r["user_id"] for r in receivers], ["staff-a", "mobile:13900000002", "unbound:C"])
		self.assertTrue(receivers[2]["unbound"])
		self.assertEqual(receivers[2]["enabled"], 0)
		self.assertEqual(get_all.call_args_list[0].kwargs["filters"], {"name": ["in", ["A", "B", "C"]]})

	def test_duplicate_binding_outside_rule_is_rejected_as_ambiguous(self):
		with patch.object(frappe, "get_meta") as meta, patch.object(frappe, "get_all", side_effect=[
			[frappe._dict(name="A", enabled=1, language="zh", dingtalk_user_id="shared", mobile_no="")],
			[frappe._dict(dingtalk_user_id="shared"), frappe._dict(dingtalk_user_id="shared")],
		]):
			meta.return_value.has_field.return_value = True
			self.assertTrue(group.get_group_receivers("config", ["A"])[0]["ambiguous"])

	def test_group_dispatch_uses_webhook_not_private_chat_api(self):
		response = MagicMock(status_code=200)
		response.json.return_value = {"errcode": 0}
		with (
			patch.object(robot, "get_dingtalk_config", return_value=self.config),
			patch.object(robot, "render_dingtalk_message", return_value="message"),
			patch.object(robot, "send_private_chat_message") as private,
			patch.object(group.requests, "post", return_value=response) as post,
		):
			result = robot.send_private_chat_notification(frappe._dict(doctype="Sales Order", name="TEST"),
				make_rule(notification_channel="DingTalk Group"), receivers=[{"user_id": "staff-id"}])
		self.assertEqual(result[0]["status"], "Sent")
		private.assert_not_called()
		post.assert_called_once()

	def test_group_config_does_not_require_private_app_credentials(self):
		from draft_notifications.draft_notifications.doctype.dingtalk_robot_config.dingtalk_robot_config import DingTalkRobotConfig

		DingTalkRobotConfig.validate(self.config)
		self.config.get_password.assert_called_once_with("group_webhook")

	def test_rule_channel_and_robot_type_must_match(self):
		from draft_notifications.draft_notifications.doctype.draft_notification_rule.draft_notification_rule import DraftNotificationRule

		for channel, kind, valid in (("DingTalk Group", "Group Webhook", True),
			("DingTalk Group", "Private Chat", False), ("DingTalk Private Chat", "Group Webhook", False)):
			rule = make_rule(notification_channel=channel, recipient_type="Fixed Users")
			with self.subTest(channel=channel, kind=kind), patch.object(frappe, "db", MagicMock()) as db:
				db.get_value.return_value = kind
				if valid:
					DraftNotificationRule.validate(rule)
				else:
					with self.assertRaises(frappe.ValidationError):
						DraftNotificationRule.validate(rule)

	def test_validate_webhook_is_local_only_and_does_not_send(self):
		with patch.object(robot, "get_dingtalk_config", return_value=self.config), patch.object(group.requests, "post") as post:
			self.assertTrue(robot.validate_dingtalk_config("group")["success"])
		post.assert_not_called()

	def test_private_chat_api_rejects_group_config(self):
		with patch.object(robot, "get_dingtalk_config", return_value=self.config), patch.object(robot, "get_dingtalk_access_token") as token:
			with self.assertRaises(robot.DingTalkAPIError):
				robot.send_private_chat_message("group", ["staff-id"], "message")
		token.assert_not_called()


class TestGroupLogRouting(TestCase):
	def test_group_channel_can_combine_with_email_and_desk(self):
		self.assertTrue(notifications.channel_uses_email("Email + DingTalk Group"))
		self.assertTrue(notifications.channel_uses_desk("Email + DingTalk Group"))
		self.assertTrue(notifications.channel_uses_dingtalk("DingTalk Group"))
		self.assertFalse(notifications.channel_uses_dingtalk_private_chat("DingTalk Group"))

	def test_group_dedup_cannot_match_private_chat_success(self):
		db = MagicMock()
		db.exists.return_value = False
		with patch.object(frappe, "db", db):
			self.assertFalse(notifications.already_sent_dingtalk(frappe._dict(doctype="Sales Order", name="TEST"), make_rule(), "staff-id", "DingTalk Group"))
		self.assertEqual(db.exists.call_count, 1)
		self.assertEqual(db.exists.call_args.args[1]["notification_channel"], "DingTalk Group")

	def test_group_retry_preserves_original_transport_and_config(self):
		case = delivery_tests.TestNotificationRetry()
		case.setUp()
		try:
			case.log.notification_channel = "DingTalk Group"
			case.log.delivery_channel = "DingTalk Group"
			case.log.dingtalk_user_id = "staff-id"
			case.log.dingtalk_config = "original-group"
			with patch.object(notifications, "send_dingtalk_private_chat_for_rule", return_value=frappe._dict(name="NEW", status="Sent")) as sender:
				notifications.retry_failed_log("OLD-LOG")
			self.assertEqual(sender.call_args.args[1].notification_channel, "DingTalk Group")
			self.assertEqual(sender.call_args.args[1].dingtalk_config, "original-group")
			self.assertEqual(sender.call_args.kwargs["target_id"], "staff-id")
		finally:
			case.doCleanups()

	def test_group_rule_filters_permissions_and_logs_the_actual_group_channel(self):
		case = delivery_tests.TestNotificationDelivery()
		case.setUp()
		try:
			case.rule.notification_channel = "DingTalk Group"
			receivers = [
				{"user_id": "ok-id", "user": "ok", "enabled": 1},
				{"user_id": "disabled-id", "user": "disabled", "enabled": 0},
				{"user_id": "denied-id", "user": "denied", "enabled": 1},
				{"user_id": "unbound:unbound-user", "user": "unbound-user", "enabled": 1, "unbound": True},
			]
			def send(doc, rule, *, receivers, on_result, summary_receivers):
				on_result([{"user_id": row["user_id"], "status": "Sent", "reason": None} for row in receivers])
			with patch.object(group, "get_group_receivers", return_value=receivers) as resolve, patch.object(robot, "send_private_chat_notification", side_effect=send) as sender:
				notifications.send_dingtalk_private_chat_for_rule(case.doc, case.rule)
			self.assertEqual(resolve.call_args.kwargs["receiver_users"], ["dynamic-user"])
			self.assertEqual([row["user_id"] for row in sender.call_args.kwargs["receivers"]], ["ok-id"])
			self.assertEqual(sender.call_args.kwargs["summary_receivers"], receivers)
			self.assertEqual([row.status for row in case.logs], ["Sent", "Skipped", "Skipped", "Skipped"])
			self.assertTrue(all(row.notification_channel == "DingTalk Group" for row in case.logs))
		finally:
			case.doCleanups()

	def test_group_test_endpoint_requires_config_write_permission(self):
		with patch.object(frappe, "has_permission", return_value=False), patch.object(group.requests, "post") as post:
			with self.assertRaises(frappe.PermissionError):
				group.send_test_group("group", "erp-user")
		post.assert_not_called()

	def test_targeting_and_deduplication_keep_full_group_summary_audience(self):
		for targeted in (False, True):
			with self.subTest(targeted=targeted):
				case = delivery_tests.TestNotificationDelivery()
				case.setUp()
				try:
					case.rule.notification_channel = "DingTalk Group"
					rows = [{"user_id": name, "user": name, "enabled": 1} for name in ("A", "B")]
					def send(doc, rule, *, receivers, on_result, summary_receivers):
						on_result([{"user_id": row["user_id"], "status": "Sent", "reason": None} for row in receivers])
					with (
						patch.object(group, "get_group_receivers", return_value=rows),
						patch.object(notifications, "already_sent_dingtalk", side_effect=lambda doc, rule, user_id, channel: user_id == "A" and not targeted),
						patch.object(robot, "send_private_chat_notification", side_effect=send) as sender,
					):
						notifications.send_dingtalk_private_chat_for_rule(case.doc, case.rule,
							target_id="B" if targeted else None, retry_of="ROOT" if targeted else None)
					self.assertEqual(sender.call_args.kwargs["receivers"], rows[1:])
					self.assertEqual(sender.call_args.kwargs["summary_receivers"], rows)
				finally:
					case.doCleanups()

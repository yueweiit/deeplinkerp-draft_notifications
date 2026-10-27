"""Group Webhook transport. Credentials must never appear in exceptions or results."""

import base64
import hashlib
import hmac
import socket
import ssl
import time
from collections import Counter
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import frappe
import requests
from frappe import _
from urllib3 import exceptions as urllib3_errors

from draft_notifications.dingtalk_robot import DingTalkAPIError, get_language_key, unique_receivers


GROUP_CHANNEL = "DingTalk Group"


def validate_webhook_url(value):
	try:
		parts = urlsplit(value or "")
		query = parse_qs(parts.query, keep_blank_values=True)
		valid = (
			parts.scheme == "https" and parts.hostname == "oapi.dingtalk.com"
			and parts.port in (None, 443) and parts.path == "/robot/send"
			and not parts.username and not parts.password and not parts.fragment
			and set(query) == {"access_token"} and len(query["access_token"]) == 1
			and bool(query["access_token"][0].strip())
		)
	except (ValueError, TypeError):
		valid = False
	if not valid:
		raise DingTalkAPIError(_("Enter a valid DingTalk group Webhook URL without signing parameters."))
	return value


def build_signed_webhook(webhook, secret, timestamp=None):
	validate_webhook_url(webhook)
	if not secret:
		return webhook
	timestamp = str(timestamp if timestamp is not None else int(time.time() * 1000))
	digest = hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
	parts = urlsplit(webhook)
	query = parse_qs(parts.query)
	query.update(timestamp=[timestamp], sign=[base64.b64encode(digest).decode()])
	return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def get_group_receivers(config_name, receiver_users=None):
	"""Group mentions come only from the rule's ERP users, never private-chat defaults."""
	if not receiver_users:
		return []
	fields = ["name", "enabled", "language", "mobile_no"]
	if frappe.get_meta("User").has_field("dingtalk_user_id"):
		fields.append("dingtalk_user_id")
	users = frappe.get_all("User", filters={"name": ["in", list(receiver_users)]}, fields=fields)
	receivers = []
	for user in users:
		user_id = (user.get("dingtalk_user_id") or "").strip()
		mobile = (user.mobile_no or "").strip()
		# Missing bindings remain visible as a skipped recipient in the caller's log.
		receivers.append({
			"user_id": user_id or (f"mobile:{mobile}" if mobile else f"unbound:{user.name}"),
			"user": user.name, "enabled": user.enabled, "language": get_language_key(user.language),
			"unbound": not user_id and not mobile,
		})
	# Check bindings across all ERP users, including users outside the selected rule.
	bindings = Counter()
	ids = [row["user_id"] for row in receivers if not row["user_id"].startswith(("mobile:", "unbound:"))]
	mobiles = [row["user_id"].removeprefix("mobile:") for row in receivers if row["user_id"].startswith("mobile:")]
	if ids:
		for row in frappe.get_all("User", filters={"dingtalk_user_id": ["in", ids]}, fields=["dingtalk_user_id"]):
			bindings[row.dingtalk_user_id] += 1
	if mobiles:
		for row in frappe.get_all("User", filters={"mobile_no": ["in", mobiles]}, fields=["mobile_no"]):
			bindings[f"mobile:{row.mobile_no}"] += 1
	for receiver in receivers:
		receiver["ambiguous"] = bindings[receiver["user_id"]] > 1
	return unique_receivers(receivers)


def send_group_message(config, receiver_ids, content, title):
	if (config.get("robot_type") or "Private Chat") != "Group Webhook":
		raise DingTalkAPIError(_("Select a group Webhook robot configuration for this channel."))
	ids = list(dict.fromkeys(receiver_ids))
	if not ids or any(not value or value.startswith("unbound:") or any(c.isspace() for c in value) for value in ids):
		raise DingTalkAPIError(_("Group mentions require a DingTalk UserId or mobile number."))
	user_ids = [value for value in ids if not value.startswith("mobile:")]
	mobiles = [value.removeprefix("mobile:") for value in ids if value.startswith("mobile:")]
	keyword = (config.get("group_keyword") or "").strip()
	text = f"{keyword}\n{content}" if keyword else content
	mentions = " ".join(f"@{value.removeprefix('mobile:')}" for value in ids)
	payload = {
		"msgtype": "markdown", "markdown": {"title": title, "text": f"{text}\n\n{mentions}"},
		"at": {"atUserIds": user_ids, "atMobiles": mobiles, "isAtAll": False},
	}
	result = _post_group_message(config, payload)
	if result["kind"] == "configuration":
		raise DingTalkAPIError(_("Check the group Webhook URL and signing secret configuration."))
	if result["kind"] == "network":
		raise DingTalkAPIError(_("DingTalk group request failed ({0}); check connectivity and retry status.").format(result["exception_type"]))
	if result["kind"] == "http":
		raise DingTalkAPIError(_("DingTalk group request failed with HTTP {0}.").format(result["status"]))
	if result["kind"] == "invalid":
		raise DingTalkAPIError(_("DingTalk returned an invalid group response."))
	if result["code"] != 0:
		raise DingTalkAPIError(_("DingTalk group rejected the message (code {0}); check robot security settings.").format(result["code"]))
	return {"accepted": True}


def sanitized_exception_types(error):
	"""Return only allowlisted class labels, never exception text or request objects."""
	labels = {
		requests.exceptions.ConnectTimeout: "ConnectTimeout",
		requests.exceptions.ReadTimeout: "ReadTimeout",
		requests.exceptions.Timeout: "Timeout",
		requests.exceptions.SSLError: "SSLError",
		requests.exceptions.ProxyError: "ProxyError",
		requests.exceptions.ConnectionError: "ConnectionError",
		requests.exceptions.HTTPError: "HTTPError",
		requests.exceptions.RequestException: "RequestException",
		ssl.SSLEOFError: "SSLEOFError", ssl.SSLCertVerificationError: "SSLCertVerificationError",
		ssl.SSLError: "SSLError", socket.gaierror: "gaierror",
		TimeoutError: "TimeoutError", ConnectionResetError: "ConnectionResetError",
		ConnectionRefusedError: "ConnectionRefusedError", OSError: "OSError",
		UnicodeEncodeError: "UnicodeEncodeError", TypeError: "TypeError", ValueError: "ValueError",
		RuntimeError: "RuntimeError",
	}
	for name in ("MaxRetryError", "NameResolutionError", "NewConnectionError", "ConnectTimeoutError",
		"ReadTimeoutError", "ProtocolError", "SSLError", "ProxyError"):
		kind = getattr(urllib3_errors, name, None)
		if isinstance(kind, type):
			labels[kind] = name
	pending = [error]
	seen = set()
	result = []
	while pending and len(seen) < 12:
		current = pending.pop(0)
		if not isinstance(current, BaseException) or id(current) in seen:
			continue
		seen.add(id(current))
		label = next((labels[kind] for kind in type(current).__mro__ if kind in labels), "Exception")
		if label not in result:
			result.append(label)
		# Follow causes, not their string representations (which include the URL).
		pending.extend([current.__cause__, current.__context__, getattr(current, "reason", None),
			getattr(current, "original_error", None)])
		pending.extend(item for item in current.args if isinstance(item, BaseException))
	return " -> ".join(result)


def _post_group_message(config, payload):
	# The credential-bearing request frame returns sanitized data instead of escaping
	# via exceptions, so even tracebacks with local variables cannot expose the URL.
	try:
		url = build_signed_webhook(config.get_password("group_webhook"), config.get_password("group_secret", raise_exception=False))
	except Exception:
		return {"kind": "configuration"}
	try:
		response = requests.post(url, json=payload, timeout=20, allow_redirects=False)
	except Exception as exc:
		return {"kind": "network", "exception_type": sanitized_exception_types(exc)}
	if response.status_code != 200:
		return {"kind": "http", "status": response.status_code}
	try:
		result = response.json()
	except ValueError:
		return {"kind": "invalid"}
	if not isinstance(result, dict) or type(result.get("errcode")) is not int:
		return {"kind": "invalid"}
	# Never return raw provider text, which can echo credentials or request values.
	return {"kind": "response", "code": result["errcode"]}

@frappe.whitelist(methods=["POST"])
def send_test_group(config_name, user, message=None):
	if not frappe.has_permission("DingTalk Robot Config", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	from draft_notifications.dingtalk_robot import get_dingtalk_config

	config = get_dingtalk_config(config_name)
	if not config:
		frappe.throw(_("No enabled DingTalk Robot Config found: {0}.").format(config_name))
	config.check_permission("write")
	receivers = get_group_receivers(config_name, [user])
	if not receivers or any(not row.get("enabled") or row.get("ambiguous") or row.get("unbound") for row in receivers):
		frappe.throw(_("Select an enabled ERP user with a unique DingTalk UserId or mobile number."))
	try:
		return send_group_message(config, [row["user_id"] for row in receivers],
			message or _("This is a test message from Draft Notifications."), _("Draft Notifications Test"))
	except DingTalkAPIError as exc:
		frappe.throw(str(exc))

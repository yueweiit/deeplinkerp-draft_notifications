import json

import frappe
import requests
from requests import RequestException
from frappe import _
from frappe.utils import get_url_to_form


DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
DINGTALK_PRIVATE_CHAT_SEND_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
TOKEN_CACHE_KEY = "draft_notifications_dingtalk_access_token"
TOKEN_CACHE_TTL = 7000


class DingTalkAPIError(Exception):
	pass


def clear_dingtalk_token_cache(config_name=None):
	if config_name:
		frappe.cache().delete_value(f"{TOKEN_CACHE_KEY}::{config_name}")
	else:
		frappe.cache().delete_keys(TOKEN_CACHE_KEY)


def post_dingtalk_json(url, payload, headers, timeout, operation):
	try:
		response = requests.post(url, json=payload, headers=headers, timeout=timeout)
	except RequestException as exc:
		raise DingTalkAPIError(_("DingTalk {0} request failed: {1}").format(operation, exc)) from exc

	data = get_response_json(response)
	if response.status_code >= 400:
		if response.status_code in (401, 403):
			clear_dingtalk_token_cache()
		raise_dingtalk_error(operation, response=response, data=data)

	return data


def get_response_json(response):
	try:
		return response.json()
	except ValueError:
		return {}


def is_dingtalk_error(data, code_field="errorCode", message_field="errorMessage"):
	code = data.get(code_field)
	message = data.get(message_field)
	return bool(message or (code not in (None, "", 0, "0")))


def raise_dingtalk_error(operation, response=None, data=None):
	raise DingTalkAPIError(format_dingtalk_error(operation, response=response, data=data))


def format_dingtalk_error(operation, response=None, data=None, response_text=None, response_headers=None):
	data = data or {}
	status_code = response.status_code if response else None
	if response_headers is None:
		response_headers = response.headers if response else {}

	code = data.get("errorCode") or data.get("code") or data.get("errcode") or status_code or ""
	message = data.get("errorMessage") or data.get("message") or data.get("errmsg") or ""
	request_id = get_dingtalk_request_id(data=data, headers=response_headers)

	if not message:
		if response_text is None and response is not None:
			response_text = response.text
		message = (response_text or "").strip()[:500]

	details = []
	if status_code:
		details.append(_("HTTP {0}").format(status_code))
	if code:
		details.append(_("code {0}").format(code))
	if request_id:
		details.append(_("request id {0}").format(request_id))

	if status_code == 403:
		message = "{0} {1}".format(
			message,
			_(
				"Check whether the DingTalk app has the qyapi_robot_sendmsg permission, "
				"the RobotCode belongs to this app, and the robot is enabled."
			),
		).strip()

	return _("DingTalk {0} failed ({1}): {2}").format(
		operation,
		", ".join(details) or _("unknown error"),
		message or _("No error message returned by DingTalk."),
	)


def get_dingtalk_request_id(data=None, headers=None):
	data = data or {}
	headers = headers or {}
	request_id = data.get("requestId") or data.get("request_id")
	if request_id:
		return request_id

	for header in ("x-acs-request-id", "x-acs-trace-id", "x-request-id"):
		if headers.get(header):
			return headers.get(header)

	return None


def get_dingtalk_config(config_name=None):
	filters = {"enabled": 1}
	if config_name:
		filters["name"] = config_name

	configs = frappe.get_all("DingTalk Robot Config", filters=filters, fields=["name"], limit=1)
	if not configs:
		return None

	return frappe.get_doc("DingTalk Robot Config", configs[0].name)


def get_dingtalk_access_token(config_name=None):
	cache_key = f"{TOKEN_CACHE_KEY}::{config_name or 'default'}"
	cached_token = frappe.cache().get_value(cache_key)
	if cached_token:
		return cached_token

	config = get_dingtalk_config(config_name)
	if not config:
		raise DingTalkAPIError(_("No enabled DingTalk Robot Config found."))

	data = post_dingtalk_json(
		DINGTALK_TOKEN_URL,
		payload={
			"appKey": config.app_key,
			"appSecret": config.get_password("app_secret"),
		},
		headers={"Content-Type": "application/json"},
		timeout=20,
		operation="access token",
	)

	if is_dingtalk_error(data, code_field="code", message_field="message"):
		raise_dingtalk_error("access token", data=data)

	access_token = data.get("accessToken")
	if not access_token:
		raise DingTalkAPIError(_("DingTalk did not return accessToken."))

	expires_in = int(data.get("expireIn") or 7200)
	frappe.cache().set_value(cache_key, access_token, expires_in_sec=min(max(expires_in - 200, 60), TOKEN_CACHE_TTL))
	return access_token


def send_private_chat_message(config_name, receiver_user_ids, content, title=None, _config=None):
	config = _config or get_dingtalk_config(config_name)
	if not config:
		raise ValueError(_("No enabled DingTalk Robot Config found: {0}").format(config_name))

	receiver_user_ids = unique([user_id for user_id in receiver_user_ids if user_id])
	if not receiver_user_ids:
		raise ValueError(_("DingTalk receiver list cannot be empty."))

	if len(receiver_user_ids) > 20:
		raise ValueError(_("DingTalk receiver count exceeds limit: {0}/{1}").format(len(receiver_user_ids), 20))

	request_body = build_private_chat_payload(
		robot_code=config.robot_code,
		receiver_user_ids=receiver_user_ids,
		content=content,
		title=title or _("Draft Notification"),
	)

	result = post_dingtalk_json(
		DINGTALK_PRIVATE_CHAT_SEND_URL,
		payload=request_body,
		headers={
			"Content-Type": "application/json",
			"x-acs-dingtalk-access-token": get_dingtalk_access_token(config_name),
		},
		timeout=30,
		operation="private chat message",
	)

	if is_dingtalk_error(result):
		raise_dingtalk_error("private chat message", data=result)

	return result or {"sent": True}


def build_private_chat_payload(robot_code, receiver_user_ids, content, title=None):
	return {
		"robotCode": robot_code,
		"userIds": receiver_user_ids,
		"msgKey": "sampleMarkdown",
		"msgParam": json.dumps(
			{
				"title": title or _("Draft Notification"),
				"text": content,
			},
			ensure_ascii=False,
		),
	}


def send_ding_message(config_name, receiver_user_ids, content, remind_type=None):
	return send_private_chat_message(config_name, receiver_user_ids, content)


def send_private_chat_notification(doc, rule, receiver_users=None, content=None):
	config_name = rule.get("dingtalk_config")
	if not config_name:
		raise ValueError(_("DingTalk Robot Config is required for rule {0}.").format(rule.name))

	config = get_dingtalk_config(config_name)
	if not config:
		raise DingTalkAPIError(_("No enabled DingTalk Robot Config found: {0}").format(config_name))

	receivers = get_dingtalk_receivers(config_name, receiver_users=receiver_users, _config=config)
	if not receivers:
		frappe.log_error(
			title=f"DingTalk notification has no receivers: {rule.name}",
			message=f"{doc.doctype} {doc.name} has no DingTalk receiver user ids.",
		)
		return None

	results = []
	for language_key, receiver_ids in group_receivers_by_language(receivers).items():
		results.append(
			send_private_chat_message(
				config_name=config_name,
				receiver_user_ids=receiver_ids,
				content=content or render_dingtalk_message(doc, rule, language_key),
				title=f"{doc.doctype} {doc.name}",
				_config=config,
			)
		)

	return results


def get_dingtalk_receivers(config_name, receiver_users=None, _config=None):
	receivers = []
	config = _config or get_dingtalk_config(config_name)

	if config:
		for row in config.recipients or []:
			if row.user_id:
				receivers.append({"user_id": row.user_id, "language": None})

	if receiver_users and user_has_dingtalk_user_id_field():
		users_data = frappe.get_all(
			"User",
			filters={"name": ["in", list(receiver_users)], "dingtalk_user_id": ["is", "set"]},
			fields=["dingtalk_user_id", "language"],
		)
		for user_data in users_data:
			receivers.append({"user_id": user_data.dingtalk_user_id, "language": get_language_key(user_data.language)})

	return unique_receivers(receivers)


def get_dingtalk_receiver_ids(config_name, receiver_users=None):
	return [receiver["user_id"] for receiver in get_dingtalk_receivers(config_name, receiver_users=receiver_users)]


def group_receivers_by_language(receivers):
	grouped = {}
	for receiver in receivers:
		key = receiver.get("language")
		grouped.setdefault(key, []).append(receiver["user_id"])
	return grouped


def render_dingtalk_message(doc, rule, language_key=None):
	template = get_localized_dingtalk_template(rule, language_key)
	if not template:
		template = "【{{ doc.doctype }} Notification】\nDocument: {{ doc.name }}\nLink: {{ doc_url }}"

	return frappe.render_template(
		template,
		{
			"doc": doc,
			"doc_url": get_url_to_form(doc.doctype, doc.name),
			"doc_type": doc.doctype,
			"doc_name": doc.name,
		},
	)


def get_localized_dingtalk_template(rule, language_key=None):
	if language_key:
		localized_template = rule.get(f"dingtalk_message_{language_key}")
		if localized_template:
			return localized_template

	return rule.get("dingtalk_message_template")


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


def get_remind_type_value(remind_type):
	if not remind_type:
		return 1
	if isinstance(remind_type, int):
		return remind_type
	return int(str(remind_type).split("-", 1)[0])


def user_has_dingtalk_user_id_field():
	return frappe.get_meta("User").has_field("dingtalk_user_id")


def unique(values):
	seen = set()
	result = []
	for value in values:
		if value in seen:
			continue
		seen.add(value)
		result.append(value)
	return result


def unique_receivers(receivers):
	seen = set()
	result = []
	for receiver in receivers:
		user_id = receiver.get("user_id")
		if not user_id or user_id in seen:
			continue
		seen.add(user_id)
		result.append(receiver)
	return result


def validate_dingtalk_config(config_name):
	try:
		get_dingtalk_access_token(config_name)
		return {"success": True, "message": _("Connection succeeded.")}
	except Exception as exc:
		return {"success": False, "message": str(exc)}


@frappe.whitelist()
def test_dingtalk_config(config_name):
	if not frappe.has_permission("DingTalk Robot Config", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	return validate_dingtalk_config(config_name)


@frappe.whitelist()
def send_test_private_chat(config_name, user_id, message=None):
	if not frappe.has_permission("DingTalk Robot Config", "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	try:
		return send_private_chat_message(
			config_name=config_name,
			receiver_user_ids=[user_id],
			content=message or _("This is a test message from Draft Notifications."),
			title=_("Draft Notifications Test"),
		)
	except (DingTalkAPIError, ValueError) as exc:
		frappe.throw(str(exc))


@frappe.whitelist()
def send_test_ding(config_name, user_id, message=None):
	return send_test_private_chat(config_name, user_id, message)

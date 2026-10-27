# Draft Notifications

Configurable draft document email notifications for Frappe.

独立仓库为 [yueweiit/deeplinkerp-draft_notifications](https://github.com/yueweiit/deeplinkerp-draft_notifications)，开发主线和 PR 目标均为 `main`。Frappe App 名保持 `draft_notifications`。

## What It Does

Draft Notifications sends email after a configured document event. Existing rules default to
`After Insert`, which keeps the original behavior for newly created draft documents.

Rules are configured with the `Draft Notification Rule` DocType. When a matching document event occurs,
the app creates a Desk notification, an `Email Queue` record, and a `Draft Notification Log` record.
The app requests immediate sending for the Email Queue record, and the scheduler syncs queued log rows
to `Sent` or `Failed`.

## Rule Setup

Create a `Draft Notification Rule` with:

- `Document Type`: target DocType to watch.
- `Trigger Event`: when to send. Supported values are `After Insert`, `On Update`, `On Submit`, and `On Cancel`.
- `Recipient Type`: where recipients come from.
- `Subject`: optional Jinja template.
- `Message`: optional Jinja template.
- `Respect Document Read Permission`: skip users who cannot read the document.
- `Include Owner`: also notify the document owner.
- `Do Not Send Twice For Same Document And User`: skip when a queued or sent log already exists.

The Desk notification appears in the bell notification dropdown as a Frappe `Notification Log` with
type `Alert`. Alert notifications do not trigger Frappe's separate notification email, so users only
receive the email created by this app.

Supported recipient types:

- `Fixed Users`: users from the child table.
- `Users With Role`: all users assigned to the selected role.
- `User Field`: a field on the target document that stores a User id.
- `Owner`: the target document owner.
- `Custom Method`: a Python dotted path that receives the document and returns a user or list of users.

## Templates

`Subject` and `Message` use Frappe/Jinja templates.

Available variables:

- `doc`: the draft document.
- `doc_url`: the Desk form URL for the draft document.

Example subject:

```jinja
New draft {{ doc.doctype }} {{ doc.name }}
```

Example message:

```jinja
<p>A new draft {{ doc.doctype }} has been created: <a href="{{ doc_url }}">{{ doc.name }}</a></p>
```

## Custom Methods

Custom methods must point to installed app modules and cannot point to private modules or methods.

By default, allowed prefixes are derived from installed app names, excluding `frappe`, for example:

```text
erpnext.
draft_notifications.
```

You can override the allowed prefixes in `site_config.json`:

```json
{
  "draft_notification_allowed_method_prefixes": ["crm_integration.", "draft_notifications."]
}
```

Example method:

```python
def get_draft_notification_users(doc):
	return [doc.owner]
```

## Log Statuses

- `Queued`: an Email Queue record exists. It may already be sent; the scheduler updates the log status.
- `Sent`: the Email Queue record was sent successfully.
- `Skipped`: the recipient was skipped, usually because of missing email, permissions, or deduplication.
- `Failed`: email queueing or sending failed.

## Retry Failed Logs

Failed notification logs are not retried automatically. Frappe Email Queue still uses its native
transport retry policy before reaching a terminal error. Manual notification retries require document
write permission on the log; the default System Manager role has this permission. Disabled rules
cannot be retried.

You can retry one failed log:

```python
frappe.call(
	"draft_notifications.draft_notifications.draft_notification.retry_failed_log",
	log_name="DRAFT-NOTIFY-2026-00001",
)
```

You can retry a batch:

```python
frappe.call(
	"draft_notifications.draft_notifications.draft_notification.retry_failed_logs",
	limit=100,
)
```

Retries retain the original channel and notification period. Each retry resolves and locks the root
log, then checks the whole historical retry tree per delivery channel and recipient. All new attempts
point `retry_of` directly to that root; old multi-level chains remain readable. A successful or queued
grandchild prevents resending from either its parent or its root, including in bulk retry. Cyclic retry
chains are rejected. Email retries create a new Email Queue; Desk-only retries do not send email.

Email and Desk now have separate outcome logs with `delivery_channel` identifying the actual transport;
`notification_channel` retains the configured source channel. A Desk insertion failure is recorded as
Failed while email queueing continues, and retrying that Desk log never sends another email. Email-only
retry logs likewise do not recreate an already successful Desk alert. Legacy logs without
`delivery_channel` retain their original combined-channel interpretation. DingTalk retries target the
failed recipient and original robot configuration;
legacy aggregate logs re-resolve the rule's current recipients and skip known successful recipients.
Old aggregate logs cannot reconstruct per-recipient outcomes that were never recorded.

## DingTalk Delivery

The supported DingTalk channel sends enterprise application robot **private chat** messages, not a
custom group robot Webhook. Legacy DING channel names use the same private chat implementation.
Configure AppKey, AppSecret and RobotCode in DingTalk Robot Config, then select it on the rule.
Recipients combine the config's fixed UserIds with rule users' `User.dingtalk_user_id` values.

Email, Desk and mapped DingTalk recipients share enabled-user and document read-permission checks.
With Respect Document Read Permission enabled, fixed DingTalk UserIds also require an unambiguous
ERP user binding and read permission. Without a binding they are skipped. Explicitly disabling that
rule option permits unbound fixed IDs; disabled mapped users and ambiguous bindings are still skipped.

DingTalk messages are grouped by language and split into batches of at most 20. Recipient logs are
reserved before sending and record Sent/Failed separately, so a later batch failure preserves earlier
successes. Deduplication uses rule, document, channel, robot, recipient and (for date rules) period;
email success does not suppress DingTalk delivery. Manual retries skip known queued/successful targets
also when ordinary rule deduplication is off. Rule-row locking serializes DingTalk eligibility and sends.
The SDK response fields `invalidStaffIdList`, `flowControlledStaffIdList` and `filteredStaffIdList` are
handled as failures (see the [official SDK response model](https://github.com/alibabacloud-go/dingtalk/blob/master/robot_1_0/client.go)).
Sent means the sending API accepted that recipient; it is not a read receipt. Network timeouts or process
failure after remote acceptance can still leave an uncertain outcome; exactly-once external delivery
is not guaranteed. No automatic DingTalk retry is added.

## DingTalk Group Webhook

Select **Group Webhook** as the robot type in DingTalk Robot Config. The full Webhook URL and optional
signing secret use Password fields. Private-chat AppKey/AppSecret/RobotCode are not required for this
type. When the bot uses keyword security, fill Group Security Keyword; it is prepended to the message.
The URL must be an HTTPS `oapi.dingtalk.com/robot/send` URL containing only its access token; signing
parameters are generated for each request. Redirects are disabled and provider errors are sanitized. Network failures retain
only allowlisted exception type labels, including wrapped TLS/DNS causes (for example
`SSLError -> MaxRetryError -> SSLEOFError`), in the existing reason/DingTalk error fields.
Exception messages, credential-bearing URLs and raw request/response objects are not retained.
Unknown exception class names fall back to `Exception`; cause traversal is cycle-safe and bounded.
This applies to new failures only; old generic failure logs cannot be reconstructed.
Protocol references: [group robot access](https://open.dingtalk.com/document/robots/custom-robot-access)
and [security settings](https://open.dingtalk.com/document/robots/customize-robot-security-settings).

Choose DingTalk Group, Email + DingTalk Group or Desk + DingTalk Group on a notification rule and select
the matching group configuration. Group mentions use **only the rule's selected ERP users**; private-chat
Default Recipients are not added. Each selected user needs a unique `User.dingtalk_user_id`, or falls
back to `User.mobile_no`. Disabled, unbound, ambiguously bound and (when read checks are enabled)
unauthorized users are skipped with a log. The selected people must be members of the target group;
Webhook acceptance cannot verify that a mention resolved or was read. All group members can see the
message even though only selected users are mentioned; this transport is suitable only for groups
already authorized to receive the business information. `isAtAll` is always false.

Group sending reuses language batching, per-recipient outcomes, transport/config-aware deduplication
and root-locked retries. A retried group log cannot switch to the private-chat API. HTTP or provider
failure marks the attempted batch Failed; success means the Webhook accepted the group message.
Webhook delivery still has the external timeout/uncertain-outcome limits described above.

For a **Sales Order / Document Event / On Submit** group rule, submitted CRM orders (with
`custom_crm_order_no`) retain their configured message under a localized **New sales order** heading.
A separate historical financial-review section is appended automatically. It lists only ERP and CRM
order numbers, oldest first, up to 10 rows, with the total, any remaining count and a filtered list link.
There is no extra rule field or scheduled reminder. Email, Desk and private-chat messages are unchanged.

History means earlier-created, submitted CRM orders in the same company whose stable process state is
`Pending Deposit Confirmation` (the CRM finance-release pending state). The current order, cancelled,
closed, stopped and on-hold orders are excluded; `Deposit Confirmation Processing`, rejected and released
states are excluded as well. A pending financial review is distinct from ERP document submission.
The query uses the intersection of native Sales Order list permissions for the complete rule audience
before targeting, permission skips, deduplication or language/20-person batching. Existing group logs
for this rule, order and robot retain previous recipients when the rule changes or only a failed recipient
is retried. Required field read and masking permissions are checked per recipient (including the
filter/count fields), independently of the worker's identity and the rule's ordinary read-check option.
If any recipient lacks access to required original values, or a previous recipient is disabled, missing
or cannot be identified from a legacy aggregate log, the historical section is omitted. Native
Administrator masking behavior is retained; no global user/session switching is needed.
An empty permission intersection also omits the section; the filtered link applies the viewer's own
permissions when opened. Group-wide visibility still requires an authorized group as explained above.

Chinese, English and Spanish headings follow the existing recipient language batches. Counts and rows
come from one bounded query. History query or rendering failures preserve the new-order message with a
localized warning and a diagnostic log; a permission denial omits the section. Implementation and isolated
regressions are in `draft_notifications/pending_sales_orders.py` and
`draft_notifications/tests/test_pending_sales_orders.py`. This digest adds no database fields or migration.
Restart notification workers and refresh translation caches through the normal release process when
upgrading an already running installation; existing workers may retain loaded Python modules.

Validate Webhook checks URL format locally and does **not** contact DingTalk or send a message.
Send Test Group Message requires config write permission and an explicitly selected ERP user; it posts
a real group message when invoked. No test message or real credential is included in the automated tests.
After an authorized site migration, save the Webhook and match the bot's actual signing/keyword settings
before using that button. The group Webhook and security settings have not been configured or tested live.

## Upgrade and Verification

This change adds read-only log fields `dingtalk_user_id`, `dingtalk_config`, `retry_of`, and
`delivery_channel`, plus default System Manager write permission for manual retry. Run the normal site migration during an authorized
release. Notification sending and manual retry fail closed until the required log columns exist. Existing
Group support additionally adds robot type, encrypted Webhook/signing fields and an optional keyword,
and extends the rule/log channel options. Existing private-chat configurations retain their default type.
Site-specific permission overrides must be reviewed separately; no broad permission bypass is used.

Isolated regressions are in `draft_notifications/tests/test_delivery.py`, `test_dingtalk_group.py`
and `test_pending_sales_orders.py` in the same directory. They mock database access
and outbound sends; passing them does not verify real SMTP/DingTalk credentials or delivery.

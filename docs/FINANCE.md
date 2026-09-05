# Money

The finance module answers questions about your own bank account, and it does
it without any of that account reaching a language model.

That last part is the whole design, not a setting. Every other skill hands its
result back to the model, which reads it and phrases a reply — which means the
result travels to OpenAI or Anthropic with the next request. A finance skill
built that way puts your balance, your merchant names and your salary in a
prompt on every question. So these tools do not return anything. They raise
`FinalAnswer`, which ends the turn with a sentence this module built itself,
from numbers, on your machine.

The five rules the module is built to, all of them enforced by tests in
`services/core/tests/test_finance_acceptance.py`:

1. **No transaction data leaves the machine.** Nothing on any runtime path puts
   a balance, an amount or a merchant into a prompt.
2. **Credentials are isolated.** The bank token lives in `finance.env` at 0600,
   read by this module and nothing else. It is not a setting, so it is not in
   `config.toml`, not sent to a client, not in a settings export, and not in
   the settings panel.
3. **It reports, it does not decide.** No answer resolves to yes or no. You get
   the figures; the judgement is yours.
4. **Read-only by default.** One write exists — the payday transfer — and it is
   off, behind a second dry-run switch that is also on, behind a hard cap.
5. **Local persistence only.** SQLite, in your data directory, at 0600.

## What it does

**Affordability.** *"How much have I got?"*, *"Can I afford £200?"* — the same
answer either way:

> £340 available. 11 days until payday. That spend leaves £140, which is £12.70
> a day.

Available is the balance minus everything still due to leave before payday, so
the phone bill on the 20th is already taken off. Payday moves off weekends and
bank holidays the way your employer's does.

**Large-spend alerts.** A debit over the threshold and N.O.V.A. says so:
merchant, amount, what is left. Nothing else — a running commentary on your
spending is not an alert, it is nagging.

**A cooling-off queue.** *"I want to buy some headphones, £200."* It notes it
and asks you again 48 hours later, telling you what buying it now would leave.
Then *"how much have I saved by not buying things?"* — which is the number
worth having.

**The payday split.** When salary lands, move a fixed amount into savings.
Off by default; see [Transfers](#transfers-the-only-write) before turning it on.

## Getting started without a bank

Start here. It needs no credentials and everything above works:

1. Download a CSV statement from your bank.
2. In the settings panel, **Money**: turn it on, leave the provider on `csv`,
   and put the file's path in **Statement path**.
3. Set **Payday day**, and add your **Committed** outgoings — rent, phone,
   subscriptions — with the day of the month each one leaves.
4. Ask it *"how much have I got?"*

The importer reads the shapes banks actually export: `Date`/`Transaction Date`,
`Amount (GBP)`, `Counter Party`/`Description`, brackets for negatives, and
either row order. Re-importing the same file does not double-count.

## Connecting a bank

Currently Starling. Monzo fits the same interface and would go in
`nova/finance/adapters/monzo.py`; it needs OAuth refresh handling that Starling's
personal access tokens do not.

1. Create a personal access token at
   [developer.starlingbank.com](https://developer.starlingbank.com) with
   `account:read`, `balance:read` and `transaction:read`.
2. On the machine running the core:

   ```bash
   cd /var/lib/nova              # your NOVA_HOME / data directory
   cp finance.env.example finance.env
   chmod 600 finance.env
   nano finance.env              # paste the token after NOVA_FINANCE_TOKEN=
   systemctl restart nova
   ```

3. In the settings panel, set the provider to `starling`.

`finance.env` is in `.gitignore`, it is never read by anything but
`nova/finance`, and an environment variable of the same name overrides it — so
a systemd drop-in or a container secret works without the token touching a
file:

```ini
# systemctl edit nova
[Service]
Environment=NOVA_FINANCE_TOKEN=...
```

> **Upgrading from an earlier build?** `finance.starling_access_token` used to
> be a setting. It no longer exists: the core ignores it on load and drops it
> the next time settings are saved. If you had one set, delete the line from
> `config.toml` yourself and revoke that token — it has been sitting in a file
> that gets sent to every connected client.

If the file's permissions are looser than 0600 the core logs
`finance_secrets_readable_by_others` at every start and tells you the fix. It
still loads it — a broken assistant with no obvious cause helps nobody — but
the warning does not go away until you `chmod 600` it.

## Alerts: polling, or webhooks

**Polling** is on by default and needs nothing: every `refresh_minutes` the
module asks the bank what is new. Fifteen minutes is the default. Alerts arrive
up to that late, which for a home setup is usually fine and requires no open
port.

**Webhooks** are the bank pushing a transaction the second it happens. Faster,
and more to go wrong:

1. Put a shared secret in `finance.env` as `NOVA_FINANCE_WEBHOOK_SECRET`.
   Without it the listener refuses to start — an endpoint that believes
   anything claiming to be a transaction would let anyone on your network make
   N.O.V.A. announce a purchase that never happened.
2. Turn on **Webhook enabled** in settings.
3. The listener binds to `127.0.0.1:8770` by default, because turning a feature
   on should not open a port. Put a reverse proxy or a tunnel (Cloudflare
   Tunnel, Tailscale Funnel) in front of it and register that public URL with
   the bank.
4. Register the URL in the Starling developer portal.

Every delivery is checked against the secret before its body is parsed, in
constant time; anything unsigned or wrongly signed is dropped with a 401 and
logged. Deliveries are deduplicated on the transaction id, so the bank's
retries — which are normal traffic, not an anomaly — produce one alert.

Both paths can run at once. Whichever sees a transaction first alerts on it;
the other finds it already claimed and stays quiet.

## Transfers: the only write

Four separate refusals stand between the module and your account, because the
cost of a bug here is somebody's money:

| Setting | Default | What it does |
|---|---|---|
| `enable_transfers` | `false` | Nothing moves at all while this is off. |
| `transfer_dry_run` | `true` | Logs what it would have done, moves nothing. |
| `transfer_max` | `100` | A transfer above this is **refused, not clamped** — clamping would still move money nobody asked to move. |
| the adapter | — | Has to have opted into being able to move anything. |

Turning on `enable_transfers` is not the same decision as turning off
`transfer_dry_run`, which is why they are two switches. Run it in dry run for a
month first: `finance.db` records every rehearsal, and *"run the payday split"*
tells you what it would have done.

The split fires when a credit arrives that looks like salary — over
`salary_min`, and matching `salary_pattern` if you set one — at most once a
day.

## What is stored, and where

`finance.db`, in the data directory, at 0600. Four tables: transactions seen,
purchases waiting out their cooling-off period, transfers (including dry runs),
and webhook deliveries already handled. Separate from `memory.db` on purpose —
conversation memory is summarised, pruned on a TTL and fed to a model, none of
which are things to do to a transaction log. Deleting `finance.db` deletes
everything the module knows and breaks nothing else.

## Layout

```
nova/finance/
  ledger.py            SQLite: transactions, cooling-off, transfers, dedupe
  budget.py            The arithmetic. Pure, deterministic, no I/O
  phrasing.py          Numbers → the sentence that gets spoken
  module.py            The one entry point; everything returns a finished string
  service.py           Lifecycle, polling, alerts, the cooling-off prompt
  webhook.py           Signature verification and the listener
  secrets.py           finance.env, and checking who can read it
  adapters/
    base.py            The interface a bank has to meet
    csv_import.py      A statement file standing in for a bank
    starling.py        Starling, read-only apart from the pot transfer
nova/skills/builtin/finance.py   The voice tools, all of which raise FinalAnswer
```

## Troubleshooting

**"finance is disabled in settings"** — turn it on in the **Money** section.

**"no statement file configured"** — the provider is `csv` and no path is set.

**"no bank token"** — the provider is `starling` and `finance.env` has no
`NOVA_FINANCE_TOKEN`, or the core has not been restarted since it was added.

**Starling refused the token** — the token is missing a scope. It needs
`account:read`, `balance:read` and `transaction:read`; the payday split also
needs `savings-goal:read` and `savings-goal-transfer:create`.

**No alerts** — check `journalctl -u nova | grep finance`. Transactions dated
before the service started are treated as history rather than news, so nothing
already in the account when you turned it on will be announced.

**The cooling-off question never came** — do-not-disturb and quiet hours are
allowed to swallow it. When that happens the purchase stays in the queue and is
offered again fifteen minutes later, rather than being marked as asked; *"what
am I waiting on?"* lists it in the meantime.

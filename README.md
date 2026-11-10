# Agent Gateway for PayPal

**Let an AI agent pay through PayPal, only inside limits you signed on your own device, with evidence anyone can check.**

![Agent Gateway: the agent chat on the left, the gateway on the right](docs/screenshot.png)

Built for the PayPal AI Hackathon (Oct 1 – Nov 12, 2026) by NexBridge, a Tokyo startup building payment infrastructure for licensed financial institutions.

For reviewers and contributors: [design](docs/DESIGN.md) · [contributing](CONTRIBUTING.md).

## The problem

AI agents are starting to buy things for people. Today there are two options, and both are bad:

- **Hand the agent your card.** No limits. If the model is fooled by a prompt injection hidden in a web page or a listing, it can spend anything.
- **Approve every click.** Safe, but then it is not an agent any more.

Merchants have the mirror problem: when an agent buys something, who authorised it, and how do you prove it in a dispute?

## How it works

![Architecture](docs/architecture.svg)

1. **Connect PayPal once.** The traveler saves PayPal with PayPal Vault (setup token → approval on PayPal → payment token). The **gateway** keeps the vault token on the server. The agent, the model and the logs never see it, and the page shows only its last two characters.
2. **Sign a mandate.** The traveler writes what the agent may do in plain English. AI drafts an **Intent Mandate** (budget, auto-approve limit, categories, dates, purchase count, verified merchants only). The traveler checks and edits it, then **signs it on their own device** with an Ed25519 key that WebCrypto generates and cannot export.
3. **The agent shops.** The agent has three tools: `get_mandate`, `search_inventory`, `request_purchase(sku, reason)`. It cannot name a price or a payee: the price comes from the catalog and the payee is fixed.
4. **The gateway decides, in plain code.** Every request is checked against the signed mandate:

   | Check | Kind | Fails when |
   |---|---|---|
   | Mandate signed and active | hard | no mandate, revoked |
   | Within dates | hard | today is after `valid_until` |
   | Category allowed | hard | item category not in the mandate |
   | Merchant verified | hard | merchant unverified and `verified_merchants_only` |
   | Fits total budget | hard | paid + waiting + price > budget |
   | Purchase count | hard | count ≥ `max_purchases` |
   | Auto-approve limit | soft | price > auto-approve limit |
   | Not a repeat | soft | same SKU already bought |

   Any hard fail → **DENIED**. Only soft fails → **waits for the traveler**, who signs a **Cart Mandate** (human present). The gateway runs the hard checks again before paying. All pass → the gateway creates a PayPal order with the vault token, payer not present.
5. **Evidence.** Each purchase gets an evidence bundle: the signed Intent Mandate, the Cart Mandate (and the traveler's signature, if any), the gateway's checks, the PayPal order and capture IDs with the order read back from PayPal, any refund, and the hash-chained ledger up to that purchase. `tools/verify_evidence.py` checks it offline.

**Even if the AI is fooled, it can't spend outside the limits you signed.** The demo catalog has a listing (H-399) whose text tells AI agents to book a $1,280 suite without asking. "Simulate hijacked agent" sends exactly that request through the same gateway. Under the demo mandate it is denied (merchant not verified, over budget). The button uses the real checks, so the result always depends on the mandate that is signed. In our live runs, Claude skipped this listing on its own and explained why. The gateway is for the times a model doesn't.

## Quick start (demo mode, no keys)

Python 3.9 or newer, standard library only. No `pip install`.

```bash
git clone https://github.com/nexbridge-xyz/agent-gateway-paypal
cd agent-gateway-paypal
python3 server.py --demo
```

Open <http://localhost:8000> in a current Chrome, Edge, Safari or Firefox (Ed25519 in WebCrypto is required), then:

1. **Connect PayPal** → a page plainly labelled *Simulated wallet approval (demo mode)* → **Approve**.
2. **Draft with AI** → check the fields → **Sign on this device** → **Sign mandate**.
3. Click the **Book my Osaka trip** chip → **Send**. The agent buys two trains (auto-approved) and asks for the hotel (above the $150.00 limit).
4. **Approve & sign** the hotel.
5. **Simulate hijacked agent** → denied.
6. **Verify chain**, then **Evidence** on the hotel, and run `python3 tools/verify_evidence.py evidence-<id>.json`.
7. **Refund** the hotel (budget comes back) or **Revoke mandate** (everything after is denied).

**Reset demo** clears mandates, purchases and the ledger but keeps the PayPal connection and device key. `python3 server.py --reset-all` deletes `data/state.json`: the PayPal connection, the registered device keys, mandates, purchases and the ledger. The signing key in your browser stays, and the page registers it again when it loads. The trip dates are not hard-coded: the demo trip is the first Friday at least 10 days from today, Friday to Sunday.

The header always shows what is real and what is simulated: `PayPal simulated` / `PayPal Sandbox`, and `Simulated AI · scripted agent` / the model name. In demo mode the agent's *thinking* is scripted, but the signatures, the gateway checks, the ledger and the evidence are the same code as in live mode.

## Live mode: PayPal sandbox

1. [developer.paypal.com](https://developer.paypal.com) → **Testing Tools → Sandbox Accounts** → create two accounts with country **United States**: one **Business**, one **Personal**.
2. **Apps & Credentials → Sandbox → Create App**, linked to the US Business sandbox account.
3. In the app, under **Payment capabilities**, check **Save payment methods** and click **Save Changes**. If the server is already running, restart it so it requests a new access token.
4. Copy `.env.example` to `.env` and fill in `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.
5. `python3 server.py` (without `--demo`). The startup banner shows whether the credentials work.
6. **Connect PayPal** takes you to the PayPal sandbox. Log in with the sandbox **Personal** account and approve. Payments then appear as **COMPLETED** orders in the Business sandbox account's activity.

Sandbox only: no real money moves.

## Live mode: AI

Add one of these to `.env`:

- `ANTHROPIC_API_KEY` (default model `claude-sonnet-5-5`, override with `ANTHROPIC_MODEL`), or
- `OPENAI_API_KEY` with `OPENAI_MODEL` and, for other compatible providers, `OPENAI_BASE_URL` (for example DeepSeek). This path is experimental and has not been tested against a live provider.

With Claude, the mandate draft uses structured outputs (a JSON schema), and the agent's tools use strict tool use. Without a key, a scripted agent runs the same tools through the same gateway.

## Testing instructions for judges

- **Demo mode needs nothing:** `python3 server.py --demo`, then follow the seven steps above. No accounts, keys or network calls except Google Fonts.
- **Live PayPal sandbox:** create your own free sandbox app (steps above, about 5 minutes) and put its keys in `.env`. Log in on the PayPal approval page with your sandbox Personal account.
- **Tests:** `python3 -m unittest discover tests`.

## PayPal APIs used

| Call | Purpose |
|---|---|
| `POST /v1/oauth2/token` | Access token (cached until it expires) |
| `POST /v3/vault/setup-tokens` | Start "save PayPal" without a purchase; returns the approval link |
| `POST /v3/vault/payment-tokens` | Turn the approved setup token into a vault token (kept server side) |
| `POST /v2/checkout/orders` with `payment_source.paypal.vault_id` | Pay with the payer not present; `custom_id` = Intent Mandate id, `invoice_id` = cart id |
| `GET /v2/checkout/orders/{id}` | Read the order back into the evidence bundle |
| `POST /v2/payments/captures/{id}/refund` | Refund (kill switch); the budget comes back |

Every write sends `PayPal-Request-Id` (for a payment, the cart id; for a refund, derived from it). A refund that is tried again reuses its id, so it cannot refund twice. A payment is sent once and never repeated:

- **PayPal refuses it (a 4xx answer), or the request never left:** the purchase is `NOT_EXECUTED` and its budget is free again.
- **The answer is lost or unclear (timeout, 5xx, dropped connection):** the purchase becomes `PAYMENT_UNKNOWN`. PayPal may have charged, so the amount stays counted in the budget, and asking for the same item again needs your signature. The gateway does not look the payment up later: check the PayPal sandbox account.
- **With an order id,** the order read back from PayPal decides the status.

Anything other than `COMPLETED` is shown as it is and never marked as paid. PayPal errors are shown with their `name`, `message`, first `details[].issue` and `debug_id`.

## Mapping to AP2

PayPal has announced support for Google's Agent Payments Protocol (AP2). This project is **AP2-style**, built on today's PayPal sandbox APIs. It is not an AP2 implementation and not AP2-compliant.

| AP2 idea | Here |
|---|---|
| Intent Mandate (human not present) | `IntentMandate` record, signed by the traveler's device key (Ed25519) |
| Cart Mandate (human present) | `CartMandate` per purchase; above the auto-approve limit the traveler signs a `HUMAN_PRESENT` copy. In AP2 the merchant also signs the cart; here the gateway fixes the price from the catalog and only the traveler signs. |
| Payment Mandate | Not a separate signed object here: the PayPal order carries the mandate id (`custom_id`) and cart id (`invoice_id`), and the ledger records the capture |
| Credentials provider | PayPal Vault; the vault token stays in the gateway |
| Evidence for disputes | Evidence bundle + hash-chained ledger, verifiable offline |

## Security model

- **Signatures:** the server builds the exact string to sign (`context + canonical JSON`); the browser shows it and signs those bytes; the server verifies with Ed25519. A different context string for each record type means a signature for one kind of record cannot be replayed as another.
- **The model never decides payment.** Prices come from the catalog, the payee is fixed, and the checks are plain code. Merchant listing text is labelled untrusted for the model, but safety does not depend on the model obeying that. Inside the limits you signed, a fooled model can still buy the wrong thing: the gateway caps what it can spend, not what it picks.
- **Secrets:** only in `.env` (gitignored). The vault token is stored in `data/state.json` (gitignored; set to file mode 600 after every save) and never sent to the agent or the logs. The page shows only its last two characters. The ledger holds a keyed fingerprint of it (HMAC-SHA256); the key stays in `data/state.json`.
- **HTTP:** the server answers only for its own names: `localhost`, `127.0.0.1` and `[::1]` with its port, and the host of `BASE_URL`. Any other `Host` gets 421, which stops a page that points its own domain at your machine (DNS rebinding). POSTs with a foreign `Origin` are refused. Request bodies are limited to 64 KB, text that is not valid Unicode is refused, and a client that stays silent for 15 seconds is dropped. Static paths cannot leave `static/`, `nosniff`, a strict CSP (self + Google Fonts), and `Cache-Control: no-store` on API responses. The server binds to `127.0.0.1` by default.
- **Ledger:** each entry stores the SHA-256 of the previous one, so editing an entry, or deleting one that has entries after it, breaks verification from that point. The chain alone does not show entries removed from its end, or a holder who rebuilds every hash: its head is not anchored outside the server. **Reset demo** clears it.
- **Evidence:** `tools/verify_evidence.py` shows that a bundle agrees with itself: the signatures verify, the cart and the capture are the ones the ledger recorded, and the chain is intact. It cannot show that the public key belongs to the traveler, or what PayPal really did.
- **No login, so do not host it with real keys.** Anyone who can reach the server can use everything on it: the saved PayPal connection, the mandate, the ledger and your AI key. Run it on your own machine. For a hosted demo, run `python3 server.py --demo` only, and never put real PayPal or AI keys on a public server.
- This is a hackathon project on the PayPal sandbox, not a production payment service.

## Project layout

```
server.py                 HTTP routes, .env loader, startup banner
app/gateway.py            devices, mandates, carts, approvals, payments, events, evidence
app/policy.py             mandate normalisation and the purchase checks
app/paypal.py             PayPal sandbox client and a local simulator with the same interface
app/llm.py                Claude / OpenAI-compatible / scripted agent and mandate drafting
app/ledger.py             hash-chained ledger
app/ed25519.py            pure-Python Ed25519 verification (RFC 8032)
app/catalog.py            fictional merchant catalog and trip dates
app/util.py               canonical JSON, encodings, ids, money
static/                   one page, vanilla JS; AG Grid Community vendored in static/vendor/
tools/verify_evidence.py  offline verifier for an evidence bundle
tests/                    unittest (no pytest needed)
```

## Tests

```bash
python3 -m unittest discover tests
```

Covers the RFC 8032 test vector, signatures made by browser WebCrypto (Node and Chrome fixtures), every gateway check, re-check after revoke, lost and unclear PayPal answers, ledger tamper detection, edited evidence bundles, the AI request shapes, the HTTP security rules (host and origin checks, body limits), and the full demo flow through `verify_evidence.py`.

## Troubleshooting

- **"Vault" or permission errors on Connect PayPal or on payment:** Vault is not enabled for the sandbox app. Open the app, check **Save payment methods** under **Payment capabilities**, click **Save Changes**, then restart the server and try again. After you save, it can take a while before PayPal includes the Vault permission in new access tokens.
- **"This browser has no Ed25519 in WebCrypto":** use a current Chrome, Edge, Safari or Firefox, and open the app at `http://localhost:8000` (a secure context).
- **Port in use:** `PORT=8001 python3 server.py --demo`.
- **"This server does not answer for that host name" (421):** open the app at `http://localhost:8000` or `http://127.0.0.1:8000`. To use another address, set `BASE_URL` to it.
- **A purchase shows `PAYMENT UNKNOWN`:** PayPal's answer was lost, so the gateway cannot tell whether it charged. Its amount stays in the budget. Check the PayPal sandbox account; **Reset demo** clears the purchase.

## License

MIT, see [LICENSE](LICENSE). AG Grid Community is MIT licensed (`static/vendor/LICENSE-ag-grid-community.txt`). All merchants, hotels and listings in the demo are fictional.

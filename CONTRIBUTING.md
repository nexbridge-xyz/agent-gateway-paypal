# Contributing

Thank you for helping. Please read [docs/DESIGN.md](docs/DESIGN.md) before changing the gateway, the checks or the PayPal code.

## Hackathon freeze

This repository is an entry in the PayPal AI Hackathon. From the submission deadline (2026-11-12 12:00 PT) until judging ends (2026-12-15), `main` stays exactly as it was when the entry was submitted. You can still open branches and pull requests in that period, but they will not be merged into `main` until judging has ended.

## Set up

- Python 3.9 or newer. There is nothing to install: the project uses only the standard library.
- A current Chrome, Edge, Safari or Firefox (the page needs Ed25519 in WebCrypto).

Run the demo (simulated PayPal, scripted agent, no keys):

```bash
python3 server.py --demo
```

Then open http://localhost:8000.

Run the tests:

```bash
python3 -m unittest discover tests
```

The tests need no network and no keys.

## How to make a change

1. Create a branch from `main`. Do not push to `main` directly.
2. Make your change. Add or update tests for it.
3. Run `python3 -m unittest discover tests`. Every test must pass.
4. If you changed the browser code or the flow, run the demo and walk through it once in a browser.
5. Open a pull request. Say what changed and why, and how you tested it.

Keep each pull request to one topic. Keep the project free of third-party dependencies and build steps.

## Keys and secrets

- Never commit `.env`, `data/`, or any key, token or password. Both `.env` and `data/` are in `.gitignore`.
- To run in live mode, use your own PayPal sandbox app and your own AI key. Put them in your own `.env`, copied from `.env.example`. Do not share keys in issues, pull requests or chat.
- The project uses the PayPal sandbox only. Do not add live PayPal credentials.

## Rules the code must keep

- Money moves only through `request_purchase` and the checks in `app/policy.py`. Do not give the agent a tool that can pay, approve, refund or revoke.
- The vault token stays on the server. It must not appear in tool results, events, the ledger, evidence bundles, logs or the UI (except masked).
- Model output is shown as plain text in the page, never as HTML.
- Use fictional merchants and listings only.

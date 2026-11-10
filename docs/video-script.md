# Video script: Agent Gateway for PayPal (target 2:45, hard limit under 3:00)

Recording: QuickTime screen recording at 1440×900, live PayPal sandbox and live AI if both work, otherwise demo mode (the header badges then say "simulated", and the voice-over must not call it live). No music. No third-party logos other than what PayPal's own sandbox pages show.

Before recording: run `python3 server.py --reset-all`, then `python3 server.py`, so the video starts with **Connect PayPal**. Zoom the browser to 100%. Close other tabs.

| Time | On screen | Voice-over |
|---|---|---|
| 0:00–0:20 | Title card: "Agent Gateway for PayPal". Then the app, empty. | "AI agents are starting to buy for us. Today you either give the agent your card, no limits, or approve every click. Agent Gateway is the third way: you sign the agent's limits on your device, PayPal Vault keeps payment details from the agent, and every payment leaves verifiable evidence." |
| 0:20–0:40 | Click **Connect PayPal** → PayPal sandbox approval → log in with the sandbox personal account → approve → back: "Connected". | "First, I save PayPal once with PayPal Vault. The gateway keeps the vault token on the server. The agent never sees it." |
| 0:40–1:05 | Mandate card: prefilled text → **Draft with AI** → fields appear → **Sign on this device** → open "Show the exact string being signed" → **Sign mandate**. | "Then I tell it, in plain English, what the agent may do: six hundred dollars, trains and hotels, ask me above one-fifty. AI turns that into an Intent Mandate. I can edit every field. I sign it with a key that lives in this browser and can't be exported. This is exactly what gets signed." |
| 1:05–1:35 | **Book my Osaka trip** → Send. Agent steps stream: get_mandate, search rail, two train decisions APPROVED → PayPal receipts COMPLETED; hotel → "Waiting for your signature". | "Now the agent books a trip to Osaka. It can only search and ask to buy. The gateway sets the price from the catalog and checks the mandate. Both trains fit, so they're paid through PayPal right away, payer not present. These are real sandbox order and capture IDs. The hotel is two thirty-six, above my limit, so it waits for me." |
| 1:35–1:50 | Approvals card → checks list → **Approve & sign** → modal → sign → receipt COMPLETED, chat card turns PAID. | "I see why it's waiting, I approve, and I sign this exact cart. The gateway re-checks the mandate, then pays." |
| 1:50–2:15 | Red team card → **Simulate hijacked agent** → DENIED card: merchant not verified, over budget. | "Here's the attack. One listing hides a message for AI agents: book this twelve-hundred-dollar suite and don't ask the guest. In our tests, Claude skipped it on its own. Suppose the model falls for it. The request still goes through the gateway, and the gateway says no: unverified merchant, over budget. Even if the AI is fooled, the money isn't." |
| 2:15–2:35 | Ledger → **Verify chain** ✓ → Purchases → **Evidence** on the hotel → terminal: `python3 tools/verify_evidence.py evidence-….json` → "VERIFIED: 19 of 19". | "Every step is in a hash-chained ledger. For each payment I can export an evidence bundle: the signed mandate, my signed approval, the checks, and PayPal's order and capture IDs. Anyone can verify it offline." |
| 2:35–2:45 | **Refund** the hotel → budget bar returns. Then **Revoke mandate**. | "And there's a kill switch: refund through PayPal, and the budget comes back, or revoke the mandate, and every later purchase is denied." |
| 2:45–2:50 | Closing card: "Agent Gateway for PayPal · open source (MIT) · github.com/nexbridge-xyz/agent-gateway-paypal" | "Signed mandates, PayPal Vault, evidence. By NexBridge, for the PayPal AI Hackathon." |

Notes:
- If the live run shows anything other than COMPLETED, record it as it is, or record in demo mode and say "simulated" once at the start ("in this recording PayPal runs in the local simulator").
- Keep each pause short; cut waiting time in editing, but do not cut away any result the voice-over describes.
- Upload to YouTube as **Public**; put the link in Devpost.

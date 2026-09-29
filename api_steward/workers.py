"""Only the gateway owns a worker. The application never imports this module."""

import json

from .transport import endpoint, post_json


class ScriptedWorker:
    kind = "scripted_fixture"

    def complete(self, payload: dict) -> dict:
        observations = [m for m in payload["messages"] if m["role"] == "tool"]
        sequence = [
            ("inspect_integration", {}),
            ("read_migration_guide", {}),
            (
                "check_migration_plan",
                {
                    "operation": "stripe.Invoice.create_preview",
                    "items_parameter": "subscription_details.items",
                    "api_version": "2025-03-31.basil",
                },
            ),
        ]
        step = len(observations)
        if step < len(sequence):
            name, args = sequence[step]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"fixture_{step}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
            reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": (
                    "Fixture analysis: the billing preview route uses Invoice.upcoming and "
                    "subscription_items. Proposed target: Invoice.create_preview with "
                    "subscription_details.items and API 2025-03-31.basil. Preserve customer, "
                    "subscription, item, price and quantity inputs, and the currency/amount_due "
                    "interface. The mapping checker passed. No patch was generated or executed; "
                    "billing behavior and side effects remain unverified. Next: qualify the "
                    "real baseline and freeze independent acceptance tests."
                ),
            }
            reason = "stop"
        # Deliberately no fabricated token usage or inference timings.
        return {
            "model": payload["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        }


class CompatibleWorker:
    kind = "openai_compatible"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 30):
        self.url, self.api_key, self.timeout = endpoint(base_url), api_key, timeout

    def complete(self, payload: dict) -> dict:
        return post_json(self.url, payload, self.timeout, self.api_key)

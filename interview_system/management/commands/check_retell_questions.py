"""Read-only prompt inspection and optional stateless real-provider conversation."""
import json
from urllib.parse import quote
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the configured agent prompt; optionally test synthetic question order via Retell playground (not audio)."

    def add_arguments(self, parser):
        parser.add_argument("--playground", action="store_true")

    def handle(self, *args, **options):
        key, agent = settings.RETELL_API_KEY, settings.RETELL_AGENT_ID
        if not key or not agent:
            raise CommandError("Retell is not configured.")
        headers = {"Authorization": f"Bearer {key}"}
        def request(method, path, **kwargs):
            try:
                with requests.request(method, "https://api.retellai.com/" + path,
                        headers=headers, timeout=(3, 25), allow_redirects=False, **kwargs) as response:
                    if response.status_code != 200:
                        raise CommandError(f"Retell verification returned HTTP {response.status_code}.")
                    return response.json()
            except (requests.RequestException, ValueError):
                raise CommandError("Retell verification failed; no provider details logged.") from None
        data = request("GET", "get-agent/" + quote(agent, safe=""))
        engine = data.get("response_engine", {})
        if engine.get("type") != "retell-llm":
            raise CommandError("Inspect this agent's response-engine prompt manually.")
        llm = request("GET", "get-retell-llm/" + quote(engine["llm_id"], safe=""))
        if "{{interview_questions}}" not in llm.get("general_prompt", ""):
            raise CommandError("Agent prompt does not consume {{interview_questions}}.")
        result = {"agent_version": data.get("version"), "prompt_variable_present": True,
                  "live_audio_verified": False, "playground_order_verified": False}
        if options["playground"]:
            questions = [
                {"text": "For the cobalt orchard scenario, explain how you would retry a failed queue job.", "category": "TECHNICAL"},
                {"text": "For the recruiter-added amber compass scenario, describe a team decision you changed.", "category": "BEHAVIORAL"},
            ]
            from interview_system.integrations.retell_client import _question_context
            _, question_context = _question_context({"id": "g04-synthetic-review"}, [
                {**q, "approved": True, "interview_id": "g04-synthetic-review",
                 "source": "MANUAL" if index else "GENERATED"} for index, q in enumerate(questions)])
            messages = [{"role": "user", "content": "Hello. I am ready to begin the interview."}]
            positions = []
            for turn in range(6):
                response = request("POST", "agent-playground-completion/" + quote(agent, safe=""),
                    json={"messages": messages, "dynamic_variables": {"interview_questions": question_context}})
                additions = [{"role": m["role"], "content": m.get("content", "")} for m in response.get("messages", [])
                    if m.get("role") in ("agent", "user")]
                messages.extend(additions)
                text = " ".join(m["content"] for m in additions if m["role"] == "agent").lower()
                if all(marker in text for marker in ("cobalt orchard", "amber compass")):
                    raise CommandError("Agent asked both distinctive questions in one turn instead of awaiting an answer.")
                for index, marker in enumerate(("cobalt orchard", "amber compass")):
                    if marker in text and index not in positions:
                        positions.append(index)
                if positions == [0, 1]:
                    break
                messages.append({"role": "user", "content": "I would use bounded retries with backoff and idempotency, investigate the evidence with the team, then document the decision. Please continue."})
            result["playground_order_verified"] = positions == [0, 1]
        self.stdout.write(json.dumps(result))
        if options["playground"] and not result["playground_order_verified"]:
            raise CommandError("Synthetic question order was not verified; inspect the agent prompt.")

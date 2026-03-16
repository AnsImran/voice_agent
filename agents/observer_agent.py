"""Observer agent for parallel hallucination monitoring."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from livekit.agents import ConversationItemAddedEvent
from livekit.agents.llm import ChatContext

from utils import ensure_session_trace_id, load_prompt, trace_log

logger = logging.getLogger("doheny-surf-desk.observer")


class ObserverAgent:
    """
    Parallel observer that monitors conversation quality and fact correctness.

    This observer does not become the active voice agent. It listens to
    conversation events and injects guardrail hints into the current agent
    context when a hallucinated business fact is detected.
    """

    def __init__(self, session, llm):
        self.session = session
        self.instructions = load_prompt("observer_prompt.yaml", include_reading_guidelines=False)
        self.business_facts = self._load_business_facts()
        self.llm = llm
        self.conversation_history: list[dict] = []
        self.sent_signatures: set[str] = set()
        self.last_eval_transcript_count = 0
        self.eval_threshold = 6  # 3 user + assistant pairs
        self._evaluating = False
        self._last_context_hash = ""

        self._setup_listeners()

        logger.info(
            "ObserverAgent initialized: model=%s eval_threshold=%s",
            self.llm.model if hasattr(self.llm, "model") else "custom",
            self.eval_threshold,
        )

    def _load_business_facts(self) -> str:
        """Load Happy Hound facts used as truth source for hallucination checks."""
        facts_path = Path(__file__).resolve().parent.parent / "business_info_happy_hound.txt"
        if facts_path.exists():
            return facts_path.read_text(encoding="utf-8", errors="ignore")
        logger.warning("Observer facts file missing: %s", facts_path)
        return "Business facts file is unavailable."

    @staticmethod
    def _extract_text(event: ConversationItemAddedEvent) -> str:
        chunks: list[str] = []
        for content in event.item.content:
            if isinstance(content, str):
                chunks.append(content.strip())
                continue

            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
                continue

            text_attr = getattr(content, "text", None)
            if isinstance(text_attr, str) and text_attr.strip():
                chunks.append(text_attr.strip())
        return " ".join(chunk for chunk in chunks if chunk)

    def _setup_listeners(self):
        """Set up session event listeners."""

        @self.session.on("conversation_item_added")
        def conversation_item_added(event: ConversationItemAddedEvent):
            role = event.item.role
            if role not in {"user", "assistant"}:
                return
            trace_id = ensure_session_trace_id(self.session.userdata)

            transcript_text = self._extract_text(event)
            if not transcript_text:
                return

            logger.info("[OBSERVER] Captured %s turn: %s", role, transcript_text)
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_OBSERVER",
                trace_id=trace_id,
                message="observer.capture_turn",
                role=role,
                transcript=transcript_text,
            )
            self.conversation_history.append(
                {
                    "text": transcript_text,
                    "participant": role,
                    "timestamp": None,
                }
            )

            total_segments = len(self.conversation_history)
            new_segments = total_segments - self.last_eval_transcript_count

            if new_segments >= self.eval_threshold:
                logger.info("[OBSERVER] Triggering evaluation (%s segments)", new_segments)
                trace_log(
                    logger=logger,
                    flag_name="HH_TRACE_OBSERVER",
                    trace_id=trace_id,
                    message="observer.trigger_evaluation",
                    total_segments=total_segments,
                    new_segments=new_segments,
                )
                asyncio.create_task(self._evaluate_with_llm())
                self.last_eval_transcript_count = total_segments

    async def _evaluate_with_llm(self):
        """Use LLM to evaluate recent conversation for hallucinated facts."""
        if self._evaluating:
            return

        self._evaluating = True
        try:
            trace_id = ensure_session_trace_id(self.session.userdata)
            recent_history = self.conversation_history[-12:]
            if not recent_history:
                return

            conversation_text = "\n".join(
                [f"{msg['participant']}: {msg['text']}" for msg in recent_history]
            )
            userdata_summary = self._format_userdata_summary(self.session.userdata)
            tool_facts = self._format_tool_facts(self.session.userdata)
            self._last_context_hash = hashlib.sha256(
                f"{conversation_text}\n{tool_facts}".encode("utf-8")
            ).hexdigest()
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_OBSERVER",
                trace_id=trace_id,
                message="observer.evaluate_context",
                context_hash=self._last_context_hash,
                userdata_summary=userdata_summary,
                tool_facts=tool_facts,
            )

            try:
                eval_prompt = self.instructions.format(
                    conversation_text=conversation_text,
                    userdata_summary=userdata_summary,
                    business_facts=self.business_facts,
                    tool_facts=tool_facts,
                )
            except KeyError as exc:
                logger.error("Missing key in observer prompt formatting: %s", exc)
                eval_prompt = (
                    "Review this conversation for incorrect business claims:\n"
                    f"{conversation_text}"
                )

            chat_ctx = ChatContext()
            chat_ctx.add_message(role="user", content=eval_prompt)

            response_text = ""
            async with self.llm.chat(chat_ctx=chat_ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        response_text += chunk.delta.content

            if not response_text:
                return

            eval_result = self._parse_eval_response(response_text)
            if eval_result:
                await self._process_eval_result(eval_result)
        except Exception as exc:
            logger.error("Error during observer LLM evaluation: %s", exc, exc_info=True)
        finally:
            self._evaluating = False

    def _parse_eval_response(self, response_text: str) -> dict | None:
        """Parse LLM response as JSON and normalize fields."""
        try:
            result = json.loads(response_text.strip())
            if isinstance(result, dict):
                return self._validate_eval_result(result)
        except json.JSONDecodeError:
            pass

        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                result = json.loads(response_text[start:end].strip())
                if isinstance(result, dict):
                    return self._validate_eval_result(result)
            except json.JSONDecodeError:
                pass

        logger.error("[OBSERVER] Failed to parse JSON response: %s", response_text[:180])
        return None

    @staticmethod
    def _validate_eval_result(result: dict) -> dict:
        """Validate and normalize hallucination schema with defaults."""
        return {
            "hallucination_detected": bool(result.get("hallucination_detected", False)),
            "incorrect_claim": str(result.get("incorrect_claim", "")).strip(),
            "correct_fact": str(result.get("correct_fact", "")).strip(),
            "details": str(result.get("details", "")).strip(),
        }

    @staticmethod
    def _format_userdata_summary(userdata) -> str:
        """Format lightweight session-state summary for observer grounding."""
        parts = []
        if userdata.name:
            parts.append(f"Name: {userdata.name}")
        if userdata.phone:
            parts.append(f"Phone: {userdata.phone}")
        if userdata.dog_weight_lbs is not None:
            parts.append(f"DogWeightLbs: {userdata.dog_weight_lbs}")
        if userdata.dog_size:
            parts.append(f"DogSize: {userdata.dog_size}")
        if userdata.requested_services:
            parts.append(f"Services: {', '.join(userdata.requested_services)}")
        if getattr(userdata, "service_family", None):
            parts.append(f"ServiceFamily: {userdata.service_family}")
        if getattr(userdata, "service_plan", None):
            parts.append(f"ServicePlan: {userdata.service_plan}")
        if userdata.requested_date:
            parts.append(f"RequestedDate: {userdata.requested_date}")
        if userdata.requested_time:
            parts.append(f"RequestedTime: {userdata.requested_time}")
        if userdata.quoted_total is not None:
            parts.append(f"QuotedTotal: {userdata.quoted_total}")

        return ", ".join(parts) if parts else "No user data yet"

    @staticmethod
    def _format_tool_facts(userdata) -> str:
        """Serialize runtime tool facts for observer grounding."""
        try:
            return json.dumps(getattr(userdata, "runtime_tool_facts", {}), ensure_ascii=False)
        except Exception:
            return "{}"

    async def _process_eval_result(self, eval_result: dict):
        """Process hallucination output and inject corrective guardrail hint once per signature."""
        if not eval_result.get("hallucination_detected"):
            return

        incorrect_claim = eval_result.get("incorrect_claim", "")
        correct_fact = eval_result.get("correct_fact", "")
        details = eval_result.get("details", "")
        signature = hashlib.sha256(
            f"{incorrect_claim}|{correct_fact}|{self._last_context_hash}".lower().encode("utf-8")
        ).hexdigest()

        if signature in self.sent_signatures:
            return

        logger.warning("[OBSERVER] Hallucination detected: %s", incorrect_claim or details)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_OBSERVER",
            trace_id=ensure_session_trace_id(self.session.userdata),
            message="observer.hallucination_detected",
            incorrect_claim=incorrect_claim,
            correct_fact=correct_fact,
            details=details,
            context_hash=self._last_context_hash,
        )
        await self._send_guardrail_hint(
            severity="CRITICAL",
            trigger="Potential business-fact hallucination",
            hint=(
                f"Incorrect claim: {incorrect_claim or 'Not specified'}\n"
                f"Correct fact: {correct_fact or 'Not specified'}\n"
                f"Analysis: {details or 'Fact mismatch with business info source.'}\n\n"
                "Action: Correct the misinformation immediately, provide the verified fact, "
                "and continue with accurate details only."
            ),
        )
        self.sent_signatures.add(signature)

    async def _send_guardrail_hint(self, severity: str, trigger: str, hint: str):
        """Inject a guardrail hint into the active agent's context."""
        logger.warning("[OBSERVER] %s: %s", severity, trigger)
        logger.info("[OBSERVER] Hint: %s", hint)

        if not hasattr(self.session, "current_agent") or not self.session.current_agent:
            logger.warning("No active agent to inject hint into")
            return

        current_agent = self.session.current_agent
        hint_message = f"""[GUARDRAIL ALERT - {severity}]: {trigger}

{hint}

ACKNOWLEDGMENT REQUIRED: In your next response, briefly acknowledge this alert and take the required action."""

        ctx_copy = current_agent.chat_ctx.copy()
        ctx_copy.add_message(
            role="system",
            content=hint_message,
        )
        await current_agent.update_chat_ctx(ctx_copy)


async def start_observer(session, llm=None):
    """Start observer agent for an AgentSession."""
    observer = ObserverAgent(session, llm=llm)
    logger.info("Observer agent started with hallucination monitoring")
    return observer

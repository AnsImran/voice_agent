"""Front desk agent for initial consultation and routing."""
import logging

from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T
from tools.availability_provider import resolve_service_selection
from utils import (
    ensure_session_trace_id,
    load_prompt,
    resolve_agent_tts,
    trace_log,
    userdata_diff,
    userdata_snapshot,
)

logger = logging.getLogger("doheny-surf-desk.frontdesk")


class FrontDeskAgent(BaseAgent):
    """Agent responsible for greeting customers and routing them appropriately."""
    
    def __init__(self, chat_ctx=None):
        agent_kwargs = {
            "instructions": load_prompt('frontdesk_prompt.yaml', include_business_facts=True),
            "chat_ctx": chat_ctx,
        }
        tts_descriptor = resolve_agent_tts("frontdesk")
        if tts_descriptor:
            agent_kwargs["tts"] = tts_descriptor
        super().__init__(
            **agent_kwargs,
        )
    
    async def on_enter(self) -> None:
        """Called when agent starts.

        Detects re-entry (transfer back from Scheduler/Intake) via chat_ctx
        history and uses a re-entry-specific instruction that tells the LLM
        NOT to auto-call start_booking from stale chat history. Also sets a
        one-turn guard flag so start_booking refuses on the first turn after
        re-entry (defense in depth).
        """
        userdata = self.session.userdata
        # Re-entry is set explicitly by the transferring agent via the
        # reentry_target flag in runtime_tool_facts. chat_ctx.items is not a
        # reliable signal because LiveKit populates chat_ctx with the system
        # prompt on init, so it's non-empty even on first entry.
        is_reentry = userdata.runtime_tool_facts.pop("reentry_target", None) == "frontdesk"

        if is_reentry:
            userdata.runtime_tool_facts["frontdesk_awaiting_fresh_turn"] = True
            await self.session.generate_reply(
                instructions=(
                    "You are the Front Desk. The caller was just transferred back to you. "
                    "Greet them with 'Welcome back to the Front Desk' and ask what they would "
                    "like to do now — more questions, a different booking, or cancel. "
                    "CRITICAL: Do NOT call start_booking based on prior chat history. Prior "
                    "booking intent is paused. Only call start_booking if the caller states "
                    "a fresh, explicit new booking intent in their very next turn or later."
                )
            )
            return

        userdata.runtime_tool_facts.pop("frontdesk_awaiting_fresh_turn", None)
        await self.session.generate_reply(
            instructions=(
                "Warmly greet the customer and introduce yourself as the front desk "
                "assistant for Happy Hound. Ask how you can help today with daycare, "
                "boarding, training, grooming, or availability questions."
            )
        )

    @function_tool
    async def start_booking(
        self,
        context: RunContext_T,
        service_request: str | None = None,
    ) -> "BaseAgent | str":
        """Start booking workflow by transferring caller to SchedulerAgent.

        Args:
            context: RunContext with userdata
            service_request: Caller's stated service intent (e.g. "daycare",
                "Golden Leash Club Card", "grooming for my lab")

        Returns:
            SchedulerAgent instance (transfers the call), or a wait-for-fresh-
            turn string if FrontDesk just re-entered and the LLM tried to
            auto-book from stale chat history.
        """
        userdata = context.userdata

        if userdata.runtime_tool_facts.get("frontdesk_awaiting_fresh_turn"):
            userdata.runtime_tool_facts.pop("frontdesk_awaiting_fresh_turn", None)
            return (
                "WAITING_FOR_FRESH_INTENT: The caller was just transferred back to Front Desk "
                "and has not yet stated a fresh booking intent. Do NOT auto-book from prior "
                "chat history. Greet them, ask what they want to do, and only call "
                "start_booking if they explicitly request a new booking in a fresh turn."
            )

        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)

        family, plan = resolve_service_selection(
            service_request,
            existing_family=userdata.service_family
            or (userdata.requested_services[0] if userdata.requested_services else None),
            existing_plan=userdata.service_plan,
        )
        userdata.service_family = family
        userdata.service_plan = plan
        userdata.requested_services = [family]
        userdata.selection_source = "frontdesk.start_booking"
        userdata.runtime_tool_facts["frontdesk_selection"] = {
            "service_request": service_request,
            "service_family": family,
            "service_plan": plan,
        }

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="frontdesk.start_booking",
            from_agent="FrontDeskAgent",
            to_agent="SchedulerAgent",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        await self.session.say(
            "Great. I'm now transferring your call to our Scheduling Department to check "
            "availability. Kindly wait a moment while I connect you."
        )

        from agents.scheduler_agent import SchedulerAgent
        return SchedulerAgent(chat_ctx=self.chat_ctx)

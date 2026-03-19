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
            "instructions": load_prompt('frontdesk_prompt.yaml'),
            "chat_ctx": chat_ctx,
        }
        tts_descriptor = resolve_agent_tts("frontdesk")
        if tts_descriptor:
            agent_kwargs["tts"] = tts_descriptor
        super().__init__(
            **agent_kwargs,
        )
    
    async def on_enter(self) -> None:
        """Called when agent starts."""
        await self.session.generate_reply(
            instructions="Warmly greet the customer and introduce yourself as the front desk assistant for Happy Hound. "
            "Ask how you can help today with daycare, boarding, training, grooming, or availability questions."
        )
    
    @function_tool
    async def start_booking(
        self,
        context: RunContext_T,
        service_request: str | None = None,
    ) -> BaseAgent:
        """Start booking workflow by transferring caller to IntakeAgent.

        Phase behavior:
        - Move from consultation into profile collection
        - Preserve conversation context during handoff

        Args:
            context: RunContext with userdata
            
        Returns:
            IntakeAgent instance
        """
        userdata = context.userdata
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
        userdata.handoff_pending_action = "scheduler_on_enter"
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
            to_agent="IntakeAgent",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        await self.session.say(
            "Great. I'm now transferring your call to our Intake Department to collect "
            "your details. Kindly wait a moment while I connect you."
        )

        from agents.intake_agent import IntakeAgent
        return IntakeAgent(chat_ctx=self.chat_ctx)

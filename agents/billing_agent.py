"""Billing agent for quote confirmation and structured handoff email."""
from __future__ import annotations

import logging

from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T
from tools.availability_provider import (
    compute_selection_quote,
    get_service_display_label,
)
from tools.handoff_email_tools import build_handoff_payload, send_handoff_email
from utils import (
    ensure_session_trace_id,
    load_prompt,
    trace_log,
    userdata_diff,
    userdata_snapshot,
)

logger = logging.getLogger("doheny-surf-desk.billing")


class BillingAgent(BaseAgent):
    """Agent responsible for final quote confirmation and human handoff."""

    def __init__(self, chat_ctx=None):
        super().__init__(
            instructions=load_prompt("billing_prompt.yaml"),
            chat_ctx=chat_ctx,
        )

    def _quote_basis(self, userdata) -> dict:
        return {
            "service_family": userdata.service_family
            or (userdata.requested_services[0] if userdata.requested_services else "daycare"),
            "service_plan": userdata.service_plan,
            "dog_size": userdata.dog_size,
        }

    def _ensure_quote(self, userdata, force_recompute: bool = False) -> dict:
        basis = self._quote_basis(userdata)
        stored_basis = userdata.runtime_tool_facts.get("quote_basis")

        should_recompute = force_recompute or (
            userdata.quoted_subtotal is None
            or userdata.quoted_tax is None
            or userdata.quoted_total is None
            or stored_basis != basis
        )

        if should_recompute:
            quote = compute_selection_quote(
                service_family=basis["service_family"],
                service_plan=basis["service_plan"],
                dog_size=basis["dog_size"],
            )
            userdata.quoted_subtotal = float(quote["subtotal"])
            userdata.quoted_tax = float(quote["tax"])
            userdata.quoted_total = float(quote["total"])
            userdata.quote_notes = str(quote["quote_notes"])
            userdata.total_amount = float(quote["total"])
            userdata.runtime_tool_facts["quote_basis"] = basis
            userdata.runtime_tool_facts["active_quote"] = quote

        if not userdata.handoff_status:
            userdata.handoff_status = "pending"

        service_label = get_service_display_label(
            basis["service_family"],
            basis["service_plan"],
        )
        return {
            "service_label": service_label,
            "billing_cycle": userdata.runtime_tool_facts.get("active_quote", {}).get(
                "billing_cycle", "per_visit"
            ),
        }

    async def on_enter(self) -> None:
        """Announce total and ask user for confirmation before sending handoff."""
        userdata = self.session.userdata
        trace_id = ensure_session_trace_id(userdata)
        pending = getattr(userdata, "handoff_pending_action", None)
        userdata.handoff_pending_action = None

        quote_meta = self._ensure_quote(userdata, force_recompute=True)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="billing.on_enter",
            pending_action=pending,
            quote_basis=userdata.runtime_tool_facts.get("quote_basis"),
            quote=userdata.runtime_tool_facts.get("active_quote"),
        )

        await self.session.say(
            f"You're all set for {quote_meta['service_label']}. "
            f"Your total is ${userdata.quoted_total:.2f} ({quote_meta['billing_cycle']}). "
            "If that looks good, I can send your full request to our team now."
        )

    @function_tool
    async def calculate_total(self, context: RunContext_T) -> str:
        """Show quote breakdown for the current request."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.calculate_total.call",
        )
        quote_meta = self._ensure_quote(userdata, force_recompute=True)

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_STATE",
            trace_id=trace_id,
            message="tool.calculate_total.state_diff",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        return (
            "COST_BREAKDOWN:\n"
            f"Service: {quote_meta['service_label']}\n"
            f"Billing Cycle: {quote_meta['billing_cycle']}\n"
            f"Subtotal: ${userdata.quoted_subtotal:.2f}\n"
            f"Tax: ${userdata.quoted_tax:.2f}\n"
            f"TOTAL: ${userdata.quoted_total:.2f}\n"
            f"Notes: {userdata.quote_notes}\n"
            "Tell user: Read the total and ask if they want you to send this request now."
        )

    @function_tool
    async def send_structured_handoff(
        self,
        context: RunContext_T,
        notes: str = "",
    ) -> str:
        """Send structured session state to staff via SMTP handoff email."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.send_structured_handoff.call",
            notes=notes,
        )

        self._ensure_quote(userdata, force_recompute=False)

        if notes.strip():
            extra_notes = notes.strip()
            userdata.quote_notes = (
                f"{userdata.quote_notes} | Additional caller note: {extra_notes}"
                if userdata.quote_notes
                else f"Additional caller note: {extra_notes}"
            )

        payload = build_handoff_payload(userdata)
        userdata.handoff_status = "pending"

        try:
            result = send_handoff_email(payload)
        except ValueError as exc:
            userdata.handoff_status = "failed"
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="tool.send_structured_handoff.config_error",
                error=str(exc),
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )
            return (
                f"HANDOFF_CONFIG_ERROR: {exc}. "
                "Tell user: I have your request, but there is a system configuration issue. "
                "A team member will follow up shortly."
            )
        except RuntimeError as exc:
            userdata.handoff_status = "failed"
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_STATE",
                trace_id=trace_id,
                message="tool.send_structured_handoff.send_error",
                error=str(exc),
                changes=userdata_diff(before, userdata_snapshot(userdata)),
            )
            return (
                f"HANDOFF_SEND_FAILED: {exc}. "
                "Tell user: I could not send the handoff right now, but your request is saved and "
                "our team will still follow up."
            )

        userdata.handoff_status = "sent"
        userdata.payment_status = "pending_human_followup"
        userdata.runtime_tool_facts["handoff_email"] = {
            "message_id": result["message_id"],
            "subject": result["subject"],
        }

        trace_log(
            logger=logger,
            flag_name="HH_TRACE_STATE",
            trace_id=trace_id,
            message="tool.send_structured_handoff.state_diff",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )

        await self.session.say(
            "Done. I sent your full request details to our team for follow-up."
        )

        return (
            f"HANDOFF_SENT: Message ID {result['message_id']}. "
            f"Subject: {result['subject']}. "
            "Tell user: Everything is submitted and staff will contact them shortly."
        )

    @function_tool
    async def mark_handoff_pending(
        self,
        context: RunContext_T,
        reason: str = "Customer is still deciding",
    ) -> str:
        """Mark session as pending when caller is not ready to finalize yet."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        userdata.handoff_status = "pending"
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_TOOLS",
            trace_id=trace_id,
            message="tool.mark_handoff_pending",
            reason=reason,
        )
        return (
            f"HANDOFF_PENDING: {reason}. "
            "Tell user: No problem, you can continue whenever ready."
        )

    @function_tool
    async def return_to_frontdesk(self, context: RunContext_T) -> BaseAgent:
        """Return customer to front desk for additional help."""
        userdata = context.userdata
        trace_id = ensure_session_trace_id(userdata)
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="billing.return_to_frontdesk",
            from_agent="BillingAgent",
            to_agent="FrontDeskAgent",
            summary=userdata.summarize(),
        )
        await self.session.say(
            "Sure, I'll transfer you back to the front desk for anything else you need."
        )

        frontdesk = context.userdata.personas.get("frontdesk")
        if frontdesk:
            from agents.frontdesk_agent import FrontDeskAgent

            return FrontDeskAgent(chat_ctx=self.chat_ctx)
        return "ERROR: FrontDesk agent not available in personas registry."

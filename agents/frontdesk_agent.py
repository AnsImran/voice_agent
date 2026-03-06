"""Front desk agent for initial consultation and routing."""
from livekit.agents.llm import function_tool

from .base_agent import BaseAgent, RunContext_T
from utils import load_prompt


class FrontDeskAgent(BaseAgent):
    """Agent responsible for greeting customers and routing them appropriately."""
    
    def __init__(self, chat_ctx=None):
        super().__init__(
            instructions=load_prompt('frontdesk_prompt.yaml'),
            chat_ctx=chat_ctx,
        )
    
    async def on_enter(self) -> None:
        """Called when agent starts."""
        await self.session.generate_reply(
            instructions="Warmly greet the customer and introduce yourself as the front desk assistant for Happy Hound. "
            "Ask how you can help today with daycare, boarding, training, grooming, or availability questions."
        )
    
    @function_tool
    async def start_booking(self, context: RunContext_T) -> BaseAgent:
        """Start request collection mode for availability and booking.

        Phase I behavior:
        - Collect service/date/time and relevant dog details
        - Explain that staff will confirm final availability
        - Keep user in FrontDeskAgent until live API integration is added

        Args:
            context: RunContext with userdata
            
        Returns:
            FrontDeskAgent instance
        """
        await self.session.say(
            "Great, I can help with that. For now, I will collect your request and our team will confirm exact availability. "
            "Please share the service you want, your preferred date and time, and a few details about your dog."
        )
        
        return self

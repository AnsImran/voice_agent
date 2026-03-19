"""Intake agent for collecting customer profile information."""
import logging

from livekit.agents.beta.workflows import TaskGroup

from .base_agent import BaseAgent
from tasks.dog_weight_task import DogWeightTask
from tasks.name_task import NameTask
from tasks.phone_task import PhoneTask
from utils import (
    ensure_session_trace_id,
    trace_log,
    userdata_diff,
    userdata_snapshot,
)

# Legacy intake tasks retained for future use:
# from tasks.age_task import AgeTask
# from tasks.email_task import GetEmailTask
# from tasks.experience_task import ExperienceTask

logger = logging.getLogger("doheny-surf-desk.intake")


class IntakeAgent(BaseAgent):
    """Agent responsible for collecting customer profile information via sequential tasks."""
    
    def __init__(self, chat_ctx=None):
        # Note: We need LLM for tasks to use session.generate_reply()
        # Tasks will use the session's LLM, not the agent's LLM
        agent_kwargs = {
            "instructions": (
                "You collect customer profile information using sequential tasks. "
                "The tasks handle all communication. Keep prompts natural for spoken delivery, "
                "ask one thing at a time, and avoid list formatting."
            ),
            "chat_ctx": chat_ctx,
        }
        # Keep Intake on session-level TTS because TaskGroup task utterances also
        # synthesize with session defaults; this avoids mid-intake voice flips.
        super().__init__(**agent_kwargs)
    
    async def on_enter(self) -> None:
        """Called when agent starts - run profile collection tasks sequentially."""
        await self.session.say(
            "Hi, this is the Intake Department at Happy Hound. "
            "I'll collect a few quick details to get your booking started."
        )

        # Create TaskGroup for sequential profile collection
        task_group = TaskGroup()
        
        task_group.add(
            lambda: NameTask(),
            id="name_task",
            description="Collects customer's full name"
        )
        
        task_group.add(
            lambda: PhoneTask(),
            id="phone_task",
            description="Collects phone number with confirmation"
        )

        task_group.add(
            lambda: DogWeightTask(),
            id="dog_weight_task",
            description="Collects dog weight and size tier"
        )

        # Execute all tasks sequentially
        # Note: This will wait for user input for each task
        results = await task_group
        task_results = results.task_results
        
        # Update userdata from task results
        userdata = self.session.userdata
        trace_id = ensure_session_trace_id(userdata)
        before = userdata_snapshot(userdata)
        userdata.name = task_results["name_task"].name
        userdata.phone = task_results["phone_task"].phone
        userdata.dog_weight_lbs = task_results["dog_weight_task"].weight_lbs
        userdata.dog_size = task_results["dog_weight_task"].dog_size
        userdata.handoff_pending_action = "scheduler_on_enter"
        userdata.selection_source = userdata.selection_source or "intake.tasks"
        userdata.runtime_tool_facts["intake_profile"] = {
            "name": userdata.name,
            "phone": userdata.phone,
            "dog_weight_lbs": userdata.dog_weight_lbs,
            "dog_size": userdata.dog_size,
        }

        await self.session.say(
            "Your details have been recorded. "
            "I'm now transferring your call to our Scheduling Department to check availability. "
            "Kindly wait a moment while I connect you."
        )
        
        # Transfer to scheduler agent with chat context
        from agents.scheduler_agent import SchedulerAgent
        trace_log(
            logger=logger,
            flag_name="HH_TRACE_HANDOFFS",
            trace_id=trace_id,
            message="intake.complete_handoff",
            from_agent="IntakeAgent",
            to_agent="SchedulerAgent",
            changes=userdata_diff(before, userdata_snapshot(userdata)),
        )
        self.session.update_agent(SchedulerAgent(chat_ctx=self.chat_ctx))


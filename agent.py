"""
---
title: Happy Hound Booking Agent
category: complex-agents
tags: [multi_agent, tasks, observer_pattern, llm_evaluation, context_injection, phone_receptionist]
difficulty: advanced
description: Voice receptionist workflow for Happy Hound with parallel hallucination observer and structured handoff
requires: livekit-agents>=1.3.0
demonstrates:
  - FrontDesk -> Intake -> Scheduler handoff pattern
  - Background observer for hallucination/fact-check monitoring
  - Task-based profile collection with typed results
  - Structured session-state handoff for human follow-up
---
"""
import logging
import os
from dotenv import load_dotenv

from livekit.agents import (
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.voice import AgentSession
from livekit.plugins import silero, noise_cancellation, openai
from livekit.plugins.turn_detector.multilingual import MultilingualModel


# Import agents
from agents.base_agent import SurfBookingData
from agents.frontdesk_agent import FrontDeskAgent
from agents.intake_agent import IntakeAgent
from agents.scheduler_agent import SchedulerAgent
from agents.observer_agent import start_observer
from utils import ensure_session_trace_id, parse_env_bool, trace_log

# Load environment
load_dotenv(dotenv_path='.env')

logger = logging.getLogger("doheny-surf-desk")
DEFAULT_SESSION_TTS = "deepgram/aura-2:arcas"


def configure_runtime_logging() -> None:
    """Ensure verbose console logging is enabled for local debugging."""
    level_name = os.getenv("HH_LOG_LEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.DEBUG)

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        )
    root_logger.setLevel(level)

    # Keep package loggers explicitly aligned with the selected level.
    logging.getLogger("livekit").setLevel(level)
    logging.getLogger("livekit.agents").setLevel(level)
    logging.getLogger("doheny-surf-desk").setLevel(level)


async def entrypoint(ctx: JobContext):
    """Main entrypoint for Happy Hound booking agent.
    
    Sets up multi-agent session with:
    - FrontDeskAgent: Greets and routes callers into booking flow
    - IntakeAgent: Collects customer profile via sequential TaskGroup
    - SchedulerAgent: Checks service availability, confirms slots, and sends SMTP handoff payload
    - ObserverAgent: Monitors for hallucinated business facts (parallel)
    """
    logger.info("Starting Happy Hound booking agent in room %s", ctx.room.name)
    
    # Connect to the room
    await ctx.connect()
    
    # Initialize userdata for the session
    userdata = SurfBookingData()
    userdata.email = os.getenv("DEFAULT_CUSTOMER_EMAIL")
    trace_id = ensure_session_trace_id(userdata)
    
    # Create all agent instances
    frontdesk_agent = FrontDeskAgent()
    intake_agent = IntakeAgent()
    scheduler_agent = SchedulerAgent()
    # gear_agent = GearAgent()  # Gear flow intentionally bypassed in active path.
    # billing_agent = BillingAgent()  # Billing flow merged into Scheduler for active path.
    
    # Register all agents in userdata for handoffs
    userdata.personas = {
        "frontdesk": frontdesk_agent,
        "intake": intake_agent,
        "scheduler": scheduler_agent,
        # "gear": gear_agent,  # Keep disabled but available for future re-enable.
        # "billing": billing_agent,  # Kept disabled in active path.
    }
    trace_log(
        logger=logger,
        flag_name="HH_TRACE_HANDOFFS",
        trace_id=trace_id,
        message="entrypoint.personas_initialized",
        personas=list(userdata.personas.keys()),
    )
    
    # Create the agent session with LiveKit inference gateway
    session = AgentSession[SurfBookingData](
        userdata=userdata,
        vad=silero.VAD.load(),
        stt="deepgram/nova-2",
        llm="openai/gpt-4o",
        tts=os.getenv("SESSION_TTS", DEFAULT_SESSION_TTS),
        turn_detection=MultilingualModel(),
    )

    # Start observer in parallel using a stronger model for fact-check evaluation.
    llm = openai.LLM(model="gpt-5")
    await start_observer(session, llm)
    trace_log(
        logger=logger,
        flag_name="HH_TRACE_HANDOFFS",
        trace_id=trace_id,
        message="entrypoint.observer_started",
        observer_model="gpt-5",
    )
    
    # Start the session with FrontDeskAgent
    logger.info("Starting session with FrontDeskAgent")
    await session.start(
        agent=frontdesk_agent, # You can change the starting agent here to debug some specific part of the workflow
        room=ctx.room,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )

    if parse_env_bool("HH_ENABLE_BACKGROUND_AUDIO", default=True):
        ambient_volume = 0.15
        try:
            ambient_volume = float(os.getenv("HH_BACKGROUND_AUDIO_VOLUME", "0.15"))
        except ValueError:
            ambient_volume = 0.15

        try:
            background_audio = BackgroundAudioPlayer(
                ambient_sound=AudioConfig(
                    BuiltinAudioClip.OFFICE_AMBIENCE,
                    volume=max(0.0, min(ambient_volume, 1.0)),
                )
            )
            await background_audio.start(room=ctx.room, agent_session=session)
            trace_log(
                logger=logger,
                flag_name="HH_TRACE_HANDOFFS",
                trace_id=trace_id,
                message="entrypoint.background_audio_started",
                clip="OFFICE_AMBIENCE",
                volume=ambient_volume,
            )
        except Exception as exc:
            logger.warning("Background audio could not be started: %s", exc)

if __name__ == "__main__":
    configure_runtime_logging()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


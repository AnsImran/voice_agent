"""Dog weight collection task."""
from dataclasses import dataclass

from livekit.agents import AgentTask, RunContext
from livekit.agents.llm.tool_context import function_tool
from livekit.agents.voice import SpeechHandle


def derive_dog_size_from_weight(weight_lbs: float) -> str:
    """Map dog weight to Happy Hound size tiers."""
    if weight_lbs <= 19:
        return "small"
    if weight_lbs <= 60:
        return "medium"
    if weight_lbs <= 100:
        return "large"
    return "x-large"


@dataclass
class DogWeightResult:
    """Result from dog weight collection."""

    weight_lbs: float
    dog_size: str


class DogWeightTask(AgentTask[DogWeightResult]):
    """Task to collect dog weight in pounds and derive size tier."""

    def __init__(self):
        super().__init__(
            instructions="""You are collecting the dog's weight in pounds (lbs).
Ask for weight naturally. When they provide it:
1. Call record_dog_weight()
2. Read back the weight and derived size
3. Ask for explicit confirmation
4. When confirmed, call confirm_dog_weight()

CRITICAL: Never call confirm_dog_weight() in the same turn as record_dog_weight().
Wait for explicit user confirmation.
After calling confirm_dog_weight(), do not add any closing message. End the task immediately."""
        )
        self._weight_lbs: float | None = None
        self._dog_size: str | None = None
        self._record_handle: SpeechHandle | None = None

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Ask for the dog's current weight in pounds."
        )

    @function_tool()
    async def record_dog_weight(self, weight_lbs: float, ctx: RunContext) -> str:
        """Record weight and compute size tier."""
        self._record_handle = ctx.speech_handle

        if weight_lbs < 2:
            return (
                f"Weight {weight_lbs} lbs seems too low. Please ask them to repeat the "
                "weight in pounds."
            )
        if weight_lbs > 300:
            return (
                f"Weight {weight_lbs} lbs seems too high. Please double-check the value."
            )

        self._weight_lbs = round(weight_lbs, 1)
        self._dog_size = derive_dog_size_from_weight(self._weight_lbs)
        return (
            f"Dog weight recorded: {self._weight_lbs} lbs ({self._dog_size}). "
            f"Say: 'Great, I have {self._weight_lbs} pounds, which maps to our "
            f"{self._dog_size} size tier. Is that correct?' "
            "DO NOT call confirm_dog_weight yet."
        )

    @function_tool()
    async def confirm_dog_weight(self, ctx: RunContext) -> str | None:
        """Confirm weight after explicit user confirmation."""
        await ctx.wait_for_playout()

        if ctx.speech_handle == self._record_handle:
            return "Do not confirm yet. Ask the caller to explicitly confirm the weight first."

        if self._weight_lbs is None or self._dog_size is None:
            return "No dog weight is recorded yet. Ask for the dog's weight in pounds first."

        self.complete(
            DogWeightResult(
                weight_lbs=self._weight_lbs,
                dog_size=self._dog_size,
            )
        )

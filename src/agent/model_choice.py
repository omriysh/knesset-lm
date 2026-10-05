"""The Gemini/Gemma model of each part of a research run. The web visitor picks them (no server defaults);
from_config is the default for code that runs the agent without a visitor (tests, scripts)."""

import re
from dataclasses import asdict, dataclass, fields

import config

MODEL_ID_PATTERN = re.compile(r"^(gemini|gemma)-[a-z0-9.\-]{1,60}$")


@dataclass(frozen=True)
class ResearchModels:
    intent: str          # outer machine's intent classification; also the plan validator
    planner: str
    critic: str          # before and after execution
    executor: str
    synthesizer: str     # writes the sourced answer
    answer_editor: str   # outer machine's final formatting of the answer (the browser also uses it for the meeting chat)

    @classmethod
    def from_config(cls) -> "ResearchModels":
        return cls(
            intent=config.INTENT_MODEL,
            planner=config.PLANNER_MODEL,
            critic=config.CRITIC_MODEL,
            executor=config.EXECUTOR_MODEL,
            synthesizer=config.SYNTHESIZER_MODEL,
            answer_editor=config.ANSWER_EDITOR_MODEL,
        )

    @classmethod
    def from_dict(cls, models: dict) -> "ResearchModels":
        return cls(**{field.name: models[field.name] for field in fields(cls)})

    def to_dict(self) -> dict:
        return asdict(self)

    def model_ids(self) -> set[str]:
        return set(asdict(self).values())

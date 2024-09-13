from abc import ABC, abstractmethod
from typing import Any, Dict

from datahub.ingestion.api.common import PipelineContext


class PipelineRunListener(ABC):
    @abstractmethod
    def on_start(self, ctx: PipelineContext) -> None:
        # Perform
        pass

    @abstractmethod
    def on_completion(
        self,
        status: str,
        report: Dict[str, Any],
        ctx: PipelineContext,
    ) -> None:
        pass

    @classmethod
    @abstractmethod
    def create(cls, config_dict: dict, ctx: PipelineContext) -> "PipelineRunListener":
        # Creation and initialization.
        pass

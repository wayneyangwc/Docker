import logging
from datetime import datetime, timezone
from functools import partial
from typing import Dict, Iterable, List, Optional

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.api.decorators import (
    SupportStatus,
    config_class,
    platform_name,
    support_status,
)
from datahub.ingestion.api.source import MetadataWorkUnitProcessor, SourceReport
from datahub.ingestion.api.source_helpers import auto_workunit_reporter
from datahub.ingestion.api.workunit import MetadataWorkUnit
from datahub.ingestion.source.datahub.config import DataHubSourceConfig
from datahub.ingestion.source.datahub.datahub_api_reader import DataHubApiReader
from datahub.ingestion.source.datahub.datahub_database_reader import (
    DataHubDatabaseReader,
)
from datahub.ingestion.source.datahub.datahub_kafka_reader import DataHubKafkaReader
from datahub.ingestion.source.datahub.report import DataHubSourceReport
from datahub.ingestion.source.datahub.state import StatefulDataHubIngestionHandler
from datahub.ingestion.source.state.stateful_ingestion_base import (
    StatefulIngestionSourceBase,
)
from datahub.metadata.schema_classes import ChangeTypeClass

logger = logging.getLogger(__name__)


@platform_name("DataHub")
@config_class(DataHubSourceConfig)
@support_status(SupportStatus.TESTING)
class DataHubSource(StatefulIngestionSourceBase):
    platform: str = "datahub"

    def __init__(self, config: DataHubSourceConfig, ctx: PipelineContext):
        super().__init__(config, ctx)
        self.config = config
        self.report: DataHubSourceReport = DataHubSourceReport()
        self.stateful_ingestion_handler = StatefulDataHubIngestionHandler(self)

    @classmethod
    def create(cls, config_dict: Dict, ctx: PipelineContext) -> "DataHubSource":
        config: DataHubSourceConfig = DataHubSourceConfig.parse_obj(config_dict)
        return cls(config, ctx)

    def get_report(self) -> SourceReport:
        return self.report

    def get_workunit_processors(self) -> List[Optional[MetadataWorkUnitProcessor]]:
        # Exactly replicate data from DataHub source
        return [partial(auto_workunit_reporter, self.get_report())]

    def get_workunits_internal(self) -> Iterable[MetadataWorkUnit]:
        self.report.stop_time = datetime.now(tz=timezone.utc)
        logger.info(f"Ingesting DataHub metadata up until {self.report.stop_time}")
        state = self.stateful_ingestion_handler.get_last_run_state()

        if self.config.pull_from_datahub_api:
            yield from self._get_api_workunits()

        if self.config.database_connection is not None:
            yield from self._get_database_workunits(
                from_createdon=state.database_createdon_datetime
            )
            self._commit_progress()
        else:
            logger.info(
                "Skipping ingestion of versioned aspects as no database_connection provided"
            )

        if self.config.kafka_connection is not None:
            yield from self._get_kafka_workunits(from_offsets=state.kafka_offsets)
            self._commit_progress()
        else:
            logger.info(
                "Skipping ingestion of timeseries aspects as no kafka_connection provided"
            )

    def _get_database_workunits(
        self, from_createdon: datetime
    ) -> Iterable[MetadataWorkUnit]:
        if self.config.database_connection is None:
            return

        logger.info(f"Fetching database aspects starting from {from_createdon}")
        reader = DataHubDatabaseReader(
            self.config, self.config.database_connection, self.report
        )
        mcps = reader.get_aspects(from_createdon, self.report.stop_time)
        for i, (mcp, createdon) in enumerate(mcps):
            yield mcp.as_workunit()
            self.report.num_database_aspects_ingested += 1

            if (
                self.config.commit_with_parse_errors
                or not self.report.num_database_parse_errors
            ):
                self.stateful_ingestion_handler.update_checkpoint(
                    last_createdon=createdon
                )
            self._commit_progress(i)

    def _get_kafka_workunits(
        self, from_offsets: Dict[int, int]
    ) -> Iterable[MetadataWorkUnit]:
        if self.config.kafka_connection is None:
            return

        logger.info("Fetching timeseries aspects from kafka")
        with DataHubKafkaReader(
            self.config, self.config.kafka_connection, self.report, self.ctx
        ) as reader:
            mcls = reader.get_mcls(
                from_offsets=from_offsets, stop_time=self.report.stop_time
            )
            for i, (mcl, offset) in enumerate(mcls):
                mcp = MetadataChangeProposalWrapper.try_from_mcl(mcl)
                if mcp.changeType == ChangeTypeClass.DELETE:
                    self.report.num_timeseries_deletions_dropped += 1
                    logger.debug(
                        f"Dropping timeseries deletion of {mcp.aspectName} on {mcp.entityUrn}"
                    )
                    continue

                if isinstance(mcp, MetadataChangeProposalWrapper):
                    yield mcp.as_workunit()
                else:
                    yield MetadataWorkUnit(
                        id=f"{mcp.entityUrn}-{mcp.aspectName}-{i}", mcp_raw=mcp
                    )
                self.report.num_kafka_aspects_ingested += 1

                if (
                    self.config.commit_with_parse_errors
                    or not self.report.num_kafka_parse_errors
                ):
                    self.stateful_ingestion_handler.update_checkpoint(
                        last_offset=offset
                    )
                self._commit_progress(i)

    def _get_api_workunits(self) -> Iterable[MetadataWorkUnit]:
        if self.ctx.graph is None:
            self.report.report_failure(
                "datahub_api",
                "Specify datahub_api on your ingestion recipe to ingest from the DataHub API",
            )
            return

        reader = DataHubApiReader(self.config, self.report, self.ctx.graph)
        for mcp in reader.get_aspects():
            yield mcp.as_workunit()

    def _commit_progress(self, i: Optional[int] = None) -> None:
        """Commit progress to stateful storage, if there have been no errors.

        If an index `i` is provided, only commit if we are at the appropriate interval
        as per `config.commit_state_interval`.
        """
        on_interval = (
            i
            and self.config.commit_state_interval
            and i % self.config.commit_state_interval == 0
        )

        if i is None or on_interval:
            self.stateful_ingestion_handler.commit_checkpoint()

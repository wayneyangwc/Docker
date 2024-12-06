import html
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from pydantic.fields import Field
from tableauserverclient import Server

import datahub.emitter.mce_builder as builder
from datahub.configuration.common import ConfigModel
from datahub.ingestion.source import tableau_constant as c
from datahub.metadata.com.linkedin.pegasus2avro.dataset import (
    DatasetLineageType,
    FineGrainedLineage,
    FineGrainedLineageDownstreamType,
    FineGrainedLineageUpstreamType,
)
from datahub.metadata.com.linkedin.pegasus2avro.schema import (
    ArrayTypeClass,
    BooleanTypeClass,
    DateTypeClass,
    NullTypeClass,
    NumberTypeClass,
    SchemaField,
    SchemaFieldDataType,
    StringTypeClass,
    TimeTypeClass,
)
from datahub.metadata.schema_classes import (
    BytesTypeClass,
    GlobalTagsClass,
    TagAssociationClass,
    UpstreamClass,
)
from datahub.sql_parsing.sqlglot_lineage import ColumnLineageInfo, SqlParsingResult

logger = logging.getLogger(__name__)


class TableauLineageOverrides(ConfigModel):
    platform_override_map: Optional[Dict[str, str]] = Field(
        default=None,
        description="A holder for platform -> platform mappings to generate correct dataset urns",
    )
    database_override_map: Optional[Dict[str, str]] = Field(
        default=None,
        description="A holder for database -> database mappings to generate correct dataset urns",
    )


class MetadataQueryException(Exception):
    pass


workbook_graphql_query = """
    {
      id
      name
      luid
      uri
      projectName
      owner {
        username
      }
      description
      uri
      createdAt
      updatedAt
      tags {
          name
      }
      sheets {
        id
      }
      dashboards {
        id
      }
      embeddedDatasources {
        id
      }
    }
"""

sheet_graphql_query = """
{
    id
    name
    path
    luid
    createdAt
    updatedAt
    tags {
        name
    }
    containedInDashboards {
        name
        path
    }
    workbook {
        id
        name
        projectName
        luid
        owner {
          username
        }
    }
    datasourceFields {
        __typename
        id
        name
        description
        datasource {
        id
        name
        }
        ... on ColumnField {
        dataCategory
        role
        dataType
        aggregation
        }
        ... on CalculatedField {
        role
        dataType
        aggregation
        formula
        }
        ... on GroupField {
        role
        dataType
        }
        ... on DatasourceField {
        remoteField {
            __typename
            id
            name
            description
            folderName
            ... on ColumnField {
            dataCategory
            role
            dataType
            aggregation
            }
            ... on CalculatedField {
            role
            dataType
            aggregation
            formula
            }
            ... on GroupField {
            role
            dataType
            }
        }
        }
    }
}
"""

dashboard_graphql_query = """
{
    id
    name
    path
    luid
    createdAt
    updatedAt
    sheets {
        id
        name
    }
    tags {
        name
    }
    workbook {
        id
        name
        projectName
        luid
        owner {
          username
        }
    }
}
"""

embedded_datasource_graphql_query = """
{
    __typename
    id
    name
    hasExtracts
    extractLastRefreshTime
    extractLastIncrementalUpdateTime
    extractLastUpdateTime
    downstreamSheets {
        name
        id
    }
    upstreamTables {
        id
        name
        database {
            name
        }
        schema
        fullName
        connectionType
        description
        columnsConnection {
            totalCount
        }
    }
    fields {
        __typename
        id
        name
        description
        isHidden
        folderName
        upstreamFields {
            name
            datasource {
                id
            }
        }
        upstreamColumns {
            name
            table {
                __typename
                id
            }
        }
        ... on ColumnField {
            dataCategory
            role
            dataType
            defaultFormat
            aggregation
        }
        ... on CalculatedField {
            role
            dataType
            defaultFormat
            aggregation
            formula
        }
        ... on GroupField {
            role
            dataType
        }
    }
    upstreamDatasources {
        id
        name
    }
    workbook {
        id
        name
        projectName
        luid
        owner {
          username
        }
    }
}
"""

custom_sql_graphql_query = """
{
      id
      name
      query
      columns {
        id
        name
        remoteType
        description
        referencedByFields {
          datasource {
            __typename
            id
            name
            upstreamTables {
              id
              name
              database {
                name
              }
              schema
              fullName
              connectionType
            }
            ... on PublishedDatasource {
              projectName
              luid
            }
            ... on EmbeddedDatasource {
              workbook {
                id
                name
                projectName
                luid
              }
            }
          }
        }
      }
      tables {
        id
        name
        database {
          name
        }
        schema
        fullName
        connectionType
        description
        columnsConnection {
            totalCount
        }
      }
      connectionType
      database{
        name
        connectionType
      }
}
"""

published_datasource_graphql_query = """
{
    __typename
    id
    name
    luid
    hasExtracts
    extractLastRefreshTime
    extractLastIncrementalUpdateTime
    extractLastUpdateTime
    upstreamTables {
      id
      name
      database {
        name
      }
      schema
      fullName
      connectionType
      description
      columnsConnection {
            totalCount
        }
    }
    fields {
        __typename
        id
        name
        description
        isHidden
        folderName
        upstreamFields {
            name
            datasource {
                id
            }
        }
        upstreamColumns {
            name
            table {
                __typename
                id
            }
        }
        ... on ColumnField {
            dataCategory
            role
            dataType
            defaultFormat
            aggregation
            }
        ... on CalculatedField {
            role
            dataType
            defaultFormat
            aggregation
            formula
            }
        ... on GroupField {
            role
            dataType
            }
    }
    owner {
      username
    }
    description
    uri
    projectName
    tags {
        name
    }
}
        """

database_tables_graphql_query = """
{
    id
    isEmbedded
    columns {
      remoteType
      name
    }
}
"""

# https://referencesource.microsoft.com/#system.data/System/Data/OleDb/OLEDB_Enum.cs,364
FIELD_TYPE_MAPPING = {
    "INTEGER": NumberTypeClass,
    "REAL": NumberTypeClass,
    "STRING": StringTypeClass,
    "DATETIME": TimeTypeClass,
    "DATE": DateTypeClass,
    "TUPLE": ArrayTypeClass,
    "SPATIAL": NullTypeClass,
    "BOOLEAN": BooleanTypeClass,
    "TABLE": ArrayTypeClass,
    "UNKNOWN": NullTypeClass,
    "EMPTY": NullTypeClass,
    "NULL": NullTypeClass,
    "I2": NumberTypeClass,
    "I4": NumberTypeClass,
    "R4": NumberTypeClass,
    "R8": NumberTypeClass,
    "CY": NumberTypeClass,
    "BSTR": StringTypeClass,
    "IDISPATCH": NullTypeClass,
    "ERROR": NullTypeClass,
    "BOOL": BooleanTypeClass,
    "VARIANT": NullTypeClass,
    "IUNKNOWN": NullTypeClass,
    "DECIMAL": NumberTypeClass,
    "UI1": NumberTypeClass,
    "ARRAY": ArrayTypeClass,
    "BYREF": StringTypeClass,
    "I1": NumberTypeClass,
    "UI2": NumberTypeClass,
    "UI4": NumberTypeClass,
    "I8": NumberTypeClass,
    "UI8": NumberTypeClass,
    "GUID": StringTypeClass,
    "VECTOR": ArrayTypeClass,
    "FILETIME": TimeTypeClass,
    "RESERVED": NullTypeClass,
    "BYTES": BytesTypeClass,
    "STR": StringTypeClass,
    "WSTR": StringTypeClass,
    "NUMERIC": NumberTypeClass,
    "UDT": StringTypeClass,
    "DBDATE": DateTypeClass,
    "DBTIME": TimeTypeClass,
    "DBTIMESTAMP": TimeTypeClass,
    "HCHAPTER": NullTypeClass,
    "PROPVARIANT": NullTypeClass,
    "VARNUMERIC": NumberTypeClass,
    "WDC_INT": NumberTypeClass,
    "WDC_FLOAT": NumberTypeClass,
    "WDC_STRING": StringTypeClass,
    "WDC_DATETIME": TimeTypeClass,
    "WDC_BOOL": BooleanTypeClass,
    "WDC_DATE": DateTypeClass,
    "WDC_GEOMETRY": NullTypeClass,
}


def get_tags_from_params(params: List[str] = []) -> GlobalTagsClass:
    tags = [
        TagAssociationClass(tag=builder.make_tag_urn(tag.upper()))
        for tag in params
        if tag
    ]
    return GlobalTagsClass(tags=tags)


def tableau_field_to_schema_field(field, ingest_tags):
    nativeDataType = field.get("dataType", "UNKNOWN")
    TypeClass = FIELD_TYPE_MAPPING.get(nativeDataType, NullTypeClass)

    schema_field = SchemaField(
        fieldPath=field["name"],
        type=SchemaFieldDataType(type=TypeClass()),
        description=make_description_from_params(
            field.get("description", ""), field.get("formula")
        ),
        nativeDataType=nativeDataType,
        globalTags=(
            get_tags_from_params(
                [
                    field.get("role", ""),
                    field.get("__typename", ""),
                    field.get("aggregation", ""),
                ]
            )
            if ingest_tags
            else None
        ),
    )

    return schema_field


@lru_cache(128)
def get_platform(connection_type: str) -> str:
    # connection_type taken from
    # https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_concepts_connectiontype.htm
    #  datahub platform mapping is found here
    # https://github.com/datahub-project/datahub/blob/master/metadata-service/war/src/main/resources/boot/data_platforms.json

    if connection_type in ("textscan", "textclean", "excel-direct", "excel", "csv"):
        platform = "external"
    elif connection_type in (
        "hadoophive",
        "hive",
        "hortonworkshadoophive",
        "maprhadoophive",
        "awshadoophive",
    ):
        platform = "hive"
    elif connection_type in ("mysql_odbc", "mysql"):
        platform = "mysql"
    elif connection_type in ("webdata-direct:oracle-eloqua", "oracle"):
        platform = "oracle"
    elif connection_type in ("tbio", "teradata"):
        platform = "teradata"
    elif connection_type in ("sqlserver"):
        platform = "mssql"
    elif connection_type in ("athena"):
        platform = "athena"
    elif connection_type.endswith("_jdbc"):
        # e.g. convert trino_jdbc -> trino
        platform = connection_type[: -len("_jdbc")]
    else:
        platform = connection_type
    return platform


@lru_cache(128)
def get_fully_qualified_table_name(
    platform: str,
    upstream_db: str,
    schema: str,
    table_name: str,
) -> str:
    if platform == "athena":
        upstream_db = ""
    database_name = f"{upstream_db}." if upstream_db else ""
    final_name = table_name.replace("[", "").replace("]", "")

    schema_name = f"{schema}." if schema else ""

    fully_qualified_table_name = f"{database_name}{schema_name}{final_name}"

    # do some final adjustments on the fully qualified table name to help them line up with source systems:
    # lowercase it
    fully_qualified_table_name = fully_qualified_table_name.lower()
    # strip double quotes, escaped double quotes and backticks
    fully_qualified_table_name = (
        fully_qualified_table_name.replace('\\"', "")
        .replace('"', "")
        .replace("\\", "")
        .replace("`", "")
    )

    if platform in ("athena", "hive", "mysql", "clickhouse"):
        # it two tier database system (athena, hive, mysql), just take final 2
        fully_qualified_table_name = ".".join(
            fully_qualified_table_name.split(".")[-2:]
        )
    else:
        # if there are more than 3 tokens, just take the final 3
        fully_qualified_table_name = ".".join(
            fully_qualified_table_name.split(".")[-3:]
        )

    return fully_qualified_table_name


@dataclass
class TableauUpstreamReference:
    database: Optional[str]
    schema: Optional[str]
    table: str

    connection_type: str

    @classmethod
    def create(
        cls, d: dict, default_schema_map: Optional[Dict[str, str]] = None
    ) -> "TableauUpstreamReference":
        # Values directly from `table` object from Tableau
        database = t_database = d.get(c.DATABASE, {}).get(c.NAME)
        schema = t_schema = d.get(c.SCHEMA)
        table = t_table = d.get(c.NAME) or ""
        t_full_name = d.get(c.FULL_NAME)
        t_connection_type = d[c.CONNECTION_TYPE]  # required to generate urn
        t_id = d[c.ID]

        parsed_full_name = cls.parse_full_name(t_full_name)
        if parsed_full_name and len(parsed_full_name) == 3:
            database, schema, table = parsed_full_name
        elif parsed_full_name and len(parsed_full_name) == 2:
            schema, table = parsed_full_name
        else:
            logger.debug(
                f"Upstream urn generation ({t_id}):"
                f"  Did not parse full name {t_full_name}: unexpected number of values",
            )

        if not schema and default_schema_map and database in default_schema_map:
            schema = default_schema_map[database]

        if database != t_database:
            logger.debug(
                f"Upstream urn generation ({t_id}):"
                f" replacing database {t_database} with {database} from full name {t_full_name}"
            )
        if schema != t_schema:
            logger.debug(
                f"Upstream urn generation ({t_id}):"
                f" replacing schema {t_schema} with {schema} from full name {t_full_name}"
            )
        if table != t_table:
            logger.debug(
                f"Upstream urn generation ({t_id}):"
                f" replacing table {t_table} with {table} from full name {t_full_name}"
            )

        # TODO: See if we can remove this -- made for redshift
        if (
            schema
            and t_table
            and t_full_name
            and t_table == t_full_name
            and schema in t_table
        ):
            logger.debug(
                f"Omitting schema for upstream table {t_id}, schema included in table name"
            )
            schema = ""

        return cls(
            database=database,
            schema=schema,
            table=table,
            connection_type=t_connection_type,
        )

    @staticmethod
    def parse_full_name(full_name: Optional[str]) -> Optional[List[str]]:
        # fullName is observed to be in formats:
        #  [database].[schema].[table]
        #  [schema].[table]
        #  [table]
        #  table
        #  schema

        # TODO: Validate the startswith check. Currently required for our integration tests
        if full_name is None or not full_name.startswith("["):
            return None

        return full_name.replace("[", "").replace("]", "").split(".")

    def make_dataset_urn(
        self,
        env: str,
        platform_instance_map: Optional[Dict[str, str]],
        lineage_overrides: Optional[TableauLineageOverrides] = None,
    ) -> str:
        (
            upstream_db,
            platform_instance,
            platform,
            original_platform,
        ) = get_overridden_info(
            connection_type=self.connection_type,
            upstream_db=self.database,
            lineage_overrides=lineage_overrides,
            platform_instance_map=platform_instance_map,
        )

        table_name = get_fully_qualified_table_name(
            original_platform,
            upstream_db or "",
            self.schema,
            self.table,
        )

        return builder.make_dataset_urn_with_platform_instance(
            platform, table_name, platform_instance, env
        )


def get_overridden_info(
    connection_type: Optional[str],
    upstream_db: Optional[str],
    platform_instance_map: Optional[Dict[str, str]],
    lineage_overrides: Optional[TableauLineageOverrides] = None,
) -> Tuple[Optional[str], Optional[str], str, str]:
    original_platform = platform = get_platform(connection_type)
    if (
        lineage_overrides is not None
        and lineage_overrides.platform_override_map is not None
        and original_platform in lineage_overrides.platform_override_map.keys()
    ):
        platform = lineage_overrides.platform_override_map[original_platform]

    if (
        lineage_overrides is not None
        and lineage_overrides.database_override_map is not None
        and upstream_db is not None
        and upstream_db in lineage_overrides.database_override_map.keys()
    ):
        upstream_db = lineage_overrides.database_override_map[upstream_db]

    platform_instance = (
        platform_instance_map.get(original_platform) if platform_instance_map else None
    )

    if original_platform in ("athena", "hive", "mysql"):  # Two tier databases
        upstream_db = None

    return upstream_db, platform_instance, platform, original_platform


def make_description_from_params(description, formula):
    """
    Generate column description
    """
    final_description = ""
    if description:
        final_description += f"{description}\n\n"
    if formula:
        final_description += f"formula: {formula}"
    return final_description


def make_upstream_class(
    parsed_result: Optional[SqlParsingResult],
) -> List[UpstreamClass]:
    upstream_tables: List[UpstreamClass] = []

    if parsed_result is None:
        return upstream_tables

    for dataset_urn in parsed_result.in_tables:
        upstream_tables.append(
            UpstreamClass(type=DatasetLineageType.TRANSFORMED, dataset=dataset_urn)
        )
    return upstream_tables


def make_fine_grained_lineage_class(
    parsed_result: Optional[SqlParsingResult],
    dataset_urn: str,
    out_columns: List[Dict[Any, Any]],
) -> List[FineGrainedLineage]:
    # 1) fine grained lineage links are case sensitive
    # 2) parsed out columns are always lower cased
    # 3) corresponding Custom SQL output columns can be in any case lower/upper/mix
    #
    # we need a map between 2 and 3 that will be used during building column level linage links (see below)
    out_columns_map = {
        col.get(c.NAME, "").lower(): col.get(c.NAME, "") for col in out_columns
    }

    fine_grained_lineages: List[FineGrainedLineage] = []

    if parsed_result is None:
        return fine_grained_lineages

    cll: List[ColumnLineageInfo] = (
        parsed_result.column_lineage if parsed_result.column_lineage is not None else []
    )

    for cll_info in cll:
        downstream = (
            [
                builder.make_schema_field_urn(
                    dataset_urn,
                    out_columns_map.get(
                        cll_info.downstream.column.lower(),
                        cll_info.downstream.column,
                    ),
                )
            ]
            if cll_info.downstream is not None
            and cll_info.downstream.column is not None
            else []
        )
        upstreams = [
            builder.make_schema_field_urn(column_ref.table, column_ref.column)
            for column_ref in cll_info.upstreams
        ]

        fine_grained_lineages.append(
            FineGrainedLineage(
                downstreamType=FineGrainedLineageDownstreamType.FIELD,
                downstreams=downstream,
                upstreamType=FineGrainedLineageUpstreamType.FIELD_SET,
                upstreams=upstreams,
            )
        )

    return fine_grained_lineages


def get_unique_custom_sql(custom_sql_list: List[dict]) -> List[dict]:
    unique_custom_sql = []
    for custom_sql in custom_sql_list:
        unique_csql = {
            "id": custom_sql.get("id"),
            "name": custom_sql.get("name"),
            # We assume that this is unsupported custom sql if "actual tables that this query references"
            # are missing from api result.
            "isUnsupportedCustomSql": True if not custom_sql.get("tables") else False,
            "query": custom_sql.get("query"),
            "connectionType": custom_sql.get("connectionType"),
            "columns": custom_sql.get("columns"),
            "tables": custom_sql.get("tables"),
            "database": custom_sql.get("database"),
        }
        datasource_for_csql = []
        for column in custom_sql.get("columns", []):
            for field in column.get("referencedByFields", []):
                datasource = field.get("datasource")
                if datasource not in datasource_for_csql and datasource is not None:
                    datasource_for_csql.append(datasource)

        unique_csql["datasources"] = datasource_for_csql
        unique_custom_sql.append(unique_csql)
    return unique_custom_sql


def clean_query(query: str) -> str:
    """
    Clean special chars in query
    """
    query = query.replace("<<", "<").replace(">>", ">").replace("\n\n", "\n")
    query = html.unescape(query)
    return query


def make_filter(filter_dict: dict) -> str:
    filter = ""
    for key, value in filter_dict.items():
        if filter:
            filter += ", "
        filter += f"{key}: {json.dumps(value)}"
    return filter


def query_metadata(
    server: Server,
    main_query: str,
    connection_name: str,
    first: int,
    offset: int,
    qry_filter: str = "",
) -> dict:
    query = """{{
        {connection_name} (first:{first}, offset:{offset}, filter:{{{filter}}})
        {{
            nodes {main_query}
            pageInfo {{
                hasNextPage
                endCursor
            }}
            totalCount
        }}
    }}""".format(
        connection_name=connection_name,
        first=first,
        offset=offset,
        filter=qry_filter,
        main_query=main_query,
    )
    return server.metadata.query(query)


def get_filter_pages(query_filter: dict, page_size: int) -> List[dict]:
    filter_pages = [query_filter]
    # If this is primary id filter so we can use divide this query list into
    # multiple requests each with smaller filter list (of order page_size).
    # It is observed in the past that if list of primary ids grow beyond
    # a few ten thousands then tableau server responds with empty response
    # causing below error:
    # tableauserverclient.server.endpoint.exceptions.NonXMLResponseError: b''
    if (
        len(query_filter.keys()) == 1
        and query_filter.get(c.ID_WITH_IN)
        and isinstance(query_filter[c.ID_WITH_IN], list)
        and len(query_filter[c.ID_WITH_IN]) > 100 * page_size
    ):
        ids = query_filter[c.ID_WITH_IN]
        filter_pages = [
            {
                c.ID_WITH_IN: ids[
                    start : (
                        start + page_size if start + page_size < len(ids) else len(ids)
                    )
                ]
            }
            for start in range(0, len(ids), page_size)
        ]

    return filter_pages

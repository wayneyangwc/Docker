from datahub.configuration.common import AllowDenyPattern

UUID_REGEX = r"[a-f0-9]{8}[-_][a-f0-9]{4}[-_][a-f0-9]{4}[-_][a-f0-9]{4}[-_][a-f0-9]{12}"


def is_schema_allowed(
    schema_pattern: AllowDenyPattern,
    schema_name: str,
    db_name: str,
    match_fully_qualified_schema_name: bool,
) -> bool:
    if match_fully_qualified_schema_name:
        return schema_pattern.allowed(f"{db_name}.{schema_name}")
    else:
        return schema_pattern.allowed(schema_name)

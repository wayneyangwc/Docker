import logging
from typing import Any, Dict, Optional

import pydantic
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from snowflake.connector.network import (
    DEFAULT_AUTHENTICATOR,
    EXTERNAL_BROWSER_AUTHENTICATOR,
    KEY_PAIR_AUTHENTICATOR,
    OAUTH_AUTHENTICATOR,
)

from datahub.configuration.common import AllowDenyPattern, ConfigModel
from datahub.configuration.oauth import OAuthConfiguration, OAuthIdentityProvider
from datahub.configuration.time_window_config import BaseTimeWindowConfig
from datahub.configuration.validate_field_rename import pydantic_renamed_field
from datahub.ingestion.source.snowflake.constants import (
    CLIENT_PREFETCH_THREADS,
    CLIENT_SESSION_KEEP_ALIVE,
)
from datahub.ingestion.source.sql.oauth_generator import OAuthTokenGenerator
from datahub.ingestion.source.sql.sql_config import SQLCommonConfig, make_sqlalchemy_uri
from datahub.utilities.config_clean import (
    remove_protocol,
    remove_suffix,
    remove_trailing_slashes,
)

logger: logging.Logger = logging.getLogger(__name__)

APPLICATION_NAME: str = "acryl_datahub"

VALID_AUTH_TYPES: Dict[str, str] = {
    "DEFAULT_AUTHENTICATOR": DEFAULT_AUTHENTICATOR,
    "EXTERNAL_BROWSER_AUTHENTICATOR": EXTERNAL_BROWSER_AUTHENTICATOR,
    "KEY_PAIR_AUTHENTICATOR": KEY_PAIR_AUTHENTICATOR,
    "OAUTH_AUTHENTICATOR": OAUTH_AUTHENTICATOR,
}

SNOWFLAKE_HOST_SUFFIX = ".snowflakecomputing.com"


class BaseSnowflakeConfig(ConfigModel):
    # Note: this config model is also used by the snowflake-usage source.

    options: dict = pydantic.Field(
        default_factory=dict,
        description="Any options specified here will be passed to [SQLAlchemy.create_engine](https://docs.sqlalchemy.org/en/14/core/engines.html#sqlalchemy.create_engine) as kwargs.",
    )

    scheme: str = "snowflake"
    username: Optional[str] = pydantic.Field(
        default=None, description="Snowflake username."
    )
    password: Optional[pydantic.SecretStr] = pydantic.Field(
        default=None, exclude=True, description="Snowflake password."
    )
    private_key: Optional[str] = pydantic.Field(
        default=None,
        description="Private key in a form of '-----BEGIN PRIVATE KEY-----\\nprivate-key\\n-----END PRIVATE KEY-----\\n' if using key pair authentication. Encrypted version of private key will be in a form of '-----BEGIN ENCRYPTED PRIVATE KEY-----\\nencrypted-private-key\\n-----END ENCRYPTED PRIVATE KEY-----\\n' See: https://docs.snowflake.com/en/user-guide/key-pair-auth.html",
    )

    private_key_path: Optional[str] = pydantic.Field(
        default=None,
        description="The path to the private key if using key pair authentication. Ignored if `private_key` is set. See: https://docs.snowflake.com/en/user-guide/key-pair-auth.html",
    )
    private_key_password: Optional[pydantic.SecretStr] = pydantic.Field(
        default=None,
        exclude=True,
        description="Password for your private key. Required if using key pair authentication with encrypted private key.",
    )

    oauth_config: Optional[OAuthConfiguration] = pydantic.Field(
        default=None,
        description="oauth configuration - https://docs.snowflake.com/en/user-guide/python-connector-example.html#connecting-with-oauth",
    )
    authentication_type: str = pydantic.Field(
        default="DEFAULT_AUTHENTICATOR",
        description='The type of authenticator to use when connecting to Snowflake. Supports "DEFAULT_AUTHENTICATOR", "OAUTH_AUTHENTICATOR", "EXTERNAL_BROWSER_AUTHENTICATOR" and "KEY_PAIR_AUTHENTICATOR".',
    )
    account_id: str = pydantic.Field(
        description="Snowflake account identifier. e.g. xy12345,  xy12345.us-east-2.aws, xy12345.us-central1.gcp, xy12345.central-us.azure, xy12345.us-west-2.privatelink. Refer [Account Identifiers](https://docs.snowflake.com/en/user-guide/admin-account-identifier.html#format-2-legacy-account-locator-in-a-region) for more details.",
    )
    warehouse: Optional[str] = pydantic.Field(
        default=None, description="Snowflake warehouse."
    )
    role: Optional[str] = pydantic.Field(default=None, description="Snowflake role.")
    connect_args: Optional[Dict[str, Any]] = pydantic.Field(
        default=None,
        description="Connect args to pass to Snowflake SqlAlchemy driver",
        exclude=True,
    )

    def get_account(self) -> str:
        assert self.account_id
        return self.account_id

    rename_host_port_to_account_id = pydantic_renamed_field("host_port", "account_id")

    @pydantic.validator("account_id")
    def validate_account_id(cls, account_id: str) -> str:
        account_id = remove_protocol(account_id)
        account_id = remove_trailing_slashes(account_id)
        account_id = remove_suffix(account_id, SNOWFLAKE_HOST_SUFFIX)
        return account_id

    @pydantic.validator("authentication_type", always=True)
    def authenticator_type_is_valid(cls, v, values):
        if v not in VALID_AUTH_TYPES.keys():
            raise ValueError(
                f"unsupported authenticator type '{v}' was provided,"
                f" use one of {list(VALID_AUTH_TYPES.keys())}"
            )
        if (
            values.get("private_key") is not None
            or values.get("private_key_path") is not None
        ) and v != "KEY_PAIR_AUTHENTICATOR":
            raise ValueError(
                f"Either `private_key` and `private_key_path` is set but `authentication_type` is {v}. "
                f"Should be set to 'KEY_PAIR_AUTHENTICATOR' when using key pair authentication"
            )
        if v == "KEY_PAIR_AUTHENTICATOR":
            # If we are using key pair auth, we need the private key path and password to be set
            if (
                values.get("private_key") is None
                and values.get("private_key_path") is None
            ):
                raise ValueError(
                    f"Both `private_key` and `private_key_path` are none. "
                    f"At least one should be set when using {v} authentication"
                )
        elif v == "OAUTH_AUTHENTICATOR":
            cls._check_oauth_config(values.get("oauth_config"))
        logger.info(f"using authenticator type '{v}'")
        return v

    @staticmethod
    def _check_oauth_config(oauth_config: Optional[OAuthConfiguration]) -> None:
        if oauth_config is None:
            raise ValueError(
                "'oauth_config' is none but should be set when using OAUTH_AUTHENTICATOR authentication"
            )
        if oauth_config.use_certificate is True:
            if oauth_config.provider == OAuthIdentityProvider.OKTA:
                raise ValueError(
                    "Certificate authentication is not supported for Okta."
                )
            if oauth_config.encoded_oauth_private_key is None:
                raise ValueError(
                    "'base64_encoded_oauth_private_key' was none "
                    "but should be set when using certificate for oauth_config"
                )
            if oauth_config.encoded_oauth_public_key is None:
                raise ValueError(
                    "'base64_encoded_oauth_public_key' was none"
                    "but should be set when using use_certificate true for oauth_config"
                )
        elif oauth_config.client_secret is None:
            raise ValueError(
                "'oauth_config.client_secret' was none "
                "but should be set when using use_certificate false for oauth_config"
            )

    def get_sql_alchemy_url(
        self,
        database: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[pydantic.SecretStr] = None,
        role: Optional[str] = None,
    ) -> str:
        if username is None:
            username = self.username
        if password is None:
            password = self.password
        if role is None:
            role = self.role
        return make_sqlalchemy_uri(
            self.scheme,
            username,
            password.get_secret_value() if password else None,
            self.account_id,
            f'"{database}"' if database is not None else database,
            uri_opts={
                # Drop the options if value is None.
                key: value
                for (key, value) in {
                    "authenticator": VALID_AUTH_TYPES.get(self.authentication_type),
                    "warehouse": self.warehouse,
                    "role": role,
                    "application": APPLICATION_NAME,
                }.items()
                if value
            },
        )

    _computed_connect_args: Optional[dict] = None

    def get_connect_args(self) -> dict:
        """
        Builds connect args, adding defaults and reading a private key from the file if needed.
        Caches the results in a private instance variable to avoid reading the file multiple times.
        """

        if self._computed_connect_args is not None:
            return self._computed_connect_args

        connect_args: Dict[str, Any] = {
            # Improves performance and avoids timeout errors for larger query result
            CLIENT_PREFETCH_THREADS: 10,
            CLIENT_SESSION_KEEP_ALIVE: True,
            # Let user override the default config values
            **(self.connect_args or {}),
        }

        if (
            "private_key" not in connect_args
            and self.authentication_type == "KEY_PAIR_AUTHENTICATOR"
        ):
            if self.private_key is not None:
                pkey_bytes = self.private_key.replace("\\n", "\n").encode()
            else:
                assert (
                    self.private_key_path
                ), "missing required private key path to read key from"
                with open(self.private_key_path, "rb") as key:
                    pkey_bytes = key.read()

            p_key = serialization.load_pem_private_key(
                pkey_bytes,
                password=self.private_key_password.get_secret_value().encode()
                if self.private_key_password is not None
                else None,
                backend=default_backend(),
            )

            pkb: bytes = p_key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )

            connect_args["private_key"] = pkb

        self._computed_connect_args = connect_args
        return connect_args

    def get_options(self) -> dict:
        options_connect_args: Dict = self.get_connect_args()
        options_connect_args.update(self.options.get("connect_args", {}))
        self.options["connect_args"] = options_connect_args
        return self.options

    def get_oauth_connection(self) -> snowflake.connector.SnowflakeConnection:
        assert (
            self.oauth_config
        ), "oauth_config should be provided if using oauth based authentication"
        generator = OAuthTokenGenerator(
            client_id=self.oauth_config.client_id,
            authority_url=self.oauth_config.authority_url,
            provider=self.oauth_config.provider,
            username=self.username,
            password=self.password,
        )
        if self.oauth_config.use_certificate:
            response = generator.get_token_with_certificate(
                private_key_content=str(self.oauth_config.encoded_oauth_public_key),
                public_key_content=str(self.oauth_config.encoded_oauth_private_key),
                scopes=self.oauth_config.scopes,
            )
        else:
            assert self.oauth_config.client_secret
            response = generator.get_token_with_secret(
                secret=str(self.oauth_config.client_secret.get_secret_value()),
                scopes=self.oauth_config.scopes,
            )
        try:
            token = response["access_token"]
        except KeyError:
            raise ValueError(
                f"access_token not found in response {response}. "
                "Please check your OAuth configuration."
            )
        connect_args = self.get_options()["connect_args"]
        return snowflake.connector.connect(
            user=self.username,
            account=self.account_id,
            token=token,
            role=self.role,
            warehouse=self.warehouse,
            authenticator=VALID_AUTH_TYPES.get(self.authentication_type),
            application=APPLICATION_NAME,
            **connect_args,
        )

    def get_key_pair_connection(self) -> snowflake.connector.SnowflakeConnection:
        connect_args = self.get_options()["connect_args"]

        return snowflake.connector.connect(
            user=self.username,
            account=self.account_id,
            warehouse=self.warehouse,
            role=self.role,
            authenticator=VALID_AUTH_TYPES.get(self.authentication_type),
            application=APPLICATION_NAME,
            **connect_args,
        )

    def get_connection(self) -> snowflake.connector.SnowflakeConnection:
        connect_args = self.get_options()["connect_args"]
        if self.authentication_type == "DEFAULT_AUTHENTICATOR":
            return snowflake.connector.connect(
                user=self.username,
                password=self.password.get_secret_value() if self.password else None,
                account=self.account_id,
                warehouse=self.warehouse,
                role=self.role,
                application=APPLICATION_NAME,
                **connect_args,
            )
        elif self.authentication_type == "OAUTH_AUTHENTICATOR":
            return self.get_oauth_connection()
        elif self.authentication_type == "KEY_PAIR_AUTHENTICATOR":
            return self.get_key_pair_connection()
        elif self.authentication_type == "EXTERNAL_BROWSER_AUTHENTICATOR":
            return snowflake.connector.connect(
                user=self.username,
                password=self.password.get_secret_value() if self.password else None,
                account=self.account_id,
                warehouse=self.warehouse,
                role=self.role,
                authenticator=VALID_AUTH_TYPES.get(self.authentication_type),
                application=APPLICATION_NAME,
                **connect_args,
            )
        else:
            # not expected to be here
            raise Exception("Not expected to be here.")


class SnowflakeConfig(BaseSnowflakeConfig, BaseTimeWindowConfig, SQLCommonConfig):
    include_table_lineage: bool = pydantic.Field(
        default=True,
        description="If enabled, populates the snowflake table-to-table and s3-to-snowflake table lineage. Requires appropriate grants given to the role and Snowflake Enterprise Edition or above.",
    )
    include_view_lineage: bool = pydantic.Field(
        default=True,
        description="If enabled, populates the snowflake view->table and table->view lineages. Requires appropriate grants given to the role, and include_table_lineage to be True. view->table lineage requires Snowflake Enterprise Edition or above.",
    )

    database_pattern: AllowDenyPattern = AllowDenyPattern(
        deny=[r"^UTIL_DB$", r"^SNOWFLAKE$", r"^SNOWFLAKE_SAMPLE_DATA$"]
    )

    ignore_start_time_lineage: bool = False
    upstream_lineage_in_report: bool = False

    @pydantic.root_validator(skip_on_failure=True)
    def validate_include_view_lineage(cls, values):
        if (
            "include_table_lineage" in values
            and not values.get("include_table_lineage")
            and values.get("include_view_lineage")
        ):
            raise ValueError(
                "include_table_lineage must be True for include_view_lineage to be set."
            )
        return values

"""Per-provider credential resolution for cloud providers.

Consumes the global configuration loaded by
:func:`wisp.config.settings.load_toml_config` (which reads ``wisp.toml``) and
resolves credentials from (in priority order):
1. Explicit values in the global TOML config
2. Environment variables / Provider CLI/SDK default chains
"""

import configparser
from abc import ABC
from dataclasses import dataclass
from pathlib import Path

from wisp.config.settings import load_toml_config
from wisp.utils.logger import logger


class BaseCredentials(ABC):
    """Base class for provider credentials."""

    region: str = ""

    def is_explicitly_configured(self) -> bool:
        """Return True if any field is explicitly set (not relying on default chain)."""
        raise NotImplementedError("Subclasses must implement this method.")


# ─── AWS Credentials ─────────────────────────────────────────────


@dataclass
class AWSCredentials(BaseCredentials):
    """AWS authentication credentials.

    All fields optional — if not set, boto3 default credential chain is used
    (env vars, ~/.aws/credentials, ~/.aws/config, IAM roles).
    """

    region: str = ""
    profile: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""

    def is_explicitly_configured(self) -> bool:
        """True if any field is explicitly set (not relying on default chain)."""
        return any(
            [
                self.region,
                self.profile,
                self.access_key_id,
                self.secret_access_key,
                self.session_token,
            ]
        )

    def to_boto3_params(self) -> dict[str, str]:
        """Convert to boto3 client/session parameters (excludes empty values)."""
        params = {}
        if self.region:
            params["region_name"] = self.region
        if self.profile:
            params["profile_name"] = self.profile
        if self.access_key_id:
            params["aws_access_key_id"] = self.access_key_id
        if self.secret_access_key:
            params["aws_secret_access_key"] = self.secret_access_key
        if self.session_token:
            params["aws_session_token"] = self.session_token
        return params


# ─── OCI Credentials ─────────────────────────────────────────────


@dataclass
class OCICredentials(BaseCredentials):
    """OCI authentication credentials.

    All fields optional — if not set, OCI SDK default chain is used
    (~/.oci/config, env vars OCI_TENANCY/OCI_USER/OCI_FINGERPRINT/
    OCI_PRIVATE_KEY_PATH/OCI_REGION).
    """

    tenancy_ocid: str = ""
    user_ocid: str = ""
    fingerprint: str = ""
    private_key_path: str = ""
    region: str = ""
    compartment_ocid: str = ""
    profile: str = "DEFAULT"

    def is_explicitly_configured(self) -> bool:
        """True if any field is explicitly set."""
        return any(
            [
                self.tenancy_ocid,
                self.user_ocid,
                self.fingerprint,
                self.private_key_path,
                self.region,
                self.compartment_ocid,
                self.profile != "DEFAULT",
            ]
        )

    def get_compartment_ocid(self) -> str:
        """Return compartment OCID, defaulting to tenancy OCID (root)."""
        return self.compartment_ocid or self.tenancy_ocid

    def to_oci_config(self) -> dict[str, str]:
        """Convert to OCI SDK config dict (excludes empty values)."""
        config = {}
        if self.tenancy_ocid:
            config["tenancy"] = self.tenancy_ocid
        if self.user_ocid:
            config["user"] = self.user_ocid
        if self.fingerprint:
            config["fingerprint"] = self.fingerprint
        if self.private_key_path:
            config["key_file"] = self.private_key_path
        if self.region:
            config["region"] = self.region
        if self.profile != "DEFAULT":
            config["profile"] = self.profile
        return config


# ─── Resolution Functions ────────────────────────────────────────


def _read_aws_cli_files(profile: str) -> dict[str, str]:
    """Best-effort, network-free peek at ``~/.aws/{credentials,config}``.

    Mirrors what :func:`resolve_oci_credentials` already does via the OCI
    SDK's own config loader — AWS just doesn't ship an equivalent
    ``from_file()`` helper we can call the same way, so this reads the same
    two INI files the AWS CLI itself reads. Never touches the network (no
    STS calls, no instance-metadata lookups): this only tells us whether
    *something* is on disk, not whether it's actually valid.

    Args:
        profile: Profile name from ``wisp.toml``, or ``""`` for the
            AWS CLI's own "default" profile.

    Returns:
        dict[str, str]: Whichever of ``region``/``access_key_id``/
            ``secret_access_key``/``session_token`` it found. Empty if the
            files don't exist or don't have that profile.
    """
    creds_section = profile or "default"
    config_section = f"profile {profile}" if profile else "default"
    found: dict[str, str] = {}

    creds_path = Path.home() / ".aws" / "credentials"
    if creds_path.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(creds_path)
        except configparser.Error:
            parser = None
        if parser is not None and parser.has_section(creds_section):
            section = parser[creds_section]
            for toml_key, ini_key in (
                ("access_key_id", "aws_access_key_id"),
                ("secret_access_key", "aws_secret_access_key"),
                ("session_token", "aws_session_token"),
            ):
                if section.get(ini_key):
                    found[toml_key] = section[ini_key]

    config_path = Path.home() / ".aws" / "config"
    if config_path.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path)
        except configparser.Error:
            parser = None
        if parser is not None and parser.has_section(config_section):
            region = parser[config_section].get("region")
            if region:
                found["region"] = region

    return found


def resolve_aws_credentials() -> AWSCredentials:
    """Resolve AWS credentials from TOML config + the AWS CLI's own files.

    Values set in ``wisp.toml`` take priority; anything not set there falls
    back to whatever ``~/.aws/credentials`` / ``~/.aws/config`` already have
    for that profile (this is what previously only happened for OCI —
    without it, a perfectly normal ``aws configure`` setup showed up as
    "no configurado" in the TUI even though deploys worked fine, because
    boto3 was finding those files on its own, just later and separately).

    Note there's no hardcoded region fallback here (unlike before): an
    empty ``region`` is left empty so boto3 keeps resolving it itself
    (profile → env var → its own default) instead of Wisp silently forcing
    a region the person never asked for.
    """
    from wisp.providers.aws.constants import DEFAULT_AWS_REGION

    toml_config = load_toml_config()
    aws_toml = toml_config.get("aws", {})
    profile = aws_toml.get("profile", "")

    cli_config = _read_aws_cli_files(profile)

    return AWSCredentials(
        region=aws_toml.get("region", cli_config.get("region", DEFAULT_AWS_REGION)),
        profile=profile,
        access_key_id=aws_toml.get(
            "access_key_id", cli_config.get("access_key_id", "")
        ),
        secret_access_key=aws_toml.get(
            "secret_access_key", cli_config.get("secret_access_key", "")
        ),
        session_token=aws_toml.get(
            "session_token", cli_config.get("session_token", "")
        ),
    )


def resolve_oci_credentials() -> OCICredentials:
    """Resolve OCI credentials from TOML config + ~/.oci/config + environment.

    Values set in ``wisp.toml`` take priority; anything not set falls back to
    the OCI SDK default config chain (~/.oci/config, env vars).
    """
    from wisp.providers.oci.constants import DEFAULT_COMPARTMENT_ID

    toml_config = load_toml_config()
    oci_toml = toml_config.get("oci", {})

    # Try to load from OCI config file as fallback
    oci_config = {}
    try:
        import oci as oci_sdk

        oci_config = oci_sdk.config.from_file(
            profile_name=oci_toml.get("profile", "DEFAULT")
        )
    except Exception:
        pass  # Config file not found or invalid, use empty defaults

    return OCICredentials(
        tenancy_ocid=oci_toml.get("tenancy_ocid", oci_config.get("tenancy", "")),
        user_ocid=oci_toml.get("user_ocid", oci_config.get("user", "")),
        fingerprint=oci_toml.get("fingerprint", oci_config.get("fingerprint", "")),
        private_key_path=oci_toml.get(
            "private_key_path", oci_config.get("key_file", "")
        ),
        region=oci_toml.get("region", oci_config.get("region", "")),
        compartment_ocid=oci_toml.get("compartment_ocid", DEFAULT_COMPARTMENT_ID),
        profile=oci_toml.get("profile", "DEFAULT"),
    )


# ─── GCP Credentials ─────────────────────────────────────────────


@dataclass
class GCPCredentials(BaseCredentials):
    """Google Cloud credentials.

    ``project_id`` is the only truly required value for a deploy; everything
    else is optional. When ``credentials_path`` is set (service account key
    JSON, downloadable from the GCP Console), the SDK uses it directly — no
    gcloud CLI needed. Otherwise, Application Default Credentials (ADC) are
    used — ``GOOGLE_APPLICATION_CREDENTIALS`` env var,
    ``gcloud auth application-default login``, or the GCE metadata server.
    """

    project_id: str = ""
    region: str = ""
    zone: str = ""
    credentials_path: str = ""

    def is_explicitly_configured(self) -> bool:
        """True if any field is explicitly set (not relying on ADC)."""
        return any([self.project_id, self.region, self.zone, self.credentials_path])

    def get_zone(self) -> str:
        """Return the zone, deriving ``{region}-a`` when only a region is set.

        GCP zones are region-scoped (e.g. ``us-central1-a``); if only the
        region is configured, the first zone of the region is assumed.
        """
        if self.zone:
            return self.zone
        if self.region:
            return f"{self.region}-a"
        return ""

    def to_gcp_config(self) -> dict[str, str]:
        """Convert to ``pulumi_gcp.Provider`` config values (excludes empties)."""
        config = {}
        if self.project_id:
            config["project"] = self.project_id
        if self.region:
            config["region"] = self.region
        if self.zone:
            config["zone"] = self.zone
        return config


def resolve_gcp_credentials() -> GCPCredentials:
    """Resolve GCP credentials from TOML config + google.auth defaults.

    Values set in ``wisp.toml`` (``[gcp]``) take priority; anything not set
    there falls back to the environment (``GOOGLE_CLOUD_PROJECT`` /
    ``GCLOUD_PROJECT``) and :func:`google.auth.default` — which resolves the
    project and the Application Default Credentials (ADC) in one call.
    """
    from wisp.providers.gcp.constants import DEFAULT_GCP_REGION

    toml_config = load_toml_config()
    gcp_toml = toml_config.get("gcp", {})

    project_id = gcp_toml.get("project_id", "")
    if not project_id:
        # Best-effort fallback: env vars first (work even without ADC), then
        # google.auth.default() (resolves project alongside the credentials;
        # if ADC is not set up it raises and the project simply stays empty).
        import os

        project_id = (
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT")
            or ""
        )
        if not project_id:
            try:
                import google.auth

                _, detected_project = google.auth.default()
                if detected_project:
                    project_id = detected_project
            except Exception:
                pass

    region = gcp_toml.get("region", DEFAULT_GCP_REGION)
    zone = gcp_toml.get("zone", "")
    if not zone and region:
        zone = f"{region}-a"

    return GCPCredentials(
        project_id=project_id,
        region=region,
        zone=zone,
        credentials_path=gcp_toml.get("credentials_path", ""),
    )


# ─── Validation Functions ────────────────────────────────────────


def validate_aws_credentials(creds: AWSCredentials) -> bool:
    """Validate AWS credentials by calling STS GetCallerIdentity.

    Returns True if credentials work, False otherwise.
    Uses boto3 with resolved credentials (or default chain if empty).
    """
    try:
        import boto3

        client = boto3.client("sts", **creds.to_boto3_params())
        client.get_caller_identity()
        return True
    except Exception:
        return False


def validate_oci_credentials(creds: OCICredentials) -> bool:
    """Validate OCI credentials by calling Identity list_region_subscriptions.

    Returns True if credentials work, False otherwise.
    """
    try:
        import oci

        config = creds.to_oci_config()
        if not config:
            return False  # No explicit creds to validate
        identity_client = oci.identity.IdentityClient(config)
        identity_client.list_region_subscriptions(tenancy_id=config["tenancy"])
        return True
    except Exception:
        return False


def validate_gcp_credentials(creds: GCPCredentials) -> bool:
    """Validate GCP credentials by listing the project's regions.

    Returns True if credentials work, False otherwise. Uses the Compute SDK
    with explicitly loaded credentials — the service account file when
    ``credentials_path`` is set, otherwise :func:`google.auth.default` (ADC).
    """
    try:
        from google.cloud import compute_v1

        client = compute_v1.RegionsClient(credentials=_load_google_credentials(creds))
        request = compute_v1.ListRegionsRequest(project=creds.project_id)
        list(client.list(request=request))
        return True
    except Exception:
        return False


def _load_google_credentials(creds: GCPCredentials):
    """Load explicit google.auth credentials from a GCPCredentials instance.

    Resolution order:
    1. Service account key file (``credentials_path``) — downloadable from
       the GCP Console; works without the gcloud CLI. Loaded with the
       ``cloud-platform`` scope (service-account JWTs without scopes are
       rejected with ``invalid_scope``).
    2. :func:`google.auth.default` (ADC) — ``GOOGLE_APPLICATION_CREDENTIALS``
       env var, ``gcloud auth application-default login``, or the GCE
       metadata server.
    """
    if creds.credentials_path:
        import json

        from google.auth.transport.requests import Request as AuthRequest
        from google.oauth2 import service_account

        try:
            with open(creds.credentials_path) as f:
                key_type = json.load(f).get("type", "")
        except (OSError, ValueError, json.JSONDecodeError):  # fmt: skip
            key_type = ""

        if key_type != "service_account":
            logger.warning(
                f"'{creds.credentials_path}' is a '{key_type or 'unknown'}' "
                "credential, not a service account key. User-account "
                "credentials may not be resolvable by the GCP APIs ('Gaia id "
                "not found'). Use a service account key JSON instead "
                "(Console → IAM & Admin → Service Accounts → Keys → Add Key)."
            )

        # Explicit scopes are required: a service-account JWT without scopes
        # is rejected by Google's token endpoint with `invalid_scope`.
        resolved = service_account.Credentials.from_service_account_file(
            creds.credentials_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        # Refresh eagerly so auth problems surface with their real cause
        # (e.g. "Invalid JWT Signature" for a revoked/rotated key) instead of
        # a misleading 401 from the first API request.
        resolved.refresh(AuthRequest())
        return resolved

    import google.auth

    resolved, _ = google.auth.default()
    return resolved


# ─── VPS (bring-your-own-server) Credentials ─────────────────────


@dataclass
class VPSCredentials(BaseCredentials):
    """Connection details for a user-provided VPS.

    The user already owns the server, so there is nothing to provision and
    nothing to destroy: only SSH access matters. Authentication is
    public-key based (the server's ``authorized_keys`` must contain the user's
    public key); password authentication is deliberately out of scope.
    """

    host: str = ""
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_path: str = ""

    @property
    def region(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Placeholder region so CLI flows that read ``credentials.region``
        keep working (a user-owned VPS has no provider regions)."""
        return "vps"

    def is_explicitly_configured(self) -> bool:
        """Return True when a host is set in the TOML ``[vps]`` section."""
        return bool(self.host)


def resolve_vps_credentials() -> VPSCredentials:
    """Resolve VPS connection details from the TOML ``[vps]`` section.

    Args:
        None.

    Returns:
        VPSCredentials: Host/user/port/key path; empty host when unset.
    """
    toml_config = load_toml_config()
    vps_toml = toml_config.get("vps", {})
    return VPSCredentials(
        host=str(vps_toml.get("host", "")),
        ssh_user=str(vps_toml.get("ssh_user", "root")),
        ssh_port=int(vps_toml.get("ssh_port", 22)),
        ssh_key_path=str(vps_toml.get("ssh_key_path", "")),
    )

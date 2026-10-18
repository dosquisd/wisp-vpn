"""Pulumi program for the GCP provider.

Defines the infrastructure declared during ``pulumi up``: a Compute Instance
(e2-micro, SPOT scheduling) attached to the default network with an ephemeral
external IP, plus a VPC-level firewall rule opening the WireGuard (UDP) and
SSH (TCP 22) ports. Generates an RSA 4096 key pair via TLS.
Values are surfaced back to the caller via ``pulumi.export`` outputs.
"""

import uuid

import pulumi
import pulumi_gcp as gcp
import pulumi_tls as tls
from pulumi import automation as auto

from wisp.providers.gcp.constants import (
    DEFAULT_GCP_FIREWALL_NAME,
    DEFAULT_GCP_NETWORK,
    DEFAULT_GCP_TARGET_TAG,
    DEFAULT_GCP_VM_INSTANCE,
    DEFAULT_IMAGE_FAMILY,
    DEFAULT_IMAGE_PROJECT,
)
from wisp.utils import get_public_ip, get_wireguard_port, logger

# Guards one-time plugin installation per process.
_plugins_ready = False


def ensure_plugins() -> None:
    """Install the Pulumi ``gcp`` and ``tls`` plugins once per process."""
    global _plugins_ready
    if _plugins_ready:
        return
    auto.LocalWorkspace().install_plugin("gcp", "v9.37.0")
    auto.LocalWorkspace().install_plugin("tls", "v5.5.1")
    _plugins_ready = True


def get_default_username(image_family: str) -> str:
    """Guess the default SSH username from an image family.

    Maps common distributions to their cloud default users (Ubuntu ->
    ``ubuntu``, Debian -> ``debian``), falling back to ``ubuntu``.
    """
    family_lower = image_family.lower()
    if "ubuntu" in family_lower:
        return "ubuntu"
    elif "debian" in family_lower:
        return "debian"
    return "ubuntu"


def _provider_opts(provider: pulumi.Resource | None) -> dict:
    """Build resource options dict with provider if provided."""
    return {"opts": pulumi.ResourceOptions(provider=provider)} if provider else {}


def get_firewall(
    force_current_ip: bool = False,
    *,
    name: str | None = None,
    wireguard_port: int | None = None,
    source_ranges: list[str] | None = None,
    provider: pulumi.Resource | None = None,
) -> gcp.compute.Firewall:
    """Create the VPC-level firewall rule for the WireGuard VM.

    GCP has no per-instance security groups; ingress is opened via a global
    (project-level) firewall rule on the default network, targeting instances
    by network tag. Opens inbound UDP to the WireGuard port (the tunnel) and
    TCP 22 (SSH), and allows all egress (implicit in GCP). Source is
    ``<current-ip>/32`` when ``force_current_ip`` is set, otherwise
    ``0.0.0.0/0``.

    Args:
        force_current_ip (bool): Restrict ingress to the caller's public IP.
        name (str | None): Firewall rule name.
        wireguard_port (int | None): UDP port to open; random if ``None``.
        source_ranges (list[str] | None): Explicit ingress CIDRs; computed if
            ``None``.

    Returns:
        gcp.compute.Firewall: The created firewall rule resource.
    """
    if name is None:
        name = DEFAULT_GCP_FIREWALL_NAME
        logger.debug(f"No firewall name provided, using default '{name}'")

    if wireguard_port is None:
        wireguard_port = get_wireguard_port()
        logger.debug(f"No WireGuard port provided, using default '{wireguard_port}'")

    if source_ranges is None:
        if force_current_ip:
            source_ranges = [f"{get_public_ip()}/32"]
        else:
            source_ranges = ["0.0.0.0/0"]

        logger.debug(f"No source ranges provided, using default '{source_ranges}'")

    return gcp.compute.Firewall(
        name,
        network=DEFAULT_GCP_NETWORK,
        direction="INGRESS",
        allows=[
            gcp.compute.FirewallAllowArgs(
                protocol="udp",
                ports=[str(wireguard_port)],
            ),
            gcp.compute.FirewallAllowArgs(
                protocol="tcp",
                ports=["22"],
            ),
        ],
        source_ranges=source_ranges,
        target_tags=[DEFAULT_GCP_TARGET_TAG],
        **_provider_opts(provider),
    )


def create_gcp_instance(
    region: str,
    /,
    force_current_ip: bool = False,
    *,
    project_id: str | None = None,
    credentials_path: str | None = None,
    custom_resource_name: str | None = None,
    zone: str | None = None,
    machine_type: str = DEFAULT_GCP_VM_INSTANCE,
    # Security group related
    firewall_name: str | None = None,
    wireguard_port: int | None = None,
    source_ranges: list[str] | None = None,
) -> None:
    """Pulumi program: declare the GCP instance and its dependencies.

    Creates an explicit ``gcp.Provider`` (carrying project/region/zone and
    the service account key when provided) and attaches it to every resource,
    launches a SPOT-scheduled :class:`gcp.compute.Instance` attached to the
    default network with an ephemeral external IP, plus the firewall rule.
    Exports the outputs consumed by the provider: ``instance_id``,
    ``instance_public_ip``, ``instance_private_ip``, ``instance_private_key``,
    ``wireguard_port``, and ``ssh_user``.

    Args:
        region (str): Target region (positional-only).
        force_current_ip (bool): Restrict the firewall to the caller's IP.
        project_id (str | None): GCP project ID; resources fall back to the
            provider's global config if ``None``.
        credentials_path (str | None): Path to the service account key JSON
            used by the Pulumi provider; falls back to ADC if ``None``.
        custom_resource_name (str | None): Instance resource name; a
            ``wisp-instance-<hex>`` name is generated if ``None``.
        zone (str | None): Target zone; ``{region}-a`` if ``None``.
        machine_type (str): Compute instance machine type.
        firewall_name (str | None): Firewall rule name.
        wireguard_port (int | None): UDP port; random if ``None``.
        source_ranges (list[str] | None): Explicit ingress CIDRs.
    """
    ensure_plugins()

    # Explicitly create GCP provider with unique name to avoid duplicate URN
    # errors; it carries the project/region/zone for every resource.
    gcp_provider = (
        gcp.Provider(
            "gcp-provider",
            project=project_id,
            region=region,
            zone=zone,
            # Service account key path — bypasses the gcloud/ADC chain so the
            # deploy authenticates as the service account (per the Pulumi GCP
            # docs, `credentials` accepts a path to the SA key JSON).
            credentials=credentials_path,
        )
        if project_id
        else None
    )
    if custom_resource_name is None:
        custom_resource_name = f"wisp-instance-{uuid.uuid4().hex[:8]}"
        logger.debug(
            f"No custom resource name provided, using default '{custom_resource_name}'"
        )

    if zone is None:
        zone = f"{region}-a"
        logger.debug(f"No zone provided, using default '{zone}'")

    if wireguard_port is None:
        wireguard_port = get_wireguard_port()
        logger.debug(f"No WireGuard port provided, using default '{wireguard_port}'")

    logger.debug(
        f"Creating GCP instance in region '{region}' (zone '{zone}') "
        f"with type '{machine_type}'"
    )

    # Firewall rule (project-level, global) targeting the instance by network tag
    get_firewall(
        force_current_ip,
        name=firewall_name,
        wireguard_port=wireguard_port,
        source_ranges=source_ranges,
        provider=gcp_provider,
    )

    # Generate an RSA 4096 private key for SSH access
    logger.debug("Generating RSA 4096 private key for SSH")

    tls_private_key = tls.PrivateKey(
        "wireguard-private-key", algorithm="RSA", rsa_bits=4096
    )

    # Create the SPOT-scheduled Compute Instance attached to the default network
    instance = gcp.compute.Instance(
        custom_resource_name,
        name=custom_resource_name,
        machine_type=machine_type,
        zone=zone,
        project=project_id,
        boot_disk=gcp.compute.InstanceBootDiskArgs(
            initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
                image=f"{DEFAULT_IMAGE_PROJECT}/{DEFAULT_IMAGE_FAMILY}",
            ),
        ),
        network_interfaces=[
            gcp.compute.InstanceNetworkInterfaceArgs(
                network=DEFAULT_GCP_NETWORK,
                access_configs=[gcp.compute.InstanceNetworkInterfaceAccessConfigArgs()],
            )
        ],
        scheduling=gcp.compute.InstanceSchedulingArgs(
            provisioning_model="SPOT",
            # Required with SPOT: Pulumi defaults `preemptible` to False when
            # unset, and GCP rejects `preemptible=false` + `SPOT` as
            # contradicting (Error 400).
            preemptible=True,
            on_host_maintenance="TERMINATE",
            instance_termination_action="DELETE",
            automatic_restart=False,
        ),
        metadata={
            # GCP requires the `ssh-keys` metadata key (hyphenated) with the
            # username as a `user:` prefix — `ssh_authorized_keys` (the OCI
            # convention) is ignored by GCP, which breaks SSH auth.
            # NOTE: `public_key_openssh` is a Pulumi Output — it MUST be
            # combined via Output.concat, never f-strings (an f-string
            # evaluates before the engine resolves it and lands the literal
            # "Calling __str__ on an Output[T]..." error in the metadata).
            "ssh-keys": pulumi.Output.concat(
                get_default_username(DEFAULT_IMAGE_FAMILY),
                ":",
                tls_private_key.public_key_openssh,
            ),
        },
        tags=[DEFAULT_GCP_TARGET_TAG],
        **_provider_opts(gcp_provider),
    )

    pulumi.export("instance_id", instance.instance_id)
    pulumi.export(
        "instance_public_ip", instance.network_interfaces[0].access_configs[0].nat_ip
    )
    pulumi.export("instance_private_ip", instance.network_interfaces[0].network_ip)
    pulumi.export(
        "instance_private_key", pulumi.Output.secret(tls_private_key.private_key_pem)
    )
    pulumi.export("wireguard_port", wireguard_port)
    pulumi.export("ssh_user", get_default_username(DEFAULT_IMAGE_FAMILY))

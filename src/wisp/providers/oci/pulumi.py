"""Pulumi program for the OCI provider.

Defines the infrastructure declared during ``pulumi up``: a VCN with Internet
Gateway, Route Table, Security List, Subnet, and a Compute Instance (Flex shape).
Generates an RSA 4096 key pair via TLS.
Values are surfaced back to the caller via ``pulumi.export`` outputs.
"""

import uuid

import oci as oci_sdk
import pulumi
import pulumi_oci as oci
import pulumi_tls as tls
from pulumi import automation as auto

from wisp.config.constants import DEFAULT_TAG
from wisp.providers.oci.constants import (
    DEFAULT_DNS_LABEL,
    DEFAULT_IMAGE_OS,
    DEFAULT_IMAGE_OS_VERSION,
    DEFAULT_INSTANCE_NAME_PREFIX,
    DEFAULT_INTERNET_GATEWAY_NAME,
    DEFAULT_MEMORY_IN_GBS,
    DEFAULT_OCPUS,
    DEFAULT_ROUTE_TABLE_NAME,
    DEFAULT_SECURITY_LIST_NAME,
    DEFAULT_SHAPE,
    DEFAULT_SUBNET_CIDR,
    DEFAULT_SUBNET_DNS_LABEL,
    DEFAULT_SUBNET_NAME,
    DEFAULT_VCN_CIDR,
    DEFAULT_VCN_NAME,
)
from wisp.utils import get_public_ip, get_wireguard_port, logger

# Guards one-time plugin installation per process.
_plugins_ready = False


def ensure_plugins() -> None:
    """Install the Pulumi ``oci`` and ``tls`` plugins once per process."""
    global _plugins_ready
    if _plugins_ready:
        return
    auto.LocalWorkspace().install_plugin("oci", "v4.22.0")
    auto.LocalWorkspace().install_plugin("tls", "v5.5.1")
    _plugins_ready = True


def _provider_opts(provider: pulumi.Resource | None) -> dict:
    """Build resource options dict with provider if provided."""
    return {"opts": pulumi.ResourceOptions(provider=provider)} if provider else {}


def get_ubuntu_image_id(
    compartment_id: str,
    region: str | None = None,
    shape: str = DEFAULT_SHAPE,
    operating_system: str = DEFAULT_IMAGE_OS,
    operating_system_version: str = DEFAULT_IMAGE_OS_VERSION,
) -> str:
    """Find the most recent Ubuntu image ID using the OCI SDK."""
    config = oci_sdk.config.from_file(profile_name="DEFAULT")
    if region:
        config["region"] = region

    compute_client = oci_sdk.core.ComputeClient(config)

    images = compute_client.list_images(
        compartment_id=compartment_id,
        operating_system=operating_system,
        operating_system_version=operating_system_version,
        shape=shape,
        sort_by="TIMECREATED",
        sort_order="DESC",
        lifecycle_state="AVAILABLE",
    ).data

    if not images:
        raise RuntimeError(
            f"No available {operating_system} {operating_system_version} image found "
            f"for shape {shape} in compartment {compartment_id}"
        )

    latest_image = images[0]
    logger.debug(
        f"Selected image: {latest_image.display_name} ({latest_image.id}) "
        f"created {latest_image.time_created}"
    )
    return latest_image.id


def get_default_username(image_os: str) -> str:
    """Guess the default SSH username from an image OS."""
    os_lower = image_os.lower()
    if "ubuntu" in os_lower:
        return "ubuntu"
    elif "oracle" in os_lower or "ol" in os_lower:
        return "opc"
    return "ubuntu"


def get_security_list(
    compartment_id: str,
    vcn_id: pulumi.Input[str],
    force_current_ip: bool = False,
    *,
    name: str | None = None,
    display_name: str | None = None,
    wireguard_port: int | None = None,
    cidr_blocks: list[str] | None = None,
    provider: pulumi.Resource | None = None,
) -> oci.core.SecurityList:
    """Create the security list for the WireGuard VM."""
    if name is None:
        name = DEFAULT_SECURITY_LIST_NAME
        logger.debug(f"No security list name provided, using default '{name}'")

    if display_name is None:
        display_name = "Security list for WISP - WireGuard VPN"
        logger.debug(
            f"No security list display name provided, using default '{display_name}'"
        )

    if wireguard_port is None:
        wireguard_port = get_wireguard_port()
        logger.debug(f"No WireGuard port provided, using default '{wireguard_port}'")

    if cidr_blocks is None:
        if force_current_ip:
            cidr_blocks = [f"{get_public_ip()}/32"]
        else:
            cidr_blocks = ["0.0.0.0/0"]

        logger.debug(f"No CIDR blocks provided, using default '{cidr_blocks}'")

    kwargs = dict(
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        display_name=display_name,
        egress_security_rules=[
            oci.core.SecurityListEgressSecurityRuleArgs(
                destination="0.0.0.0/0",
                protocol="all",
                destination_type="CIDR_BLOCK",
                stateless=False,
            )
        ],
        ingress_security_rules=[
            oci.core.SecurityListIngressSecurityRuleArgs(
                protocol="17",
                source=cidr_blocks[0] if cidr_blocks else "0.0.0.0/0",
                description="WireGuard VPN",
                udp_options=oci.core.SecurityListIngressSecurityRuleUdpOptionsArgs(
                    max=wireguard_port,
                    min=wireguard_port,
                ),
                source_type="CIDR_BLOCK",
                stateless=False,
            ),
            oci.core.SecurityListIngressSecurityRuleArgs(
                protocol="6",
                source=cidr_blocks[0] if cidr_blocks else "0.0.0.0/0",
                description="SSH",
                tcp_options=oci.core.SecurityListIngressSecurityRuleTcpOptionsArgs(
                    max=22,
                    min=22,
                ),
                source_type="CIDR_BLOCK",
                stateless=False,
            ),
        ],
        freeform_tags=DEFAULT_TAG,
        **_provider_opts(provider),
    )
    return oci.core.SecurityList(name, **kwargs)


def create_vcn(
    compartment_id: str,
    *,
    cidr_block: str = DEFAULT_VCN_CIDR,
    dns_label: str = DEFAULT_DNS_LABEL,
    display_name: str = DEFAULT_VCN_NAME,
    provider: pulumi.Resource | None = None,
) -> oci.core.Vcn:
    """Create a Virtual Cloud Network (VCN)."""
    return oci.core.Vcn(
        "wisp-vcn",
        compartment_id=compartment_id,
        cidr_block=cidr_block,
        display_name=display_name,
        dns_label=dns_label,
        freeform_tags=DEFAULT_TAG,
        **_provider_opts(provider),
    )


def create_internet_gateway(
    compartment_id: str,
    vcn_id: pulumi.Input[str],
    *,
    display_name: str = DEFAULT_INTERNET_GATEWAY_NAME,
    provider: pulumi.Resource | None = None,
) -> oci.core.InternetGateway:
    """Create an Internet Gateway for the VCN."""
    return oci.core.InternetGateway(
        "wisp-ig",
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        display_name=display_name,
        enabled=True,
        freeform_tags=DEFAULT_TAG,
        **_provider_opts(provider),
    )


def create_route_table(
    compartment_id: str,
    vcn_id: pulumi.Input[str],
    internet_gateway_id: pulumi.Input[str],
    *,
    display_name: str = DEFAULT_ROUTE_TABLE_NAME,
    provider: pulumi.Resource | None = None,
) -> oci.core.RouteTable:
    """Create a Route Table with a default route to the Internet Gateway."""
    return oci.core.RouteTable(
        "wisp-rt",
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        display_name=display_name,
        route_rules=[
            oci.core.RouteTableRouteRuleArgs(
                network_entity_id=internet_gateway_id,
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
            )
        ],
        freeform_tags=DEFAULT_TAG,
        **_provider_opts(provider),
    )


def create_subnet(
    compartment_id: str,
    vcn_id: pulumi.Input[str],
    route_table_id: pulumi.Input[str],
    security_list_ids: list[pulumi.Input[str]],
    *,
    cidr_block: str = DEFAULT_SUBNET_CIDR,
    dns_label: str = DEFAULT_SUBNET_DNS_LABEL,
    display_name: str = DEFAULT_SUBNET_NAME,
    provider: pulumi.Resource | None = None,
) -> oci.core.Subnet:
    """Create a public subnet in the VCN."""
    return oci.core.Subnet(
        "wisp-subnet",
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        cidr_block=cidr_block,
        display_name=display_name,
        dns_label=dns_label,
        prohibit_internet_ingress=False,
        prohibit_public_ip_on_vnic=False,
        route_table_id=route_table_id,
        security_list_ids=security_list_ids,
        freeform_tags=DEFAULT_TAG,
        **_provider_opts(provider),
    )


def get_availability_domain(
    compartment_id: str,
    region: str | None = None,
    provider: pulumi.Resource | None = None,
) -> oci.identity.GetAvailabilityDomainsResult:
    """Get the first available availability domain in the compartment."""
    if provider:
        opts = pulumi.InvokeOptions(provider=provider)
    else:
        opts = pulumi.InvokeOptions(provider=oci.Provider("oci", region=region))
    ads = oci.identity.get_availability_domains(
        compartment_id=compartment_id,
        opts=opts,
    )
    return ads


def create_compute_instance(
    compartment_id: str,
    subnet_id: pulumi.Input[str],
    availability_domain: str,
    ssh_public_key: pulumi.Input[str],
    region: str | None = None,
    *,
    shape: str = DEFAULT_SHAPE,
    ocpus: float = DEFAULT_OCPUS,
    memory_in_gbs: float = DEFAULT_MEMORY_IN_GBS,
    display_name: str | None = None,
    image_id: pulumi.Input[str] | None = None,
    provider: pulumi.Resource | None = None,
) -> oci.core.Instance:
    """Create a Compute Instance (Flex shape) with the given parameters."""
    if display_name is None:
        display_name = f"{DEFAULT_INSTANCE_NAME_PREFIX}-{uuid.uuid4().hex[:8]}"
        logger.debug(
            f"No instance display name provided, using default '{display_name}'"
        )

    return oci.core.Instance(
        display_name,
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        shape=shape,
        shape_config=oci.core.InstanceShapeConfigArgs(
            ocpus=ocpus,
            memory_in_gbs=memory_in_gbs,
        ),
        create_vnic_details=oci.core.InstanceCreateVnicDetailsArgs(
            assign_public_ip="true",
            assign_private_dns_record=True,
            display_name=f"{display_name}-vnic",
            subnet_id=subnet_id,
        ),
        metadata={
            "ssh_authorized_keys": ssh_public_key,
        },
        source_details=oci.core.InstanceSourceDetailsArgs(
            source_type="image",
            source_id=image_id,
        ),
        display_name=display_name,
        freeform_tags=DEFAULT_TAG,
        **_provider_opts(provider),
    )


def create_oci_instance(
    region: str,
    compartment_id: str,
    force_current_ip: bool = False,
    *,
    # Instance related
    custom_resource_name: str | None = None,
    shape: str = DEFAULT_SHAPE,
    ocpus: float = DEFAULT_OCPUS,
    memory_in_gbs: float = DEFAULT_MEMORY_IN_GBS,
    # Network related
    vcn_cidr: str = DEFAULT_VCN_CIDR,
    subnet_cidr: str = DEFAULT_SUBNET_CIDR,
    # Security list related
    security_list_name: str | None = None,
    wireguard_port: int | None = None,
    cidr_blocks: list[str] | None = None,
) -> None:
    """Pulumi program: declare the OCI instance and its dependencies.

    Creates the VCN, Internet Gateway, Route Table, Security List, Subnet,
    generates an RSA 4096 :class:`tls.PrivateKey` and launches an
    :class:`oci.core.Instance`.
    Exports the outputs consumed by the provider:
    ``instance_id``, ``instance_public_ip``, ``instance_private_ip``,
    ``instance_private_key``, ``wireguard_port``, and ``ssh_user``.
    """
    ensure_plugins()

    # Explicitly create OCI provider with unique name to avoid duplicate URN errors
    oci_provider = oci.Provider(
        "oci-provider",
        region=region,
    )

    if custom_resource_name is None:
        custom_resource_name = f"{DEFAULT_INSTANCE_NAME_PREFIX}-{uuid.uuid4().hex[:8]}"
        logger.debug(
            f"No custom resource name provided, using default '{custom_resource_name}'"
        )

    if wireguard_port is None:
        wireguard_port = get_wireguard_port()
        logger.debug(f"No WireGuard port provided, using default '{wireguard_port}'")

    logger.debug(
        f"Creating OCI instance in region '{region}' with shape '{shape}' "
        f"({ocpus} OCPU, {memory_in_gbs} GB)"
    )

    # Get Ubuntu 24.04 image ID using OCI SDK
    image_id = get_ubuntu_image_id(
        compartment_id=compartment_id,
        region=region,
        shape=shape,
        operating_system=DEFAULT_IMAGE_OS,
        operating_system_version=DEFAULT_IMAGE_OS_VERSION,
    )

    # Get availability domain
    ads = get_availability_domain(compartment_id, region, provider=oci_provider)
    availability_domain = ads.availability_domains[0].name
    logger.debug(f"Using availability domain: {availability_domain}")

    # Create VCN
    vcn = create_vcn(compartment_id, cidr_block=vcn_cidr, provider=oci_provider)

    # Create Internet Gateway
    internet_gateway = create_internet_gateway(
        compartment_id, vcn.id, provider=oci_provider
    )

    # Create Route Table
    route_table = create_route_table(
        compartment_id, vcn.id, internet_gateway.id, provider=oci_provider
    )

    # Create Security List
    security_list = get_security_list(
        compartment_id,
        vcn.id,
        force_current_ip,
        name=security_list_name,
        wireguard_port=wireguard_port,
        cidr_blocks=cidr_blocks,
        provider=oci_provider,
    )

    # Create Subnet
    subnet = create_subnet(
        compartment_id,
        vcn.id,
        route_table.id,
        [security_list.id],
        cidr_block=subnet_cidr,
        provider=oci_provider,
    )

    # Generate RSA 4096 private key for SSH access
    logger.debug("Generating RSA 4096 private key for SSH")

    tls_private_key = tls.PrivateKey(
        "wireguard-private-key", algorithm="RSA", rsa_bits=4096
    )

    # Create Compute Instance
    instance = create_compute_instance(
        compartment_id=compartment_id,
        subnet_id=subnet.id,
        availability_domain=availability_domain,
        ssh_public_key=tls_private_key.public_key_openssh,
        region=region,
        shape=shape,
        ocpus=ocpus,
        memory_in_gbs=memory_in_gbs,
        display_name=custom_resource_name,
        image_id=image_id,
        provider=oci_provider,
    )

    # Get the default SSH username for Ubuntu
    ssh_user = "ubuntu"

    # Export outputs
    pulumi.export("instance_id", instance.id)
    pulumi.export("instance_public_ip", instance.public_ip)
    pulumi.export("instance_private_ip", instance.private_ip)
    pulumi.export(
        "instance_private_key", pulumi.Output.secret(tls_private_key.private_key_pem)
    )
    pulumi.export("wireguard_port", wireguard_port)
    pulumi.export("ssh_user", ssh_user)

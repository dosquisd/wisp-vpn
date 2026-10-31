# Configuration

Configuration is loaded from a single user-level `wisp.toml`. Per-session
deployment settings live in the `WispConfig` dataclass, built from the
`[general]` TOML section, and defaults/paths are constants.

## `WispConfig` (`config/settings.py`)

A dataclass holding per-session, per-deployment settings. Defaults come from
`config/constants.py`; any field omitted in the `[general]` TOML section falls
back to those constants.

| Field | Type | Default | Meaning |
| ------- | ------ | --------- | --------- |
| `vm_boot_timeout` | `int` | `60` | Seconds to wait for the VM to boot / SSH to come up before configuring WireGuard |
| `wireguard_interface` | `str` | `"wg0"` | WireGuard interface name |
| `wireguard_ipv4` | `str` | `"10.66.66.1"` | Server-side WireGuard IPv4 |
| `wireguard_ipv6` | `str` | `"fd42:42:42::1"` | Server-side WireGuard IPv6 |
| `wireguard_dns1` | `str` | `"1.1.1.1"` | Primary DNS pushed to the client |
| `wireguard_dns2` | `str` | `"1.0.0.1"` | Secondary DNS pushed to the client |
| `wireguard_port` | `int` | `0` | UDP port; `0` means a random port in `49152–65535` |
| `force_current_ip` | `bool` | `False` | If true, restrict the cloud firewall ingress to your current public IP (`/32`) — client routing is always full-tunnel |
| `confirm_destroy` | `bool` | `True` | Ask for confirmation before tearing down the VPN from the tunnel view (`d`/`esc`/the destroy button). The "Don't ask again" checkbox in that dialog flips this to `False` and persists it to `[general]` |
| `max_session_ttl_minutes` | `int` | `60` | Session TTL cap for the optional OS-level watchdog (`utils/watchdog.py`): when greater than zero, deploying schedules a timer that runs `wisp destroy` when the TTL expires — it survives TUI/process death. `0` disables the watchdog. The deploy screen toggles it per run; the CLI overrides it with `--ttl` |

The TUI Configuration screen edits these values in memory; the CLI uses the
TOML-derived defaults (with `force_current_ip` overridable through the provider
call).

### `force_current_ip` behaviour

This flag only affects the **cloud firewall** — who can reach the VM. Client
routing is always full-tunnel:

- **Security rules**: when true, ingress `cidr_blocks` is `["<your-ip>/32"]`;
  otherwise `["0.0.0.0/0"]`.
- **Client AllowedIPs**: always `"0.0.0.0/0,::/0"` (route all traffic through
  the tunnel), regardless of this flag. AllowedIPs governs what the client
  routes through the tunnel — mixing it with the firewall's ingress CIDR would
  leave a VPN that only routes traffic to itself.

Your current public IP is looked up via `https://api.ipify.org`
(`utils.get_public_ip`).

## Constants (`config/constants.py`)

### Tags and Pulumi identity

| Constant | Value | Use |
| ---------- | ------- | ----- |
| `CREATED_BY_TAG` | `"wisp"` | Value for the `created-by` tag |
| `DEFAULT_TAG` | `{"created-by": "wisp"}` | Applied to all cloud resources |
| `PULUMI_PROJECT_NAME` | `"wisp-project"` | Pulumi project |
| `get_pulumi_stack_name(provider)` | `"wisp-stack-<provider>"` | Provider-specific Pulumi stack (e.g. `wisp-stack-aws`, `wisp-stack-oci`) |
| `DEFAULT_VM_BOOT_TIMEOUT_SECONDS` | `60` | Default `vm_boot_timeout` |
| `DEFAULT_SESSION_TTL_MINUTES` | `60` | Default `max_session_ttl_minutes` (watchdog off when `0`) |

### Paths

Paths are resolved from [`platformdirs`](https://pypi.org/project/platformdirs/),
independently of the current working directory, so a built wheel works from any
directory. Three environment variables override the base directories:
`WISP_DATA_DIR`, `WISP_CONFIG_DIR`, and `WISP_LOG_DIR`.

| Constant | Path |
| ---------- | ------ |
| `WISP_DATA_DIR` | `$WISP_DATA_DIR`, else `platformdirs.user_data_path("wisp")` (Linux: `~/.local/share/wisp`) |
| `WIREGUARD_KEYS_DIR` | `WISP_DATA_DIR/keys` |
| `WIREGUARD_KEY_PATH` | `WISP_DATA_DIR/keys/wireguard-key.pem` |
| `WIREGUARD_CLIENT_CONF_PATH` | `WISP_DATA_DIR/wireguard-confs/wg0-client.conf` |
| `WISP_CONFIG_DIR` | `$WISP_CONFIG_DIR`, else `platformdirs.user_config_path("wisp")` (Linux: `~/.config/wisp`) |
| `WISP_CONFIG_FILE_NAME` | `wisp.toml` |
| `WISP_USER_CONFIG_PATH` | `WISP_CONFIG_DIR/wisp.toml` — the single `wisp.toml` Wisp reads and writes |
| `WISP_ACTIVE_SESSION_PATH` | `WISP_CONFIG_DIR/active-session.json` |
| `WISP_LOG_DIR` | `$WISP_LOG_DIR`, else `platformdirs.user_log_path("wisp")` (Linux: `~/.local/state/wisp/log`) |
| `WIREGUARD_SCRIPT_PATH` | packaged data `wisp/data/wireguard-server-install.sh` (loaded via `importlib.resources`) |

Directories are created lazily by explicit helper functions
(`ensure_data_dir`, `ensure_keys_dir`, `ensure_client_conf_dir`,
`ensure_config_dir`, `ensure_log_dir`) when the code path needs them — never at
import time. The data directory is created with mode `0700`. The project-level
`wisp.toml` is no longer read: `wisp.toml` has a single, user-level location.

### WireGuard defaults

`WIREGUARD_INTERFACE="wg0"`, `WIREGUARD_IPV4="10.66.66.1"`,
`WIREGUARD_IPV6="fd42:42:42::1"`, `WIREGUARD_DNS1="1.1.1.1"`,
`WIREGUARD_DNS2="1.0.0.1"`, `WIREGUARD_CLIENT_NAME=""`.

## AWS constants (`providers/aws/constants.py`)

| Constant | Value |
| ---------- | ------- |
| `DEFAULT_AWS_REGION` | `"us-east-2"` |
| `DEFAULT_EC2_INSTANCE_TYPE` | `"t3.micro"` |
| `DEFAULT_AMI_NAME` | `ubuntu/images/hvm-ssd-gp3/ubuntu-resolute-26.04-amd64-server-20260619` |
| `DEFAULT_AMI_OWNER` | `"099720109477"` (Canonical) |

> The default AMI is region-dependent; the code selects the most recent AMI
> matching the name filter and owner in the target region.

## OCI constants (`providers/oci/constants.py`)

| Constant | Value |
| ---------- | ------- |
| `DEFAULT_COMPARTMENT_ID` | `""` (empty → defaults to tenancy OCID) |
| `DEFAULT_SHAPE` | `"VM.Standard.A1.Flex"` |
| `DEFAULT_OCPUS` | `2.0` |
| `DEFAULT_MEMORY_IN_GBS` | `8.0` |
| `DEFAULT_IMAGE_OS` | `"Canonical Ubuntu"` |
| `DEFAULT_IMAGE_OS_VERSION` | `"24.04"` |
| `DEFAULT_VCN_CIDR` | `"10.0.0.0/16"` |
| `DEFAULT_SUBNET_CIDR` | `"10.0.0.0/24"` |
| `DEFAULT_DNS_LABEL` | `"wispvcn"` |
| `DEFAULT_SUBNET_DNS_LABEL` | `"wispsubnet"` |
| `DEFAULT_SECURITY_LIST_NAME` | `"wisp-security-list"` |
| `DEFAULT_ROUTE_TABLE_NAME` | `"wisp-route-table"` |
| `DEFAULT_INTERNET_GATEWAY_NAME` | `"wisp-internet-gateway"` |
| `DEFAULT_VCN_NAME` | `"wisp-vcn"` |
| `DEFAULT_SUBNET_NAME` | `"wisp-subnet"` |
| `DEFAULT_INSTANCE_NAME_PREFIX` | `"wisp-instance"` |
| `DEFAULT_KEY_PAIR_NAME` | `"wisp-key-pair"` |

## GCP constants (`providers/gcp/constants.py`)

| Constant | Value |
| ---------- | ------- |
| `DEFAULT_GCP_VM_INSTANCE` | `"e2-micro"` |
| `DEFAULT_GCP_REGION` | `"us-central1"` |
| `DEFAULT_GCP_ZONE` | `"us-central1-a"` |
| `DEFAULT_GCP_NETWORK` | `"default"` |
| `DEFAULT_IMAGE_PROJECT` | `"ubuntu-os-cloud"` |
| `DEFAULT_IMAGE_FAMILY` | `"ubuntu-2404-lts-amd64"` |
| `DEFAULT_GCP_FIREWALL_NAME` | `"wisp-firewall"` |
| `DEFAULT_GCP_TARGET_TAG` | `"wisp"` |

> GCP provisioning uses the **SPOT model**: `on_host_maintenance=TERMINATE`,
> `instance_termination_action=DELETE`, `automatic_restart=False`. The
> instance attaches to the default VPC network with an ephemeral external IP;
> ingress is opened via a VPC-level firewall rule targeting the instance by
> network tag.

## Daemon / protocol constants

- `UnixSocketTransport.SOCKET_PATH = "/run/wisp.sock"` (`daemon/transport.py`).
- `WindowsNamedPipeTransport.PIPE_NAME = r"\\.\pipe\wisp"` (`daemon/transport.py`).
- Random WireGuard port range: `49152–65535` (`utils/randoms.py`,
  `get_wireguard_port`).

## Logging

`utils/logger.py` configures a `wisp` logger with:

- A rotating file handler at `WISP_LOG_DIR/wisp.log` (5 MB per file, 3 backups),
  level
  `DEBUG`.
- A console (stderr) handler at level `ERROR` by default. In CLI mode,
  `main.py` raises this handler to `DEBUG`.

## File permissions by operating system

- **Linux/macOS**: `secure_file()` sets permissions to `0600` (owner-only).
- **Windows**: Uses `icacls` with well-known SIDs (`*S-1-5-18:F` for SYSTEM,
  `*S-1-5-32-544:F` for Administrators) because account names
  ("Administrators", "SYSTEM") are localized on non-English Windows
  installations, which makes `icacls` fail to resolve them by name.

## Provider configuration (`wisp.toml`)

Per-provider settings are stored in the `[aws]`, `[oci]`, `[gcp]`, and `[vps]`
sections of `wisp.toml` (the providers supported today; see `ProviderEnum`
([`src/wisp/providers/base.py`](../src/wisp/providers/base.py)) for the
current list). These sections are optional; if omitted, the provider SDK uses
its default credential chain.

Here is what a `wisp.toml` looks like with every section filled in (all
values are examples — copy [`wisp.toml.example`](../wisp.toml.example) to
`WISP_CONFIG_DIR/wisp.toml` as a starting point):

```toml
[general]
default_provider = "aws"
vm_boot_timeout = 60
wireguard_interface = "wg0"
force_current_ip = false

[aws]
region = "us-east-2"

[oci]
region = "sa-bogota-1"
compartment_ocid = "ocid1.compartment.oc1..aaaaaaa..."

[gcp]
project_id = "my-project-123456"
region = "us-central1"
zone = "us-central1-a"
credentials_path = "/home/user/keys/sa-key.json"

[vps]
host = "203.0.113.10"
ssh_user = "root"
ssh_port = 22
ssh_key_path = "~/.ssh/id_ed25519"
```

The per-field details for each section follow.

### `[aws]` section

AWS credentials are **not** configured in this section — per the
[Pulumi AWS installation & configuration
docs](https://www.pulumi.com/registry/packages/aws/installation-configuration/),
Pulumi and boto3 use the standard AWS CLI/SDK credential chain (env vars,
`~/.aws/credentials`, `~/.aws/config`, IAM roles). Configure them via
`aws configure` or environment variables instead.

|  Field   | Type  |       Default        |          Meaning           |
|----------|-------|----------------------|----------------------------|
| `region` | `str` | `AWS_DEFAULT_REGION` | AWS region for deployments |

### `[oci]` section

OCI credentials are **not** configured in this section — per the
[Pulumi OCI installation & configuration
docs](https://www.pulumi.com/registry/packages/oci/installation-configuration/),
Pulumi and the OCI SDK use the standard OCI config chain (`~/.oci/config`
profile-based, env vars). Configure them via `oci setup config` or
environment variables instead.

| Field | Type | Default | Meaning |
| ------- | ------ | --------- | --------- |
| `region` | `str` | `""` (SDK default) | OCI region for deployments (e.g. `"sa-bogota-1"`) |
| `profile` | `str` | `"DEFAULT"` | Profile name in `~/.oci/config` — used to resolve credentials for region listing/validation |
| `compartment_ocid` | `str` | `DEFAULT_COMPARTMENT_ID` | Compartment OCID (passed to the Pulumi deploy; defaults to tenancy) |

### `[gcp]` section

Everything configured in this section is passed to the Pulumi deploy — per
the [Pulumi GCP installation & configuration
docs](https://www.pulumi.com/registry/packages/gcp/installation-configuration/),
the project, region, and zone go to the `gcp.Provider` resource (whose
constructor also accepts `credentials`), and `credentials_path`
authenticates the deploy as the service account (bypassing the gcloud/ADC
chain).

| Field | Type | Default | Meaning |
| ------- | ------ | --------- | --------- |
| `project_id` | `str` | `""` (falls back to `GOOGLE_CLOUD_PROJECT` env var or `google.auth.default()`) | GCP project ID to deploy into |
| `credentials_path` | `str` | `""` (ADC) | Path to a service account key file (JSON) — authenticates the Pulumi deploy without the gcloud CLI |
| `region` | `str` | `""` (SDK default) | GCP region for deployments (e.g. `"us-central1"`) |
| `zone` | `str` | `""` (derived as `{region}-a`) | GCP zone for deployments (e.g. `"us-central1-a"`) |

All fields are optional. Cloud credentials are not configured in `wisp.toml` —
each provider uses its standard CLI/SDK credential chain for authentication:

- **AWS**: env vars, `~/.aws/credentials`, `~/.aws/config`, IAM roles
  (`aws configure` / environment variables)
- **OCI**: `~/.oci/config` profile, env vars (`OCI_TENANCY`, `OCI_USER`,
  `OCI_FINGERPRINT`, `OCI_PRIVATE_KEY_PATH`, `OCI_REGION`) — `oci setup config`
  / environment variables
- **GCP**: `GOOGLE_CREDENTIALS` / `GOOGLE_APPLICATION_CREDENTIALS` env vars,
  ADC, or gcloud (`gcloud auth application-default login`)

> **Important**: There is **no cross-provider default region**. Each provider
> has its own configured region (`[aws].region`, `[oci].region`,
> `[gcp].region`), which is used when `-r/--region` is not supplied on the CLI.
>
> **What each section actually configures**: AWS → region only; OCI → region,
> profile (for credential resolution/validation), and `compartment_ocid`
> (passed to the Pulumi deploy); GCP → everything (all fields are passed to
> the Pulumi deploy or used by wisp's own SDK calls).

### `[vps]` section

Bring-your-own-server: Wisp configures WireGuard on a Linux host you already
own, over SSH. Nothing is provisioned — no Pulumi, no cloud API — and on
cleanup Wisp tears down only the interface it created (named `wisp-<hex>`,
never your own `wg0`): the tunnel is brought down (which also removes the
firewall rules the installer added), the config file is deleted, and the
packages Wisp installed are removed too — but only those a manifest records
as missing before the install, and only while your server has no other
WireGuard config. `iptables` and `resolvconf` are never removed.
Authentication is public-key based; password authentication is not supported.

| Field | Type | Default | Meaning |
| ------- | ------ | --------- | --------- |
| `host` | `str` | `""` (required) | The server's public hostname or IP |
| `ssh_user` | `str` | `"root"` | SSH user to connect as (needs sudo for the installer) |
| `ssh_port` | `int` | `22` | SSH port |
| `ssh_key_path` | `str` | `""` (agent/default keys) | Path to YOUR private key — it is used as-is, never generated or deleted |

The host's `authorized_keys` must contain your public key. The TUI deploy
screen also collects these fields per run (the TOML section only pre-fills
the form), and the CLI overrides them with `--host`, `--ssh-user`,
`--ssh-port`, and `--ssh-key-path`.

## Pulumi backend

Stack state (including the encrypted per-deployment SSH key) lives in the
Pulumi backend you authenticated with:

- `pulumi login` (from the Quick start) defaults to the
  [Pulumi Cloud](https://www.pulumi.com/docs/iac/concepts/state-and-backends/)
  service — the state is stored under your organization and survives local
  machine loss.
- `pulumi login --local` or the `PULUMI_CONFIG_PASSPHRASE` flow keeps the
  state on disk, encrypted with the passphrase. If you lose the machine and
  that file, the state is gone with it: `wisp gc` would no longer see the
  leftover stacks, and cloud resources would have to be cleaned from the
  provider console.

The backend choice affects `wisp gc`: it inspects the stacks of whichever
backend is currently authenticated.

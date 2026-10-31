"""Deploy screen: pick provider and region, review, and launch a deployment."""

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import CenterMiddle, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, Select, Static, Switch

from wisp.cli.screens.base import WispScreen
from wisp.cli.screens.progress import ProgressScreen
from wisp.config.settings import get_default_region_for
from wisp.providers import PROVIDERS_MAP, ProviderEnum
from wisp.providers.base import get_provider_display_name
from wisp.providers.vps import VPS_PLACEHOLDER_REGION
from wisp.wireguard import tunnel_conflict

# Static region list used until live AWS regions are fetched (or if that fails).
FALLBACK_AWS_REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "eu-west-3",
    "ap-southeast-1",
    "ap-northeast-1",
    "sa-east-1",
]

# Static region list for OCI (fallback)
FALLBACK_OCI_REGIONS = [
    "us-ashburn-1",
    "us-phoenix-1",
    "eu-frankfurt-1",
    "uk-london-1",
    "ap-tokyo-1",
    "ap-sydney-1",
    "ap-seoul-1",
    "ap-mumbai-1",
    "sa-saopaulo-1",
    "ca-toronto-1",
    "eu-zurich-1",
    "me-jeddah-1",
]

# Static region list for GCP (fallback)
FALLBACK_GCP_REGIONS = [
    "us-central1",
    "us-east1",
    "us-west1",
    "europe-west1",
    "europe-west3",
    "asia-east1",
    "asia-southeast1",
    "southamerica-east1",
    "australia-southeast1",
]


class DeployScreen(WispScreen):
    """Provider/region selection with a live deploy summary."""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    CSS = """
    /* The deploy card must fit a 24-row terminal without scrolling: a scroll on
       this screen hides exactly what matters — the Start/Cancel buttons. On a
       24-row terminal the card's content area is 20 rows, so every widget must
       fit within that budget. Margins are trimmed hard and the card padding is
       dropped; the intro subtitle was removed (title + numbered steps already
       carry it) and the TTL row keeps no top margin — that is what pays for
       the bordered Input and Switch, which keep their full default look. */
    #deploy-card {
        padding: 0 2;
        height: auto;
    }

    #deploy-card .title {
        margin-bottom: 0;
    }

    #deploy-container {
        height: auto;
    }

    #deploy-container Label {
        color: #94a3b8;
    }

    #region-status {
        height: 1;
    }

    /* VPS connection fields: hidden unless the Custom VPS provider is
       selected; the region step hides in exactly the opposite case. Both
       containers need explicit `height: auto` — a plain Vertical defaults to
       `1fr` and would balloon to fill the card, leaving a huge blank gap. */
    #vps-fields {
        display: none;
        height: auto;
    }

    #region-step {
        height: auto;
    }

    /* TTL row: Label, bordered Switch and bordered Input keep their default
       look (the row measures 3 — the Switch's tall border and the Input's
       height are what make it read as a proper field), so the row gives up
       its top margin instead of its affordances. */
    #ttl-row {
        height: auto;
        margin-top: 0;
    }

    #ttl-row Label {
        color: #94a3b8;
        width: 1fr;
    }

    #ttl-row Input {
        width: 12;
    }

    #deploy-summary {
        background: #0b0f19;
        border: solid #334155;
        padding: 0;
        height: auto;
    }

    .btn-group Button {
        margin-right: 1;
        width: 1fr;
    }

    .btn-group {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card", id="deploy-card"):
                yield Static(
                    "[bold cyan]Deployment Wizard[/bold cyan]", classes="title"
                )

                with Vertical(id="deploy-container"):
                    provider_val = self._initial_provider()
                    yield Label("1. Cloud Provider:")
                    yield Select(
                        options=[
                            ("Amazon Web Services (AWS)", "aws"),
                            ("Oracle Cloud Infrastructure (OCI)", "oci"),
                            ("Google Cloud Platform (GCP)", "gcp"),
                            ("Custom VPS (bring your own server)", "vps"),
                        ],
                        value=provider_val,
                        allow_blank=False,
                        id="select-provider",
                    )

                    with Vertical(id="region-step"):
                        yield Label("2. Deployment Region:")
                        current_region = self.app.state.selected_region  # type: ignore[attr-defined]
                        regions = self._get_fallback_regions(provider_val)
                        if current_region in regions:
                            regions = [
                                current_region,
                                *[r for r in regions if r != current_region],
                            ]
                        yield Select(
                            options=[(r, r) for r in regions],
                            value=current_region,
                            allow_blank=False,
                            id="select-region",
                        )
                        yield Static(
                            "[dim]Syncing regions...[/dim]",
                            id="region-status",
                        )

                    # Bring-your-own-server fields: shown only for the VPS
                    # provider, filled from the resolved credentials.
                    with Vertical(id="vps-fields"):
                        yield Label("2. VPS Host (IP or hostname):")
                        yield Input(
                            id="input-vps-host",
                            value=self.app.state.vps_credentials.host,  # type: ignore[attr-defined]
                            placeholder="e.g. 203.0.113.10",
                        )
                        yield Label("SSH User:")
                        yield Input(
                            id="input-vps-ssh-user",
                            value=self.app.state.vps_credentials.ssh_user,  # type: ignore[attr-defined]
                            placeholder="e.g. root",
                        )
                        yield Label("SSH Port:")
                        yield Input(
                            id="input-vps-ssh-port",
                            value=str(self.app.state.vps_credentials.ssh_port),  # type: ignore[attr-defined]
                            type="integer",
                            placeholder="22",
                        )
                        yield Label(
                            "SSH Private Key Path (empty = agent/default keys):"
                        )
                        yield Input(
                            id="input-vps-ssh-key-path",
                            value=self.app.state.vps_credentials.ssh_key_path,  # type: ignore[attr-defined]
                            placeholder="e.g. ~/.ssh/id_ed25519",
                        )
                        yield Label(
                            "WireGuard Port (0 = random; open it on your firewall):"
                        )
                        yield Input(
                            id="input-vps-wireguard-port",
                            value=str(self.app.state.config.wireguard_port),  # type: ignore[attr-defined]
                            type="integer",
                            placeholder="0",
                        )

                    with Horizontal(id="ttl-row"):
                        yield Label("Session TTL watchdog (minutes, 0 = off):")
                        yield Switch(
                            id="switch-ttl",
                            value=self.app.state.effective_session_ttl() > 0,  # type: ignore[attr-defined]
                        )
                        yield Input(
                            id="input-ttl-minutes",
                            value=str(self.app.state.effective_session_ttl()),  # type: ignore[attr-defined]
                            type="integer",
                        )

                    yield Label("3. Summary & Confirmation:")
                    yield Static(
                        self._build_summary(current_region, provider_val),
                        id="deploy-summary",
                    )

                with Horizontal(classes="btn-group"):
                    yield Button(
                        "Start Deployment",
                        id="btn-start-deploy",
                        variant="primary",
                        classes="btn-primary",
                    )
                    yield Button(
                        "Cancel",
                        id="btn-cancel",
                        variant="default",
                        classes="btn-secondary",
                    )
        yield Footer()

    def _initial_provider(self) -> str:
        """Return the provider to preselect in the provider Select.

        Falls back to ``"aws"`` when the state carries a value the Select has no
        option for, so the widget never starts on a blank selection.
        """
        candidate = str(self.app.state.provider_name)  # type: ignore[attr-defined]
        known = {p.value for p in ProviderEnum}
        return candidate if candidate in known else ProviderEnum.AWS.value

    def _get_fallback_regions(self, provider: str) -> list[str]:
        """Get fallback regions for a provider."""
        if provider == "oci":
            return FALLBACK_OCI_REGIONS
        elif provider == "gcp":
            return FALLBACK_GCP_REGIONS
        elif provider == "vps":
            return [VPS_PLACEHOLDER_REGION]
        return FALLBACK_AWS_REGIONS

    def _preferred_region(self, provider_val: str, regions: list[str]) -> str:
        """Pick the region the Select should pre-select for a provider.

        Mirrors the initial pre-selection (provider's configured default from
        ``[<provider>].region`` in ``wisp.toml``) so switching providers keeps
        landing on the region the user actually deploys to, instead of the
        first list entry. Falls back to keeping a still-valid selection, then
        to the first region.

        Args:
            provider_val (str): Provider identifier the regions belong to.
            regions (list[str]): The available regions for that provider.

        Returns:
            str: The region to pre-select.
        """
        default_region = get_default_region_for(provider_val)
        if default_region and default_region in regions:
            return default_region
        region_select = self.query_one("#select-region", Select)
        if region_select.value in regions:
            return str(region_select.value)
        return regions[0]

    def on_mount(self) -> None:
        self._rendered_region_defaults = self._current_region_defaults()
        self._toggle_vps_fields(str(self.query_one("#select-provider", Select).value))
        self.fetch_live_regions()

    def on_screen_resume(self) -> None:
        # Installed screens keep their widgets across visits: compose-time
        # values go stale after the config screen saves, so re-read them here.
        self._refresh_config_defaults()
        region_select = self.query_one("#select-region", Select)
        current = str(region_select.value)
        self.query_one("#deploy-summary", Static).update(
            self._build_summary(current, self._initial_provider())
        )

    def _current_region_defaults(self) -> dict[str, str | None]:
        """Snapshot the configured default region per region-based provider.

        Args:
            None.

        Returns:
            dict[str, str | None]: Provider identifier -> configured region
            (``None`` when unset), for the providers that have a region step.
        """
        return {
            ProviderEnum.AWS.value: get_default_region_for(ProviderEnum.AWS.value)
            or None,
            ProviderEnum.OCI.value: get_default_region_for(ProviderEnum.OCI.value)
            or None,
            ProviderEnum.GCP.value: get_default_region_for(ProviderEnum.GCP.value)
            or None,
        }

    def _refresh_config_defaults(self) -> None:
        """Re-read the config-backed values this screen pre-filled at mount.

        The TTL row and the VPS tunnel port always mirror the current
        ``WispConfig`` (per-run overrides happen at deploy time and are not
        stored here). The region is only re-selected when the selected
        provider's configured default changed since the screen last rendered —
        an explicit user pick survives the round trip untouched.
        """
        state = self.app.state  # type: ignore[attr-defined]
        cfg = state.config
        self.query_one("#input-ttl-minutes", Input).value = str(
            cfg.max_session_ttl_minutes
        )
        self.query_one("#switch-ttl", Switch).value = cfg.max_session_ttl_minutes > 0
        self.query_one("#input-vps-wireguard-port", Input).value = str(
            cfg.wireguard_port
        )

        provider_val = str(self.query_one("#select-provider", Select).value)
        new_defaults = self._current_region_defaults()
        old_defaults = getattr(self, "_rendered_region_defaults", None) or {}
        if new_defaults.get(provider_val) != old_defaults.get(provider_val):
            self._rendered_region_defaults = new_defaults
            regions = getattr(self, "_last_regions", None) or []
            if regions:
                region_select = self.query_one("#select-region", Select)
                preferred = self._preferred_region(provider_val, regions)
                if preferred != str(region_select.value):
                    region_select.value = preferred

    @work(thread=True)
    def fetch_live_regions(self) -> None:
        """Fetch live regions for the selected provider in a background thread.

        The provider is captured at fetch start and threaded through every UI
        callback: a slow response for a provider the user already left is
        dropped instead of overwriting the Select with another provider's
        regions (fast-switching race), and a failed fetch repopulates the
        Select with the provider's standard list instead of leaving the
        previous provider's options in place.
        """
        provider_val = str(self.query_one("#select-provider", Select).value)
        try:
            provider_cls = PROVIDERS_MAP[ProviderEnum(provider_val)]
            provider = provider_cls()
            regions = list(provider.get_available_regions())
            if regions:
                self.app.call_from_thread(
                    self._update_regions_ui, provider_val, sorted(regions)
                )
            else:
                self.app.call_from_thread(self._region_fetch_failed, provider_val)
        except Exception:
            self.app.call_from_thread(self._region_fetch_failed, provider_val)

    def _update_regions_ui(self, provider_val: str, regions: list[str]) -> None:
        try:
            if str(self.query_one("#select-provider", Select).value) != provider_val:
                return  # Stale fetch: the user already switched providers.
            self._last_regions = list(regions)
            region_select = self.query_one("#select-region", Select)
            current_val = self._preferred_region(provider_val, regions)
            region_select.set_options([(r, r) for r in regions])
            region_select.value = current_val

            provider_name = get_provider_display_name(provider_val)
            status = self.query_one("#region-status", Static)
            status.update(
                f"[green]✓ {len(regions)} regions available for {provider_name}[/green]"
            )
            self.query_one("#deploy-summary", Static).update(
                self._build_summary(str(current_val), provider_val)
            )
        except Exception:
            pass

    def _region_fetch_failed(self, provider_val: str) -> None:
        try:
            if str(self.query_one("#select-provider", Select).value) != provider_val:
                return  # Stale fetch: the user already switched providers.
            # Keeping the previous provider's options here would let the user
            # deploy to a region that does not exist for the selected provider,
            # so the Select is repopulated with the standard list.
            regions = self._get_fallback_regions(provider_val)
            self._last_regions = list(regions)
            region_select = self.query_one("#select-region", Select)
            current_val = self._preferred_region(provider_val, regions)
            region_select.set_options([(r, r) for r in regions])
            region_select.value = current_val

            provider_name = get_provider_display_name(provider_val)
            status = self.query_one("#region-status", Static)
            status.update(f"[dim](Using standard {provider_name} region list)[/dim]")
            self.query_one("#deploy-summary", Static).update(
                self._build_summary(str(current_val), provider_val)
            )
        except Exception:
            pass

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "select-provider":
            # Provider changed - fetch new regions and swap the middle step:
            # cloud providers pick a region, Custom VPS shows its fields.
            self._toggle_vps_fields(str(event.value))
            self.fetch_live_regions()
        elif event.select.id == "select-region" and event.value is not Select.BLANK:
            self.query_one("#deploy-summary", Static).update(
                self._build_summary(
                    str(event.value),
                    str(self.query_one("#select-provider", Select).value),
                )
            )

    def _toggle_vps_fields(self, provider_val: str) -> None:
        """Show the VPS connection fields and hide the region step (or swap
        back) when the provider selection changes.

        Args:
            provider_val (str): Newly selected provider identifier.
        """
        try:
            is_vps = provider_val == ProviderEnum.VPS.value
            self.query_one("#vps-fields", Vertical).styles.display = (
                "block" if is_vps else "none"
            )
            self.query_one("#region-step", Vertical).styles.display = (
                "none" if is_vps else "block"
            )
        except Exception:
            pass

    def _build_summary(self, region: str, provider_val: str) -> str:
        """Build the deployment summary text for the given region.

        The provider is passed in rather than read from the widget: this runs
        during :meth:`compose`, before the Selects exist in the DOM, so querying
        ``#select-provider`` at that point raises ``NoMatches``.

        Args:
            region (str): Region to describe.
            provider_val (str): Selected provider identifier.

        Returns:
            str: Markup for the summary panel.
        """
        state = self.app.state  # type: ignore[attr-defined]
        cfg = state.config
        provider_name = get_provider_display_name(provider_val)
        port_text = f"UDP {cfg.wireguard_port}" if cfg.wireguard_port > 0 else "Random"
        ip_mode = "Your IP only (/32)" if cfg.force_current_ip else "Any IP (0.0.0.0/0)"

        # Custom VPS: the host is the deployment target; there is no region
        # and no cloud credential chain — SSH access is the whole story.
        if provider_val == ProviderEnum.VPS.value:
            creds = state.vps_credentials
            host = creds.host or "(not set)"
            return (
                f"[cyan]Target:[/cyan] {provider_name} ({host})   "
                f"[cyan]Timeout:[/cyan] {cfg.vm_boot_timeout}s\n"
                f"[cyan]Port:[/cyan] {port_text}   "
                f"[cyan]SSH user:[/cyan] {creds.ssh_user}@{creds.ssh_port}"
            )

        # Credential status
        creds = state.get_credentials_for_provider(provider_val)
        cred_status = (
            "[green]✓ Configured[/green]"
            if creds and creds.is_explicitly_configured()
            else "[yellow]⚠ Auto-detected[/yellow]"
        )

        return (
            f"[cyan]Target:[/cyan] {provider_name} ({region})   [cyan]Timeout:[/cyan] {cfg.vm_boot_timeout}s\n"
            f"[cyan]Port:[/cyan] {port_text}   [cyan]DNS:[/cyan] {cfg.wireguard_dns1}, {cfg.wireguard_dns2}\n"
            f"[cyan]Firewall:[/cyan] {ip_mode}   [cyan]Credentials:[/cyan] {cred_status}"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-start-deploy":
            self.start_deployment()
        elif event.button.id == "btn-cancel":
            self.action_back()

    def start_deployment(self) -> None:
        """Persist the selection into state and push the progress screen."""
        state = self.app.state  # type: ignore[attr-defined]
        provider_val = str(self.query_one("#select-provider", Select).value)
        region_val = str(self.query_one("#select-region", Select).value)

        state.provider_name = provider_val
        state.selected_region = region_val

        # Per-deployment TTL watchdog: the switch decides whether it is armed
        # at all, and the minutes field overrides the configured default.
        ttl_switch = self.query_one("#switch-ttl", Switch)
        ttl_raw = self.query_one("#input-ttl-minutes", Input).value.strip()
        state.session_ttl_minutes = (
            max(0, int(ttl_raw)) if ttl_switch.value and ttl_raw.isdigit() else 0
        )

        if provider_val == ProviderEnum.VPS.value:
            self._apply_vps_fields(state)

        # Refuse before provisioning: `wg0` is the default name of every
        # WireGuard setup, and a predictable local conflict must not cost a
        # deployed VM.
        conflict = tunnel_conflict()
        if conflict:
            self.notify(conflict, title="Tunnel conflict", severity="error", timeout=15)
            return

        self.app.push_screen(ProgressScreen())

    def _apply_vps_fields(self, state) -> None:
        """Read the VPS connection fields into the state's credentials.

        The form is the source of truth for this deployment; the TOML ``[vps]``
        section only pre-fills it.

        Args:
            state (AppState): The app's shared state object.
        """
        host = self.query_one("#input-vps-host", Input).value.strip()
        ssh_user = self.query_one("#input-vps-ssh-user", Input).value.strip() or "root"
        ssh_port_raw = self.query_one("#input-vps-ssh-port", Input).value.strip()
        key_path = self.query_one("#input-vps-ssh-key-path", Input).value.strip()
        wg_port_raw = self.query_one("#input-vps-wireguard-port", Input).value.strip()

        creds = state.vps_credentials
        creds.host = host
        creds.ssh_user = ssh_user
        creds.ssh_port = int(ssh_port_raw) if ssh_port_raw.isdigit() else 22
        creds.ssh_key_path = key_path

        # The tunnel port feeds WispConfig: 0 keeps the installer's random
        # choice (the user opens the port themselves afterwards).
        state.config.wireguard_port = int(wg_port_raw) if wg_port_raw.isdigit() else 0

    def action_back(self) -> None:
        self.app.pop_screen()

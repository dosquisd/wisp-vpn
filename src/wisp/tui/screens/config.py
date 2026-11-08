"""Configuration screen: edit the in-memory session :class:`WispConfig` and
inspect (read-only) the resolved provider credentials.
"""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import CenterMiddle, Horizontal, Vertical
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    Static,
    Switch,
)

from wisp.config.settings import describe_config_sources, save_session_config
from wisp.tui.screens.base import WispScreen
from wisp.tui.screens.modal import ConfirmActionScreen, ConfirmDiscardScreen


def _mask(value: str, keep: int = 4) -> str:
    """Mask a secret, keeping only its last few characters visible."""
    if not value:
        return "[dim]— no configurado —[/dim]"
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


def _cred_line(label: str, value: str, *, sensitive: bool, revealed: bool) -> str:
    """Render one ``label: value`` line, masking sensitive values by default."""
    if not sensitive:
        shown = value if value else "[dim]— default —[/dim]"
    else:
        shown = value if revealed else _mask(value)
    return f"[dim]{label}:[/dim] {shown}"


class ConfigScreen(WispScreen):
    """Form to edit session config in memory, with inline validation."""

    BINDINGS = [
        Binding("escape", "back", "Back", show=True),
    ]

    CSS = """
    #form-container {
        height: auto;
        margin-bottom: 1;
    }

    #config-source-note {
        margin-bottom: 1;
    }

    #form-container Label {
        color: #94a3b8;
    }

    #switch-row {
        height: 3;
        align: left middle;
        margin-top: 1;
        margin-bottom: 1;
    }

    #switch-row Label {
        margin-top: 0;
        margin-right: 2;
    }

    #credentials-heading {
        margin-top: 0;
    }

    #credentials-section {
        height: auto;
    }

    ConfigScreen .subtitle {
        margin-bottom: 0;
    }

    ConfigScreen .title {
        margin-bottom: 0;
    }

    Collapsible {
        margin-bottom: 0;
    }

    Collapsible Label {
        color: #94a3b8;
    }

    Collapsible Input {
        margin-bottom: 1;
    }

    .cred-reveal-row {
        height: 3;
        align: left middle;
        margin-top: 1;
    }

    .cred-reveal-row Label {
        margin-top: 0;
        margin-right: 2;
    }

    .btn-group {
        height: 3;
        margin-top: 1;
    }

    .btn-group Button {
        margin-right: 1;
        width: 1fr;
    }

    #error-message {
        color: #ef4444;
        text-align: center;
        height: 1;
    }

    #dirty-marker {
        color: #facc15;
        text-align: center;
        height: 1;
    }
    """

    def __init__(self) -> None:
        """Start with no baseline and no pending navigation decision."""
        super().__init__()
        self._baseline: tuple[str, str, str, str, str, bool, str, str, str] | None = (
            None
        )
        self._pending_action: str = ""
        self._preserve_dirty: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with CenterMiddle():
            with Vertical(classes="card"):
                yield Static("[bold cyan]Session Settings[/bold cyan]", classes="title")
                yield Static(
                    "Modify deployment parameters for this session.",
                    classes="subtitle",
                )
                yield Static(describe_config_sources(), id="config-source-note")
                yield Static("", id="dirty-marker")

                with Vertical(id="form-container"):
                    yield Label("VM Boot Timeout (seconds):")
                    yield Input(
                        id="input-vm-boot-timeout",
                        value=str(self.app.state.config.vm_boot_timeout),
                        type="integer",
                    )

                    yield Label("WireGuard Port (0 = random 49152-65535):")
                    yield Input(
                        id="input-wireguard-port",
                        value=str(self.app.state.config.wireguard_port),
                        type="integer",
                    )

                    yield Label("WireGuard Interface:")
                    yield Input(
                        id="input-wireguard-interface",
                        value=self.app.state.config.wireguard_interface,
                    )

                    yield Label("Primary DNS:")
                    yield Input(
                        id="input-dns1",
                        value=self.app.state.config.wireguard_dns1,
                    )

                    yield Label("Secondary DNS:")
                    yield Input(
                        id="input-dns2",
                        value=self.app.state.config.wireguard_dns2,
                    )

                    yield Label("Session TTL (minutes, 0 = watchdog off):")
                    yield Input(
                        id="input-session-ttl",
                        value=str(self.app.state.config.max_session_ttl_minutes),
                        type="integer",
                    )

                    with Horizontal(id="switch-row"):
                        yield Label("Restrict access to my public IP only:")
                        yield Switch(
                            id="switch-force-ip",
                            value=self.app.state.config.force_current_ip,
                        )

                yield Static(
                    "[bold]Credentials[/bold] [dim](read-only — from wisp.toml or env)[/dim]",
                    id="credentials-heading",
                    classes="subtitle",
                )
                with Vertical(id="credentials-section"):
                    with Collapsible(
                        title=self._aws_cred_title(),
                        collapsed=True,
                        id="cred-aws-collapsible",
                    ):
                        yield Label("Region (editable — saved to wisp.toml):")
                        yield Input(
                            id="input-aws-region",
                            value=self.app.state.aws_credentials.region,
                            placeholder="e.g. us-east-2",
                        )
                        yield Static(
                            self._aws_cred_text(revealed=False), id="aws-cred-text"
                        )
                        with Horizontal(classes="cred-reveal-row"):
                            yield Label("Show full values:")
                            yield Switch(id="switch-reveal-aws", value=False)

                    with Collapsible(
                        title=self._oci_cred_title(),
                        collapsed=True,
                        id="cred-oci-collapsible",
                    ):
                        yield Label("Region (editable — saved to wisp.toml):")
                        yield Input(
                            id="input-oci-region",
                            value=self.app.state.oci_credentials.region,
                            placeholder="e.g. us-ashburn-1",
                        )
                        yield Static(
                            self._oci_cred_text(revealed=False), id="oci-cred-text"
                        )
                        with Horizontal(classes="cred-reveal-row"):
                            yield Label("Show full values:")
                            yield Switch(id="switch-reveal-oci", value=False)

                    with Collapsible(
                        title=self._gcp_cred_title(),
                        collapsed=True,
                        id="cred-gcp-collapsible",
                    ):
                        yield Label("Region (editable — saved to wisp.toml):")
                        yield Input(
                            id="input-gcp-region",
                            value=self.app.state.gcp_credentials.region,
                            placeholder="e.g. us-central1",
                        )
                        yield Static(
                            self._gcp_cred_text(revealed=False), id="gcp-cred-text"
                        )
                        with Horizontal(classes="cred-reveal-row"):
                            yield Label("Show full values:")
                            yield Switch(id="switch-reveal-gcp", value=False)

                yield Static("", id="error-message")

                with Horizontal(classes="btn-group"):
                    yield Button(
                        "Save",
                        id="btn-save",
                        variant="primary",
                        classes="btn-primary",
                    )
                    yield Button(
                        "Reset",
                        id="btn-reset",
                        variant="default",
                        classes="btn-secondary",
                    )
                    yield Button(
                        "Back",
                        id="btn-back",
                        variant="default",
                        classes="btn-secondary",
                    )
        yield Footer()

    # ─── Credentials (read-only, masked) ──────────────────────────

    def _aws_cred_title(self) -> str:
        creds = self.app.state.aws_credentials
        status = "Configured" if creds.is_explicitly_configured() else "Auto-detected"
        return f"AWS — {status}"

    def _oci_cred_title(self) -> str:
        creds = self.app.state.oci_credentials
        status = "Configured" if creds.is_explicitly_configured() else "Auto-detected"
        return f"OCI — {status}"

    def _gcp_cred_title(self) -> str:
        creds = self.app.state.gcp_credentials
        if creds.is_explicitly_configured():
            return "GCP — Configured"
        return "GCP — Auto-detected (ADC)"

    def _aws_cred_text(self, revealed: bool) -> str:
        creds = self.app.state.aws_credentials
        return "\n".join(
            [
                _cred_line("Perfil", creds.profile, sensitive=False, revealed=revealed),
                _cred_line(
                    "Access Key ID",
                    creds.access_key_id,
                    sensitive=True,
                    revealed=revealed,
                ),
                _cred_line(
                    "Secret Access Key",
                    creds.secret_access_key,
                    sensitive=True,
                    revealed=revealed,
                ),
                _cred_line(
                    "Session Token",
                    creds.session_token,
                    sensitive=True,
                    revealed=revealed,
                ),
            ]
        )

    def _oci_cred_text(self, revealed: bool) -> str:
        creds = self.app.state.oci_credentials
        return "\n".join(
            [
                _cred_line(
                    "Tenancy OCID",
                    creds.tenancy_ocid,
                    sensitive=True,
                    revealed=revealed,
                ),
                _cred_line(
                    "User OCID", creds.user_ocid, sensitive=True, revealed=revealed
                ),
                _cred_line(
                    "Fingerprint", creds.fingerprint, sensitive=True, revealed=revealed
                ),
                _cred_line(
                    "Private Key Path",
                    creds.private_key_path,
                    sensitive=False,
                    revealed=revealed,
                ),
                _cred_line(
                    "Compartment OCID",
                    creds.get_compartment_ocid(),
                    sensitive=True,
                    revealed=revealed,
                ),
                _cred_line("Perfil", creds.profile, sensitive=False, revealed=revealed),
            ]
        )

    def _gcp_cred_text(self, revealed: bool) -> str:
        creds = self.app.state.gcp_credentials
        return "\n".join(
            [
                _cred_line(
                    "Project ID", creds.project_id, sensitive=False, revealed=revealed
                ),
                _cred_line("Region", creds.region, sensitive=False, revealed=revealed),
                _cred_line(
                    "Zona", creds.get_zone(), sensitive=False, revealed=revealed
                ),
                _cred_line(
                    "Credentials Path",
                    creds.credentials_path,
                    sensitive=True,
                    revealed=revealed,
                ),
            ]
        )

    def on_switch_changed(self, event: Switch.Changed) -> None:
        # force-ip is part of the form, so any switch flip may change the
        # dirty state; the reveal toggles are just below and independent.
        self._refresh_dirty_state()

        if event.switch.id == "switch-reveal-aws":
            self.query_one("#aws-cred-text", Static).update(
                self._aws_cred_text(revealed=event.value)
            )
        elif event.switch.id == "switch-reveal-oci":
            self.query_one("#oci-cred-text", Static).update(
                self._oci_cred_text(revealed=event.value)
            )
        elif event.switch.id == "switch-reveal-gcp":
            self.query_one("#gcp-cred-text", Static).update(
                self._gcp_cred_text(revealed=event.value)
            )

    # ─── General settings form ────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._request_save()
        elif event.button.id == "btn-reset":
            self._request_reset()
        elif event.button.id == "btn-back":
            self.action_back()

    def _request_save(self) -> None:
        """Ask before persisting the form to ``wisp.toml``.

        Validation runs first so an invalid form never opens the modal — it
        shows the inline error instead. A clean form (no edits since the last
        save/load) has nothing to persist, so it notifies instead of asking.
        Cancelling the modal keeps every current edit (the dirty state must
        survive the modal closing).
        """
        if self._validate_form() is None:
            return
        if not self._is_dirty():
            self.notify("No changes to save.", severity="information")
            return
        self._preserve_dirty = self._is_dirty()
        self.app.push_screen(
            ConfirmActionScreen(
                title="Save settings?",
                body="Changes will apply to this session and be written\n"
                "to the active wisp.toml.",
                confirm_label="Save",
            ),
            self._on_save_choice,
        )

    def _on_save_choice(self, confirmed: bool | None) -> None:
        if confirmed:
            self.save_config()

    def _validate_form(self) -> tuple[int, int, str, str, str, bool, int] | None:
        """Read the editable ``[general]`` fields and validate them.

        Shows an inline error and returns ``None`` when any field is invalid
        (timeout < 5, port out of ``0-65535``, negative TTL, empty
        interface/DNS); otherwise the parsed
        ``(timeout, port, interface, dns1, dns2, force_ip, ttl)``.
        """
        timeout_raw = self.query_one("#input-vm-boot-timeout", Input).value.strip()
        port_raw = self.query_one("#input-wireguard-port", Input).value.strip()
        interface = self.query_one("#input-wireguard-interface", Input).value.strip()
        dns1 = self.query_one("#input-dns1", Input).value.strip()
        dns2 = self.query_one("#input-dns2", Input).value.strip()
        ttl_raw = self.query_one("#input-session-ttl", Input).value.strip()
        force_ip = self.query_one("#switch-force-ip", Switch).value

        error_label = self.query_one("#error-message", Static)

        try:
            timeout_val = int(timeout_raw)
            if timeout_val < 5:
                error_label.update("[!] Timeout must be at least 5 seconds.")
                return None
        except ValueError:
            error_label.update("[!] Timeout must be an integer.")
            return None

        try:
            port_val = int(port_raw)
            if port_val < 0 or port_val > 65535:
                error_label.update("[!] Port must be between 0 and 65535.")
                return None
        except ValueError:
            error_label.update("[!] Port must be an integer.")
            return None

        try:
            ttl_val = int(ttl_raw)
            if ttl_val < 0:
                error_label.update("[!] Session TTL cannot be negative.")
                return None
        except ValueError:
            error_label.update("[!] Session TTL must be an integer.")
            return None

        if not interface:
            error_label.update("[!] WireGuard interface name cannot be empty.")
            return None

        if not dns1 or not dns2:
            error_label.update("[!] DNS servers cannot be empty.")
            return None

        return (timeout_val, port_val, interface, dns1, dns2, force_ip, ttl_val)

    def save_config(self) -> None:
        """Validate the form and, if valid, persist it to ``AppState`` *and*
        to the active ``wisp.toml`` (see :func:`save_session_config`).

        Reached normally as the confirmed step after :meth:`_request_save`'s
        modal, but also called directly (e.g. the discard modal's "Save"),
        so it re-validates here — an invalid form never reaches the disk, it
        just bounces with an inline error.
        """
        validated = self._validate_form()
        if validated is None:
            return
        timeout_val, port_val, interface, dns1, dns2, force_ip, ttl_val = validated

        aws_region = self.query_one("#input-aws-region", Input).value.strip()
        oci_region = self.query_one("#input-oci-region", Input).value.strip()
        gcp_region = self.query_one("#input-gcp-region", Input).value.strip()

        # Update in-memory state
        state = self.app.state
        state.config.vm_boot_timeout = timeout_val
        state.config.wireguard_port = port_val
        state.config.wireguard_interface = interface
        state.config.wireguard_dns1 = dns1
        state.config.wireguard_dns2 = dns2
        state.config.force_current_ip = force_ip
        state.config.max_session_ttl_minutes = ttl_val

        # Persist to disk: [general] fully, plus just the `region` key of
        # [aws]/[oci]/[gcp] — everything else in those sections (credentials,
        # profile, etc.) is left untouched, since this form never edits them.
        error_label = self.query_one("#error-message", Static)
        try:
            target_path = save_session_config(
                state.config,
                provider_regions={
                    "aws": aws_region,
                    "oci": oci_region,
                    "gcp": gcp_region,
                },
            )
        except Exception as exc:  # e.g. tomli-w missing, permissions, disk full
            error_label.update(f"[!] Could not save to disk: {exc}")
            return

        # Regions may have changed what's "explicitly configured" for each
        # provider, so re-resolve credentials and refresh the collapsibles
        # to match — and reset the reveal toggles as a safe default.
        state.refresh_credentials()
        self._refresh_credentials_ui()

        self.notify(f"Settings saved to {target_path}", severity="information")
        self.app.pop_screen()

    def _refresh_credentials_ui(self) -> None:
        """Re-render the credentials collapsibles from freshly resolved state."""
        try:
            self.query_one(
                "#cred-aws-collapsible", Collapsible
            ).title = self._aws_cred_title()
            self.query_one("#aws-cred-text", Static).update(
                self._aws_cred_text(revealed=False)
            )
            self.query_one("#switch-reveal-aws", Switch).value = False

            self.query_one(
                "#cred-oci-collapsible", Collapsible
            ).title = self._oci_cred_title()
            self.query_one("#oci-cred-text", Static).update(
                self._oci_cred_text(revealed=False)
            )
            self.query_one("#switch-reveal-oci", Switch).value = False

            self.query_one(
                "#cred-gcp-collapsible", Collapsible
            ).title = self._gcp_cred_title()
            self.query_one("#gcp-cred-text", Static).update(
                self._gcp_cred_text(revealed=False)
            )
            self.query_one("#switch-reveal-gcp", Switch).value = False
        except Exception:
            pass

    def reset_config(self) -> None:
        """Reset the session config to what's in ``wisp.toml`` and repopulate
        the form from that reloaded config (not from hardcoded defaults —
        see note below)."""
        state = self.app.state
        state.reset_config()

        # NOTE: previously this repopulated the form from a bare
        # WispConfig() (i.e. hardcoded constant defaults), which drifted
        # from what state.reset_config() actually loaded whenever wisp.toml
        # overrides any [general] value. Reading back from state.config
        # keeps the form and the in-memory state in sync.
        cfg = state.config
        self.query_one("#input-vm-boot-timeout", Input).value = str(cfg.vm_boot_timeout)
        self.query_one("#input-wireguard-port", Input).value = str(cfg.wireguard_port)
        self.query_one(
            "#input-wireguard-interface", Input
        ).value = cfg.wireguard_interface
        self.query_one("#input-dns1", Input).value = cfg.wireguard_dns1
        self.query_one("#input-dns2", Input).value = cfg.wireguard_dns2
        self.query_one("#input-session-ttl", Input).value = str(
            cfg.max_session_ttl_minutes
        )
        self.query_one("#switch-force-ip", Switch).value = cfg.force_current_ip

        # Also discard any unsaved edits to the provider region fields,
        # reverting them to whatever is currently resolved (not necessarily
        # what's on disk, if credentials were just refreshed elsewhere).
        self.query_one("#input-aws-region", Input).value = state.aws_credentials.region
        self.query_one("#input-oci-region", Input).value = state.oci_credentials.region
        self.query_one("#input-gcp-region", Input).value = state.gcp_credentials.region

        self.query_one("#error-message", Static).update("")
        self.notify("Settings reset from wisp.toml", severity="warning")

        # The form now mirrors the reloaded state again, so it is the clean
        # baseline for any further edit.
        self._baseline = self._form_values()
        self._refresh_dirty_state()

    def on_screen_resume(self) -> None:
        # The screen just became current. Normally that means a fresh visit, so
        # whatever the widgets show is the saved/loaded truth — snapshot it as
        # the clean baseline. The one exception is returning from the discard
        # modal after cancel: the edits were explicitly kept, so re-snapshotting
        # here would silently mark them as clean.
        if self._preserve_dirty:
            self._preserve_dirty = False
        else:
            self._baseline = self._form_values()
            self._refresh_dirty_state()

        # Textual focuses the first Input before the first layout pass, so
        # the scroll_visible() it triggers computes against pre-layout
        # dimensions and leaves the card scrolled even when the input was
        # already in view (the title ends up cut off). Re-anchor the scroll
        # once the layout settles.
        self.call_after_refresh(self.query_one(".card").scroll_to, y=0, animate=False)

    def _form_values(self) -> tuple[str, str, str, str, str, bool, str, str, str, str]:
        """Snapshot the editable form: the seven [general] fields and the three
        per-provider region inputs. The reveal switches are deliberately
        excluded — flipping them is not a config change."""
        return (
            self.query_one("#input-vm-boot-timeout", Input).value.strip(),
            self.query_one("#input-wireguard-port", Input).value.strip(),
            self.query_one("#input-wireguard-interface", Input).value.strip(),
            self.query_one("#input-dns1", Input).value.strip(),
            self.query_one("#input-dns2", Input).value.strip(),
            self.query_one("#input-session-ttl", Input).value.strip(),
            self.query_one("#switch-force-ip", Switch).value,
            self.query_one("#input-aws-region", Input).value.strip(),
            self.query_one("#input-oci-region", Input).value.strip(),
            self.query_one("#input-gcp-region", Input).value.strip(),
        )

    def _is_dirty(self) -> bool:
        """True when the form differs from its mount snapshot (unsaved edits)."""
        return self._baseline is not None and self._form_values() != self._baseline

    def _refresh_dirty_state(self) -> None:
        """Show/hide the 'unsaved changes' marker to match reality."""
        marker = self.query_one("#dirty-marker", Static)
        if self._is_dirty():
            marker.update("[yellow]●[/yellow] Unsaved changes")
        else:
            marker.update("")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_dirty_state()

    def _confirm_unsaved(self, *, action: str) -> None:
        """Guard a destructive navigation while there are unsaved edits.

        ``action`` is ``"back"`` or ``"reset"``; the modal's decision is
        handled by :meth:`_handle_unsaved_choice`. The dirty edits must survive
        a Cancel, so :meth:`on_screen_resume` is told not to re-snapshot the
        baseline when the modal pops.
        """
        self._pending_action = action
        self._preserve_dirty = True
        self.app.push_screen(ConfirmDiscardScreen(), self._handle_unsaved_choice)

    def _handle_unsaved_choice(self, result: str | None) -> None:
        """Apply the modal's ``save``/``discard``/``None`` (cancel) verdict."""
        if result == "save":
            # The modal already popped itself when dismissing; save_config()
            # pops the config screen now that it is current again.
            self.save_config()
            return
        if result == "discard":
            self.reset_config()
            if self._pending_action == "back":
                self.app.pop_screen()
            return
        self._refresh_dirty_state()

    def _request_reset(self) -> None:
        """Reset always confirms when there are edits. With unsaved edits the
        existing discard modal asks first (save/discard/cancel); when the form
        is clean there is nothing to reload, so it notifies instead of
        confirming."""
        if self._is_dirty():
            self._confirm_unsaved(action="reset")
            return
        self.notify(
            "Nothing to reset — the form already matches wisp.toml.",
            severity="information",
        )

    def action_back(self) -> None:
        if self._is_dirty():
            self._confirm_unsaved(action="back")
        else:
            self.app.pop_screen()

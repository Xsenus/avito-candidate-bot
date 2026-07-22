from __future__ import annotations

import os
from typing import Callable, Protocol

from .conversation import ConversationState
from .invitations import GoogleSheetInvitationSource
from .service_centers import (
    ServiceCenterSelection,
    form_option_for,
    parse_service_center_overrides,
    resolve_service_center,
)
from .yandex_form import CandidateApplication, FormSubmissionUncertainError


class FormSubmitter(Protocol):
    def submit(self, application: CandidateApplication) -> None: ...


PersistCallback = Callable[[ConversationState], None]


class CandidateWorkflow:
    def __init__(
        self,
        form_submitter: FormSubmitter,
        invitation_source: GoogleSheetInvitationSource,
        *,
        service_center_overrides: dict[str, str] | None = None,
        form_warehouse_overrides: dict[str, str] | None = None,
    ) -> None:
        self.form_submitter = form_submitter
        self.invitation_source = invitation_source
        self.service_center_overrides = service_center_overrides or {}
        self.form_warehouse_overrides = form_warehouse_overrides or {}

    @classmethod
    def from_env(cls, form_submitter: FormSubmitter) -> "CandidateWorkflow":
        source = GoogleSheetInvitationSource(
            os.getenv("INVITATIONS_SHEET_ID", "1D6aP4Vjt05QMRIogvdtX0wKblbgnNrg-I8lF0Fq26zs"),
            os.getenv("INVITATIONS_SHEET_GID", "420777109"),
        )
        overrides = parse_service_center_overrides(
            os.getenv("SERVICE_CENTER_OVERRIDES_JSON", "")
        )
        form_overrides = parse_service_center_overrides(
            os.getenv("FORM_WAREHOUSE_OVERRIDES_JSON", "")
        )
        return cls(
            form_submitter,
            source,
            service_center_overrides=overrides,
            form_warehouse_overrides=form_overrides,
        )

    def complete(
        self,
        state: ConversationState,
        *,
        persist: PersistCallback = lambda state: None,
    ) -> str:
        if state.application_status not in {"pending", "submitting", "submitted"}:
            raise RuntimeError(
                f"Заявка не готова к отправке: {state.application_status}"
            )

        catalog = self.invitation_source.load()
        if state.service_center and state.warehouse_selection_source == "candidate":
            selected_template = catalog.find(state.service_center)
            selection = ServiceCenterSelection(
                selected_template.service_center.removeprefix("СЦ ").strip()
            )
        else:
            selection = resolve_service_center(
                state.city,
                state.item_id,
                catalog,
                self.service_center_overrides,
            )
        state.service_center = selection.name

        if state.application_status != "submitted":
            application = CandidateApplication(
                warehouse=form_option_for(
                    selection.name, self.form_warehouse_overrides
                ),
                tariff=state.tariff,
                last_name=state.last_name or "",
                first_name=state.first_name or "",
                citizenship=state.citizenship,
                phone=state.phone or "",
                internship_date=state.internship_date or "",
            )
            state.application_status = "submitting"
            state.last_error = None
            persist(state)
            try:
                self.form_submitter.submit(application)
            except FormSubmissionUncertainError as exc:
                state.application_status = "uncertain"
                state.last_error = str(exc)
                persist(state)
                raise
            except Exception as exc:
                state.application_status = "pending"
                state.last_error = str(exc)
                persist(state)
                raise
            state.application_status = "submitted"
            persist(state)

        return catalog.find(selection.name).render(state.internship_date or "")


def mark_invitation_sent(state: ConversationState) -> None:
    state.application_status = "completed"
    state.step = "done"
    state.last_error = None
    state.next_retry_at = None
    state.submission_attempts = 0
    state.alert_sent = False

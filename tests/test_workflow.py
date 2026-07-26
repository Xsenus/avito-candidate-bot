from avito_bot.conversation import ConversationState
from avito_bot.invitations import InvitationCatalog
from avito_bot.workflow import CandidateWorkflow, mark_invitation_sent
from avito_bot.yandex_form import FormSubmissionUncertainError


class FakeForm:
    def __init__(self):
        self.applications = []

    def submit(self, application):
        self.applications.append(application)


class FailingForm:
    def submit(self, application):
        raise RuntimeError("form unavailable")


class UncertainForm:
    def submit(self, application):
        raise FormSubmissionUncertainError("submit result unknown")


class FakeInvitationSource:
    def load(self):
        return InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n'
            '"Кемерово","Вы записаны ДАТА на склад в Кемерово"\n'
        )


class RostovInvitationSource:
    def load(self):
        return InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n'
            '"Ростов","Приглашение Ростов ДАТА. '
            'Стажировка начинается в 9:00"\n'
        )


def ready_state():
    return ConversationState(
        step="ready_to_submit",
        city="Кемерово",
        item_id="8288057518",
        last_name="Травкин",
        first_name="Виталий",
        phone="+79272069701",
        internship_date="23.07.2026",
        application_status="pending",
    )


def test_form_is_confirmed_before_invitation_is_returned():
    form = FakeForm()
    workflow = CandidateWorkflow(form, FakeInvitationSource())
    state = ready_state()
    statuses = []

    invitation = workflow.complete(
        state, persist=lambda current: statuses.append(current.application_status)
    )

    assert statuses == ["submitting", "submitted"]
    assert form.applications[0].warehouse == "СЦ Кемерово"
    assert form.applications[0].phone == "+79272069701"
    assert invitation.startswith("Вы записаны 23.07. на склад в Кемерово")
    assert invitation.endswith("До встречи!")


def test_submitted_form_is_not_sent_twice_when_invitation_is_retried():
    form = FakeForm()
    workflow = CandidateWorkflow(form, FakeInvitationSource())
    state = ready_state()
    state.application_status = "submitted"

    invitation = workflow.complete(state)
    mark_invitation_sent(state)

    assert not form.applications
    assert invitation.startswith("Вы записаны")
    assert state.application_status == "completed"
    assert state.step == "done"


def test_form_failure_keeps_application_pending_and_returns_no_invitation():
    workflow = CandidateWorkflow(FailingForm(), FakeInvitationSource())
    state = ready_state()

    try:
        workflow.complete(state)
    except RuntimeError as exc:
        assert str(exc) == "form unavailable"
    else:
        raise AssertionError("form failure must be propagated")

    assert state.application_status == "pending"
    assert state.step == "ready_to_submit"
    assert state.last_error == "form unavailable"


def test_rostov_uses_different_form_and_invitation_names():
    form = FakeForm()
    workflow = CandidateWorkflow(form, RostovInvitationSource())
    state = ready_state()
    state.city = "Ростов-на-Дону"

    invitation = workflow.complete(state)

    assert form.applications[0].warehouse == "СЦ Ростов-на-Дону"
    assert invitation.startswith("Приглашение Ростов 23.07.")


class SelectedWarehouseInvitationSource:
    def load(self):
        return InvitationCatalog.from_csv(
            '"СЦ","Текст сообщения"\n'
            '"Печатники","Приглашение Печатники ДАТА"\n'
        )


def test_candidate_selected_warehouse_has_priority_over_listing_city():
    form = FakeForm()
    workflow = CandidateWorkflow(form, SelectedWarehouseInvitationSource())
    state = ready_state()
    state.city = "Москва"
    state.service_center = "Печатники"
    state.warehouse_choice = 4
    state.warehouse_selection_source = "candidate"

    invitation = workflow.complete(state)

    assert form.applications[0].warehouse == "СЦ Печатники"
    assert invitation.startswith("Приглашение Печатники 23.07.")


def test_unknown_submit_result_is_not_automatically_retried():
    workflow = CandidateWorkflow(UncertainForm(), FakeInvitationSource())
    state = ready_state()

    try:
        workflow.complete(state)
    except FormSubmissionUncertainError:
        pass
    else:
        raise AssertionError("uncertain result must be propagated")

    assert state.application_status == "uncertain"
    assert state.last_error == "submit result unknown"

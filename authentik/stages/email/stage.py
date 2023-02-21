"""authentik multi-stage authentication engine"""
from datetime import timedelta

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.text import slugify
from django.utils.timezone import now
from django.utils.translation import gettext as _
from rest_framework.fields import CharField
from rest_framework.serializers import ValidationError

from authentik.flows.challenge import Challenge, ChallengeResponse, ChallengeTypes
from authentik.flows.models import FlowToken
from authentik.flows.planner import PLAN_CONTEXT_IS_RESTORED, PLAN_CONTEXT_PENDING_USER
from authentik.flows.stage import ChallengeStageView
from authentik.flows.views.executor import QS_KEY_TOKEN, SESSION_KEY_GET
from authentik.interfaces.models import InterfaceType
from authentik.interfaces.views import reverse_interface
from authentik.stages.email.models import EmailStage
from authentik.stages.email.tasks import send_mails
from authentik.stages.email.utils import TemplateEmailMessage

PLAN_CONTEXT_EMAIL_SENT = "email_sent"
PLAN_CONTEXT_EMAIL_OVERRIDE = "email"


class EmailChallenge(Challenge):
    """Email challenge"""

    component = CharField(default="ak-stage-email")


class EmailChallengeResponse(ChallengeResponse):
    """Email challenge resposen. No fields. This challenge is
    always declared invalid to give the user a chance to retry"""

    component = CharField(default="ak-stage-email")

    def validate(self, attrs):
        raise ValidationError(detail="email-sent", code="email-sent")


class EmailStageView(ChallengeStageView):
    """Email stage which sends Email for verification"""

    response_class = EmailChallengeResponse

    def get_full_url(self, **kwargs) -> str:
        """Get full URL to be used in template"""
        base_url = reverse_interface(
            self.request,
            InterfaceType.FLOW,
            flow_slug=self.executor.flow.slug,
        )
        relative_url = f"{base_url}?{urlencode(kwargs)}"
        return self.request.build_absolute_uri(relative_url)

    def get_token(self) -> FlowToken:
        """Get token"""
        pending_user = self.get_pending_user()
        current_stage: EmailStage = self.executor.current_stage
        valid_delta = timedelta(
            minutes=current_stage.token_expiry + 1
        )  # + 1 because django timesince always rounds down
        identifier = slugify(f"ak-email-stage-{current_stage.name}-{pending_user}")
        # Don't check for validity here, we only care if the token exists
        tokens = FlowToken.objects.filter(identifier=identifier)
        if not tokens.exists():
            return FlowToken.objects.create(
                expires=now() + valid_delta,
                user=pending_user,
                identifier=identifier,
                flow=self.executor.flow,
                _plan=FlowToken.pickle(self.executor.plan),
            )
        token = tokens.first()
        # Check if token is expired and rotate key if so
        if token.is_expired:
            token.expire_action()
        return token

    def send_email(self):
        """Helper function that sends the actual email. Implies that you've
        already checked that there is a pending user."""
        pending_user = self.get_pending_user()
        email = self.executor.plan.context.get(PLAN_CONTEXT_EMAIL_OVERRIDE, None)
        if not email:
            email = pending_user.email
        current_stage: EmailStage = self.executor.current_stage
        token = self.get_token()
        # Send mail to user
        message = TemplateEmailMessage(
            subject=_(current_stage.subject),
            to=[email],
            language=pending_user.locale(self.request),
            template_name=current_stage.template,
            template_context={
                "url": self.get_full_url(**{QS_KEY_TOKEN: token.key}),
                "user": pending_user,
                "expires": token.expires,
            },
        )
        send_mails(current_stage, message)

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # Check if the user came back from the email link to verify
        if QS_KEY_TOKEN in request.session.get(
            SESSION_KEY_GET, {}
        ) and self.executor.plan.context.get(PLAN_CONTEXT_IS_RESTORED, False):
            messages.success(request, _("Successfully verified Email."))
            if self.executor.current_stage.activate_user_on_success:
                user = self.get_pending_user()
                user.is_active = True
                user.save()
            return self.executor.stage_ok()
        if PLAN_CONTEXT_PENDING_USER not in self.executor.plan.context:
            self.logger.debug("No pending user")
            messages.error(self.request, _("No pending user."))
            return self.executor.stage_invalid()
        # Check if we've already sent the initial e-mail
        if PLAN_CONTEXT_EMAIL_SENT not in self.executor.plan.context:
            self.send_email()
            self.executor.plan.context[PLAN_CONTEXT_EMAIL_SENT] = True
        return super().get(request, *args, **kwargs)

    def get_challenge(self) -> Challenge:
        challenge = EmailChallenge(
            data={
                "type": ChallengeTypes.NATIVE.value,
                "title": _("Email sent."),
            }
        )
        return challenge

    def challenge_valid(self, response: ChallengeResponse) -> HttpResponse:
        return super().challenge_invalid(response)

    def challenge_invalid(self, response: ChallengeResponse) -> HttpResponse:
        if PLAN_CONTEXT_PENDING_USER not in self.executor.plan.context:
            messages.error(self.request, _("No pending user."))
            return super().challenge_invalid(response)
        self.send_email()
        # We can't call stage_ok yet, as we're still waiting
        # for the user to click the link in the email
        return super().challenge_invalid(response)

"""Intentional admin allowlist. Evidence is metadata-only and cannot be mutated.

Domain workflow commands remain in their versioned API/services, not ModelAdmin.
"""
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from . import models
from .audit import admin_actor, record_audit

# Every concrete application model must occur in exactly one category below.
MANAGED_MODELS = {'User', 'Job', 'ScoringRubric'}
EXCLUDED_MODELS = {
    'SocketTicket': 'Temporary credential digest and Clerk session bindings; no admin access.',
    'RetellCall': 'Encrypted credentials, provider identifiers and private media; inspect Interview/Session metadata instead.',
    'ClerkWebhookState': 'Internal provisioning ordering/tombstone state; service-owned.',
    'FrameAdmissionGate': 'Internal serialization singleton; editing would bypass bounded admission.',
    'ConversationReceipt': 'Idempotency receipt containing private response material; service-owned.',
    'QuestionPreparationIntent': 'Internal transactional intent; inspect its assessment/question set instead.',
    'QuestionReviewDelivery': 'Internal delivery marker; no manual retry or identity edits.',
    'NotificationRetry': 'Internal immutable retry generation; use attributed retry service.',
}

# Explicit displayed fields: never auto-expose future model fields or JSON payloads.
INSPECTION_FIELDS = {
    'RecruiterProfile': ('company_name', 'industry', 'company_size'),
    'CandidateProfile': ('years_experience',),
    'JobSkill': ('skill_name', 'is_required'),
    'Resume': ('status', 'content_hash', 'parser_version', 'uploaded_at'),
    'Application': ('status', 'version', 'created_at', 'updated_at'),
    'ApplicationAssessment': ('revision', 'status', 'match_score', 'eligibility', 'outcome', 'error_code', 'attempts', 'parser_version', 'matcher_version', 'completed_at'),
    'ScreeningEvent': ('source', 'from_status', 'to_status', 'created_at'),
    'ApplicationDecision': ('outcome', 'override', 'application_version', 'created_at'),
    'QuestionSet': ('assessment_revision', 'revision', 'input_hash', 'generation_status', 'error_code', 'attempts', 'generation_number', 'approved_at', 'frozen_at', 'frozen_revision', 'legacy'),
    'PreparedQuestion': ('category', 'source', 'position', 'archived_at', 'generation_number', 'recruiter_edited'),
    'ParsedResume': ('parsed_at',),
    'Interview': ('status', 'scheduled_at', 'completed_at', 'completion_outcome', 'evaluation_status', 'created_at'),
    'Question': ('category', 'source', 'approved', 'position', 'created_at'),
    'TemplateQuestion': ('category', 'created_at'),
    'InterviewSession': ('started_at', 'ended_at', 'audio_ended_at', 'audio_terminal_status', 'video_required', 'creation_uncertain', 'lifecycle_version', 'video_state', 'connection_state', 'analysis_state', 'analysis_failure_reason', 'admission_sealed_at', 'sealed_admission_count', 'received_count', 'admitted_count', 'dropped_count', 'analysis_finalized_at', 'analysis_revision'),
    'InterviewConversation': ('cursor', 'revision', 'paused', 'end_kind', 'stop_requested_at', 'stop_confirmed_at', 'stop_attempts', 'created_at', 'updated_at'),
    'RetellWebhookEvent': ('event', 'content_hash', 'received_at', 'processed_at', 'attempts', 'next_attempt_at'),
    'TranscriptRevision': ('number', 'source', 'content_hash', 'created_at'),
    'EvaluationContext': ('content_hash', 'contract_version', 'created_at'),
    'TranscriptPreparation': ('state', 'deadline', 'next_sync_at', 'sync_attempts', 'selection_version'),
    'InterviewEvaluation': ('revision', 'status', 'model_name', 'prompt_version', 'final_evidence', 'run_number', 'score', 'coverage', 'error_code', 'attempts', 'published_at', 'created_at'),
    'AnswerEvaluation': ('question_id',),
    'EvaluationRelease': ('created_at',),
    'FeedbackReport': ('revision', 'contract_version', 'status', 'content_hash', 'error_code', 'prepared_at', 'released_at', 'edit_version', 'amendments_hash', 'retracted_at', 'created_at'),
    'WorkflowAction': ('action', 'expected_version', 'created_at'),
    'AnswerExtraction': ('contract_version', 'input_hash', 'content_hash', 'created_at'),
    'FeedbackChange': ('version', 'action', 'created_at'),
    'Response': ('created_at',),
    'BehavioralAnalysis': ('pipeline_version', 'model_version', 'rules_version', 'revision', 'finalized_at', 'sealed_admission_count', 'report_schema_version', 'source_revision', 'created_at'),
    'FrameJob': ('generation', 'sequence', 'admission_number', 'received_at', 'expires_at', 'state', 'attempts', 'terminal_at', 'terminal_reason', 'payload_ready', 'publication_attempts'),
    'FrameAnalysis': ('ts', 'pipeline_version', 'model_version', 'rules_version'),
    'ScoringPolicyState': ('next_version', 'epoch', 'rebuild_pending'),
    'ScoreRevision': ('number', 'key', 'status', 'final_score', 'screening_match_percentage', 'interview_performance_percentage', 'coverage', 'error_code', 'attempts', 'created_at', 'completed_at'),
    'CandidateScore': ('final_score', 'created_at', 'updated_at'),
    'Notification': ('type', 'is_read', 'created_at'),
    'NotificationOutbox': ('event', 'resource_type', 'resource_id', 'revision', 'business_key', 'inapp_delivered_at', 'email_status', 'email_attempts', 'delivery_generation', 'total_email_attempts', 'next_attempt_at', 'email_sent_at', 'error_code', 'created_at'),
    'IntegrityThresholds': ('look_away_limit', 'integrity_flag_limit'),
    'AuditLogEntry': ('action', 'target_type', 'target_id', 'business_key', 'created_at'),
}
ADMIN_REGISTRATION_ALLOWLIST = frozenset(MANAGED_MODELS | INSPECTION_FIELDS.keys())


class NoDeleteAdmin(admin.ModelAdmin):
    actions = None
    inlines = ()

    def has_delete_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        raise PermissionDenied('Admin deletion is disabled; use an approved lifecycle/retention service.')

    def delete_queryset(self, request, queryset):
        raise PermissionDenied('Admin bulk deletion is disabled.')


class InspectionAdmin(NoDeleteAdmin):
    """No add/change routes, inline edits, actions or unsafe FK string rendering."""
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        raise PermissionDenied('Evidence inspection only; use the domain service.')

    def history_view(self, request, object_id, extra_context=None):
        # Old Django LogEntry.object_repr values can contain private __str__ text.
        # Attributed platform AuditLogEntry metadata is the supported history.
        raise PermissionDenied('Use the metadata-only platform audit inspection.')

    def render_change_form(self, request, context, *args, **kwargs):
        # Django's default subtitle/original calls model.__str__, which can
        # include private question wording or nested candidate/profile details.
        obj = context.get('original')
        if obj is not None:
            context['original'] = f'{self.model._meta.verbose_name} {obj.pk}'
            context['subtitle'] = context['original']
        return super().render_change_form(request, context, *args, **kwargs)

    @admin.display(description='Record ID')
    def record_id(self, obj):
        return str(obj.pk)

    @admin.display(description='Related record IDs')
    def references(self, obj):
        return '; '.join(f'{field.name}: {getattr(obj, field.attname)}'
                         for field in obj._meta.fields if field.is_relation)


class AuditedManagementAdmin(NoDeleteAdmin):
    mutable_fields = ()

    def has_add_permission(self, request):
        return self.model is models.ScoringRubric and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.model is not models.ScoringRubric and super().has_change_permission(request, obj)

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            actor = admin_actor(request.user)  # Fail closed, including creates/no-op requests.
            previous = self.model.objects.select_for_update().get(pk=obj.pk) if change else None
            if previous is None and self.model is not models.ScoringRubric:
                raise PermissionDenied('Identity and workflow creation must use the application service.')
            if previous is not None:
                changed = [f.name for f in obj._meta.fields if getattr(previous, f.attname) != getattr(obj, f.attname)]
                unsafe = set(changed) - set(self.mutable_fields)
                if unsafe:
                    raise ValidationError('Admin cannot alter protected fields; use the domain workflow.')
            else:
                if obj.active or obj.policy_version != 'COMBINED_V1':
                    raise ValidationError('Create an inactive current-policy rubric; activate through the attributed API.')
                changed = ['weight_match', 'weight_interview', 'weight_behavioral', 'active']
            if previous is None:
                obj.full_clean()
            else:
                for name in self.mutable_fields:
                    obj._meta.get_field(name).clean(getattr(obj, name), obj)
                obj.clean()
            super().save_model(request, obj, form, change)
            if changed:
                if self.model is models.User:
                    action = 'account.activated' if obj.is_active else 'account.deactivated'
                elif self.model is models.Job:
                    action = 'job.status_changed'
                else:
                    action = 'rubric.created'
                record_audit(actor=actor, action=action, target=obj, changed_fields=changed)


@admin.register(models.User)
class UserAdmin(AuditedManagementAdmin):
    mutable_fields = ('is_active',)
    list_display = ('id', 'email', 'role', 'is_active')
    list_filter = ('role', 'is_active')
    search_fields = ('=id', 'email')
    fields = ('id', 'email', 'role', 'is_active', 'created_at', 'updated_at')
    readonly_fields = ('id', 'email', 'role', 'created_at', 'updated_at')


@admin.register(models.Job)
class JobAdmin(AuditedManagementAdmin):
    mutable_fields = ('status',)
    list_display = ('id', 'title', 'status', 'created_at')
    list_filter = ('status', 'job_type', 'experience_level')
    search_fields = ('=id', 'title')
    fields = ('id', 'title', 'status', 'created_at', 'updated_at')
    readonly_fields = ('id', 'title', 'created_at', 'updated_at')


@admin.register(models.ScoringRubric)
class ScoringRubricAdmin(AuditedManagementAdmin):
    list_display = ('id', 'name', 'version', 'policy_version', 'active')
    list_filter = ('active', 'policy_version')
    search_fields = ('=id', 'name')
    fields = ('id', 'name', 'weight_match', 'weight_interview', 'weight_behavioral', 'version', 'policy_version', 'active', 'created_at')
    readonly_fields = ('id', 'version', 'policy_version', 'active', 'created_at')

    def get_readonly_fields(self, request, obj=None):
        return self.fields if obj else self.readonly_fields


for name, inspection_fields in INSPECTION_FIELDS.items():
    model = getattr(models, name)
    # The actual primary key is read through record_id, not an editable input.
    fields = ('record_id', 'references') + inspection_fields
    statuses = tuple(f for f in ('status', 'state', 'event', 'source', 'category', 'type', 'email_status', 'action', 'completion_outcome') if f in inspection_fields)
    attrs = {
        '__module__': __name__,
        'fields': fields,
        'readonly_fields': fields,
        'list_display': ('record_id',) + inspection_fields[:3],
        'list_filter': statuses,
        'search_fields': ('=' + model._meta.pk.name + ('__pk' if model._meta.pk.is_relation else ''),),
        'show_full_result_count': False,
        'list_per_page': 50,
    }
    admin.site.register(model, type(name + 'InspectionAdmin', (InspectionAdmin,), attrs))

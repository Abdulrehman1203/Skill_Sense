"""Owned question-set reads and strict, revisioned recruiter commands."""
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .models import Application, QuestionSet
from .permissions import IsRecruiter
from .question_preparation import QuestionConflict, dispatch, edit_locked, prepare_locked
from .schema_contracts import QuestionSetDTO


class QuestionCommandSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    operation = serializers.ChoiceField(choices=["add", "edit", "remove", "reorder", "approve", "retry", "regenerate"])
    id = serializers.UUIDField(required=False)
    ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=100)
    text = serializers.CharField(required=False, max_length=10000, allow_blank=False)
    category = serializers.ChoiceField(choices=["BEHAVIORAL", "TECHNICAL", "SITUATIONAL"], required=False)

    def validate(self, attrs):
        operation = attrs["operation"]
        required = {"add": {"text", "category"}, "edit": {"id"}, "remove": {"id"},
                    "reorder": {"ids"}, "approve": set(), "retry": set(), "regenerate": set()}[operation]
        allowed = required | ({"text", "category"} if operation == "edit" else set()) | {"operation", "expected_revision"}
        if not required.issubset(attrs) or set(self.initial_data) - allowed:
            raise serializers.ValidationError("Supply exactly the fields supported by this operation.")
        if operation == "edit" and not {"text", "category"}.intersection(attrs):
            raise serializers.ValidationError("Supply text or category to edit.")
        return attrs


def representation(row):
    return {"id": str(row.pk), "application": str(row.application_id),
        "assessment": str(row.assessment_id) if row.assessment_id else None,
        "assessment_revision": row.assessment_revision, "revision": row.revision,
        "input_hash": row.input_hash, "generation_status": row.generation_status,
        "generation_number": row.generation_number,
        "attempts": row.attempts, "next_attempt_at": row.next_attempt_at,
        "error_code": row.error_code, "fallback_reason": row.fallback_reason,
        "approved_at": row.approved_at, "frozen_at": row.frozen_at,
        "frozen_revision": row.frozen_revision, "legacy": row.legacy,
        "questions": [{"id": str(q.pk), "text": q.text, "category": q.category,
                       "source": q.source, "position": q.position}
                      for q in row.items.filter(archived_at__isnull=True)]}


class PrivateQuestionView(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        response['Vary'] = 'Authorization'
        return response


class ApplicationQuestionSetView(PrivateQuestionView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(responses=QuestionSetDTO)
    def get(self, request, pk):
        app = get_object_or_404(Application, pk=pk, job__recruiter__user=request.user)
        row = QuestionSet.objects.filter(application=app, assessment_id=app.current_assessment_id,
            legacy=False).first()
        return Response(representation(row) if row else {"generation_status": "NOT_REQUESTED", "questions": []})

    class PreparationRequest(serializers.Serializer):
        expected_version = serializers.IntegerField(min_value=1)

    @extend_schema(request=PreparationRequest, responses={202: QuestionSetDTO})
    def post(self, request, pk):
        serializer = self.PreparationRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            app = get_object_or_404(Application.objects.select_for_update(of=("self",)),
                pk=pk, job__recruiter__user=request.user)
            if app.version != serializer.validated_data["expected_version"]:
                raise QuestionConflict("Application changed; refresh before requesting preparation.")
            assessment = app.current_assessment
            if assessment is None:
                raise QuestionConflict("Successful screening is required before preparation.")
            row = prepare_locked(app, assessment)
            return Response(representation(row), status=202)


class QuestionSetView(PrivateQuestionView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(responses=QuestionSetDTO)
    def get(self, request, pk):
        row = get_object_or_404(QuestionSet, pk=pk, application__job__recruiter__user=request.user)
        return Response(representation(row))

    @extend_schema(request=QuestionCommandSerializer, responses={200: QuestionSetDTO})
    def patch(self, request, pk):
        command = QuestionCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        data = command.validated_data
        with transaction.atomic():
            row = get_object_or_404(QuestionSet.objects.select_for_update(of=("self",)),
                pk=pk, application__job__recruiter__user=request.user)
            if data["operation"] in ("retry", "regenerate"):
                if row.frozen_at or row.revision != data["expected_revision"]:
                    raise QuestionConflict()
                if row.approved_at or row.legacy:
                    raise QuestionConflict("Approved or legacy questions cannot be regenerated.")
                if data["operation"] == "retry":
                    if row.generation_status != "FAILED" or row.items.filter(archived_at__isnull=True, recruiter_edited=False).exclude(source="MANUAL").exists():
                        raise QuestionConflict("Only failed generations can be retried.")
                else:
                    if row.generation_status not in ("READY", "FALLBACK", "FAILED"):
                        raise QuestionConflict("Wait for the current generation before regenerating.")
                    row.items.filter(archived_at__isnull=True, recruiter_edited=False).exclude(source="MANUAL").update(archived_at=timezone.now())
                    row.generation_number += 1
                row.generation_status, row.attempts = "QUEUED", 0
                row.error_code = row.fallback_reason = ""
                row.claim_token = row.lease_until = None
                row.next_attempt_at = timezone.now()
                row.revision += 1
                row.save()
                from .audit import record_audit
                record_audit(actor=request.user, action='questions.' + data['operation'], target=row,
                    changed_fields=['generation_status'], revision=row.revision, application_id=row.application_id)
                transaction.on_commit(lambda: dispatch(row.pk))
            else:
                edit_locked(row, data, request.user)
            return Response(representation(row))

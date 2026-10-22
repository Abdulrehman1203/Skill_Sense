from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .final_scoring import payload
from .models import Application
from .permissions import IsRecruiter
from .schema_contracts import ScoringHistoryDTO


class ApplicationScoringView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    @extend_schema(tags=['Scores'], responses=ScoringHistoryDTO)
    def get(self, request, pk):
        app = get_object_or_404(Application, pk=pk, job__recruiter__user=request.user)
        response = Response({'current': payload(app), 'history': list(app.score_revisions.order_by('-number').values(
            'id', 'number', 'status', 'inputs', 'final_score', 'screening_match_percentage', 'interview_performance_percentage',
            'coverage', 'error_code', 'attempts', 'created_at', 'completed_at')[:100])})
        response['Cache-Control'] = 'no-store'
        return response

"""Authorization is rechecked for every resume read; no public storage URL."""
from pathlib import Path
from django.http import FileResponse, Http404, JsonResponse
from django.urls import reverse
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.serializers import Serializer
from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes
from interview_system.models import Resume
from interview_system.permissions import IsResumeAccessible


def download_url(resume, request=None):
    if not resume.file:
        return None
    path = reverse('interview_system:resume-download', kwargs={'pk': resume.pk})
    return request.build_absolute_uri(path) if request else path


class ResumeDownloadView(GenericAPIView):
    permission_classes = [IsAuthenticated, IsResumeAccessible]
    queryset = Resume.objects.all()
    serializer_class = Serializer

    @extend_schema(responses={(200, 'application/octet-stream'): OpenApiTypes.BINARY})
    def get(self, request, pk):
        resume = self.get_object()
        try:
            stream = resume.file.open('rb')
        except (FileNotFoundError, OSError, ValueError):
            raise Http404('Document unavailable.') from None
        except Exception:
            return JsonResponse({'detail': 'Document temporarily unavailable.'}, status=503)
        suffix = Path(resume.file.name).suffix.lower()
        filename = 'resume' + (suffix if suffix in ('.pdf', '.docx') else '.bin')
        response = FileResponse(stream, as_attachment=True, filename=filename,
                                content_type='application/octet-stream')
        response['Cache-Control'] = 'private, no-store'
        response['Pragma'] = 'no-cache'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Security-Policy'] = "sandbox; default-src 'none'"
        return response

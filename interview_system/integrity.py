"""Read-time presentation only. Never imported by scoring or workflow code."""
from .models import IntegrityThresholds


def thresholds():
    return IntegrityThresholds.objects.filter(pk=1).first() or IntegrityThresholds(pk=1)


def review_context(application, limits):
    observed = None
    # Interviews are prefetched, including their analysis and frame-existence bit.
    for interview in application.interviews.all():
        session = getattr(interview, "session", None)
        analysis = getattr(session, "behavioral_analysis", None)
        if analysis is None or not getattr(interview, "has_behavioral_frames", False):
            continue
        if analysis.report_schema_version != 0:
            # B6 observation episodes are not adjudicated integrity findings.
            continue
        # Conservative exclusion of both known historical Phase 4.5 signatures.
        if not analysis.integrity_flags and (
            (analysis.attention_pct == 94.5 and analysis.look_away_count == 2)
            or (analysis.attention_pct == 80 and analysis.look_away_count == 0)
        ):
            continue
        observed = {"look_away_count": analysis.look_away_count,
                    "integrity_flag_count": len(analysis.integrity_flags or [])}
        break
    values = {"look_away_limit": limits.look_away_limit, "integrity_flag_limit": limits.integrity_flag_limit}
    exceeds = observed is not None and (
        observed["look_away_count"] > limits.look_away_limit
        or observed["integrity_flag_count"] > limits.integrity_flag_limit
    )
    return {"observed_counts": observed, "thresholds": values, "exceeds_threshold": exceeds}

"""Short-lived candidate captions from authenticated Retell transcript events."""
from django.core.cache import cache
from rest_framework.exceptions import ValidationError

from .models import InterviewConversation


def transcript_key(call_id):
    return "interview-live-transcript:" + call_id


def accept_live_transcript(call):
    if not isinstance(call, dict) or not isinstance(call.get("call_id"), str):
        raise ValidationError("Invalid transcript call.")
    turns = call.get("transcript_object")
    if not isinstance(turns, list) or len(turns) > 10000:
        raise ValidationError("Invalid transcript.")
    row = InterviewConversation.objects.select_related("interview").filter(
        call_id=call["call_id"], interview__status="LIVE",
        interview__session__audio_ended_at__isnull=True,
    ).first()
    if not row or row.interview.retell_session_id != call["call_id"]:
        return
    clean_turns = []
    end = 0.0
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("role") not in ("agent", "user"):
            continue
        if not isinstance(turn.get("content"), str):
            raise ValidationError("Invalid transcript text.")
        clean_turns.append({"role": turn["role"], "content": turn["content"]})
        for word in turn.get("words") or []:
            value = word.get("end") if isinstance(word, dict) else None
            if type(value) in (int, float) and 0 <= value < 86400:
                end = max(end, value)
    key = transcript_key(call["call_id"])
    # Provider deliveries can arrive out of order. Serialize the small cache
    # update and discard snapshots older than the last caption.
    lock = key + ":lock"
    if not cache.add(lock, True, timeout=5):
        raise RuntimeError("Transcript update is busy.")
    try:
        previous = cache.get(key)
        position = (end, len(clean_turns))
        if previous and position < tuple(previous["position"]):
            return
        cache.set(key, {"position": position, "turns": clean_turns}, timeout=7200)
    finally:
        cache.delete(lock)
    # Completion must also work when the agent omits or loses its finish tool
    # call. Only authenticated, post-question consent can request termination.
    if row.cursor == len(row.questions) and not row.stop_requested_at:
        from .conversation_control import control, finish_confirmed
        if finish_confirmed(row, call):
            control({'name': 'interview_control', 'args': {'action': 'finish'},
                     'call': call, 'source': 'verified_transcript_completion'})


def current_answer(row):
    from .conversation_control import candidate_segment

    saved = cache.get(transcript_key(row.call_id))
    if not saved:
        return ""
    users = candidate_segment(row, {"transcript_object": saved["turns"]},
                              allow_transcription_variation=True)
    return " ".join(text for _, text in users)

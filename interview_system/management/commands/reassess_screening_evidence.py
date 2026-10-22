"""Recheck completed evidence after deterministic parser fixes, preserving history."""
import copy
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from interview_system.models import Application, ApplicationAssessment
from interview_system.screening import apply_qualification_locked, evaluate


class Command(BaseCommand):
    help = 'Dry-run a targeted screening evidence recheck; --execute creates a new revision with the same saved score/policy.'

    def add_arguments(self, parser):
        parser.add_argument('--application', required=True)
        parser.add_argument('--expected-version', type=int, required=True)
        parser.add_argument('--execute', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            app = Application.objects.select_for_update().get(pk=options['application'])
        except Application.DoesNotExist:
            raise CommandError('Application not found.') from None
        if app.version != options['expected_version']:
            raise CommandError('Application changed; inspect the new version before reassessing.')
        if app.status not in ('APPLIED','UNDER_REVIEW'):
            raise CommandError('Manual and downstream stages must be preserved.')
        old = ApplicationAssessment.objects.select_for_update().filter(pk=app.current_assessment_id).first()
        if not old or old.status != 'SUCCEEDED' or old.match_score is None or old.resume_id != app.resume_id:
            raise CommandError('Current successful assessment and unchanged submitted resume are required.')
        if not old.apply_decision:
            raise CommandError('Read-only/backfill assessment cannot be converted into decision-applying work.')
        resume=app.resume
        parsed=getattr(resume,'parsed_data',None)
        if not parsed or not old.resume_hash or old.resume_hash != resume.content_hash or resume.status != 'PARSED':
            raise CommandError('Verified unchanged parsed resume is required; use normal screening for changed evidence.')
        criteria,eligibility,outcome,reasons=evaluate(old.policy,parsed,old.match_score)
        result={'application':str(app.pk),'previous_assessment':str(old.pk),'score':str(old.match_score),
            'eligibility':eligibility,'outcome':outcome,'reasons':reasons,'criteria_results':criteria,'executed':False}
        if options['execute']:
            policy=copy.deepcopy(old.policy)
            policy['evidence_reassessment_of']=str(old.pk)
            policy['evidence_rules_version']='skills-months-v2'
            row=ApplicationAssessment.objects.create(application=app,resume=resume,
                revision=(app.assessments.aggregate(n=Max('revision'))['n'] or 0)+1,
                policy=policy,status='SUCCEEDED',apply_decision=True,match_score=old.match_score,
                matched_skills=[r['requirement'] for r in criteria if r['kind']=='skill' and r['status']=='PASS'],
                missing_skills=[r['requirement'] for r in criteria if r['kind']=='skill' and r['status']!='PASS'],
                criteria_results=criteria,eligibility=eligibility,outcome=outcome,reasons=reasons,
                resume_hash=old.resume_hash,parser_version=old.parser_version,matcher_version=old.matcher_version,
                completed_at=timezone.now())
            app.current_assessment=row
            app.version+=1
            app.save(update_fields=['current_assessment','version','updated_at'])
            apply_qualification_locked(app,row)
            from interview_system.audit import record_audit
            record_audit(actor=None,action='screening.evidence_reassessed',target=row,
                changed_fields=['criteria_results','eligibility','outcome'],revision=row.revision,application_id=app.pk)
            result.update(executed=True,assessment=str(row.pk),application_version=app.version,status=app.status)
        self.stdout.write(json.dumps(result,default=str))

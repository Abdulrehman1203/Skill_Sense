"""Phase 6 Step 2: rubric persistence, admin API, and activation invariant."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import IntegrityError, close_old_connections, connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from interview_system.models import ScoringRubric, User


def rubric_data(**overrides):
    data = {
        "name": "Default",
        "weight_match": 0.5,
        "weight_interview": 0.3,
        "weight_behavioral": 0.2,
    }
    data.update(overrides)
    return data


class RubricAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create(clerk_id="rubric_admin", email="rubric-admin@example.test", role=User.Role.ADMIN)
        self.recruiter = User.objects.create(clerk_id="rubric_recruiter", email="rubric-recruiter@example.test", role=User.Role.RECRUITER)
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.list_url = reverse("interview_system:admin-rubric-list")

    def test_valid_creation_is_inactive_even_if_active_is_supplied(self):
        created = self.client.post(self.list_url, rubric_data(active=True), format="json")
        self.assertEqual(created.status_code, 201)
        self.assertFalse(created.data["active"])
        self.assertFalse(ScoringRubric.objects.get(pk=created.data["id"]).active)
        listed = self.client.get(self.list_url)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data), 1)
        self.assertEqual(listed.data[0]["name"], "Default")

    def test_weight_sum_at_tolerance_boundary(self):
        # The configured tolerance is 1e-6; probe on both sides of it.
        just_inside = self.client.post(
            self.list_url, rubric_data(name="Inside", weight_behavioral=0.200000999), format="json"
        )
        just_outside = self.client.post(
            self.list_url, rubric_data(name="Outside", weight_behavioral=0.200001001), format="json"
        )
        self.assertEqual(just_inside.status_code, 201)
        self.assertEqual(just_outside.status_code, 400)
        self.assertIn("sum to 1.0", str(just_outside.data["weights"]))
        self.assertEqual(ScoringRubric.objects.count(), 1)

    def test_activation_swaps_one_active_rubric(self):
        first = ScoringRubric.objects.create(**rubric_data(name="First"))
        second = ScoringRubric.objects.create(**rubric_data(name="Second"))
        self.assertFalse(first.active)
        self.assertFalse(second.active)
        first_url = reverse("interview_system:admin-rubric-activate", args=[first.pk])
        second_url = reverse("interview_system:admin-rubric-activate", args=[second.pk])
        self.assertEqual(self.client.post(first_url).status_code, 200)
        swapped = self.client.post(second_url)
        self.assertEqual(swapped.status_code, 200)
        self.assertTrue(swapped.data["active"])
        self.assertEqual(list(ScoringRubric.objects.filter(active=True).values_list("pk", flat=True)), [second.pk])

    def test_only_admin_can_list_create_or_activate(self):
        rubric = ScoringRubric.objects.create(**rubric_data())
        activation = reverse("interview_system:admin-rubric-activate", args=[rubric.pk])
        self.client.force_authenticate(self.recruiter)
        self.assertEqual(self.client.get(self.list_url).status_code, 403)
        self.assertEqual(self.client.post(self.list_url, rubric_data(), format="json").status_code, 403)
        self.assertEqual(self.client.post(activation).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.list_url).status_code, 401)

    def test_database_constraint_rejects_second_active_rubric(self):
        ScoringRubric.objects.create(**rubric_data(name="First"), active=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ScoringRubric.objects.create(**rubric_data(name="Second"), active=True)
        self.assertEqual(ScoringRubric.objects.filter(active=True).count(), 1)


class ConcurrentRubricActivationTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create(clerk_id="concurrent_rubric_admin", email="concurrent-rubric@example.test", role=User.Role.ADMIN)
        self.first = ScoringRubric.objects.create(**rubric_data(name="First"))
        self.second = ScoringRubric.objects.create(**rubric_data(name="Second"))
        self.third = ScoringRubric.objects.create(**rubric_data(name="Third"))
        ScoringRubric.objects.filter(pk=self.first.pk).update(active=True)

    def test_concurrent_activations_leave_exactly_one_active(self):
        gate = Barrier(3)

        def activate(rubric_id):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.admin)
                gate.wait(timeout=10)
                response = client.post(reverse("interview_system:admin-rubric-activate", args=[rubric_id]))
                return response.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_call = pool.submit(activate, self.second.pk)
            second_call = pool.submit(activate, self.third.pk)
            gate.wait(timeout=10)
            statuses = [first_call.result(timeout=20), second_call.result(timeout=20)]

        self.assertEqual(statuses, [200, 200])
        active_ids = list(ScoringRubric.objects.filter(active=True).values_list("pk", flat=True))
        self.assertEqual(len(active_ids), 1)
        self.assertIn(active_ids[0], (self.second.pk, self.third.pk))

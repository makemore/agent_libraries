"""Service contract regressions; run with engagement.test_settings for SQLite."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Profile, ProfileGrant, User
from .access import Actor, authorize
from .models import (
    Appeal, AuditRecord, Charity, Contact, ContactEngagementSummary, ContactMethod,
    Donation, Interaction, InteractionParticipant, MutationReceipt, Opportunity, Pledge,
)
from .services import (
    add_contact_method, contact_timeline, create_contact, get_contact,
    get_interaction, list_contacts, log_interaction,
)


def make_identity(charity, name, *, role=ProfileGrant.Role.FUNDRAISER, user=None):
    user = user or User.objects.create_user(email=f'{name}@example.test', full_name=name)
    profile = Profile.objects.create(charity=charity, display_name=name)
    grant = ProfileGrant.objects.create(user=user, profile=profile, role=role)
    return user, profile, grant, Actor(user.pk, profile.pk, source='mcp')


def make_links(charity, contact):
    appeal = Appeal.objects.create(charity=charity, name='Annual appeal', currency='GBP')
    opportunity = Opportunity.objects.create(
        charity=charity, contact=contact, kind='major_gift', title='A gift', currency='GBP',
    )
    pledge = Pledge.objects.create(charity=charity, contact=contact, amount='10.00', currency='GBP')
    donation = Donation.objects.create(charity=charity, contact=contact, amount='10.00', currency='GBP')
    return dict(opportunity_id=opportunity.pk, appeal_id=appeal.pk, pledge_id=pledge.pk, donation_id=donation.pk)


class CRMServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.charity = Charity.objects.create(name='First charity', default_currency='GBP')
        cls.other_charity = Charity.objects.create(name='Second charity', default_currency='GBP')
        cls.user, cls.profile, cls.grant, cls.actor = make_identity(cls.charity, 'fundraiser')
        _, cls.second_profile, _, cls.second_actor = make_identity(cls.charity, 'second', user=cls.user)
        _, cls.other_profile, _, cls.other_actor = make_identity(cls.other_charity, 'other', user=cls.user)
        cls.viewer_user, cls.viewer_profile, cls.viewer_grant, cls.viewer = make_identity(
            cls.charity, 'viewer', role=ProfileGrant.Role.VIEWER,
        )
        _, _, _, cls.admin = make_identity(cls.charity, 'admin', role=ProfileGrant.Role.ADMIN)
        cls.shared_user = User.objects.create_user(email='shared@example.test')
        ProfileGrant.objects.create(user=cls.shared_user, profile=cls.profile, role=ProfileGrant.Role.VIEWER)
        cls.shared_actor = Actor(cls.shared_user.pk, cls.profile.pk, source='mcp')
        cls.foreign_user, _, _, _ = make_identity(cls.other_charity, 'foreign-user')
        cls.contact = Contact.objects.create(charity=cls.charity, display_name='Alice Donor')
        cls.second_contact = Contact.objects.create(charity=cls.charity, display_name='Bob Donor')
        cls.other_contact = Contact.objects.create(charity=cls.other_charity, display_name='Private foreign contact')
        cls.when = timezone.now() - timedelta(hours=1)

    def create(self, actor=None, **overrides):
        values = dict(display_name='New donor', idempotency_key=str(uuid4()))
        values.update(overrides)
        return create_contact(actor or self.actor, **values)

    def method(self, actor=None, **overrides):
        values = dict(
            contact_id=self.contact.pk, kind='email', value='alice@example.test',
            idempotency_key=str(uuid4()),
        )
        values.update(overrides)
        return add_contact_method(actor or self.actor, **values)

    def log(self, actor=None, **overrides):
        values = dict(
            participants=[{'contact_id': str(self.contact.pk), 'role': 'to'}],
            channel='email', direction='inbound', occurred_at=self.when,
            idempotency_key=str(uuid4()),
        )
        values.update(overrides)
        return log_interaction(actor or self.actor, **values)

    def counts(self):
        return tuple(model.objects.count() for model in (
            Contact, ContactMethod, Interaction, InteractionParticipant, AuditRecord, MutationReceipt,
        ))

    def test_contact_create_detail_and_bounded_engagement_summary(self):
        result = self.create(
            display_name='Ada Lovelace', first_name='Ada', last_name='Lovelace',
            background='Historic supporter', do_not_solicit=True, owner_id=self.second_profile.pk,
        )
        self.assertEqual(set(result), {'id'})
        detail = get_contact(self.actor, result['id'])
        self.assertEqual(detail['display_name'], 'Ada Lovelace')
        self.assertEqual(detail['first_name'], 'Ada')
        self.assertEqual(detail['last_name'], 'Lovelace')
        self.assertEqual(detail['owner_id'], str(self.second_profile.pk))
        self.assertTrue(detail['do_not_solicit'])
        self.assertEqual(detail['background'], 'Historic supporter')
        self.assertEqual(detail['methods'], [])
        self.assertIsNone(detail['engagement_summary'])
        method = self.method(contact_id=result['id'], is_primary=True)
        ContactEngagementSummary.objects.create(
            charity=self.charity, contact_id=result['id'], event_count=123, session_count=4,
            last_web_at=self.when, computed_at=self.when,
        )
        detail = get_contact(self.viewer, result['id'])
        self.assertEqual(detail['methods'][0]['id'], method['id'])
        self.assertEqual(detail['engagement_summary']['event_count'], 123)
        self.assertEqual(set(detail['engagement_summary']), {'event_count', 'session_count', 'last_web_at', 'computed_at'})
        json.dumps(detail, allow_nan=False)

    def test_all_writes_have_provenance_audit_and_minimal_receipts(self):
        contact = self.create(background='Private background')
        method = self.method(contact_id=contact['id'])
        interaction = self.log(
            participants=[{'contact_id': contact['id']}], body='Private correspondence', summary='A summary',
        )
        records = [
            Contact.objects.get(pk=contact['id']), ContactMethod.objects.get(pk=method['id']),
            Interaction.objects.get(pk=interaction['id']), InteractionParticipant.objects.get(),
            *AuditRecord.objects.all(), *MutationReceipt.objects.all(),
        ]
        for record in records:
            with self.subTest(model=record._meta.label):
                self.assertEqual(record.charity_id, self.charity.pk)
                self.assertEqual(record.created_by_id, self.user.pk)
                self.assertEqual(record.acting_profile_id, self.profile.pk)
        self.assertEqual(AuditRecord.objects.count(), 4)
        for audit in AuditRecord.objects.all():
            self.assertEqual(audit.changes, {})
        for receipt in MutationReceipt.objects.all():
            self.assertEqual(set(receipt.result), {'id'})
            self.assertEqual(len(receipt.fingerprint), 64)

    def test_same_profile_retries_return_only_ids_without_new_writes(self):
        for operation in (self.create, self.method, self.log):
            with self.subTest(operation=operation.__name__):
                key = str(uuid4())
                result = operation(idempotency_key=key)
                before = self.counts()
                self.assertEqual(operation(idempotency_key=key), result)
                self.assertEqual(set(result), {'id'})
                self.assertEqual(self.counts(), before)

    def test_idempotency_fingerprints_every_contact_choice_and_actor_source(self):
        self.create(idempotency_key='contact')
        changes = (
            {'display_name': 'Different'}, {'kind': 'trust'}, {'first_name': 'First'},
            {'last_name': 'Last'}, {'background': 'Different'}, {'do_not_solicit': True},
            {'owner_id': self.second_profile.pk},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.create(idempotency_key='contact', **change)
        with self.assertRaises(ValidationError):
            self.create(Actor(self.user.pk, self.profile.pk, 'api'), idempotency_key='contact')
        with self.assertRaises(ValidationError):
            self.method(idempotency_key='contact')

    def test_idempotency_fingerprints_every_contact_method_choice(self):
        self.method(idempotency_key='method')
        for change in (
            {'contact_id': self.second_contact.pk}, {'kind': 'postal'},
            {'value': 'other@example.test'}, {'label': 'Work'}, {'is_primary': True},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.method(idempotency_key='method', **change)
        with self.assertRaises(ValidationError):
            self.method(Actor(self.user.pk, self.profile.pk, 'api'), idempotency_key='method')

    def test_idempotency_fingerprints_every_interaction_choice(self):
        links = make_links(self.charity, self.contact)
        self.log(idempotency_key='interaction')
        changes = [
            {'participants': [{'contact_id': self.contact.pk, 'role': 'from'}]},
            {'participants': [{'contact_id': self.contact.pk, 'role': 'to', 'name': 'Old name'}]},
            {'participants': [{'contact_id': self.contact.pk, 'role': 'to', 'address': 'old@example.test'}]},
            {'channel': 'phone'}, {'direction': 'outbound'},
            {'occurred_at': self.when + timedelta(microseconds=1)},
            {'subject': 'Subject'}, {'body': 'Body'}, {'summary': 'Summary'}, {'outcome': 'Outcome'},
            {'duration_seconds': 10}, {'delivery_status': 'delivered'}, {'purpose': 'cultivation'},
            {'source': 'mail'}, {'source': 'mail', 'external_id': 'message'},
            {'thread_id': 'thread'}, {'visibility': 'restricted'},
            *({field: value} for field, value in links.items()),
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.log(idempotency_key='interaction', **change)
        with self.assertRaises(ValidationError):
            self.log(Actor(self.user.pk, self.profile.pk, 'api'), idempotency_key='interaction')

    def test_different_user_same_profile_cannot_replay_receipt(self):
        ProfileGrant.objects.filter(user=self.shared_user, profile=self.profile).update(role='fundraiser')
        for operation in (self.create, self.method, self.log):
            key = str(uuid4())
            operation(idempotency_key=key)
            with self.subTest(operation=operation.__name__), self.assertRaises(ValidationError):
                operation(self.shared_actor, idempotency_key=key)

    def test_idempotency_keys_are_required_and_bounded(self):
        before = self.counts()
        for key in (None, '', ' ', 'x' * 129, 42):
            for operation in (self.create, self.method, self.log):
                with self.subTest(key=key, operation=operation.__name__), self.assertRaises(ValidationError):
                    operation(idempotency_key=key)
        self.assertEqual(self.counts(), before)

    def test_external_ids_conflict_across_keys_profiles_and_payloads(self):
        original = self.log(source='mail', external_id='message-1', body='Original', idempotency_key='external')
        before = self.counts()
        self.assertEqual(
            self.log(source='mail', external_id='message-1', body='Original', idempotency_key='external'), original,
        )
        for actor in (self.actor, self.second_actor):
            for body in ('Original', 'Different'):
                with self.subTest(actor=actor, body=body), self.assertRaises(ValidationError):
                    self.log(actor, source='mail', external_id='message-1', body=body)
        with self.assertRaises(ValidationError):
            self.log(source='mail', external_id='message-1', body='Different', idempotency_key='external')
        self.assertEqual(self.counts(), before)
        other = self.log(
            self.other_actor, participants=[{'contact_id': self.other_contact.pk}],
            source='mail', external_id='message-1',
        )
        self.assertNotEqual(original, other)

    def test_database_external_id_collision_is_safe_after_validation_race(self):
        original = self.log(source='mail', external_id='message-1')
        before = self.counts()
        # Simulate two profiles passing model validation before either inserts.
        # The database constraint must still reject the losing transaction.
        with patch.object(Interaction, 'full_clean', return_value=None):
            with self.assertRaisesMessage(ValidationError, 'conflicts with an existing record'):
                self.log(self.second_actor, source='mail', external_id='message-1', body='Different')
        self.assertEqual(self.counts(), before)
        self.assertEqual(get_interaction(self.actor, original['id'])['body'], '')

    def test_integrity_conflict_is_validation_error_and_rolls_back(self):
        before = self.counts()
        with patch('crm.services.audit', side_effect=IntegrityError('conflict')):
            with self.assertRaisesMessage(ValidationError, 'conflicts with an existing record'):
                self.log()
        self.assertEqual(self.counts(), before)

    def test_viewer_can_read_but_cannot_write_or_replay(self):
        interaction = self.log(idempotency_key='interaction')
        self.create(idempotency_key='contact')
        self.method(idempotency_key='method')
        self.assertTrue(list_contacts(self.viewer)['items'])
        self.assertEqual(get_contact(self.viewer, self.contact.pk)['id'], str(self.contact.pk))
        self.assertEqual(contact_timeline(self.viewer, self.contact.pk)['items'][0]['id'], interaction['id'])
        self.assertEqual(get_interaction(self.viewer, interaction['id'])['id'], interaction['id'])
        for actor in (self.viewer, self.actor):
            if actor == self.actor:
                ProfileGrant.objects.filter(pk=self.grant.pk).update(role='viewer')
            for operation, key in ((self.create, 'contact'), (self.method, 'method'), (self.log, 'interaction')):
                with self.subTest(actor=actor, operation=operation.__name__), self.assertRaises(PermissionDenied):
                    operation(actor, idempotency_key=key)

    def test_inactive_user_grant_profile_and_charity_deny_all_calls_and_replay(self):
        interaction = self.log(idempotency_key='interaction')
        self.create(idempotency_key='contact')
        self.method(idempotency_key='method')
        calls = (
            lambda: list_contacts(self.actor), lambda: get_contact(self.actor, self.contact.pk),
            lambda: contact_timeline(self.actor, self.contact.pk),
            lambda: get_interaction(self.actor, interaction['id']),
            lambda: self.create(idempotency_key='contact'), lambda: self.method(idempotency_key='method'),
            lambda: self.log(idempotency_key='interaction'),
        )
        before = self.counts()
        for model, record in ((User, self.user), (ProfileGrant, self.grant), (Profile, self.profile), (Charity, self.charity)):
            model.objects.filter(pk=record.pk).update(is_active=False)
            try:
                for call in calls:
                    with self.subTest(model=model.__name__, call=call), self.assertRaises(PermissionDenied):
                        call()
            finally:
                model.objects.filter(pk=record.pk).update(is_active=True)
        ProfileGrant.objects.filter(pk=self.grant.pk).delete()
        for call in calls:
            with self.assertRaises(PermissionDenied):
                call()
        self.assertEqual(self.counts(), before)

    def test_mutation_reauthorizes_after_initial_service_check(self):
        before = self.counts()
        calls = 0

        def revoke_before_callback(actor, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                ProfileGrant.objects.filter(pk=self.grant.pk).update(is_active=False)
            return authorize(actor, **kwargs)

        with patch('crm.mutations.authorize', side_effect=revoke_before_callback):
            with self.assertRaises(PermissionDenied):
                self.log()
        self.assertEqual(calls, 2)
        self.assertEqual(self.counts(), before)

    def test_real_actor_required_and_superuser_has_no_bypass(self):
        superuser = User.objects.create_user(email='superuser@example.test', is_superuser=True, is_staff=True)
        actors = (
            None, {'user_id': self.user.pk, 'profile_id': self.profile.pk},
            SimpleNamespace(user_id=self.user.pk, profile_id=self.profile.pk, source='mcp'),
            Actor(superuser.pk, self.profile.pk), Actor(self.user.pk, 'invalid'),
            Actor(True, self.profile.pk), Actor(str(self.user.pk), self.profile.pk),
            Actor(9223372036854775808, self.profile.pk), Actor(self.user.pk, self.profile.pk, ''),
        )
        interaction = self.log()
        for actor in actors:
            calls = (
                lambda: list_contacts(actor), lambda: get_contact(actor, self.contact.pk),
                lambda: get_interaction(actor, interaction['id']), lambda: contact_timeline(actor, self.contact.pk),
                lambda: create_contact(actor, display_name='No', idempotency_key='no'),
                lambda: add_contact_method(actor, contact_id=self.contact.pk, kind='email', value='a@example.test', idempotency_key='no'),
                lambda: log_interaction(actor, participants=[], channel='email', direction='inbound', occurred_at=self.when, idempotency_key='no'),
            )
            for call in calls:
                with self.subTest(actor=actor, call=call), self.assertRaises(PermissionDenied):
                    call()

    def test_profile_switching_changes_scope_and_attribution(self):
        original = self.create(idempotency_key='shared-key')
        switched = self.create(self.other_actor, idempotency_key='shared-key')
        self.assertNotEqual(original, switched)
        record = Contact.objects.get(pk=switched['id'])
        self.assertEqual(record.created_by_id, self.user.pk)
        self.assertEqual(record.acting_profile_id, self.other_profile.pk)
        self.assertEqual(record.charity_id, self.other_charity.pk)
        self.assertNotIn(original['id'], {item['id'] for item in list_contacts(self.other_actor)['items']})
        self.assertEqual(get_contact(self.second_actor, original['id'])['id'], original['id'])
        with self.assertRaises(ValidationError):
            get_contact(self.actor, switched['id'])

    def test_cross_tenant_contacts_owners_and_links_are_not_available(self):
        before = self.counts()
        calls = (
            lambda: get_contact(self.actor, self.other_contact.pk),
            lambda: contact_timeline(self.actor, self.other_contact.pk),
            lambda: self.method(contact_id=self.other_contact.pk),
            lambda: self.create(owner_id=self.other_profile.pk),
            lambda: self.log(participants=[{'contact_id': self.other_contact.pk}]),
        )
        for call in calls:
            with self.assertRaisesMessage(ValidationError, 'Record not found or unavailable'):
                call()
        self.assertEqual(self.counts(), before)
        foreign_links = make_links(self.other_charity, self.other_contact)
        for field, value in foreign_links.items():
            for record_id in (value, uuid4()):
                with self.subTest(field=field), self.assertRaisesMessage(ValidationError, 'Record not found or unavailable'):
                    self.log(**{field: record_id})
        own_links = make_links(self.charity, self.contact)
        result = self.log(**own_links)
        detail = get_interaction(self.actor, result['id'])
        for field, value in own_links.items():
            self.assertEqual(detail[field], str(value))

    def test_inactive_owner_is_rejected(self):
        Profile.objects.filter(pk=self.second_profile.pk).update(is_active=False)
        with self.assertRaisesMessage(ValidationError, 'Record not found or unavailable'):
            self.create(owner_id=self.second_profile.pk)

    def test_malformed_ids_are_safe_validation_errors(self):
        for value in ('not-an-id', '', None, {}, [], 1, True):
            calls = (
                lambda: get_contact(self.actor, value), lambda: get_interaction(self.actor, value),
                lambda: contact_timeline(self.actor, value), lambda: self.method(contact_id=value),
                lambda: self.log(participants=[{'contact_id': value}]),
            )
            for call in calls:
                with self.subTest(value=value, call=call), self.assertRaises(ValidationError):
                    call()
            if value is not None:
                with self.assertRaises(ValidationError):
                    self.create(owner_id=value)
                for field in ('opportunity_id', 'appeal_id', 'pledge_id', 'donation_id'):
                    with self.assertRaises(ValidationError):
                        self.log(**{field: value})

    def test_multi_participant_interaction_is_stored_once_and_timeline_deduplicates(self):
        result = self.log(participants=[
            {'contact_id': self.contact.pk, 'role': 'from'},
            {'contact_id': self.contact.pk, 'role': 'cc'},
            {'contact_id': self.second_contact.pk, 'role': 'to'},
            {'name': 'Historic outsider', 'address': 'old@example.test', 'role': 'cc'},
        ], subject='One meeting', summary='Summary', body='Detail body')
        self.assertEqual(Interaction.objects.count(), 1)
        self.assertEqual(InteractionParticipant.objects.count(), 4)
        for contact in (self.contact, self.second_contact):
            page = contact_timeline(self.actor, contact.pk, limit=1)
            self.assertEqual(len(page['items']), 1)
            self.assertIsNone(page['next_offset'])
            item = page['items'][0]
            self.assertEqual(item['id'], result['id'])
            self.assertEqual(item['subject'], 'One meeting')
            self.assertEqual(item['summary'], 'Summary')
            self.assertNotIn('body', item)
            self.assertEqual(len(item['participants']), 4)
            self.assertEqual(len({item['id'] for item in item['participants']}), 4)
        detail = get_interaction(self.actor, result['id'])
        self.assertEqual(detail['body'], 'Detail body')
        self.assertEqual(len(detail['participants']), 4)
        json.dumps(detail, allow_nan=False)

    def test_snapshots_preserve_historical_values_and_explicit_blanks(self):
        self.method(is_primary=True)
        result = self.log(participants=[
            {'contact_id': self.contact.pk, 'name': 'Old Alice', 'address': 'historic@example.test'},
            {'contact_id': self.contact.pk, 'name': '', 'address': ''},
            {'contact_id': self.contact.pk},
        ])
        Contact.objects.filter(pk=self.contact.pk).update(display_name='New name')
        ContactMethod.objects.filter(contact=self.contact).update(value='new@example.test')
        participants = get_interaction(self.actor, result['id'])['participants']
        self.assertEqual(
            {(item['name'], item['address']) for item in participants},
            {('Old Alice', 'historic@example.test'), ('', ''), ('Alice Donor', 'alice@example.test')},
        )

    def test_snapshot_fallback_uses_only_channel_primary_method(self):
        self.method(value='secondary@example.test')
        result = self.log()
        self.assertEqual(get_interaction(self.actor, result['id'])['participants'][0]['address'], '')
        self.method(is_primary=True)
        self.method(kind='phone', value='+44 20 1234 5678', is_primary=True)
        for channel, expected in (('email', 'alice@example.test'), ('phone', '+44 20 1234 5678'), ('sms', '+44 20 1234 5678'), ('meeting', ''), ('letter', '')):
            with self.subTest(channel=channel):
                result = self.log(channel=channel)
                participant = get_interaction(self.actor, result['id'])['participants'][0]
                self.assertEqual(participant['name'], 'Alice Donor')
                self.assertEqual(participant['address'], expected)

    def test_participant_user_requires_active_explicit_same_charity_grant(self):
        ungranted = User.objects.create_user(email='ungranted@example.test', is_superuser=True)
        for user_id in (ungranted.pk, self.foreign_user.pk, 9223372036854775807):
            with self.subTest(user_id=user_id), self.assertRaisesMessage(ValidationError, 'Record not found or unavailable'):
                self.log(participants=[{'contact_id': self.contact.pk}, {'user_id': user_id}])
        participants = [{'contact_id': self.contact.pk}, {'user_id': str(self.viewer_user.pk), 'role': 'from'}]
        result = self.log(participants=participants)
        user = next(item for item in get_interaction(self.actor, result['id'])['participants'] if item['user_id'])
        self.assertEqual(user['user_id'], self.viewer_user.pk)
        self.assertEqual(user['name'], 'viewer')
        self.assertEqual(user['address'], self.viewer_user.email)
        for model, record in ((User, self.viewer_user), (ProfileGrant, self.viewer_grant), (Profile, self.viewer_profile)):
            model.objects.filter(pk=record.pk).update(is_active=False)
            try:
                with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                    self.log(participants=participants)
            finally:
                model.objects.filter(pk=record.pk).update(is_active=True)

    def test_invalid_participants_and_unknown_keys_are_rejected_atomically(self):
        valid = {'contact_id': self.contact.pk}
        invalid = (
            [], {}, [valid] * 101, [None], [{'name': 'External only'}], [{'user_id': self.user.pk}],
            [valid, {}], [valid, {'name': '  ', 'address': ' '}],
            [dict(valid, user_id=self.user.pk)], [dict(valid, charity_id=self.other_charity.pk)],
            [dict(valid, created_by_id=self.foreign_user.pk)], [dict(valid, acting_profile_id=self.other_profile.pk)],
            [dict(valid, unknown='unexpected')], [dict(valid, id=str(uuid4()))],
            [dict(valid, name=None)], [dict(valid, address='x' * 321)], [dict(valid, role='')],
            [valid, {'user_id': True}], [valid, {'user_id': 'invalid'}], [valid, {'user_id': -1}],
            [valid, {'user_id': {}}], [valid, {'contact_id': self.other_contact.pk}],
        )
        before = self.counts()
        for participants in invalid:
            with self.subTest(participants=participants), self.assertRaises(ValidationError):
                self.log(participants=participants)
            self.assertEqual(self.counts(), before)

    def test_late_participant_failure_rolls_back_interaction_audits_and_receipt(self):
        save = InteractionParticipant.save
        calls = 0

        def fail_second(participant, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValidationError('Invalid second participant.')
            return save(participant, *args, **kwargs)

        before = self.counts()
        with patch.object(InteractionParticipant, 'save', fail_second):
            with self.assertRaisesMessage(ValidationError, 'Invalid second participant'):
                self.log(participants=[{'contact_id': self.contact.pk}, {'contact_id': self.second_contact.pk}])
        self.assertEqual(calls, 2)
        self.assertEqual(self.counts(), before)

    def test_restricted_visibility_is_shared_by_creator_profile_or_admin_only(self):
        restricted = self.log(visibility='restricted', body='Restricted body')
        team = self.log(body='Team body')
        for actor in (self.actor, self.shared_actor, self.admin):
            self.assertEqual(get_interaction(actor, restricted['id'])['body'], 'Restricted body')
            self.assertEqual(
                {item['id'] for item in contact_timeline(actor, self.contact.pk)['items']},
                {restricted['id'], team['id']},
            )
        # Same login, different acting profile does not retain creator access.
        for actor in (self.second_actor, self.viewer):
            with self.assertRaisesMessage(ValidationError, 'Record not found or unavailable'):
                get_interaction(actor, restricted['id'])
            self.assertEqual([item['id'] for item in contact_timeline(actor, self.contact.pk)['items']], [team['id']])
        for actor in (self.other_actor,):
            with self.assertRaises(ValidationError):
                get_interaction(actor, restricted['id'])
            with self.assertRaises(ValidationError):
                get_interaction(actor, team['id'])
        ProfileGrant.objects.filter(user=self.user, profile=self.other_profile).update(role='admin')
        with self.assertRaises(ValidationError):
            get_interaction(self.other_actor, restricted['id'])

    def test_timestamps_must_be_aware_valid_and_not_too_far_future(self):
        invalid = (
            datetime(2026, 1, 1), '2026-01-01T12:00:00', 'not-a-date', '2026-02-30T00:00:00Z',
            None, 1, {}, timezone.now() + timedelta(minutes=6),
        )
        for occurred_at in invalid:
            with self.subTest(occurred_at=occurred_at), self.assertRaises(ValidationError):
                self.log(occurred_at=occurred_at)
        for occurred_at in (self.when, self.when.isoformat(), '2000-01-01T12:00:00+01:00', timezone.now() + timedelta(minutes=4)):
            self.log(occurred_at=occurred_at)

    def test_web_meaningful_interactions_and_internal_note_rules(self):
        for purpose in ('enquiry', 'application', 'event_registration'):
            self.log(channel='web', purpose=purpose)
        for purpose in ('', 'page_view', 'page_views', 'email_opened', 'click', 'enquiry page_view'):
            with self.subTest(purpose=purpose), self.assertRaises(ValidationError):
                self.log(channel='web', purpose=purpose)
        self.log(channel='internal_note', direction='internal')
        for channel in Interaction.Channel.values:
            if channel != 'internal_note':
                with self.subTest(channel=channel), self.assertRaises(ValidationError):
                    self.log(channel=channel, direction='internal')
        for direction in ('inbound', 'outbound'):
            with self.assertRaises(ValidationError):
                self.log(channel='internal_note', direction=direction)

    def test_interaction_text_types_and_payload_bounds(self):
        before = self.counts()
        for change in (
            {'body': 'x' * 32001}, {'body': '😀' * 8001}, {'summary': 'x' * 4001},
            {'summary': '😀' * 1001}, {'subject': 'x' * 256}, {'body': None},
            {'body': '\x00'}, {'body': '\ud800'}, {'channel': 'page_view'}, {'direction': 'sideways'},
            {'visibility': 'public'}, {'duration_seconds': -1}, {'duration_seconds': True},
            {'duration_seconds': '10'}, {'duration_seconds': 2147483648}, {'external_id': 'no-source'},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.log(**change)
            self.assertEqual(self.counts(), before)
        result = self.log(body='x' * 32000, summary='x' * 4000, duration_seconds=0)
        self.assertEqual(len(get_interaction(self.actor, result['id'])['body']), 32000)
        self.assertNotIn('body', contact_timeline(self.actor, self.contact.pk)['items'][0])

    def test_contact_and_method_validation_and_primary_uniqueness(self):
        for change in (
            {'display_name': ''}, {'display_name': ' '}, {'display_name': 12},
            {'display_name': 'x' * 256}, {'kind': 'invalid'}, {'do_not_solicit': 'false'},
            {'first_name': 'x' * 151}, {'background': 'x' * 32001},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.create(**change)
        for change in (
            {'kind': 'fax'}, {'value': 'invalid-email'}, {'value': ''}, {'value': ' '},
            {'kind': 'phone', 'value': 'not a number'}, {'kind': 'phone', 'value': '12'},
            {'kind': 'phone', 'value': '1' * 21}, {'kind': 'postal', 'value': 'x' * 4001},
            {'label': 'x' * 101}, {'is_primary': 1}, {'value': None},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.method(**change)
        original = self.method(is_primary=True)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.method(value='second@example.test', is_primary=True)
        self.assertEqual(self.counts(), before)
        self.assertTrue(ContactMethod.objects.get(pk=original['id']).is_primary)
        self.method(kind='phone', value='+44 (20) 1234-5678', is_primary=True)
        self.method(kind='postal', value='1 High Street\nLondon')

    def test_contact_search_and_default_maximum_pagination(self):
        Contact.objects.bulk_create([
            Contact(charity=self.charity, display_name=f'Contact {index:03d}') for index in range(101)
        ])
        page = list_contacts(self.actor)
        self.assertEqual(len(page['items']), 50)
        self.assertEqual(page['next_offset'], 50)
        second = list_contacts(self.actor, offset=page['next_offset'])
        self.assertEqual(len(second['items']), 50)
        self.assertTrue({item['id'] for item in page['items']}.isdisjoint(item['id'] for item in second['items']))
        last = list_contacts(self.actor, offset=second['next_offset'])
        self.assertEqual(len(last['items']), 3)
        self.assertIsNone(last['next_offset'])
        self.assertEqual(len(list_contacts(self.actor, limit=100)['items']), 100)
        found = list_contacts(self.actor, search='ALICE')
        self.assertEqual([item['id'] for item in found['items']], [str(self.contact.pk)])
        self.assertEqual(list_contacts(self.actor, search='Private foreign')['items'], [])
        self.assertEqual(list_contacts(self.actor, offset=10000), {'items': [], 'next_offset': None})

    def test_timeline_pagination_orders_by_timestamp_then_id(self):
        interactions = [
            Interaction(
                id=UUID(int=index + 1), charity=self.charity, created_by=self.user, acting_profile=self.profile,
                channel='email', direction='inbound', occurred_at=self.when + timedelta(minutes=index // 2),
            ) for index in range(101)
        ]
        Interaction.objects.bulk_create(interactions)
        InteractionParticipant.objects.bulk_create([
            InteractionParticipant(charity=self.charity, interaction=item, contact=self.contact, role=role)
            for item in interactions for role in ('to', 'cc')
        ])
        expected = [str(item.pk) for item in reversed(interactions)]
        page = contact_timeline(self.actor, self.contact.pk)
        self.assertEqual([item['id'] for item in page['items']], expected[:50])
        self.assertEqual(page['next_offset'], 50)
        second = contact_timeline(self.actor, self.contact.pk, offset=50)
        self.assertEqual([item['id'] for item in second['items']], expected[50:100])
        self.assertEqual(second['next_offset'], 100)
        last = contact_timeline(self.actor, self.contact.pk, offset=100)
        self.assertEqual([item['id'] for item in last['items']], expected[100:])
        self.assertIsNone(last['next_offset'])
        self.assertEqual(len(contact_timeline(self.actor, self.contact.pk, limit=100)['items']), 100)
        self.assertEqual(contact_timeline(self.actor, self.contact.pk, offset=10000), {'items': [], 'next_offset': None})

    def test_invalid_pagination_is_rejected_for_both_lists(self):
        for values in (
            {'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': '50'},
            {'offset': -1}, {'offset': 10001}, {'offset': True}, {'offset': 1.5},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    list_contacts(self.actor, **values)
                with self.assertRaises(ValidationError):
                    contact_timeline(self.actor, self.contact.pk, **values)
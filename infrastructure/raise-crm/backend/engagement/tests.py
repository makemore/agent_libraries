"""Run with engagement.test_settings; never point this suite at production data."""

import json
from copy import deepcopy
from datetime import timedelta, timezone as datetime_timezone
from importlib import import_module
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import NotSupportedError, connections
from django.db.models import Count
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import Profile, ProfileGrant, User
from crm.access import Actor
from crm.models import Charity, Contact, ContactEngagementSummary, Donation, Website
from .checks import engagement_database_check
from .database import EngagementQuerySet, require_engagement
from .models import EngagementEvent, VisitorIdentity
from .router import EngagementRouter
from .services import bind_visitor, ingest_events, list_contact_events, refresh_contact_summary


configuration = import_module('raise.settings.engagement')


class ConfigurationTests(SimpleTestCase):
    def postgres(self, host='localhost', port='5432'):
        return {'ENGINE': 'django.db.backends.postgresql', 'HOST': host, 'PORT': port, 'NAME': 'crm'}

    def test_explicit_url_only_and_independent_result(self):
        original = {'default': self.postgres()}
        for absent in (None, ''):
            result = configuration.configure_engagement(original, absent)
            self.assertEqual(result, original)
            self.assertIsNot(result, original)
            self.assertIsNot(result['default'], original['default'])
            self.assertNotIn('engagement', result)
        result = configuration.configure_engagement(original, 'postgresql://events.example.test/events')
        self.assertEqual(result['engagement']['ENGINE'], 'django.db.backends.postgresql')
        self.assertNotIn('engagement', original)

    def test_loopback_equivalences_and_different_database_name_rejected(self):
        for host in ('localhost', 'LOCALHOST.', '127.0.0.1', '127.0.0.2', '[::1]', '[::ffff:127.0.0.1]'):
            with self.subTest(host=host), self.assertRaises(ImproperlyConfigured):
                configuration.configure_engagement(
                    {'default': self.postgres()}, f'postgresql://{host}:5432/different_database',
                )
        result = configuration.configure_engagement(
            {'default': self.postgres()}, 'postgresql://127.0.0.1:5433/events',
        )
        self.assertEqual(str(result['engagement']['PORT']), '5433')

    def test_invalid_urls_and_connection_overrides_have_sanitized_errors(self):
        for url in (
            'sqlite:///:memory:', 'mysql://events.example.test/events', 'not-a-url',
            'postgresql:///events', 'postgresql://events.example.test:invalid/events',
            'postgresql://events.example.test/events?host=localhost',
            'postgresql://events.example.test/events?service=shared',
        ):
            with self.subTest(url=url), self.assertRaises(ImproperlyConfigured) as error:
                configuration.configure_engagement({'default': self.postgres()}, url)
            self.assertNotIn(url, str(error.exception))

    def test_final_settings_are_rechecked_after_cloud_override(self):
        databases = configuration.configure_engagement(
            {'default': self.postgres()}, 'postgresql://events.example.test/events',
        )
        databases['default'] = self.postgres('events.example.test')
        with override_settings(DATABASES=databases):
            self.assertIn('engagement.E001', {error.id for error in engagement_database_check()})
            with self.assertRaises(ImproperlyConfigured):
                require_engagement()

    def test_sqlite_requires_explicit_opt_in_and_distinct_stores(self):
        databases = {
            'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'},
            'engagement': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'},
        }
        for allow in (False, 'true', 1):
            with self.subTest(allow=allow), self.assertRaises(ImproperlyConfigured):
                configuration.validate_engagement_databases(databases, allow_sqlite=allow)
        configuration.validate_engagement_databases(databases, allow_sqlite=True)
        for database in databases.values():
            database['NAME'] = 'same-test-file.sqlite3'
        with self.assertRaises(ImproperlyConfigured):
            configuration.validate_engagement_databases(databases, allow_sqlite=True)

    def test_optional_alias_does_not_allow_ingestion_and_router_is_required(self):
        with override_settings(DATABASES={'default': self.postgres()}, DATABASE_ROUTERS=[]):
            self.assertEqual(
                {error.id for error in engagement_database_check()}, {'engagement.E002'},
            )
            with self.assertRaises(ImproperlyConfigured):
                require_engagement()
        with override_settings(DATABASES={'default': self.postgres()}):
            self.assertEqual(engagement_database_check(), [])
            with self.assertRaises(ImproperlyConfigured):
                require_engagement()
        databases = {'default': self.postgres(), 'engagement': self.postgres('events.example.test')}
        databases['engagement']['TEST'] = {'MIRROR': 'default'}
        with self.assertRaises(ImproperlyConfigured):
            configuration.validate_engagement_databases(databases)


class EngagementTests(TestCase):
    databases = {'default', 'engagement'}

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email='engagement-admin@example.test')
        cls.charity = Charity.objects.create(name='First charity', default_currency='GBP')
        cls.profile = Profile.objects.create(charity=cls.charity, display_name='Admin')
        cls.grant = ProfileGrant.objects.create(user=cls.user, profile=cls.profile, role=ProfileGrant.Role.ADMIN)
        cls.actor = Actor(user_id=cls.user.pk, profile_id=cls.profile.pk)
        cls.website = Website.objects.create(charity=cls.charity, name='First site', domain='example.test')
        cls.contact = Contact.objects.create(charity=cls.charity, display_name='First contact')
        cls.other_charity = Charity.objects.create(name='Other charity', default_currency='GBP')
        cls.other_profile = Profile.objects.create(charity=cls.other_charity, display_name='Other admin')
        ProfileGrant.objects.create(user=cls.user, profile=cls.other_profile, role=ProfileGrant.Role.ADMIN)
        cls.other_actor = Actor(user_id=cls.user.pk, profile_id=cls.other_profile.pk)
        cls.other_website = Website.objects.create(charity=cls.other_charity, name='Other site', domain='other.test')
        cls.other_contact = Contact.objects.create(charity=cls.other_charity, display_name='Other contact')

    def event(self, **changes):
        return {
            'id': str(uuid4()), 'visitor_id': str(uuid4()), 'session_id': str(uuid4()),
            'event_type': 'page_view', 'occurred_at': timezone.now().isoformat(),
            'page_url': 'https://example.test/', 'consent_granted': True, **changes,
        }

    def record(self, **changes):
        return EngagementEvent(
            charity_id=self.charity.pk, website_id=self.website.pk, website_domain=self.website.domain,
            visitor_id=uuid4(), occurred_at=timezone.now(), event_type='page_view', fingerprint='0' * 64,
            **changes,
        )

    def bind(self, visitor_id, contact=None):
        return bind_visitor(
            self.actor, self.website.pk, visitor_id, (contact or self.contact).pk, 'admin_verified',
        )

    def writes(self, captured):
        return [query['sql'] for query in captured if query['sql'].lstrip().split()[0].upper() in {
            'INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'CREATE', 'ALTER', 'DROP',
        }]

    def test_router_schema_and_no_cross_database_relations(self):
        router = EngagementRouter()
        for model in (EngagementEvent, VisitorIdentity, Contact, User, ProfileGrant):
            alias = 'engagement' if model._meta.app_label == 'engagement' else 'default'
            self.assertEqual(router.db_for_read(model), alias)
            self.assertEqual(router.db_for_write(model), alias)
            for target in ('default', 'engagement', 'other'):
                self.assertEqual(router.allow_migrate(target, model._meta.app_label), target == alias)
        self.assertFalse(router.allow_relation(self.record(), self.contact))
        self.assertTrue(router.allow_relation(self.contact, self.charity))
        for model in (EngagementEvent, VisitorIdentity):
            self.assertFalse(any(field.is_relation for field in model._meta.fields))
            self.assertNotIn(model._meta.db_table, connections['default'].introspection.table_names())
            self.assertIn(model._meta.db_table, connections['engagement'].introspection.table_names())
        self.assertNotIn(Contact._meta.db_table, connections['engagement'].introspection.table_names())
        self.assertNotIn(User._meta.db_table, connections['engagement'].introspection.table_names())

    def test_explicit_aliases_rejected_for_all_managers(self):
        with self.assertNumQueries(0, using='default'):
            for model in (EngagementEvent, VisitorIdentity):
                for manager in (model.objects, model._base_manager, model._default_manager):
                    for alias in ('default', 'other', ''):
                        with self.subTest(model=model, alias=alias), self.assertRaises(ImproperlyConfigured):
                            manager.using(alias)
                        with self.assertRaises(ImproperlyConfigured):
                            manager.db_manager(alias)
                    self.assertEqual(manager.db_manager('engagement').db, 'engagement')

    def test_execution_shortcuts_and_cached_reads_cannot_use_default(self):
        operations = [
            lambda query: list(query), lambda query: list(query.values()),
            lambda query: list(query.values_list('id')), lambda query: list(query.iterator()),
            lambda query: query.count(), lambda query: query.exists(),
            lambda query: query.aggregate(total=Count('id')), lambda query: query.first(),
            lambda query: query.update(page_url=''), lambda query: query.delete(),
            lambda query: query.bulk_create([]), lambda query: query.bulk_update([], ['page_url']),
            lambda query: query._raw_delete('default'),
            lambda query: query._insert([], [], using='default'),
        ]
        with self.assertNumQueries(0, using='default'), self.assertNumQueries(0, using='engagement'):
            for model in (EngagementEvent, VisitorIdentity):
                for operation in operations:
                    with self.subTest(model=model, operation=operation), self.assertRaises(ImproperlyConfigured):
                        operation(EngagementQuerySet(model=model, using='default'))
                for operation in (list, lambda query: query.count(), lambda query: query.exists()):
                    query = EngagementQuerySet(model=model, using='default')
                    query._result_cache = []
                    with self.assertRaises(ImproperlyConfigured):
                        operation(query)

    def test_model_save_delete_refresh_and_bulk_operations_are_protected(self):
        record = self.record()
        identity = VisitorIdentity(
            charity_id=self.charity.pk, website_id=self.website.pk, visitor_id=uuid4(),
            contact_id=self.contact.pk, method='admin_verified',
        )
        for instance in (record, identity):
            with self.assertNumQueries(0, using='default'):
                for operation in (
                    lambda: instance.save(using='default'), lambda: instance.save(False, False, 'default'),
                    lambda: instance.save_base(using='default'), lambda: instance.delete(using='default'),
                    lambda: instance.refresh_from_db(using='default'),
                ):
                    with self.assertRaises(ImproperlyConfigured):
                        operation()
                instance.save(using='engagement')
                instance.refresh_from_db()
                self.assertEqual(instance._state.db, 'engagement')
                instance.delete()
        with self.assertNumQueries(0, using='default'):
            record = self.record()
            EngagementEvent.objects.bulk_create([record])
            record.page_url = 'https://example.test/discarded-path'
            EngagementEvent.objects.bulk_update([record], ['page_url'])
            self.assertEqual(EngagementEvent.objects.get(pk=record.pk).page_url, 'https://example.test/')
            EngagementEvent.objects.filter(pk=record.pk).delete()

    def test_model_validation_is_used_and_raw_queries_are_disabled(self):
        record = self.record(properties={'email': 'not-permitted'})
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            EngagementEvent.objects.bulk_create([record])
        with self.assertRaises(NotSupportedError):
            EngagementEvent.objects.raw('SELECT 1')
        with self.assertRaises(NotSupportedError):
            Contact.objects.filter(pk__in=EngagementEvent.objects.values('contact_id'))

    def test_missing_alias_fails_closed_even_without_router(self):
        databases = {'default': deepcopy(settings.DATABASES['default'])}
        with override_settings(DATABASES=databases, DATABASE_ROUTERS=[]):
            with self.assertNumQueries(0, using='default'):
                for operation in (
                    lambda: EngagementEvent.objects.count(), lambda: list(EngagementEvent.objects.all()),
                    lambda: self.record().save(), lambda: EngagementEvent.objects.bulk_create([]),
                ):
                    with self.assertRaises(ImproperlyConfigured):
                        operation()

    def test_ingestion_deduplicates_and_records_provenance_without_default_writes(self):
        event = self.event(event_type='donation_completed')
        with CaptureQueriesContext(connections['default']) as captured:
            self.assertEqual(ingest_events(self.actor, self.website.pk, [event, event]), {'accepted': 1, 'duplicates': 1})
            self.assertEqual(ingest_events(self.actor, self.website.pk, [event]), {'accepted': 0, 'duplicates': 1})
        self.assertEqual(self.writes(captured), [])
        record = EngagementEvent.objects.get()
        self.assertEqual(record.actor_user_id, self.user.pk)
        self.assertEqual(record.acting_profile_id, self.profile.pk)
        self.assertEqual(record.website_domain, self.website.domain)
        self.assertIsNone(record.contact_id)
        self.assertEqual(len(record.fingerprint), 64)
        self.assertEqual(Donation.objects.count(), 0)
        self.assertEqual(ContactEngagementSummary.objects.count(), 0)
        self.assertFalse(any('FOR UPDATE' in query['sql'].upper() for query in captured))

    def test_conflicts_are_generic_and_roll_back_entire_batch(self):
        event = self.event()
        ingest_events(self.actor, self.website.pk, [event])
        changed = {**event, 'event_type': 'donation_started'}
        with self.assertRaisesMessage(ValidationError, 'Event ID conflict.'):
            ingest_events(self.actor, self.website.pk, [self.event(), changed])
        self.assertEqual(EngagementEvent.objects.count(), 1)
        with self.assertRaisesMessage(ValidationError, 'Event ID conflict.'):
            ingest_events(self.other_actor, self.other_website.pk, [{**event, 'page_url': ''}])
        self.assertEqual(EngagementEvent.objects.count(), 1)

    def test_duplicate_race_uses_savepoint_and_checks_fingerprint(self):
        from .services import _duplicate

        event = self.event()
        ingest_events(self.actor, self.website.pk, [event])
        for changed, conflict in ((event, False), ({**event, 'event_type': 'donation_started'}, True)):
            calls = iter((False, True))

            def race(payload, fingerprint):
                return _duplicate(payload, fingerprint) if next(calls) else False

            with patch('engagement.services._duplicate', side_effect=race):
                if conflict:
                    with self.assertRaisesMessage(ValidationError, 'Event ID conflict.'):
                        ingest_events(self.actor, self.website.pk, [changed])
                else:
                    self.assertEqual(ingest_events(self.actor, self.website.pk, [changed]), {'accepted': 0, 'duplicates': 1})
        self.assertEqual(EngagementEvent.objects.count(), 1)

    def test_fingerprint_normalizes_timezone_but_preserves_microseconds(self):
        occurred_at = (timezone.now() - timedelta(minutes=1)).replace(microsecond=123456)
        event = self.event(occurred_at=occurred_at.isoformat())
        ingest_events(self.actor, self.website.pk, [event])
        equivalent = occurred_at.astimezone(datetime_timezone(timedelta(hours=2))).isoformat()
        self.assertEqual(ingest_events(self.actor, self.website.pk, [{**event, 'occurred_at': equivalent}])['duplicates'], 1)
        with self.assertRaisesMessage(ValidationError, 'Event ID conflict.'):
            ingest_events(self.actor, self.website.pk, [{**event, 'occurred_at': occurred_at + timedelta(microseconds=1)}])

    def test_consent_is_explicit_boolean_for_web_and_email(self):
        for event_type in ('page_view', 'email_opened'):
            for consent in (None, False, 1, 'true'):
                with self.subTest(consent=consent, event_type=event_type), self.assertRaises(ValidationError):
                    ingest_events(self.actor, self.website.pk, [self.event(event_type=event_type, consent_granted=consent)])
            event = self.event(event_type=event_type)
            del event['consent_granted']
            with self.assertRaises(ValidationError):
                ingest_events(self.actor, self.website.pk, [event])
        self.website.consent_required = False
        self.website.save()
        self.assertEqual(ingest_events(self.actor, self.website.pk, [event])['accepted'], 1)

    def test_urls_strip_all_sensitive_components_and_reject_unregistered_origins(self):
        event = self.event(page_url='https://EXAMPLE.TEST/private/path?discard=value#discard')
        ingest_events(self.actor, self.website.pk, [event])
        self.assertEqual(EngagementEvent.objects.get().page_url, 'https://example.test/')
        self.assertEqual(ingest_events(self.actor, self.website.pk, [{**event, 'page_url': 'https://example.test/'}])['duplicates'], 1)
        for url in (
            'https://unregistered.test/', 'https://sub.example.test/', 'https://userinfo@example.test/',
            'https://example.test:8443/', 'javascript:example.test', '/relative',
            'https://example.test\\@unregistered.test/', 'https://example.test/\n', 'https://example.test/' + 'x' * 4096,
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                ingest_events(self.actor, self.website.pk, [self.event(page_url=url)])

    def test_bounded_payload_validation_is_atomic(self):
        for changes in (
            {'id': 'invalid'}, {'visitor_id': 1}, {'session_id': ''}, {'event_type': 'custom'},
            {'occurred_at': timezone.now().replace(tzinfo=None)}, {'occurred_at': 'invalid'},
            {'occurred_at': '0001-01-01T00:00:00+01:00'},
            {'occurred_at': timezone.now() + timedelta(minutes=6)}, {'schema_version': True}, {'schema_version': 2},
            {'properties': {'page_title': 'not-permitted'}}, {'properties': {'appeal_id': str(uuid4())}},
            {'properties': {'bot': 1}}, {'properties': {'duration_seconds': -1}},
            {'properties': {'duration_seconds': float('nan')}}, {'properties': {'duration_seconds': float('inf')}},
            {'properties': {'duration_seconds': True}}, {'properties': {'duration_seconds': 86401}},
            {'properties': []}, {'actor_user_id': self.user.pk},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ingest_events(self.actor, self.website.pk, [self.event(), self.event(**changes)])
        for events in ({}, [self.event()] * 101, [None]):
            with self.assertRaises(ValidationError):
                ingest_events(self.actor, self.website.pk, events)
        self.assertEqual(EngagementEvent.objects.count(), 0)
        self.assertEqual(ingest_events(self.actor, self.website.pk, []), {'accepted': 0, 'duplicates': 0})
        self.assertEqual(ingest_events(self.actor, self.website.pk, [self.event(properties={'duration_seconds': 1.5, 'bot': False})])['accepted'], 1)

    def test_only_privileged_verified_identity_is_used_without_backfill(self):
        visitor = uuid4()
        anonymous = self.event(visitor_id=str(visitor))
        ingest_events(self.actor, self.website.pk, [anonymous])
        for contact_id in (str(self.contact.pk), str(self.other_contact.pk), None):
            with self.assertRaises(ValidationError):
                ingest_events(self.actor, self.website.pk, [self.event(contact_id=contact_id)])
        with CaptureQueriesContext(connections['default']) as captured:
            identity = self.bind(visitor)
            self.assertEqual(self.bind(visitor), identity)
        self.assertEqual(self.writes(captured), [])
        self.assertEqual(identity['actor_user_id'], self.user.pk)
        self.assertEqual(identity['acting_profile_id'], str(self.profile.pk))
        self.assertEqual(ingest_events(self.actor, self.website.pk, [anonymous])['duplicates'], 1)
        self.assertIsNone(EngagementEvent.objects.get(pk=anonymous['id']).contact_id)
        linked = self.event(visitor_id=str(visitor))
        ingest_events(self.actor, self.website.pk, [linked])
        self.assertEqual(EngagementEvent.objects.get(pk=linked['id']).contact_id, self.contact.pk)
        second = Contact.objects.create(charity=self.charity, display_name='Second contact')
        with self.assertRaisesMessage(ValidationError, 'Visitor identity conflict.'):
            self.bind(visitor, second)
        self.assertEqual(VisitorIdentity.objects.get().contact_id, self.contact.pk)

    def test_identity_race_preserves_mapping_and_provenance(self):
        visitor = uuid4()
        original = self.bind(visitor)
        identity = VisitorIdentity.objects.get()
        with patch.object(EngagementQuerySet, 'first', side_effect=[None, identity]):
            self.assertEqual(self.bind(visitor), original)
        self.assertEqual(VisitorIdentity.objects.count(), 1)

    def test_identity_is_website_scoped_and_orphaned_references_are_not_trusted(self):
        visitor = uuid4()
        self.bind(visitor)
        second_site = Website.objects.create(charity=self.charity, name='Second site', domain='second.test')
        event = self.event(visitor_id=str(visitor), page_url='')
        ingest_events(self.actor, second_site.pk, [event])
        self.assertIsNone(EngagementEvent.objects.get(pk=event['id']).contact_id)
        VisitorIdentity.objects.update(contact_id=self.other_contact.pk)
        event = self.event(visitor_id=str(visitor))
        ingest_events(self.actor, self.website.pk, [event])
        self.assertIsNone(EngagementEvent.objects.get(pk=event['id']).contact_id)

    def test_permissions_are_rechecked_and_tenant_references_hidden(self):
        for operation in (
            lambda: ingest_events(self.actor, self.other_website.pk, [self.event()]),
            lambda: self.bind(uuid4(), self.other_contact),
            lambda: list_contact_events(self.actor, self.other_contact.pk),
            lambda: list_contact_events(self.actor, uuid4()),
            lambda: refresh_contact_summary(self.actor, self.other_contact.pk),
            lambda: bind_visitor(self.actor, self.website.pk, uuid4(), self.contact.pk, 'browser'),
        ):
            with self.assertRaises(ValidationError):
                operation()
        self.grant.role = ProfileGrant.Role.FUNDRAISER
        self.grant.save()
        event = self.event()
        ingest_events(self.actor, self.website.pk, [event])
        for operation in (lambda: self.bind(uuid4()), lambda: refresh_contact_summary(self.actor, self.contact.pk)):
            with self.assertRaises(PermissionDenied):
                operation()
        self.grant.role = ProfileGrant.Role.VIEWER
        self.grant.save()
        self.assertEqual(list_contact_events(self.actor, self.contact.pk)['events'], [])
        with self.assertRaises(PermissionDenied):
            ingest_events(self.actor, self.website.pk, [event])
        self.grant.is_active = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            list_contact_events(self.actor, self.contact.pk)

    def test_inactive_website_rejects_ingest_and_binding(self):
        self.website.is_active = False
        self.website.save()
        with self.assertRaises(ValidationError):
            ingest_events(self.actor, self.website.pk, [self.event()])
        with self.assertRaises(ValidationError):
            self.bind(uuid4())

    def test_listing_is_paginated_stable_tenant_scoped_and_json_serializable(self):
        visitor = uuid4()
        self.bind(visitor)
        first = self.event(visitor_id=str(visitor), occurred_at=timezone.now() - timedelta(hours=1))
        second = self.event(visitor_id=str(visitor))
        ingest_events(self.actor, self.website.pk, [first, second])
        # Scalar IDs alone must not bypass tenant scoping even if data is corrupt.
        record = self.record(contact_id=self.contact.pk)
        record.charity_id = self.other_charity.pk
        record.save()
        result = list_contact_events(self.actor, self.contact.pk, limit=1)
        self.assertEqual([event['id'] for event in result['events']], [second['id']])
        self.assertEqual(list_contact_events(self.actor, self.contact.pk, limit=1, offset=1)['events'][0]['id'], first['id'])
        json.dumps(result, allow_nan=False)
        for limit, offset in ((0, 0), (101, 0), (True, 0), (1, -1), (1, True), (1, 100001)):
            with self.assertRaises(ValidationError):
                list_contact_events(self.actor, self.contact.pk, limit=limit, offset=offset)

    def test_summary_is_explicit_web_non_bot_only_and_one_bounded_default_write(self):
        visitor, session = uuid4(), uuid4()
        self.bind(visitor)
        last_web = timezone.now() - timedelta(minutes=2)
        events = [
            self.event(visitor_id=str(visitor), session_id=str(session), occurred_at=last_web - timedelta(minutes=1)),
            self.event(visitor_id=str(visitor), session_id=str(session), occurred_at=last_web, properties={'bot': False}),
            self.event(visitor_id=str(visitor), session_id=None, occurred_at=last_web - timedelta(seconds=30)),
            self.event(visitor_id=str(visitor), event_type='email_clicked'),
            self.event(visitor_id=str(visitor), properties={'bot': True}),
            self.event(),
        ]
        ingest_events(self.actor, self.website.pk, events)
        self.assertFalse(ContactEngagementSummary.objects.exists())
        for _ in range(2):
            with CaptureQueriesContext(connections['default']) as default_queries, CaptureQueriesContext(connections['engagement']) as event_queries:
                result = refresh_contact_summary(self.actor, self.contact.pk)
            writes = self.writes(default_queries)
            self.assertEqual(len(writes), 1)
            self.assertIn(ContactEngagementSummary._meta.db_table, writes[0])
            self.assertEqual(self.writes(event_queries), [])
            self.assertEqual(result['event_count'], 3)
            self.assertEqual(result['session_count'], 1)
            json.dumps(result, allow_nan=False)
        summary = ContactEngagementSummary.objects.get()
        self.assertEqual(summary.last_web_at, last_web)
        self.assertEqual(summary.created_by_id, self.user.pk)
        self.assertEqual(summary.acting_profile_id, self.profile.pk)
        self.assertEqual(EngagementEvent.objects.count(), len(events))
        self.assertEqual(Donation.objects.count(), 0)

    def test_empty_summary_and_sessions_do_not_merge_across_visitors_or_sites(self):
        result = refresh_contact_summary(self.actor, self.contact.pk)
        self.assertEqual((result['event_count'], result['session_count'], result['last_web_at']), (0, 0, None))
        session = uuid4()
        for visitor in (uuid4(), uuid4()):
            self.bind(visitor)
            ingest_events(self.actor, self.website.pk, [self.event(visitor_id=str(visitor), session_id=str(session))])
        site = Website.objects.create(charity=self.charity, name='Second site', domain='second.test')
        bind_visitor(self.actor, site.pk, visitor, self.contact.pk, 'admin_verified')
        ingest_events(self.actor, site.pk, [self.event(visitor_id=str(visitor), session_id=str(session), page_url='')])
        result = refresh_contact_summary(self.actor, self.contact.pk)
        self.assertEqual((result['event_count'], result['session_count']), (3, 3))
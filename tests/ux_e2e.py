"""Behavioral regression checks for the UX workbench. No live endpoints."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(IMAP_HOST='127.0.0.1', IMAP_PORT='9', IMAP_USER='demo@example.com',
                  IMAP_PASSWORD='demo', LLM_BASE_URL='', LLM_API_KEY='')
import app
import config
import engine
import learning
import store
import ux


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mail-ux-test-')
        config.DATA_DIR = self.tmp.name
        config.DB_PATH = os.path.join(self.tmp.name, 'triage.db')
        store.init_db()
        store.set_setting('proxy_mode', 'external')
        store.set_setting('llm_apply', False)
        app.app.config['TESTING'] = True
        self.client = app.app.test_client()
        self.ids = []
        for i in range(14):
            mid, _ = store.insert_message('INBOX', i + 1, 1, {
                'msgid': 'ux-%d@example.com' % i, 'from_addr': 'billing@example.com' if i % 2 else 'alex@example.com',
                'to_addr': 'demo@example.com', 'subject': 'Invoice %d' % i if i % 2 else 'Project %d' % i,
                'snippet': 'Payment request with a 20% discount' if i % 2 else 'Please review this project.',
                'date_ts': 1000 + i, 'status': 'classified', 'body_html_at': 1})
            store.update_message(mid, llm_category='Receipt' if i % 2 else 'Action', classified_by='llm', llm_needs_reply=1)
            self.ids.append(mid)

    def tearDown(self):
        self.tmp.cleanup()

    def preview(self, **changes):
        data = dict(preview_kind='rule', preview_id='0', name='Unsaved invoice rule',
                    match_mode='all', cond_field_0='subject', cond_op_0='contains',
                    cond_value_0='Invoice', move_to='Receipts', preview_subject='Invoice 123',
                    preview_from='billing@example.com', preview_body='A bill for equipment.')
        data.update(changes)
        return self.client.post('/automation/preview', data=data)

    def test_combined_filters_counts_and_neighbors(self):
        search = dict(q='Invoice', sender='billing', category='Receipt', folder='INBOX', after=1003, before=1010)
        rows = store.messages(search=search)
        self.assertEqual(store.count_messages(search=search), 4)
        self.assertEqual([r['id'] for r in rows], [self.ids[i] for i in (9, 7, 5, 3)])
        self.assertEqual(store.neighbors(self.ids[7], search=search), (self.ids[9], self.ids[5]))

    def test_literal_wildcards_and_injection(self):
        self.assertEqual(store.count_messages(search={'q': '20%'}), 7)
        self.assertEqual(store.count_messages(search={'q': "' OR 1=1 --"}), 0)
        self.assertEqual(store.count_messages(search={'q': '_'}), 0)
        self.assertEqual(store.count_messages(), 14)

    def test_filter_links_preserve_search(self):
        response = self.client.get('/messages?q=Invoice&sender=billing&per=10')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'q=Invoice', response.data)
        self.assertIn(b'sender=billing', response.data)
        self.assertIn(b'Search mail', response.data)
        detail = self.client.get('/messages/%d?q=Invoice&sender=billing' % self.ids[3])
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b'q=Invoice', detail.data)

    def test_category_correction_survives_reclass_and_return_to_old_value(self):
        mid = self.ids[0]
        for category in ('Receipt', 'Action', 'Receipt'):
            self.assertEqual(self.client.post('/messages/%d/category' % mid, data={'category': category}).status_code, 302)
        row = store.get_message(mid)
        self.assertEqual((row['llm_category'], row['classified_by']), ('Receipt', 'user'))
        result = engine.classify_and_store(row, store.all_settings())
        self.assertEqual(result['category'], 'Receipt')
        samples, _ = learning.build_dataset('category')
        self.assertEqual(next(s['label'] for s in samples if s['msg_id'] == mid), 'Receipt')
        self.assertTrue(any(e['kind'] == 'category' for e in store.get_msg_events(mid)))

    def test_invalid_category_and_missing_message_are_not_mutated(self):
        self.client.post('/messages/%d/category' % self.ids[0], data={'category': 'not-configured'})
        self.assertEqual(store.get_message(self.ids[0])['llm_category'], 'Action')
        self.assertEqual(self.client.post('/messages/999/category', data={'category': 'Receipt'}).status_code, 302)

    def test_reply_correction_can_reverse_repeatedly(self):
        mid = self.ids[0]
        for value in ('0', '1', '0', '1'):
            response = self.client.post('/messages/%d/needs-reply' % mid, data={'value': value})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(store.user_needs_reply(mid), int(value))
            self.assertEqual(engine._needs_reply_effective(mid, False), int(value))
        samples, _ = learning.build_dataset('needs_reply')
        self.assertEqual(next(s['label'] for s in samples if s['msg_id'] == mid), 1)

    def test_unsaved_rule_preview_does_not_persist_or_execute(self):
        before = (store.list_rules(), store.messages(), store.count_observations())
        with patch.object(engine, 'MailClient', side_effect=AssertionError('Preview touched mail')):
            response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['matched'])
        self.assertIn('Receipts', response.json['html'])
        self.assertEqual(before, (store.list_rules(), store.messages(), store.count_observations()))

    def test_preview_respects_guard_and_list_position(self):
        store.add_rule('Protect billing', 'all', [{'field': 'from', 'op': 'contains', 'value': 'billing@example.com'}], {})
        response = self.preview()
        self.assertEqual(response.json['result']['guard'], 'Protect billing')
        self.assertIsNone(response.json['result']['rule'])
        self.assertIsNone(response.json['result']['flow'])

    def test_preview_disabled_edit_replaces_original_position(self):
        rid = store.add_rule('Old name', 'all', [{'field': 'subject', 'op': 'contains', 'value': 'Invoice'}], {'move_to': 'Old'}, enabled=False)
        store.add_rule('Later rule', 'all', [{'field': 'subject', 'op': 'contains', 'value': 'Invoice'}], {'move_to': 'Later'})
        response = self.preview(preview_id=str(rid), name='Edited but unsaved', move_to='New')
        self.assertEqual(response.json['result']['rule']['name'], 'Edited but unsaved')
        self.assertIn('New', response.json['html'])
        self.assertEqual(store.get_rule(rid)['name'], 'Old name')

    def test_flow_preview_builds_draft_without_saving(self):
        steps = [{'type': 'draft', 'mode': 'fixed', 'body': 'Hi {sender}, thanks for your note.'}, {'type': 'tag', 'tag': 'Review'}]
        response = self.preview(preview_kind='flow', steps_json=json.dumps(steps))
        self.assertEqual(response.status_code, 200)
        self.assertIn('thanks for your note', response.json['result']['draft_preview']['body'])
        self.assertEqual(store.list_flows(), [])

    def test_rule_winner_cannot_also_trigger_ai_flow(self):
        store.add_rule('Invoice winner', 'all', [{'field': 'subject', 'op': 'contains', 'value': 'Invoice'}], {'move_to': 'Receipts'})
        store.add_flow('AI flow', 'all', [{'kind': 'category', 'value': 'Receipt'}], [{'type': 'tag', 'tag': 'Wrong'}])
        with patch.object(engine, 'classify_verdict', return_value=({'category': 'Receipt', 'confidence': .99, 'needs_reply': False}, None)):
            result = engine.simulate_email('a@example.com', 'Invoice', '', use_llm=True)
        self.assertEqual(result['rule']['name'], 'Invoice winner')
        self.assertIsNone(result['flow'])

    def test_preview_validation_and_escaping(self):
        self.assertEqual(self.preview(cond_op_0='regex', cond_value_0='[').status_code, 400)
        response = self.preview(name='<script>alert(1)</script>')
        self.assertNotIn('<script>alert(1)</script>', response.json['html'])
        self.assertIn('&lt;script&gt;', response.json['html'])

    def test_duplicate_diagnostics_ignore_disabled_predecessors(self):
        rows = [{'id': 1, 'name': 'Disabled', 'enabled': 0, 'conditions': '[{"field":"subject","op":"contains","value":"Invoice"}]', 'actions': '{}', 'match_mode': 'all'}]
        rows += [dict(rows[0], id=2, name='Enabled', enabled=1), dict(rows[0], id=3, name='Duplicate', enabled=1)]
        result = ux.rule_diagnostics(rows)
        self.assertNotIn(2, result)
        self.assertIn('Duplicate of #2', result[3])

    def test_constant_float_feature_model_roundtrip(self):
        examples = [('A' if i % 2 else 'B', 1, {'constant': .1, 'signal': i % 2}) for i in range(30)]
        model, _ = learning.train_logreg_ovr(examples, {'features': ['constant', 'signal']})
        serialized = json.loads(json.dumps(model))
        self.assertTrue(all(s > 0 for s in serialized['std']))
        self.assertIsNotNone(learning.predict_logreg_ovr(serialized, {'constant': .1, 'signal': 1}))

    def test_turbo_draft_redirects_to_editable_viewer(self):
        with patch.object(engine, 'generate_draft', return_value='Hi, thanks for your note.'):
            response = self.client.post('/messages/%d/draft?q=Project&f=needs_reply' % self.ids[0],
                                        headers={'Accept': 'text/vnd.turbo-stream.html, text/html'})
        self.assertEqual(response.status_code, 303)
        self.assertIn('q=Project', response.location)
        self.assertIn('f=needs_reply', response.location)
        viewer = self.client.get(response.location)
        self.assertEqual(viewer.status_code, 200)
        self.assertIn(b'id="reply-body"', viewer.data)
        self.assertIn(b'Hi, thanks for your note.', viewer.data)

    def test_date_filter_inclusive_display_day(self):
        response = self.client.get('/messages?after=1970-01-01&before=1970-01-01')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'14 messages', response.data)
        response = self.client.get('/messages?after=not-a-date')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Invalid date filter ignored', response.data)

    def test_empty_mock_folder_uid_tail_search(self):
        # A demo restart after Undo may leave a previously indexed folder empty.
        from types import SimpleNamespace
        import mock_e2e
        state = mock_e2e.MockState()
        state.ensure('Receipts')
        handler = mock_e2e.IMAPHandler.__new__(mock_e2e.IMAPHandler)
        handler.cur = 'Receipts'
        handler.server = SimpleNamespace(state=state)
        self.assertEqual(handler.search_uids(['UID', '2:*']), [])

    # ---- WP1: automation section classification -------------------------

    def test_automation_section_route_table(self):
        cases = {
            '/automation': 'overview',
            '/automation/': 'overview',
            '/automation/categories': 'categories',
            '/automation/categories?x=1': 'categories',
            '/automation/controls': 'controls',
            '/rules': 'rules',
            '/rules/7/edit': 'rules',
            '/rules/7/edit/': 'rules',
            '/rules/new': 'rules',
            '/flows': 'flows',
            '/flows/3/edit': 'flows',
            '/flows/new': 'flows',
            '/templates': 'drafting',
            '/templates/9/edit': 'drafting',
            '/templates/new': 'drafting',
        }
        for path, expected in cases.items():
            self.assertEqual(ux.automation_section(path), expected, path)
        for path in ('/automation/preview', '/automation/anything', '/settings',
                     '/learning', '/simulate', '/setup', '/', '/rules/test',
                     '/rules/7/toggle', ''):
            self.assertIsNone(ux.automation_section(path), path)

    # ---- WP1: category rows, references and drafting discovery -----------

    def _category_form(self, rows):
        form = MultiDict()
        form['row_count'] = str(len(rows))
        form['settings_version'] = 'test-version'
        for i, row in enumerate(rows):
            form['original_%d' % i] = str(i)
            form['name_%d' % i] = row['name']
            form['folder_%d' % i] = row['folder']
            form['configured_%d' % i] = '1' if row['configured'] else '0'
            form['mapping_present_%d' % i] = '1' if row['mapping_present'] else '0'
        return form

    def _row_index(self, rows, name):
        return next(i for i, r in enumerate(rows) if r['name'] == name)

    def test_category_rows_order_map_only_and_presence(self):
        settings = {'categories': ['Action', 'Receipt', 'Legacy'],
                    'category_folders': {'Receipt': 'Receipts', 'Promo': 'Promotions', 'Legacy': ''}}
        flows = [{'id': 5, 'name': 'Rec flow', 'enabled': 0, 'actions': '[]', 'conditions': json.dumps(
            [{'kind': 'category', 'value': 'receipt'}, {'kind': 'category', 'value': 'RECEIPT'}])}]
        heurs = [{'id': 7, 'name': 'H', 'enabled': 1, 'category': 'receipt'}]
        rows = ux.category_rows(settings, flows, heurs)
        self.assertEqual([r['name'] for r in rows], ['Action', 'Receipt', 'Legacy', 'Promo'])
        self.assertFalse(rows[0]['mapping_present'])
        self.assertEqual(rows[0]['folder'], '')
        self.assertTrue(rows[1]['mapping_present'])
        self.assertEqual(rows[2]['folder'], '')
        self.assertTrue(rows[2]['mapping_present'])  # explicit blank key preserved
        legacy = rows[3]
        self.assertFalse(legacy['configured'])
        self.assertTrue(legacy['mapping_present'])
        self.assertEqual(legacy['folder'], 'Promotions')
        # repeated category conditions deduplicate one flow reference
        self.assertEqual(rows[1]['flow_refs'], [{'id': 5, 'name': 'Rec flow', 'enabled': False}])
        self.assertEqual(rows[1]['classifier_refs'], [{'id': 7, 'name': 'H', 'enabled': True}])

    def test_automation_references_and_drafting_diagnostics(self):
        flows = [
            {'id': 1, 'name': 'Bad', 'enabled': 1, 'conditions': '{oops', 'actions': '[]'},
            {'id': 2, 'name': 'Good', 'enabled': 1, 'conditions': '[]',
             'actions': json.dumps([{'type': 'tag'}, {'type': 'draft'}, {'type': 'draft'}])},
            {'id': 3, 'name': 'NotDraft', 'enabled': 1, 'conditions': '[]',
             'actions': json.dumps([{'type': 'tag'}])},
            {'id': 4, 'name': 'BadActions', 'enabled': 1, 'conditions': '[]', 'actions': '{}'},
        ]
        refs = ux.automation_references(store.all_settings(), flows, [])
        self.assertEqual(refs['invalid_flows'],
                         [{'id': 1, 'name': 'Bad', 'reason': 'conditions is not valid JSON'}])
        drafted = ux.drafting_flows(flows)
        self.assertEqual([f['id'] for f in drafted['flows']], [2])
        self.assertEqual(drafted['flows'][0], {'id': 2, 'name': 'Good', 'enabled': True})
        self.assertEqual(drafted['invalid_flows'],
                         [{'id': 4, 'name': 'BadActions', 'reason': 'actions is not a list'}])

    def test_malformed_top_level_settings_raise(self):
        with self.assertRaises(ValueError):
            ux.category_rows({'categories': {'a': 1}, 'category_folders': {}}, [], [])
        with self.assertRaises(ValueError):
            ux.category_rows({'categories': [], 'category_folders': []}, [], [])

    def test_non_text_vocabulary_raises_actionable(self):
        with self.assertRaises(ValueError) as c1:
            ux.category_rows({'categories': ['Ok', 3], 'category_folders': {}}, [], [])
        self.assertIn('non-text', str(c1.exception))
        with self.assertRaises(ValueError) as c2:
            ux.category_rows({'categories': ['Ok', ['x']], 'category_folders': {}}, [], [])
        self.assertIn('non-text', str(c2.exception))
        with self.assertRaises(ValueError) as c3:
            ux.category_rows({'categories': [], 'category_folders': {'Ok': 5}}, [], [])
        self.assertIn('non-text', str(c3.exception))

    # ---- WP1: lossless parse and atomic save -----------------------------

    def test_category_form_lossless_roundtrip_and_atomic_save(self):
        store.set_setting('categories', ['Action', 'Receipt', 'Legacy'])
        store.set_setting('category_folders',
                          {'Receipt': 'Receipts', 'Promo': 'Promotions', 'Legacy': ''})
        settings = store.all_settings()
        refs = ux.automation_references(settings, store.list_flows(), store.list_heuristics())
        version = store.settings_version(settings)
        form = self._category_form(refs['rows'])
        cats, mapping = ux.parse_category_rows(form, settings, refs)
        self.assertEqual(cats, ['Action', 'Receipt', 'Legacy'])
        self.assertEqual(mapping, {'Receipt': 'Receipts', 'Promo': 'Promotions', 'Legacy': ''})
        self.assertTrue(store.save_category_settings(cats, mapping, version))
        self.assertEqual(store.get_setting('categories'), ['Action', 'Receipt', 'Legacy'])
        self.assertEqual(store.get_setting('category_folders'),
                         {'Receipt': 'Receipts', 'Promo': 'Promotions', 'Legacy': ''})

    def test_category_restore_explicit_blank_and_new_rows(self):
        store.set_setting('categories', ['Action', 'Receipt'])
        store.set_setting('category_folders', {'Receipt': 'Receipts', 'Promo': 'Promotions'})
        settings = store.all_settings()
        refs = ux.automation_references(settings, [], [])
        rows = refs['rows']
        form = self._category_form(rows)
        form['restore_%d' % self._row_index(rows, 'Promo')] = '1'
        form['folder_%d' % self._row_index(rows, 'Receipt')] = ''  # explicit blank
        form['folder_%d' % self._row_index(rows, 'Action')] = '  Actions  '  # trimmed new value
        cats, mapping = ux.parse_category_rows(form, settings, refs)
        self.assertEqual(cats, ['Action', 'Receipt', 'Promo'])
        self.assertEqual(mapping['Receipt'], '')          # explicit blank preserved
        self.assertEqual(mapping['Action'], 'Actions')    # edited value trimmed
        self.assertEqual(mapping['Promo'], 'Promotions')  # map-only kept
        # a wholly blank new row is ignored; a named new row is appended
        base = self._category_form(refs['rows'])
        base['row_count'] = '4'
        base['original_3'] = ''
        base['name_3'] = ''
        base['folder_3'] = ''
        base['configured_3'] = '1'
        base['mapping_present_3'] = '0'
        cats, mapping = ux.parse_category_rows(base, settings, refs)
        self.assertEqual(cats, ['Action', 'Receipt'])
        base['name_3'] = 'Fresh'
        base['folder_3'] = 'FreshFolder'
        cats, mapping = ux.parse_category_rows(base, settings, refs)
        self.assertEqual(cats, ['Action', 'Receipt', 'Fresh'])
        self.assertEqual(mapping['Fresh'], 'FreshFolder')

    def test_removal_reference_protection_and_no_mutation(self):
        store.set_setting('categories', ['Action', 'Receipt', 'Promo'])
        store.set_setting('category_folders', {'Receipt': 'Receipts', 'Promo': 'Promotions'})
        store.add_flow('Rec flow', 'all', [{'kind': 'category', 'value': 'receipt'}],
                       [{'type': 'tag', 'tag': 'x'}], enabled=False)
        store.add_heuristic('H', 'logreg', 'Receipt')
        settings = store.all_settings()
        refs = ux.automation_references(settings, store.list_flows(), store.list_heuristics())
        rows = refs['rows']
        form = self._category_form(rows)
        form['remove_%d' % self._row_index(rows, 'Receipt')] = '1'
        messages_before = store.messages()
        settings_before = (store.get_setting('categories'), store.get_setting('category_folders'))
        with self.assertRaises(ValueError) as ctx:
            ux.parse_category_rows(form, settings, refs)
        self.assertIn('referenced', str(ctx.exception))
        self.assertEqual(store.messages(), messages_before)
        self.assertEqual((store.get_setting('categories'), store.get_setting('category_folders')),
                         settings_before)
        # map-only removal without references succeeds
        form2 = self._category_form(rows)
        form2['remove_%d' % self._row_index(rows, 'Promo')] = '1'
        cats, mapping = ux.parse_category_rows(form2, settings, refs)
        self.assertEqual(cats, ['Action', 'Receipt'])
        self.assertNotIn('Promo', mapping)

    def test_removal_blocked_by_malformed_flow_json(self):
        store.set_setting('categories', ['Action', 'Receipt'])
        store.set_setting('category_folders', {'Receipt': 'Receipts'})
        fid = store.add_flow('Broken', 'all', [{'kind': 'category', 'value': 'x'}], [])
        store.update_flow(fid, conditions='{bad')
        settings = store.all_settings()
        refs = ux.automation_references(settings, store.list_flows(), store.list_heuristics())
        self.assertTrue(refs['invalid_flows'])
        form = self._category_form(refs['rows'])
        form['remove_%d' % self._row_index(refs['rows'], 'Receipt')] = '1'
        with self.assertRaises(ValueError) as ctx:
            ux.parse_category_rows(form, settings, refs)
        self.assertIn('unreadable', str(ctx.exception))

    def test_parse_rejects_malformed_or_unsafe_structures(self):
        store.set_setting('categories', ['Action', 'Receipt'])
        store.set_setting('category_folders', {'Receipt': 'Receipts'})
        settings = store.all_settings()
        refs = ux.automation_references(settings, [], [])
        good = self._category_form(refs['rows'])

        missing = MultiDict((k, v) for k, v in good.items() if k != 'original_1')
        repeated = self._category_form(refs['rows'])
        repeated.add('name_0', 'Action')
        renamed = self._category_form(refs['rows'])
        renamed['name_0'] = 'Renamed'
        bad_meta = self._category_form(refs['rows'])
        bad_meta['configured_0'] = '0'
        bad_count = self._category_form(refs['rows'])
        bad_count['row_count'] = 'not-int'
        comma = self._category_form(refs['rows'])
        comma['row_count'] = '3'
        comma['original_2'] = ''
        comma['configured_2'] = '1'
        comma['mapping_present_2'] = '0'
        comma['name_2'] = 'A,B'
        comma['folder_2'] = ''
        restore_bad = self._category_form(refs['rows'])
        restore_bad['restore_0'] = '1'  # Action is configured, not legacy
        remove_all = self._category_form(refs['rows'])
        remove_all['remove_0'] = '1'
        remove_all['remove_1'] = '1'

        for label, form in (('missing original', missing), ('repeated field', repeated),
                            ('renamed existing', renamed), ('changed metadata', bad_meta),
                            ('bad row_count', bad_count), ('comma in new name', comma),
                            ('restore non-legacy', restore_bad), ('remove all', remove_all)):
            with self.assertRaises(ValueError, msg=label):
                ux.parse_category_rows(form, settings, refs)

    def test_case_variant_categories_remain_editable_and_separate(self):
        store.set_setting('categories', ['Promo', 'promo'])
        store.set_setting('category_folders', {'Promo': 'P1', 'promo': 'P2'})
        settings = store.all_settings()
        refs = ux.automation_references(settings, [], [])
        form = self._category_form(refs['rows'])
        form['folder_0'] = 'P1-edited'
        cats, mapping = ux.parse_category_rows(form, settings, refs)
        self.assertEqual(cats, ['Promo', 'promo'])
        self.assertEqual(mapping, {'Promo': 'P1-edited', 'promo': 'P2'})

    def test_remove_all_vocabulary_allowed_when_legacy_restored(self):
        store.set_setting('categories', ['Action', 'Receipt'])
        store.set_setting('category_folders', {'Receipt': 'Receipts', 'Promo': 'Promotions'})
        settings = store.all_settings()
        refs = ux.automation_references(settings, [], [])
        rows = refs['rows']
        form = self._category_form(rows)
        form['remove_%d' % self._row_index(rows, 'Action')] = '1'
        form['remove_%d' % self._row_index(rows, 'Receipt')] = '1'
        form['restore_%d' % self._row_index(rows, 'Promo')] = '1'
        cats, mapping = ux.parse_category_rows(form, settings, refs)
        self.assertEqual(cats, ['Promo'])
        self.assertEqual(mapping, {'Promo': 'Promotions'})

    def test_removal_message_names_classifier_consumers(self):
        store.set_setting('categories', ['Action', 'Receipt'])
        store.set_setting('category_folders', {'Receipt': 'Receipts'})
        store.add_heuristic('Reply-ish', 'logreg', 'Receipt')
        settings = store.all_settings()
        refs = ux.automation_references(settings, store.list_flows(), store.list_heuristics())
        rows = refs['rows']
        form = self._category_form(rows)
        form['remove_%d' % self._row_index(rows, 'Receipt')] = '1'
        with self.assertRaises(ValueError) as ctx:
            ux.parse_category_rows(form, settings, refs)
        self.assertIn('classifier #', str(ctx.exception))
        self.assertNotIn('flow #', str(ctx.exception))

    def test_duplicate_existing_names_roundtrip_but_reject_edits(self):
        store.set_setting('categories', ['Dup', 'Dup', 'Other'])
        store.set_setting('category_folders', {'Dup': 'D'})
        settings = store.all_settings()
        refs = ux.automation_references(settings, [], [])
        form = self._category_form(refs['rows'])
        cats, mapping = ux.parse_category_rows(form, settings, refs)
        self.assertEqual(cats, ['Dup', 'Dup', 'Other'])
        self.assertEqual(mapping, {'Dup': 'D'})
        edited = self._category_form(refs['rows'])
        edited['folder_0'] = 'Elsewhere'
        with self.assertRaises(ValueError):
            ux.parse_category_rows(edited, settings, refs)

    def test_settings_version_deterministic_and_defaults(self):
        v1 = store.settings_version({'categories': ['A', 'B'], 'category_folders': {'A': '1'}})
        v2 = store.settings_version({'category_folders': {'A': '1'}, 'categories': ['A', 'B']})
        self.assertEqual(v1, v2)  # dict key order is canonical
        v3 = store.settings_version({'categories': ['B', 'A'], 'category_folders': {'A': '1'}})
        self.assertNotEqual(v1, v3)  # list order is significant
        self.assertEqual(store.settings_version({}),
                         store.settings_version(dict(store.DEFAULT_SETTINGS)))

    def test_save_category_settings_conflict_rollback_and_scope(self):
        settings = store.all_settings()
        version = store.settings_version(settings)
        store.set_setting('categories', ['LegacyOnly'])
        self.assertFalse(store.save_category_settings(['X'], {}, version))  # stale
        self.assertEqual(store.get_setting('categories'), ['LegacyOnly'])

        version = store.settings_version(store.all_settings())
        store.set_setting('llm_apply', True)
        store.set_setting('drafts_folder', 'Custom')
        self.assertTrue(store.save_category_settings(['Y'], {'Y': 'Z'}, version))
        self.assertEqual(store.get_setting('categories'), ['Y'])
        self.assertEqual(store.get_setting('category_folders'), {'Y': 'Z'})
        # only the two category keys changed
        self.assertTrue(store.get_setting('llm_apply'))
        self.assertEqual(store.get_setting('drafts_folder'), 'Custom')

        # failure after the first upsert rolls both keys back
        before = (store.get_setting('categories'), store.get_setting('category_folders'))
        version = store.settings_version(store.all_settings())
        with self.assertRaises(TypeError):
            store.save_category_settings(['X'], {'Y': object()}, version)
        self.assertEqual((store.get_setting('categories'), store.get_setting('category_folders')),
                         before)

    # ---- WP2: workspace chrome, routes and scoped forms ------------------

    def _ws_nav(self, response):
        html = response.data.decode('utf-8')
        i = html.find('<nav class="ws-nav"')
        j = html.find('</nav>', i)
        return html[i:j]

    def _side_nav(self, response):
        html = response.data.decode('utf-8')
        i = html.find('<nav class="nav">')
        j = html.find('</nav>', i)
        return html[i:j]

    def _form_data(self, form, **over):
        """Build posted data from a rendered form's own fields, then override."""
        data = {}
        for el in form.find_all(['input', 'select', 'textarea']):
            name = el.get('name')
            if not name:
                continue
            typ = (el.get('type') or '').lower()
            if typ == 'checkbox':
                data[name] = el.get('value', '1') if el.has_attr('checked') else ''
            else:
                data[name] = el.get('value') or ''
        data.update(over)
        return data

    def _cat_fields(self, rows, version, **over):
        data = {'settings_version': version, 'row_count': str(len(rows))}
        for i, r in enumerate(rows):
            data['original_%d' % i] = r['original']
            data['name_%d' % i] = r['name']
            data['folder_%d' % i] = r['folder']
            data['configured_%d' % i] = '1' if r['configured'] else '0'
            data['mapping_present_%d' % i] = '1' if r['mapping_present'] else '0'
        data.update(over)
        return data

    def _current_cat_fields(self):
        s = store.all_settings()
        rows = ux.category_rows(s, store.list_flows(), store.list_heuristics())
        display = [{'original': str(i), 'name': r['name'], 'folder': r['folder'],
                    'configured': r['configured'], 'mapping_present': r['mapping_present']}
                   for i, r in enumerate(rows)]
        return s, display, store.settings_version(s)

    def test_automation_workspace_chrome_and_single_nav(self):
        for path, active in (('/automation', 'overview'), ('/automation/categories', 'categories'),
                             ('/automation/controls', 'controls'), ('/rules', 'rules'),
                             ('/flows', 'flows'), ('/templates', 'drafting')):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            nav = self._ws_nav(response)
            self.assertIn('aria-label="Automation sections"', nav, path)
            self.assertEqual(nav.count('aria-current="page"'), 1, path)
        for path, needle in (('/rules', 'href="/rules" aria-current="page"'),
                             ('/flows', 'href="/flows" aria-current="page"'),
                             ('/templates', 'href="/templates" aria-current="page"'),
                             ('/automation/categories', 'href="/automation/categories" aria-current="page"'),
                             ('/automation/controls', 'href="/automation/controls" aria-current="page"')):
            self.assertIn(needle, self._ws_nav(self.client.get(path)), path)
        # Settings is off-workspace
        self.assertNotIn('<nav class="ws-nav"', self.client.get('/settings').data.decode())
        # More carries one Automation hub row instead of separate Rules/Flows/Templates rows
        more = self.client.get('/more').data
        self.assertIn(b'href="/automation"', more)
        self.assertNotIn(b'href="/rules"', more)
        self.assertNotIn(b'href="/flows"', more)
        self.assertNotIn(b'href="/templates"', more)
        # desktop sidebar exposes one Automation item, no separate Rules/Flows/Templates items
        side = self._side_nav(self.client.get('/'))
        self.assertIn('href="/automation"', side)
        self.assertNotIn('href="/rules"', side)
        self.assertNotIn('href="/flows"', side)
        self.assertNotIn('href="/templates"', side)

    def test_workspace_get_is_read_only_and_probe_free(self):
        before = (store.all_settings(), store.list_rules(), store.list_flows(),
                  store.list_heuristics(), store.messages())
        with patch.object(engine, 'MailClient', side_effect=AssertionError('GET touched mail')):
            for path in ('/automation', '/automation/categories', '/automation/controls',
                         '/rules', '/flows', '/templates'):
                self.assertEqual(self.client.get(path).status_code, 200, path)
        after = (store.all_settings(), store.list_rules(), store.list_flows(),
                 store.list_heuristics(), store.messages())
        self.assertEqual(before, after)

    def test_category_editor_renders_rows_map_only_and_spares(self):
        store.set_setting('categories', ['Action', 'Receipt'])
        store.set_setting('category_folders', {'Receipt': 'Receipts', 'Promo': 'Promotions'})
        response = self.client.get('/automation/categories')
        self.assertEqual(response.status_code, 200)
        page = response.data
        self.assertIn(b'name="settings_version"', page)
        self.assertIn(b'name="row_count" value="6"', page)  # 3 rows + 3 spares
        self.assertIn(b'Legacy mapping', page)
        self.assertIn(b'Keep in current folder', page)
        self.assertIn(b'id="cat-blank-row"', page)
        self.assertIn(b'name="restore_2"', page)   # Promo is map-only
        self.assertIn(b'id="filing-form"', page)
        self.assertIn(b'data-stored="0"', page)
        ux_js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'static', 'ux.js'), encoding='utf-8').read()
        self.assertIn('Enable automatic default filing? Future classifications may move mail', ux_js)

    def test_category_post_valid_303_and_lossless(self):
        s, rows, version = self._current_cat_fields()
        response = self.client.post('/automation/categories', data=self._cat_fields(rows, version))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(store.get_setting('categories'), s['categories'])
        self.assertEqual(store.get_setting('category_folders'), s['category_folders'])
        # add a new category with a destination
        rows2 = list(rows)
        response = self.client.post('/automation/categories', data=self._cat_fields(
            rows2, version, row_count=str(len(rows2) + 1), **{
                'original_%d' % len(rows2): '', 'name_%d' % len(rows2): 'Fresh',
                'folder_%d' % len(rows2): 'FreshBox',
                'configured_%d' % len(rows2): '1', 'mapping_present_%d' % len(rows2): '0'}))
        self.assertEqual(response.status_code, 303)
        self.assertIn('Fresh', store.get_setting('categories'))
        self.assertEqual(store.get_setting('category_folders').get('Fresh'), 'FreshBox')

    def test_category_post_stale_409_no_writes(self):
        s, rows, version = self._current_cat_fields()
        before = (store.get_setting('categories'), store.get_setting('category_folders'))
        response = self.client.post('/automation/categories',
                                    data=self._cat_fields(rows, 'stale-token'))
        self.assertEqual(response.status_code, 409)
        self.assertIn(b'Nothing was saved', response.data)
        self.assertIn(b'Reload current categories', response.data)
        self.assertEqual((store.get_setting('categories'), store.get_setting('category_folders')),
                         before)

    def test_category_post_invalid_422_preserves_input(self):
        s, rows, version = self._current_cat_fields()
        before = (store.get_setting('categories'), store.get_setting('category_folders'))
        i = len(rows)
        response = self.client.post('/automation/categories', data=self._cat_fields(
            rows, version, row_count=str(i + 1), **{
                'original_%d' % i: '', 'name_%d' % i: s['categories'][0],
                'folder_%d' % i: '', 'configured_%d' % i: '1', 'mapping_present_%d' % i: '0'}))
        self.assertEqual(response.status_code, 422)
        self.assertIn(b'id="form-err"', response.data)
        self.assertIn(b'duplicates an existing', response.data)
        self.assertIn(('value="%s"' % s['categories'][0]).encode(), response.data)
        self.assertEqual((store.get_setting('categories'), store.get_setting('category_folders')),
                         before)

    def test_category_post_referenced_removal_422(self):
        store.add_flow('Rec flow', 'all', [{'kind': 'category', 'value': 'Receipt'}],
                       [{'type': 'tag', 'tag': 'x'}], enabled=False)
        s, rows, version = self._current_cat_fields()
        idx = next(i for i, r in enumerate(rows) if r['name'] == 'Receipt')
        before = (store.get_setting('categories'), store.get_setting('category_folders'))
        response = self.client.post('/automation/categories',
                                    data=self._cat_fields(rows, version, **{'remove_%d' % idx: '1'}))
        self.assertEqual(response.status_code, 422)
        self.assertIn(b'referenced', response.data)
        self.assertEqual((store.get_setting('categories'), store.get_setting('category_folders')),
                         before)

    def test_category_save_does_not_touch_live_switches(self):
        s, rows, version = self._current_cat_fields()
        store.set_setting('llm_apply', False)
        store.set_setting('drafts_folder', 'KeepMe')
        self.client.post('/automation/categories', data=self._cat_fields(rows, version))
        self.assertFalse(store.get_setting('llm_apply'))
        self.assertEqual(store.get_setting('drafts_folder'), 'KeepMe')

    def test_controls_forms_own_exact_keys(self):
        page = self.client.get('/automation/controls')
        self.assertEqual(page.status_code, 200)
        for name in (b'rules_apply', b'flows_apply', b'llm_suggest', b'max_llm_per_hour',
                     b'llm_batch_per_cycle', b'classify_concurrency', b'heuristics_enabled',
                     b'heuristic_autorefine'):
            self.assertIn(b'name="%s"' % name, page.data)
        store.set_setting('rules_apply', False)
        store.set_setting('flows_apply', False)
        r = self.client.post('/settings', data={'section': 'behavior', 'scope': 'Rules live',
                                                'next': '/automation/controls', 'rules_apply': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(store.get_setting('rules_apply'))
        self.assertFalse(store.get_setting('flows_apply'))
        r = self.client.post('/settings', data={'section': 'behavior', 'scope': 'Flows live',
                                                'next': '/automation/controls', 'flows_apply': '1'})
        self.assertTrue(store.get_setting('flows_apply'))
        store.set_setting('heuristics_enabled', False)
        self.client.post('/settings', data={'section': 'behavior', 'scope': 'Classifiers',
                                            'next': '/automation/controls',
                                            'heuristics_enabled': '1', 'heuristic_autorefine': '0'})
        self.assertTrue(store.get_setting('heuristics_enabled'))
        self.assertFalse(store.get_setting('heuristic_autorefine'))
        self.client.post('/settings', data={'section': 'behavior', 'scope': 'Classification',
                                            'next': '/automation/controls', 'llm_suggest': '0',
                                            'max_llm_per_hour': '7', 'llm_batch_per_cycle': '3',
                                            'classify_concurrency': '4'})
        self.assertFalse(store.get_setting('llm_suggest'))
        self.assertEqual(store.get_setting('max_llm_per_hour'), 7)
        self.assertEqual(store.get_setting('classify_concurrency'), 4)
        # the classification form must not have changed the live switches
        self.assertTrue(store.get_setting('rules_apply'))

    def test_drafting_page_destination_and_flow_cards(self):
        store.add_flow('Drafter', 'all', [{'kind': 'category', 'value': 'Receipt'}],
                       [{'type': 'draft', 'mode': 'fixed', 'body': 'hi'}])
        store.add_flow('Mover', 'all', [{'kind': 'category', 'value': 'Receipt'}],
                       [{'type': 'move', 'folder': 'X'}])
        response = self.client.get('/templates')
        page = response.data
        self.assertIn(b'Drafting', page)
        self.assertIn(b'id="draft-destination"', page)
        self.assertIn(b'name="drafts_folder"', page)
        self.assertIn(b'Drafter', page)
        self.assertNotIn(b'Mover', page)
        self.assertIn(b'/automation/controls', self._ws_nav(response).encode() + response.data)

    def test_settings_landmarks_and_reply_detection(self):
        page = self.client.get('/settings').data
        for anchor in (b'id="sorting"', b'id="sort-rules"', b'id="sort-filing"',
                       b'id="ai-classify"', b'id="ai-classifiers"', b'id="ai-reply"'):
            self.assertIn(anchor, page)
        self.assertIn(b'/automation/categories', page)
        self.assertIn(b'/automation/controls', page)
        for field in (b'reply_tracking_enabled', b'reply_sent_folder', b'reply_identity_addresses'):
            self.assertIn(b'name="%s"' % field, page)
        # moved controls are gone from Settings
        for gone in (b'name="llm_apply"', b'name="llm_suggest"', b'name="heuristics_enabled"',
                     b'name="categories"', b'name="category_folders"'):
            self.assertNotIn(gone, page)
        self.assertEqual(app._settings_anchor('behavior', 'Filing & drafts'), 'sort-filing')
        self.assertEqual(app._settings_anchor('behavior', 'Sorting & filing'), 'sort-filing')
        self.assertEqual(app._settings_anchor('behavior', 'Checking'), 'mail-check')

    def test_vtpos_orders_workspace_sections(self):
        page = self.client.get('/').data.decode()
        self.assertLess(page.index("['/automation/categories', 1, 35]"),
                        page.index("['/automation', 1, 30]"))
        self.assertLess(page.index("['/automation', 1, 30]"),
                        page.index("['/simulate', 0, 39]"))

    # ---- WP2 corrections -------------------------------------------------

    def test_controls_rendered_forms_post_to_settings_action(self):
        # The Controls page is GET-only; its forms must target the Settings POST.
        self.assertEqual(self.client.post('/automation/controls').status_code, 405)
        soup = BeautifulSoup(self.client.get('/automation/controls').data, 'html.parser')
        scopes = {}
        for form in soup.find_all('form'):
            scope = form.find('input', {'name': 'scope'})
            if scope is None:
                continue  # base-shell forms (assistant/sidebar) are not scoped settings forms
            self.assertEqual(form.get('action'), '/settings', scope.get('value'))
            scopes[scope.get('value')] = form
        self.assertEqual(set(scopes),
                         {'Rules live', 'Flows live', 'Classification', 'Classifiers'})
        store.set_setting('rules_apply', False)
        store.set_setting('flows_apply', False)
        store.set_setting('heuristics_enabled', False)
        store.set_setting('heuristic_autorefine', False)
        store.set_setting('llm_suggest', False)
        # submit each form using its own extracted action and fields
        form = scopes['Rules live']
        self.assertEqual(self.client.post(form.get('action'), data=self._form_data(form, rules_apply='1')).status_code, 302)
        self.assertTrue(store.get_setting('rules_apply'))
        self.assertFalse(store.get_setting('flows_apply'))
        form = scopes['Flows live']
        self.client.post(form.get('action'), data=self._form_data(form, flows_apply='1'))
        self.assertTrue(store.get_setting('flows_apply'))
        self.assertTrue(store.get_setting('rules_apply'))
        form = scopes['Classifiers']
        self.client.post(form.get('action'), data=self._form_data(form, heuristics_enabled='1'))
        self.assertTrue(store.get_setting('heuristics_enabled'))
        self.assertFalse(store.get_setting('heuristic_autorefine'))
        self.assertTrue(store.get_setting('rules_apply'))
        form = scopes['Classification']
        self.client.post(form.get('action'), data=self._form_data(
            form, llm_suggest='1', max_llm_per_hour='9', llm_batch_per_cycle='2',
            classify_concurrency='5'))
        self.assertTrue(store.get_setting('llm_suggest'))
        self.assertEqual(store.get_setting('max_llm_per_hour'), 9)
        self.assertEqual(store.get_setting('llm_batch_per_cycle'), 2)
        self.assertEqual(store.get_setting('classify_concurrency'), 5)
        # the classification form did not touch the live switches
        self.assertTrue(store.get_setting('rules_apply'))
        self.assertTrue(store.get_setting('flows_apply'))

    def test_filing_and_drafting_rendered_forms_use_settings_action(self):
        soup = BeautifulSoup(self.client.get('/automation/categories').data, 'html.parser')
        filing = soup.find('form', {'id': 'filing-form'})
        self.assertIsNotNone(filing)
        self.assertEqual(filing.get('action'), '/settings')
        store.set_setting('llm_apply', False)
        self.assertEqual(self.client.post(filing.get('action'),
                                          data=self._form_data(filing, llm_apply='1')).status_code, 302)
        self.assertTrue(store.get_setting('llm_apply'))
        soup = BeautifulSoup(self.client.get('/templates').data, 'html.parser')
        dest = None
        for form in soup.find_all('form'):
            if form.find('input', {'name': 'drafts_folder'}):
                dest = form
                break
        self.assertIsNotNone(dest)
        self.assertEqual(dest.get('action'), '/settings')
        self.client.post(dest.get('action'), data=self._form_data(dest, drafts_folder='SavedBox'))
        self.assertEqual(store.get_setting('drafts_folder'), 'SavedBox')

    def test_blank_row_template_preserves_new_row_metadata(self):
        soup = BeautifulSoup(self.client.get('/automation/categories').data, 'html.parser')
        row = soup.find(id='cat-blank-row')
        self.assertIsNotNone(row)
        self.assertEqual(row.find(attrs={'data-tpl': 'original'}).get('value'), '')
        self.assertEqual(row.find(attrs={'data-tpl': 'configured'}).get('value'), '1')
        self.assertEqual(row.find(attrs={'data-tpl': 'mapping_present'}).get('value'), '0')
        self.assertEqual(row.find(attrs={'data-tpl': 'name'}).get('type'), 'text')
        self.assertEqual(row.find(attrs={'data-tpl': 'folder'}).get('type'), 'text')
        # new rows must not offer removal (the parser rejects it)
        self.assertIsNone(row.find(attrs={'data-tpl': 'remove'}))
        self.assertNotIn('Remove on save', row.get_text())
        # the clone keeps hidden metadata and clears only typed text
        ux_js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'static', 'ux.js'), encoding='utf-8').read()
        self.assertIn("el.type === 'text'", ux_js)
        self.assertNotIn("el.type === 'checkbox') { el.checked = false; } else { el.value = ''", ux_js)

    def test_overview_counts_enabled_not_live(self):
        store.set_setting('rules_apply', False)
        store.set_setting('flows_apply', False)
        page = self.client.get('/automation').data
        self.assertIn(b'rules enabled', page)
        self.assertIn(b'flows enabled', page)
        self.assertIn(b'classifiers enabled', page)
        self.assertNotIn(b'rules live', page)
        self.assertNotIn(b'flows live', page)
        self.assertNotIn(b'classifiers live', page)
        self.assertIn(b'Preview only', page)
        self.assertIn(b'Dry-run', page)

    def test_filing_copy_has_no_confidence_gate_claim(self):
        page = self.client.get('/automation/categories').data
        self.assertNotIn(b'low-confidence', page.lower())
        self.assertIn(b'Kept, guarded and already-filed', page)


if __name__ == '__main__':
    unittest.main(verbosity=2)

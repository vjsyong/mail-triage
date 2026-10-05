"""Behavioral regression checks for the UX workbench. No live endpoints."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

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


if __name__ == '__main__':
    unittest.main(verbosity=2)

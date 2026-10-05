"""Behavioral regression checks for the UX workbench. No live endpoints."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main(verbosity=2)

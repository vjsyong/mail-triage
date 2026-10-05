"""Reply outcome regressions: real MIME parsing/storage with a read-only mailbox."""
import email.message
import email.utils
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.update(IMAP_HOST='127.0.0.1', IMAP_USER='me@example.com', IMAP_PASSWORD='demo', IMAP_TLS='0')
import config
import engine
import learning
import replies
import store


class ReadOnlyMailbox:
    def __init__(self):
        self.flags = {'INBOX': '', 'Sent Items': '\\Sent', 'Drafts': '\\Drafts'}
        self.data = {f: {} for f in self.flags}
        self.validity = {f: 1 for f in self.flags}
        self.selected = None
        self.next_uid = 1

    def folders(self):
        return self.flags

    def select(self, folder):
        self.selected = folder
        return self.validity[folder]

    def ensure_selected(self, folder):
        self.select(folder)

    def search(self, *criteria):
        uids = sorted(self.data[self.selected])
        if criteria[0] == 'UID':
            lo, hi = criteria[1].split(':')
            hi = max(uids, default=0) if hi == '*' else int(hi)
            return [u for u in uids if int(lo) <= u <= hi]
        return uids

    def fetch_thread_headers(self, uid):
        return self.fetch_full(uid)['meta']

    def fetch_full(self, uid, limit=24000):
        return engine.parse_full_message(self.data[self.selected][uid], limit=limit)

    def add(self, folder, msgid, sender, recipient, body, timestamp, parent='', references='', auto='', html=None):
        message = email.message.EmailMessage()
        message['Message-ID'] = '<' + msgid + '>'
        message['From'], message['To'] = sender, recipient
        message['Subject'] = 'Reimbursement discussion'
        message['Date'] = email.utils.formatdate(timestamp, usegmt=True)
        if parent:
            message['In-Reply-To'] = '<' + parent + '>'
        if references:
            message['References'] = references
        if auto:
            message['Auto-Submitted'] = auto
        message.set_content(body)
        if html is not None:
            message.add_alternative(html, subtype='html')
        uid = self.next_uid
        self.next_uid += 1
        self.data[folder][uid] = message.as_bytes()
        return uid


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reply-test-')
        config.DATA_DIR, config.DB_PATH = self.tmp.name, os.path.join(self.tmp.name, 'triage.db')
        store.init_db()
        store.set_setting('proxy_mode', 'external')
        self.mail = ReadOnlyMailbox()
        self.now = int(time.time())
        self.calls = []
        self.verdict = {'sufficient': True, 'confidence': .99, 'reason': 'Gives the requested code and next steps.', 'unanswered_requests': []}
        self.original = self.request('request@example.com')
        self.patch = patch.object(engine, 'LLMClient', return_value=SimpleNamespace(assess_reply=self.assess))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def request(self, msgid, timestamp=None):
        uid = self.mail.add('INBOX', msgid, 'customer@example.com', 'me@example.com',
                            'Which documents do I submit, and what reimbursement code do I use?',
                            timestamp or self.now - 3600)
        self.mail.select('INBOX')
        parsed = self.mail.fetch_full(uid)
        mid, _ = store.insert_message('INBOX', uid, 1, {**parsed['meta'], 'snippet': parsed['text'], 'status': 'classified', 'body_html_at': 1})
        store.update_message(mid, llm_needs_reply=1, llm_category='Action', classified_by='llm')
        return mid

    def sent(self, msgid='sent@example.com', parent='request@example.com', folder='Sent Items', sender='me@example.com',
             recipient='customer@example.com', timestamp=None, references='', auto='', body='Use code 40500 and send your claim form.', html=None):
        return self.mail.add(folder, msgid, sender, recipient, body, timestamp or self.now - 60,
                             parent=parent, references=references, auto=auto, html=html)

    def assess(self, original, sent, original_text, sent_text):
        self.calls.append((original['id'], original_text, sent_text))
        return self.verdict

    def run_pass(self):
        return replies.reconcile(self.mail, store.all_settings())

    def test_sufficient_sent_reply_closes_request_and_survives_reclassification(self):
        self.sent()
        result = self.run_pass()
        self.assertEqual(result, {'checked': 1, 'answered': 1})
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 0)
        self.assertEqual(engine._needs_reply_effective(self.original, True), 0)
        state = replies.view_state(store.get_message(self.original))
        self.assertEqual(state['state'], 'answered')
        self.assertEqual(store.thread_headers(state['sent_id'])['sent_folder'], 'Sent Items')
        self.assertEqual(store.count_observations('reply'), 1)
        self.assertEqual(self.run_pass(), {'checked': 0, 'answered': 0})
        self.assertEqual(len(self.calls), 1)

    def test_references_fallback_matches_when_direct_parent_is_other_message(self):
        self.sent(parent='other@example.com', references='<request@example.com> <other@example.com>')
        self.run_pass()
        self.assertEqual(store.reply_checks(self.original)[0]['match'], 'References')
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 0)

    def test_partial_acknowledgement_stays_pending_and_is_not_repeated(self):
        self.sent(body='Thanks, I will reply properly later.')
        self.verdict = dict(self.verdict, sufficient=False, unanswered_requests=['Provide the requested information.'])
        self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        self.assertEqual(replies.view_state(store.get_message(self.original))['state'], 'partial')
        self.run_pass()
        self.assertEqual(len(self.calls), 1)

    def test_low_confidence_does_not_clear(self):
        self.sent()
        self.verdict['confidence'] = .60
        self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        self.assertEqual(store.reply_checks(self.original)[0]['state'], 'uncertain')

    def test_contradictory_sufficient_with_unanswered_request_stays_pending(self):
        self.sent()
        self.verdict['unanswered_requests'] = ['Still missing the code.']
        self.run_pass()
        self.assertEqual(store.reply_checks(self.original)[0]['state'], 'partial')
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)

    def test_subject_only_match_never_clears(self):
        self.sent(parent='')
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)

    def test_draft_does_not_count(self):
        self.sent(folder='Drafts')
        store.set_setting('reply_sent_folder', 'Drafts')
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)

    def test_wrong_recipient_or_non_owner_cannot_resolve(self):
        self.sent(recipient='unrelated@example.com')
        self.sent(msgid='forged@example.com', sender='another@example.com')
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)

    def test_automatic_reply_and_older_reply_do_not_count(self):
        self.sent(auto='auto-replied')
        self.sent(msgid='old@example.com', timestamp=self.now - 7200)
        self.run_pass()
        self.assertEqual(len(self.calls), 0)

    def test_plain_quoted_history_is_not_assessed_as_authored_response(self):
        self.sent(body='Thanks.\n\nOn Monday Customer wrote:\n> Use code 40500 and submit the form.')
        self.run_pass()
        self.assertEqual(self.calls[0][2], 'Thanks.')

    def test_html_quoted_history_is_excluded_even_with_plain_part_without_markers(self):
        self.sent(body='Thanks. Use code 40500.', html='<p>Thanks.</p><blockquote><p>Use code 40500.</p></blockquote>')
        self.run_pass()
        self.assertEqual(self.calls[0][2], 'Thanks.')

    def test_long_or_unverifiable_body_stays_pending(self):
        self.sent(body='x' * 7000)
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        self.assertIn('too long', store.reply_checks(self.original)[0]['reason'])

    def test_manual_reopen_requires_a_newer_sent_response(self):
        self.sent()
        self.run_pass()
        store.correct_needs_reply(self.original, True)
        self.assertEqual(engine._needs_reply_effective(self.original, False), 1)
        self.assertEqual(replies.view_state(store.get_message(self.original))['state'], 'reopened')
        self.run_pass()
        self.assertEqual(len(self.calls), 1)
        self.sent(msgid='newer@example.com', timestamp=self.now + 60)
        self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 0)

    def test_manual_change_during_assessment_wins(self):
        self.sent()
        def changed(*args):
            store.correct_needs_reply(self.original, True)
            return self.verdict
        with patch.object(engine, 'LLMClient', return_value=SimpleNamespace(assess_reply=changed)):
            self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        self.assertEqual(store.reply_checks(self.original), [])

    def test_manual_clear_before_assessment_remains_authoritative(self):
        self.sent()
        store.correct_needs_reply(self.original, False)
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(engine._needs_reply_effective(self.original, True), 0)

    def test_disabling_checks_during_assessment_prevents_automatic_clear(self):
        self.sent()
        def disabled(*args):
            store.set_setting('reply_tracking_enabled', False)
            return self.verdict
        with patch.object(engine, 'LLMClient', return_value=SimpleNamespace(assess_reply=disabled)):
            self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        self.assertEqual(store.reply_checks(self.original), [])

    def test_new_incoming_followup_does_not_inherit_old_resolution(self):
        self.sent()
        self.run_pass()
        followup = self.request('followup@example.com', timestamp=self.now)
        self.run_pass()
        self.assertEqual(store.get_message(followup)['llm_needs_reply'], 1)
        self.assertEqual(engine._needs_reply_effective(followup, True), 1)

    def test_reply_outcome_is_not_a_negative_training_label(self):
        self.sent()
        self.run_pass()
        samples, _ = learning.build_dataset('needs_reply')
        self.assertEqual(next(s['label'] for s in samples if s['msg_id'] == self.original), 1)
        self.assertEqual(store.list_labels(msg_id=self.original), [])

    def test_hourly_budget_pauses_assessment_but_preserves_late_evidence(self):
        self.sent()
        store.set_setting('max_llm_per_hour', 0)
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(len(store.sent_reply_evidence(self.now - 86400)), 1)
        self.assertEqual(store.reply_checks(self.original)[0]['state'], 'matched')
        store.set_setting('max_llm_per_hour', 40)
        self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 0)

    def test_batch_is_bounded_to_three_calls(self):
        self.sent()
        for i in range(4):
            parent = 'batch-%d@example.com' % i
            self.request(parent)
            self.sent(msgid='response-%d@example.com' % i, parent=parent)
        self.run_pass()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(store.llm_count_last_hour(), 3)

    def test_model_failure_does_not_poison_classification_and_cools_down(self):
        self.sent()
        broken = SimpleNamespace(assess_reply=lambda *args: (_ for _ in ()).throw(RuntimeError('model offline')))
        with patch.object(engine, 'LLMClient', return_value=broken):
            self.run_pass()
        self.assertEqual(store.llm_fail_count(self.original), 0)
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        self.assertEqual(store.llm_count_last_hour(), 1)
        self.run_pass()
        self.assertEqual(len(self.calls), 0)
        with patch('replies.time.time', return_value=self.now + 1000):
            self.run_pass()
        self.assertEqual(len(self.calls), 1)

    def test_newest_sent_scanned_first_with_incremental_historical_backfill(self):
        for i in range(120):
            self.sent(msgid='filler-%d@example.com' % i, parent='', timestamp=self.now - 1000 + i)
        self.sent(msgid='latest@example.com')
        self.run_pass()
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 0)
        first = store.get_setting('_reply_scan')['Sent Items']
        self.assertGreater(first['backfill_to'], 0)
        self.run_pass()
        self.assertEqual(store.get_setting('_reply_scan')['Sent Items']['backfill_to'], 0)
        self.assertEqual(len(store.sent_reply_evidence(self.now - 86400)), 121)

    def test_invalid_or_incomplete_model_contract_is_rejected(self):
        for malformed in [dict(self.verdict, sufficient='true'), dict(self.verdict, confidence=float('nan')),
                          dict(self.verdict, confidence=2), dict(self.verdict, confidence=True),
                          dict(self.verdict, reason=''), dict(self.verdict, unanswered_requests=None)]:
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    replies.validate_verdict(malformed)

    def test_sending_a_cached_draft_assesses_actual_sent_copy_not_old_draft(self):
        draft_uid = self.sent(folder='Drafts', body='Use code 40500 and send the claim form.')
        self.mail.select('Drafts')
        draft = self.mail.fetch_full(draft_uid)
        draft_id, _ = store.insert_message('Drafts', draft_uid, 1,
                                           {**draft['meta'], 'snippet': draft['text'], 'body_html': '<p>Use code 40500</p>', 'body_html_at': 1})
        sent_uid = self.sent(body='Thanks, I will reply properly later.')
        self.verdict = dict(self.verdict, sufficient=False, unanswered_requests=['Provide the requested details.'])
        self.run_pass()
        self.assertEqual(self.calls[0][2], 'Thanks, I will reply properly later.')
        self.assertEqual(store.get_message(self.original)['llm_needs_reply'], 1)
        sent = store.get_message(draft_id)
        self.assertEqual((sent['folder'], sent['uid']), ('Sent Items', sent_uid))
        self.assertEqual(sent['body_html'], '')
        self.assertEqual(store.thread_headers(draft_id)['sent_uid'], sent_uid)

    def test_real_chat_client_assessment_contract_over_mock_http(self):
        import threading
        from http.server import ThreadingHTTPServer
        import mock_e2e
        server = ThreadingHTTPServer(('127.0.0.1', 0), mock_e2e.LLMHandler)
        server.llm_lock = threading.Lock()
        server.inflight = server.max_inflight = 0
        server.calls, server.reject_ctk = [], False
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # Bypass only this test's patched factory, exercising the real implementation.
        self.patch.stop()
        try:
            client = engine.LLMClient({'base': 'http://127.0.0.1:%d/v1' % server.server_address[1],
                                       'key': 'demo', 'model': 'mock-model', 'thinking': 'off',
                                       'timeout': 5, 'fallback': None})
            value = client.assess_reply(store.get_message(self.original), {'to_addr': 'customer@example.com'},
                                        'Which documents and code do I use?', 'Send the claim form and use code 40500.')
            self.assertEqual(replies.validate_verdict(value)['state'], 'answered')
            self.assertEqual(len(server.calls), 1)
            packet = json.loads(server.calls[0]['user'])
            self.assertEqual(packet['sent']['attachments'], [])
        finally:
            self.patch.start()
            server.shutdown()
            server.server_close()

    def test_actual_attachment_evidence_reaches_assessment_without_attachment_body(self):
        uid = self.sent(body='I attached the claim form.')
        message = email.message.EmailMessage()
        message['Message-ID'] = '<sent@example.com>'
        message['From'], message['To'] = 'me@example.com', 'customer@example.com'
        message['Subject'] = 'Reimbursement discussion'
        message['Date'] = email.utils.formatdate(self.now - 60, usegmt=True)
        message['In-Reply-To'] = '<request@example.com>'
        message.set_content('I attached the claim form.')
        message.add_attachment(b'%PDF-1.4 private attachment body', maintype='application', subtype='pdf', filename='claim.pdf')
        self.mail.data['Sent Items'][uid] = message.as_bytes()
        packets = []
        def inspect(original, sent, original_text, sent_text):
            packets.append(sent['_reply_attachments'])
            self.assertNotIn('private attachment body', sent_text)
            return self.verdict
        with patch.object(engine, 'LLMClient', return_value=SimpleNamespace(assess_reply=inspect)):
            self.run_pass()
        self.assertEqual(packets[0], [{'name': 'claim.pdf', 'type': 'application/pdf'}])

    def test_header_metadata_survives_regular_index_recapture(self):
        self.sent()
        self.run_pass()
        sent = store.sent_reply_evidence(self.now - 86400)[0]
        self.mail.select('Sent Items')
        store.capture_thread_headers(sent['id'], self.mail.fetch_thread_headers(sent['uid']))
        self.assertEqual(store.thread_headers(sent['id'])['sent_folder'], 'Sent Items')
        self.assertEqual(store.reply_targets(sent['id'])[0]['id'], self.original)

    def test_ui_exposes_resolution_and_links_without_raw_header_noise(self):
        self.sent()
        self.run_pass()
        import app
        client = app.app.test_client()
        page = client.get('/messages/%d' % self.original)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Answered', page.data)
        self.assertIn(b'View sent reply', page.data)
        self.assertNotIn(b'header_evidence', page.data)
        sent = store.sent_reply_evidence(self.now - 86400)[0]
        # Cache a readable body to keep this fixture's viewer entirely local.
        store.update_message(sent['id'], snippet='Use code 40500 and send your claim form.', body_html_at=1)
        page = client.get('/messages/%d' % sent['id'])
        self.assertIn(('/messages/%d' % self.original).encode(), page.data)


if __name__ == '__main__':
    unittest.main(verbosity=2)

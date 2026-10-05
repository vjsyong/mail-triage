"""Playable UX sandbox: synthetic persistent mail + in-process IMAP/AI mocks.

No production DB, .env, OAuth account or real endpoint is required. The mock
protocol implementations are the same ones exercised by the integration suite.
"""
import importlib.util
import os
import socketserver
import sys
import threading
import time
from http.server import ThreadingHTTPServer


def serve_mock(server):
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def main():
    os.environ['UX_DEMO'] = '1'
    os.environ.setdefault('DATA_DIR', '/data')
    spec = importlib.util.spec_from_file_location('demo_mocks', os.path.join(os.path.dirname(__file__), 'tests', 'mock_e2e.py'))
    mocks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mocks)
    state = mocks.MockState()
    for name, flags in [('INBOX', ''), ('Newsletters', ''), ('Receipts', ''),
                        ('Drafts', '\\Drafts'), ('Trash', '\\Trash'), ('Sent Items', '\\Sent')]:
        state.ensure(name, flags)
    imap = socketserver.ThreadingTCPServer(('127.0.0.1', 0), mocks.IMAPHandler)
    imap.state, imap.no_copyuid = state, False
    llm = ThreadingHTTPServer(('127.0.0.1', 0), mocks.LLMHandler)
    llm.llm_lock = threading.Lock()
    llm.inflight = llm.max_inflight = 0
    llm.calls, llm.reject_ctk = [], False
    tei = ThreadingHTTPServer(('127.0.0.1', 0), mocks.TEIHandler)
    tei.calls = []
    imap_port, llm_port, tei_port = [serve_mock(server) for server in (imap, llm, tei)]
    os.environ.update(IMAP_HOST='127.0.0.1', IMAP_PORT=str(imap_port),
                      IMAP_USER='me@example.com', IMAP_PASSWORD='demo-password', IMAP_TLS='0',
                      LLM_BASE_URL='http://127.0.0.1:%d/v1' % llm_port,
                      LLM_API_KEY='demo-key', LLM_MODEL='mock-model',
                      EMBED_BASE_URL='http://127.0.0.1:%d' % tei_port,
                      RERANK_BASE_URL='http://127.0.0.1:%d' % tei_port)
    import store
    store.init_db()
    # Ports are ephemeral across restarts; keep the mock endpoints current.
    for key, value in {'proxy_mode': 'external', 'imap_host': '127.0.0.1',
                       'imap_port': imap_port, 'imap_user': 'me@example.com',
                       'imap_password': 'demo-password', 'imap_tls': '0',
                       'llm_base_url': os.environ['LLM_BASE_URL'], 'llm_api_key': 'demo-key',
                       'llm_model': 'mock-model', 'embed_base_url': os.environ['EMBED_BASE_URL'],
                       'rerank_base_url': os.environ['RERANK_BASE_URL'],
                       'rag_backend': 'legacy', 'plugin_schedules_enabled': 0}.items():
        store.set_setting(key, value)
    if not store.count_messages():
        seed(store, mocks, state)
    else:
        # Rehydrate the fake mailbox at the persisted locations, preserving UIDs.
        with store.db() as conn:
            rows = [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY id')]
        for row in rows:
            evidence = store.thread_headers(row['id'])
            temporary_uid = mocks.add_msg(state, row['from_addr'], row['subject'], row['snippet'],
                                          row['msgid'], folder=row['folder'], date=row['date'],
                                          to_addr=row['to_addr'],
                                          headers={k: evidence.get(v) or '' for k, v in
                                                   [('In-Reply-To', 'in_reply_to'), ('References', 'references'), ('Auto-Submitted', 'auto_submitted')]})
            folder = state.ensure(row['folder'])
            mail = folder['msgs'].pop(temporary_uid)
            folder['uids'].remove(temporary_uid)
            folder['msgs'][row['uid']] = mail
            folder['uids'].append(row['uid'])
            state._next_uid = max(state._next_uid, row['uid'] + 1)
    if not store.get_setting('_demo_reply_seeded', False):
        seed_replies(store, mocks, state)
    import app
    import learning
    import plugins
    import rag
    plugins.scan()
    store.set_setting('welcome_done', 1)
    for pid in ('mt-daily-digest', 'mt-invoice-finder'):
        if not plugins.get(pid).get('enabled'):
            plugins.set_enabled(pid, True)
    if not store.list_specialists():
        trained = learning.train_specialist('category', created_by='demo')
        learning.transition(trained['specialist_id'], 'shadow', reason='demo seed')
    # Seed a cheap keyword/vector index using the mock TEI, not local model downloads.
    for _ in range(10):
        result = rag.index_pass_active(limit=40)
        if not result.get('remaining'):
            break
    app.worker.state.update(last_ok=int(time.time()), last_cycle=time.time(), last_error=None)
    app.worker.start()
    app.classifier.start()
    app.indexer.start()
    app.llm_health.start()
    print('UX demo: synthetic mail / mock AI on port ' + os.environ.get('UI_PORT', '8097'), flush=True)
    app.app.run(host=os.environ.get('UI_HOST', '0.0.0.0'), port=int(os.environ.get('UI_PORT', '8097')), threaded=True)


def seed_replies(store, mocks, state):
    import engine
    now = int(time.time())
    mc = engine.MailClient().connect()
    try:
        for i, sufficient in enumerate((True, False)):
            subject = 'Reimbursement reply demo' if sufficient else 'Holding reply demo'
            request = 'Please tell me how to proceed with the June reimbursement, which claim documents to send, and the reimbursement code.'
            original_msgid = 'demo-reply-request-%d@example.com' % i
            original_date = time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(now - 3600))
            uid = mocks.add_msg(state, 'jiaying@example.com', subject, request, original_msgid, date=original_date)
            mc.select('INBOX')
            mid, _ = store.insert_message('INBOX', uid, 1, {**mc.fetch_meta(uid), 'status': 'classified'})
            store.update_message(mid, llm_category='Action', llm_needs_reply=1, llm_confidence=.95,
                                 llm_summary=request, classified_by='llm')
            response = ('Please send the June reimbursement form again and forward your student claim form. '
                        'I will check the reimbursement once those arrive. Use reimbursement code 40500.') if sufficient else 'Thanks, I will reply properly later.'
            sent_date = time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(now - 600))
            sent_uid = mocks.add_msg(state, 'me@example.com', 'Re: ' + subject,
                                     response + '\n\nOn Monday Jiaying wrote:\n> ' + request,
                                     'demo-reply-sent-%d@example.com' % i, folder='Sent Items',
                                     date=sent_date, headers={'In-Reply-To': '<' + original_msgid + '>',
                                                             'References': '<' + original_msgid + '>'},
                                     to_addr='jiaying@example.com')
            mc.select('Sent Items')
            sent_id, _ = store.insert_message('Sent Items', sent_uid, 1, {**mc.fetch_meta(sent_uid), 'status': 'sent'})
            store.update_message(sent_id, llm_category='Notification', llm_needs_reply=0, classified_by='demo')
    finally:
        mc.close()
    store.set_setting('_demo_reply_seeded', True)


def seed(store, mocks, state):
    import json
    from html import escape
    categories = ['Action', 'Notification', 'Newsletter', 'Receipt', 'Personal', 'Promo']
    examples = [
        ('jiaying@example.com', 'Re: Project reimbursement follow-up', 'Could you confirm the reimbursement status and let me know which documents to submit?'),
        ('calendar@example.com', 'Upcoming workshop: interaction design', 'The workshop starts on Friday at 14:00. Registration is open.'),
        ('news@example.com', 'Research newsletter — October edition', 'Our monthly newsletter: projects, events and research updates.'),
        ('billing@example.com', 'Invoice 8842 — equipment receipt', 'Invoice 8842. Total USD 240.00. Due date: 20 October. Thank you for your order.'),
        ('alex@example.com', 'Lunch invitation for next week', 'Would you like to join us for lunch on Tuesday? Let me know.'),
        ('offers@example.com', 'Promo: save 20% on your next order', 'This week only: a special discount on accessories.'),
    ]
    now = int(time.time())
    for i in range(120):
        category = categories[i % len(categories)]
        sender, subject, body = examples[i % len(examples)]
        ts = now - i * 7200
        date = time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(ts))
        if i >= 6:
            subject += ' · example %d' % (i + 1)
        folder = 'Newsletters' if category == 'Newsletter' else 'INBOX'
        quote = '\n\nOn Monday Alex wrote:\n> Here is the earlier project context.\n> Please review the previous notes.' if i == 0 else ''
        uid = mocks.add_msg(state, sender, subject, body + quote, 'ux-demo-%d@example.com' % i, folder=folder, date=date)
        mid, _ = store.insert_message(folder, uid, 1, {'from_addr': sender, 'to_addr': 'me@example.com',
            'subject': subject, 'msgid': 'ux-demo-%d@example.com' % i, 'date': date, 'date_ts': ts,
            'snippet': body + quote, 'status': 'classified', 'body_html_at': now,
            'body_html': '<p>Dear Alex,</p><p>' + escape(body) + '</p>' +
                         ('<blockquote>' + '<p>Earlier project context and expense notes.</p>' * 25 + '</blockquote>' if i == 0 else '')})
        store.update_message(mid, llm_category=category, llm_confidence=.95,
                             llm_summary=body, llm_reason='Synthetic example for this category.',
                             llm_needs_reply=int(category in ('Action', 'Personal')),
                             classified_by='llm', llm_suggested_folder='Receipts' if category == 'Receipt' else '')
        store.log_msg_event(mid, 'classify', json.dumps({'category': category, 'confidence': .95,
                              'by': 'demo AI', 'needs_reply': category in ('Action', 'Personal')}))
    for key, value in {'categories': categories, 'my_name': 'Alex', 'welcome_dismissed': 1,
                       'rules_apply': False, 'flows_apply': False, 'llm_apply': False,
                       'llm_suggest': True, 'max_llm_per_hour': 200,
                       'category_folders': {'Newsletter': 'Newsletters', 'Receipt': 'Receipts'}}.items():
        store.set_setting(key, value)
    store.add_rule('Protect VIP', 'all', [{'field': 'from', 'op': 'contains', 'value': 'vip@example.com'}], {})
    receipt = [{'field': 'subject', 'op': 'contains', 'value': 'Invoice'}]
    store.add_rule('Invoice organizer', 'all', receipt, {'move_to': 'Receipts'})
    store.add_rule('Duplicate invoice organizer', 'all', receipt, {'move_to': 'Receipts'})
    store.add_flow('Lunch drafts', 'all', [{'field': 'subject', 'op': 'contains', 'value': 'Lunch'}],
                   [{'type': 'draft', 'mode': 'fixed', 'body': 'Hi {sender}, thanks for the invitation. I will follow up shortly. Best, {my_name}'}, {'type': 'tag', 'tag': 'Lunch'}])
    store.add_template('Acknowledgement', 'Re: {subject}', 'Hi {sender}, thanks for your note. I will get back to you shortly. Best, {my_name}')
    # Include a real mock-mail move so Undo is immediately playable.
    import engine
    invoice = store.get_message(4)
    mc = engine.MailClient().connect()
    try:
        mc.select(invoice['folder'])
        new_uid = mc.move(invoice['uid'], 'Receipts', msgid=invoice['msgid'])
    finally:
        mc.close()
    store.record_move(invoice, 'Receipts', 'manual')
    store.update_message(4, folder='Receipts', uid=new_uid, action_taken='move:Receipts', status='llm-moved')
    store.add_agent_action('move', 'move_message', 'Move the latest project follow-up to Review',
                           {'tool': 'move_message', 'args': {'message_id': 1, 'target_folder': 'Review'}}, session_id=0)
    store.log_event('info', 'UX demo seeded with 120 synthetic messages. AI responses are mocked.')


if __name__ == '__main__':
    main()

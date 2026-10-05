"""Read-only Sent reconciliation and auditable needs-reply outcomes.

Headers establish thread membership; an independent assessment establishes whether
the newly authored response sufficiently handles the incoming request.
"""
import email.utils
import hashlib
import json
import math
import re
import threading
import time
from html.parser import HTMLParser

import store

_LOCK = threading.Lock()
CONFIDENCE = .90
BODY_LIMIT = 6000
_SENT_NAMES = {'sent', 'sent items', 'sent messages', '[gmail]/sent mail', '已发送邮件', '寄件備份'}


def _is_drafts(name, flags=''):
    return '\\drafts' in flags.lower() or name.lower() in {'drafts', 'draft', '[gmail]/drafts', '草稿', '草稿箱'}


def message_ids(value):
    ids = re.findall(r'<([^<>\s]+)>', str(value or ''))
    if not ids and value and not re.search(r'\s', str(value)):
        ids = [str(value).strip('<>')]
    result = []
    for mid in ids[-30:]:
        if '@' in mid:
            local, domain = mid.rsplit('@', 1)
            result.append(local + '@' + domain.lower())
    return result


def identities(settings):
    import engine
    values = [engine.imap_config()['user']] + list(settings.get('reply_identity_addresses') or [])
    return {email.utils.parseaddr(str(v))[1].lower() for v in values if '@' in str(v)}


def sent_folders(mc, settings):
    folders = mc.folders()
    chosen = settings.get('reply_sent_folder') or ''
    if chosen:
        return [chosen] if chosen in folders and not _is_drafts(chosen, folders[chosen]) else []
    special = [name for name, flags in folders.items() if '\\sent' in flags.lower() and not _is_drafts(name, flags)]
    return special or [name for name, flags in folders.items() if name.lower() in _SENT_NAMES and not _is_drafts(name, flags)]


def authored_text(text):
    lines = str(text or '').replace('\r\n', '\n').splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('>') or re.match(r'^On .{3,200} wrote:', stripped, re.I) or re.match(r'^在.+(?:写道|寫道)[:：]', stripped) or stripped.startswith('-----Original Message-----'):
            return '\n'.join(lines[:i]).strip()
        if re.match(r'^(From:|发件人:|寄件者:)', stripped, re.I) and sum(
                bool(re.match(r'^(From:|Sent:|Date:|To:|Subject:|发件人:|日期:|发送时间:|收件人:|主题:|寄件者:)', s.strip(), re.I))
                for s in lines[i:i + 12]) >= 3:
            return '\n'.join(lines[:i]).strip()
    return '\n'.join(lines).strip()


class _AuthoredHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.stopped, self.skip = [], False, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'blockquote' or (attrs.get('id') or '').lower() == 'divrplyfwdmsg' or 'gmail_quote' in (attrs.get('class') or '').split():
            self.stopped = True
        if tag in ('script', 'style'):
            self.skip = True
        if tag in ('p', 'br', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = False
        if tag in ('p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.stopped and not self.skip:
            self.parts.append(data)


def _verified_payload(mc, row, headers_only=False):
    headers = row.get('headers') or {}
    folder = headers.get('sent_folder') if headers.get('sent_uid') else row['folder']
    uid = headers.get('sent_uid') or row['uid']
    expected = message_ids(row.get('msgid'))
    if not expected:
        raise RuntimeError('Message-ID is unavailable for verification.')
    expected_sender = (row.get('headers') or {}).get('from_addr') or row.get('from_addr') or ''
    def read(location):
        mc.ensure_selected(location[0])
        value = {'meta': mc.fetch_thread_headers(location[1])} if headers_only else mc.fetch_full(location[1], limit=24000)
        meta = value.get('meta') or {}
        if message_ids(meta.get('msgid')) != expected or email.utils.parseaddr(meta.get('from_addr') or '')[1].lower() != email.utils.parseaddr(expected_sender)[1].lower():
            raise RuntimeError('Mailbox copy no longer matches the captured message identity.')
        return value
    try:
        return read((folder, uid))
    except (RuntimeError, KeyError):
        excluded = [f for f, flags in mc.folders().items() if _is_drafts(f, flags)]
        found = mc.locate_message(row['msgid'], preferred=folder, exclude_folders=excluded)
        if not found:
            raise RuntimeError('Mailbox copy could not be found by Message-ID.')
        value = read(found)
        uv = mc.select(found[0])
        store.update_message(row['id'], folder=found[0], uid=found[1], uidvalidity=uv)
        row.update(folder=found[0], uid=found[1], uidvalidity=uv)
        store.log_msg_event(row['id'], 'relocated', 'Located by Message-ID in %s for a reply check.' % found[0])
        return value


def _read_authored(mc, row):
    payload = _verified_payload(mc, row)
    text = payload.get('text') or ''
    if payload.get('html'):
        parser = _AuthoredHTML()
        parser.feed(payload['html'])
        text = ''.join(parser.parts)
    text = authored_text(text)
    if not text or len(text) > BODY_LIMIT:
        raise RuntimeError('Authored message is empty or too long for a reliable reply check.')
    if payload.get('attachment_count', 0) > 20:
        raise RuntimeError('Attachment evidence is too large for a reliable reply check.')
    row['_reply_attachments'] = payload.get('attachments') or []
    return text


def validate_verdict(value):
    if not isinstance(value, dict) or type(value.get('sufficient')) is not bool:
        raise ValueError('Reply assessment must contain a boolean sufficient value.')
    confidence = value.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError('Reply assessment has an invalid confidence.')
    unanswered = value.get('unanswered_requests')
    if not isinstance(unanswered, list) or not all(isinstance(v, str) for v in unanswered):
        raise ValueError('Reply assessment must list unanswered requests.')
    reason = value.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Reply assessment must explain its decision.')
    sufficient = value['sufficient'] and not unanswered and confidence >= CONFIDENCE
    state = 'answered' if sufficient else ('partial' if not value['sufficient'] or unanswered else 'uncertain')
    return {'state': state, 'confidence': confidence, 'reason': reason.strip()[:500],
            'unanswered_requests': [s[:200] for s in unanswered[:10]]}


def scan_sent(mc, settings):
    """Cache evidence even when classification or the model budget is paused."""
    checkpoints = store.get_setting('_reply_scan', {}) or {}
    folders = sent_folders(mc, settings)
    for folder in folders:
        uv = mc.select(folder)
        previous = checkpoints.get(folder) or {}
        state = dict(previous) if previous.get('uv') == uv else {'uv': uv, 'uid': 0}
        last = int(state.get('uid') or 0)
        since = time.strftime('%d-%b-%Y', time.gmtime(time.time() - 30 * 86400))
        uids = mc.search('UID', '%d:*' % (last + 1)) if last else mc.search('SINCE', since)
        uids = [u for u in uids if u > last]
        if not last and len(uids) > 100:
            state.update(backfill_min=uids[0], backfill_to=uids[-101], uid=uids[-100] - 1)
            uids = uids[-100:]
        new = uids[:100]
        older = []
        if state.get('backfill_to') and len(new) < 100:
            older = mc.search('UID', '%d:%d' % (state['backfill_min'], state['backfill_to']))[-(100 - len(new)):]
        checkpoints[folder] = state
        store.set_setting('_reply_scan', checkpoints)
        for uid in new + list(reversed(older)):
            meta = mc.fetch_thread_headers(uid)
            if not meta.get('msgid'):
                raise RuntimeError('Sent header fetch returned no Message-ID; checkpoint retained.')
            row = store.get_message_by_uid(folder, uid, uv) or store.find_message_by_msgid(meta['msgid'])
            if not row:
                store.insert_message(folder, uid, uv, {**meta, 'status': 'sent'})
                row = store.get_message_by_uid(folder, uid, uv)
            elif _is_drafts(row['folder'], mc.folders().get(row['folder'], '')):
                # A client often retains Message-ID when sending a saved draft.
                # The sent copy, not the cached draft body, becomes canonical.
                store.update_message(row['id'], folder=folder, uid=uid, uidvalidity=uv,
                                     from_addr=meta.get('from_addr') or '', to_addr=meta.get('to_addr') or '',
                                     subject=meta.get('subject') or '',
                                     date=meta['date'], date_ts=store.date_ts_from(meta['date']),
                                     sort_ts=store.date_ts_from(meta['date']), status='sent',
                                     snippet='', body_html='', body_cids='', body_html_at=0)
            store.capture_thread_headers(row['id'], meta, sent_folder=folder, sent_uid=uid, sent_uv=uv)
            if uid in new:
                state['uid'] = uid
            else:
                state['backfill_to'] = uid - 1 if uid > state['backfill_min'] else 0
            store.set_setting('_reply_scan', checkpoints)
    return folders


def _matches(original, sent, owners, correction):
    headers = sent['headers']
    sender = email.utils.parseaddr(headers.get('from_addr') or '')[1].lower()
    target = email.utils.parseaddr(original.get('from_addr') or '')[1].lower()
    targets = ({target} | set((original.get('headers') or {}).get('reply_to') or [])) - owners
    if sender not in owners or not target or target in owners or not targets.intersection(headers.get('recipients', [])):
        return False
    if str(headers.get('auto_submitted') or '').lower() not in ('', 'no') or re.match(r'^(automatic reply|auto.?reply|out of office)\s*:', sent.get('subject') or '', re.I):
        return False
    original_ts, sent_ts = original.get('date_ts') or 0, sent.get('date_ts') or 0
    if not original_ts or not sent_ts or sent_ts <= original_ts:
        return False
    if correction and correction['label'] == '1' and (sent_ts <= correction['ts'] or sent.get('sent_observed_at', 0) <= correction['ts']):
        return False
    return True


def reconcile(mc, settings):
    if not settings.get('reply_tracking_enabled', True) or not _LOCK.acquire(blocking=False):
        return {'checked': 0, 'answered': 0}
    try:
        return _reconcile(mc, settings)
    finally:
        _LOCK.release()


def _reconcile(mc, settings):
    import engine
    scan_sent(mc, settings)
    if not settings.get('llm_suggest'):
        return {'checked': 0, 'answered': 0}
    pending = store.outstanding_replies()
    by_id = {}
    for original in pending:
        for mid in message_ids(original.get('msgid')):
            by_id.setdefault(mid, []).append(original)
    budget = max(0, min(3, int(settings.get('max_llm_per_hour') or 0) - store.llm_count_last_hour()))
    checked = answered = 0
    owners = identities(settings)
    # Keep evidence independent of the indexer's folder progress and classification timing.
    for sent in store.sent_reply_evidence(time.time() - 30 * 86400):
        links = set(message_ids(sent['headers'].get('in_reply_to')) + message_ids(sent['headers'].get('references')))
        for mid in sorted(links):
            for original in by_id.get(mid, []):
                if not store.get_setting('reply_tracking_enabled') or not store.get_setting('llm_suggest'):
                    return {'checked': checked, 'answered': answered}
                if not store.get_message(original['id']).get('llm_needs_reply'):
                    continue
                revision = store.reply_correction(original['id'])
                original['headers'] = store.thread_headers(original['id'])
                if not original['headers'] or 'reply_to' not in original['headers']:
                    meta = _verified_payload(mc, original, headers_only=True)['meta']
                    store.capture_thread_headers(original['id'], meta)
                    original['headers'] = store.thread_headers(original['id'])
                if not _matches(original, sent, owners, revision):
                    continue
                previous = next((s for s in store.reply_checks(original['id']) if s.get('sent_id') == sent['id']), None)
                same_send = previous and previous.get('sent_at') == sent.get('date_ts')
                if same_send and previous['state'] != 'matched' and (not previous.get('retryable') or time.time() - previous['checked_at'] < 900):
                    continue
                outcome = {'state': 'uncertain', 'sent_id': sent['id'], 'sent_msgid': sent['msgid'],
                           'match': 'In-Reply-To' if mid in message_ids(sent['headers'].get('in_reply_to')) else 'References',
                           'confidence': 0, 'reason': '', 'retryable': False,
                           'prior_needs_reply': True, 'sent_at': sent.get('date_ts')}
                if budget <= checked:
                    if not same_send:
                        outcome.update(state='matched', reason='Sent reply matched. Waiting for the automatic-check budget to assess completeness.')
                        store.apply_reply_check(original['id'], outcome, revision)
                    continue
                checked += 1
                called_model = False
                try:
                    original_text, sent_text = _read_authored(mc, original), _read_authored(mc, sent)
                    called_model = True
                    verdict = engine.LLMClient().assess_reply(original, sent, original_text, sent_text)
                    outcome.update(validate_verdict(verdict))
                    outcome['model'] = str(verdict.get('_model') or 'configured classifier')[:120]
                    outcome['evidence_hash'] = hashlib.sha256((original_text + '\0' + sent_text).encode()).hexdigest()
                    store.add_llm_log(0, True)
                except Exception as exc:
                    # Separate accounting from classification failures: never park the original.
                    if called_model:
                        store.add_llm_log(0, False, 'reply check: ' + str(exc)[:160])
                    outcome.update(reason='Could not confirm this reply: ' + str(exc)[:300], retryable=True)
                if store.apply_reply_check(original['id'], outcome, revision) and outcome['state'] == 'answered':
                    answered += 1
                    store.record_observation(original['id'], 'reply', str(sent['id']), source='sent_mail')
                    store.log_event('info', 'Reply check: message #%s answered by sent message #%s' % (original['id'], sent['id']))
    return {'checked': checked, 'answered': answered}


def view_state(message):
    checks = store.reply_checks(message['id'])
    if not checks:
        return None
    state = dict(checks[0])
    correction = store.reply_correction(message['id'])
    if correction and correction['ts'] > state['checked_at']:
        state.update(state='reopened' if correction['label'] == '1' else 'cleared',
                     reason='Marked as needing a reply by you.' if correction['label'] == '1' else 'Cleared by you.')
    state['sent'] = store.get_message(state['sent_id'])
    return state

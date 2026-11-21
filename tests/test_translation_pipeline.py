import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

import cn_core as core


class PipelineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg.update(threads=1, batch_size=1, context=True, api_key='offline-test')
        self.stop = threading.Event()
        self.logs = []
        self.progress = []
        self.texts = ['Line %d' % i for i in range(12)]
        self.speakers = {}
        for obj, name, value in (
            (core, 'DATA_DIR', str(self.data)),
            (core, 'GAMES_PATH', str(self.root / 'games.json')),
            (core, 'get_text_info', lambda *a, **k: {
                'texts': self.texts, 'names': [], 'speakers': self.speakers, 'source_language': 'en'}),
        ):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(core.PAUSE.clear)

    def translate(self):
        return core.translate_game(self.cfg, str(self.root), core.Usage(), self.stop,
                                   self.logs.append, lambda a, b: self.progress.append((a, b)))

    def test_queue_is_bounded_and_context_uses_saved_results(self):
        self.cfg['threads'] = 2
        waited_sizes = []
        contexts = {}
        real_wait = core.wait

        def wait(futures, **kwargs):
            waited_sizes.append(len(futures))
            return real_wait(futures, **kwargs)

        def batch(cfg, sp, texts, usage, stop, ctx, speakers, after=None):
            contexts[texts[0]] = ctx
            return {t: '译文' + t[-1] for t in texts}

        with patch.object(core, 'wait', side_effect=wait), \
                patch.object(core, 'translate_batch', side_effect=batch) as api:
            result = self.translate()
        self.assertLessEqual(max(waited_sizes), 2)
        self.assertEqual(api.call_count, len(self.texts))
        self.assertEqual(result[:3], (12, 12, 0))
        self.assertTrue(any('→  译文' in line for line in contexts['Line 2']))
        self.assertEqual(len(core.load_translations(str(self.root))), 12)
        self.assertEqual(core.busy_texts(str(self.root)), 0)

    def test_completed_translations_are_not_sent_again(self):
        def batch(cfg, sp, texts, *args):
            return {t: '译文' for t in texts}
        with patch.object(core, 'translate_batch', side_effect=batch) as api:
            self.translate()
            self.translate()
        self.assertEqual(api.call_count, len(self.texts))

    def test_partial_reply_retries_only_missing_text(self):
        self.texts = ['Hello', 'Goodbye', 'Welcome']
        self.cfg.update(batch_size=3, context=False)
        calls = []

        def batch(cfg, sp, texts, *args):
            calls.append(list(texts))
            if len(calls) == 1:
                return {'Hello': '你好', 'Welcome': '欢迎'}
            return {'Goodbye': '再见'}

        with patch.object(core, 'translate_batch', side_effect=batch):
            self.assertEqual(self.translate()[:3], (3, 3, 0))
        self.assertEqual(calls, [self.texts, ['Goodbye']])

    def test_payload_limit_is_retried_with_smaller_batches(self):
        self.cfg.update(batch_size=20, context=False)
        sizes = []

        def api(cfg, messages, usage):
            payload = json.loads(messages[1]['content'])
            sizes.append(len(payload))
            if len(payload) > 5:
                raise urllib.error.HTTPError('https://example.invalid', 413, 'too large', {},
                                             io.BytesIO(b'request too large'))
            return json.dumps({k: '译文' for k in payload})

        with patch.object(core, 'call_api', side_effect=api):
            self.assertEqual(self.translate()[:3], (12, 12, 0))
        self.assertEqual(sizes, [12, 5, 5, 2])

    def test_write_failure_stashes_result_without_submitting_more(self):
        def batch(cfg, sp, texts, *args):
            return {t: '已付费的结果' for t in texts}

        with patch.object(core, 'translate_batch', side_effect=batch) as api, \
                patch.object(core, '_write_records', side_effect=OSError('disk full')), \
                patch.object(core.time, 'sleep'):
            with self.assertRaises(core.SaveError):
                self.translate()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(core.busy_texts(str(self.root)), 0)
        try:
            saved = core.UNSAVED[core.game_id(str(self.root))]['zh']
            self.assertEqual(saved, {'Line 0': '已付费的结果'})
            self.assertEqual(core.flush_unsaved(), 0)
            self.assertEqual(core.load_translations(str(self.root)), saved)
        finally:
            core.UNSAVED.pop(core.game_id(str(self.root)), None)

    def test_fatal_error_does_not_start_the_rest_of_the_game(self):
        with patch.object(core, 'translate_batch', side_effect=core.ApiRequestError('HTTP 405')) as api:
            with self.assertRaises(core.ApiRequestError):
                self.translate()
        self.assertEqual(api.call_count, 1)
        self.assertTrue(self.stop.is_set())
        self.assertEqual(core.busy_texts(str(self.root)), 0)

    def test_stop_keeps_already_completed_result_without_submitting_more(self):
        def batch(cfg, sp, texts, *args):
            return {t: '已完成' for t in texts}

        def progress(done, total):
            if done:
                self.stop.set()

        with patch.object(core, 'translate_batch', side_effect=batch) as api:
            core.translate_game(self.cfg, str(self.root), core.Usage(), self.stop,
                                self.logs.append, progress)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(core.load_translations(str(self.root)), {'Line 0': '已完成'})
        self.assertEqual(core.busy_texts(str(self.root)), 0)

    def test_late_response_is_saved_after_stop(self):
        entered, release, stored = threading.Event(), threading.Event(), threading.Event()
        errors = []
        real_write = core._write_records

        def batch(cfg, sp, texts, *args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release API')
            return {t: '迟到的结果' for t in texts}

        def write(*args):
            real_write(*args)
            stored.set()

        def run():
            try:
                self.translate()
            except Exception as e:
                errors.append(e)

        with patch.object(core, 'translate_batch', side_effect=batch), \
                patch.object(core, '_write_records', side_effect=write):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.stop.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], core.StopRequested)
                self.assertEqual(core.busy_texts(str(self.root)), 1)
            finally:
                release.set()
                worker.join(5)
                # shutdown(wait=False) retains the API future; wait for its persistence callback.
                self.assertTrue(stored.wait(5))
        self.assertEqual(core.load_translations(str(self.root)), {'Line 0': '迟到的结果'})

    def test_copied_source_reply_is_rejected_and_retried(self):
        self.texts = ['I really need to go now.']
        self.cfg.update(context=False)
        replies = iter(['I really need to go now.', '我真的得走了。'])

        def api(cfg, messages, usage):
            payload = json.loads(messages[1]['content'])
            return json.dumps({k: next(replies) for k in payload})

        with patch.object(core, 'call_api', side_effect=api) as call:
            self.assertEqual(self.translate()[:3], (1, 1, 0))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(core.load_translations(str(self.root)), {self.texts[0]: '我真的得走了。'})

    def test_context_skips_ui_text_and_adds_following_lines(self):
        self.texts = ['Hi there.', 'Settings', 'Bye now.', 'Target line.', 'Next line.', 'Quit', 'Last line.']
        self.speakers = {t: 'Amy' for t in self.texts if t not in ('Settings', 'Quit')}
        seen = {}

        def batch(cfg, sp, texts, usage, stop, ctx, speakers, after=None):
            seen[texts[0]] = (ctx, after)
            return {t: '译文' for t in texts}

        with patch.object(core, 'translate_batch', side_effect=batch):
            self.translate()
        ctx, after = seen['Target line.']
        self.assertEqual(ctx, ['Amy：Hi there.  →  译文', 'Amy：Bye now.  →  译文'])
        self.assertEqual(after, ['Amy：Next line.', 'Amy：Last line.'])

    def test_game_list_survives_failed_atomic_replace(self):
        core.save_games(['original'])
        with patch.object(core.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                core.save_games(['new'])
        self.assertEqual(core.load_games(), ['original'])
        self.assertEqual(list(self.root.glob('games.json.*.tmp')), [])


class QualityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        p = patch.object(core, 'DATA_DIR', str(self.root / 'data'))
        p.start()
        self.addCleanup(p.stop)
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg['api_key'] = 'offline-test'
        self.stop = threading.Event()

    def test_untranslated_detection(self):
        self.assertTrue(core.looks_untranslated('I really need to go.', 'I really need to go.'))
        self.assertTrue(core.looks_untranslated('おはよう、先輩', 'おはよう，学长'))
        self.assertTrue(core.looks_untranslated('안녕하세요', '안녕하세요'))
        self.assertFalse(core.looks_untranslated('Steam', 'Steam'))
        self.assertFalse(core.looks_untranslated('Hello [mc]!', '你好，[mc]！'))
        self.assertFalse(core.looks_untranslated('John Smith', '约翰・史密斯'))
        self.assertFalse(core.looks_untranslated('bg hall empty', 'bg hall empty'))
        self.assertFalse(core.looks_untranslated('Bite Your Tongue 2026', 'Bite Your Tongue 2026'))
        shader = 'uniform sampler2D tex0; uniform float u_bend;'
        self.assertFalse(core.looks_untranslated(shader, shader))
        self.assertFalse(core.is_done('I really need to go.', {'I really need to go.': 'I really need to go.'}))

    def test_batch_drops_copied_lines_and_sends_following_context(self):
        sent = []

        def api(cfg, messages, usage, stop):
            sent.append(messages[1]['content'])
            return json.dumps({'1': 'Where are you going now?', '2': '你好'})

        with patch.object(core, 'call_with_retry', side_effect=api):
            out = core.translate_batch(self.cfg, 'sp', ['Where are you going now?', 'Hello'], core.Usage(),
                                       self.stop, ['Amy：Hi'], None, ['Bob：Later'])
        self.assertEqual(out, {'Hello': '你好'})
        self.assertIn('后文', sent[0])
        self.assertIn('Bob：Later', sent[0])

    def test_menu_choices_are_marked(self):
        Menu = core._fake_class('renpy.ast', 'Menu')
        node = Menu()
        node.items = [('Go left', 'True', None), ('Stay here', 'True', None)]
        with patch.object(core, 'script_sources', return_value=[('test.rpyc', lambda: b'')]), \
                patch.object(core, 'rpyc_statements', return_value=[node]):
            info = core.extract_texts(str(self.root))
        self.assertEqual(info['texts'], ['Go left', 'Stay here'])
        self.assertEqual(info['speakers']['Go left'], core.MENU_SPEAKER)
        self.assertIn(core.MENU_SPEAKER, core.system_prompt(self.cfg))

    def test_recurring_proper_nouns_are_found(self):
        texts = ['We met at Ravenwood today.', 'I went to Ravenwood again.', 'Back at Ravenwood, it rained.',
                 'and Jake ran', 'so Jake ran', 'then Jake ran',
                 'then Hope said', 'and Hope said', 'so Hope said', 'I hope so.',
                 'see you on Monday', 'later on Monday', 'early on Monday',
                 'made with Ren\'Py', 'built in Ren\'Py', 'runs on Ren\'Py']
        terms = core.find_terms(texts, names=['Jake'])
        self.assertEqual(terms, ['Ravenwood'])

    def test_glossary_terms_saved_and_rejected_terms_not_asked_again(self):
        def api(cfg, messages, usage, stop):
            self.assertIn('候选词', messages[1]['content'])
            return json.dumps({'Jake': '杰克', 'Ravenwood': '渡鸦林', 'Wow': None})

        with patch.object(core, 'call_with_retry', side_effect=api) as call:
            gl = core.ensure_glossary(self.cfg, str(self.root), ['Jake'], core.Usage(), self.stop,
                                      lambda m: None, terms=['Ravenwood', 'Wow'])
            self.assertEqual(gl, {'Jake': '杰克', 'Ravenwood': '渡鸦林'})
            core.ensure_glossary(self.cfg, str(self.root), ['Jake'], core.Usage(), self.stop,
                                 lambda m: None, terms=['Ravenwood', 'Wow'])
        self.assertEqual(call.call_count, 1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg['api_key'] = 'offline-test'
        self.usage = core.Usage()
        self.stop = threading.Event()

    def http_error(self, code, detail, headers=None):
        return urllib.error.HTTPError('https://example.invalid', code, 'test', headers or {},
                                      io.BytesIO(detail.encode('utf-8')))

    def call(self):
        return core.call_with_retry(self.cfg, [], self.usage, self.stop)

    def test_permanent_http_errors_are_not_retried(self):
        for code, detail in ((400, 'unsupported temperature'), (405, 'method not allowed'),
                             (422, 'invalid request parameter')):
            with self.subTest(code=code), patch.object(core, 'call_api',
                    side_effect=self.http_error(code, detail)) as api:
                with self.assertRaises(core.ApiRequestError):
                    self.call()
                self.assertEqual(api.call_count, 1)

    def test_payload_limit_can_still_be_split_into_smaller_batches(self):
        for code, detail in ((400, 'context length exceeded'), (413, 'too large'),
                             (422, 'too many tokens')):
            with self.subTest(code=code), patch.object(core, 'call_api',
                    side_effect=self.http_error(code, detail)) as api:
                with self.assertRaises(RuntimeError) as exc:
                    self.call()
                self.assertNotIsInstance(exc.exception, core.ApiRequestError)
                self.assertEqual(api.call_count, 1)

    def test_transient_http_errors_still_retry(self):
        for code in (429, 503):
            with self.subTest(code=code), patch.object(core, 'call_api', side_effect=[
                    self.http_error(code, 'try again', {'Retry-After': '1'}), '成功']) as api, \
                    patch.object(core.time, 'sleep'):
                self.assertEqual(self.call(), '成功')
                self.assertEqual(api.call_count, 2)

    def test_unsupported_json_mode_falls_back(self):
        with patch.object(core, 'call_api', side_effect=[
                self.http_error(400, 'response_format not supported'), '成功']) as api:
            self.assertEqual(self.call(), '成功')
        self.assertFalse(self.cfg['json_mode'])
        self.assertEqual(api.call_count, 2)

    def test_invalid_json_response_is_not_sent_repeatedly(self):
        for raw in (b'<html>maintenance</html>', b'\xff\xfe'):
            with self.subTest(raw=raw):
                response = io.BytesIO(raw)
                with patch.object(core.urllib.request, 'urlopen', return_value=response) as api:
                    with self.assertRaises(core.ApiResponseError):
                        self.call()
                    self.assertEqual(api.call_count, 1)

    def test_valid_api_response_records_usage(self):
        raw = json.dumps({'choices': [{'message': {'content': '你好'}}],
                          'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}).encode()
        with patch.object(core.urllib.request, 'urlopen', return_value=io.BytesIO(raw)):
            self.assertEqual(self.call(), '你好')
        self.assertEqual((self.usage.hit, self.usage.miss, self.usage.out), (0, 10, 2))


if __name__ == '__main__':
    unittest.main()

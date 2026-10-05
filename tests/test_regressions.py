import ast
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

import cn_core as core
import cn_gui
import cn_unity as unity


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        for obj, name, value in (
            (core, 'DATA_DIR', str(self.data)),
            (unity, 'CACHE_PATH', str(self.data / 'unity_cache.v2.zh.json')),
            (unity, 'META_PATH', str(self.data / 'unity_cache.v2.meta.json')),
            (unity, 'LEGACY_META_PATH', str(self.data / 'unity_cache.meta.json')),
        ):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = copy.deepcopy(core.DEFAULT_CONFIG)
        self.cfg['api_key'] = 'test-key-not-for-network'
        self.logs = []

    def game(self):
        root = self.root / 'UnityGame'
        (root / 'Test_Data' / 'Managed').mkdir(parents=True)
        (root / 'Test.exe').write_bytes(b'fixture')
        p = root / 'BepInEx' / 'core'
        p.mkdir(parents=True)
        (p / 'BepInEx.dll').write_bytes(b'existing loader')
        return str(root)

    def plugin_zip(self):
        path = self.root / 'plugin.zip'
        with zipfile.ZipFile(path, 'w') as z:
            for name in ('XUnity.AutoTranslator.Plugin.Core.dll', 'XUnity.AutoTranslator.Plugin.BepInEx.dll',
                         'Translators/CustomTranslate.dll', 'ExIni.dll'):
                z.writestr('BepInEx/plugins/XUnity.AutoTranslator/' + name, b'complete ' + name.encode())
        return str(path)

    def install(self, root):
        with patch.object(unity, 'download', return_value=self.plugin_zip()):
            return unity.install(root, self.cfg, self.logs.append, threading.Event())

    def set_config(self, root, raw):
        p = Path(unity.config_path(root))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        return p


class InstallTests(IsolatedTest):
    def test_existing_config_restored_byte_for_byte_after_repeated_install_and_launch(self):
        root = self.game()
        unity._extract_new(self.plugin_zip(), root, [])
        original = b'\xef\xbb\xbf[Service]\r\nEndpoint=GoogleTranslate\r\n[General]\r\nLanguage=ja\r\n'
        cp = self.set_config(root, original)
        self.install(root)
        self.cfg['unity_from_lang'] = 'ko'
        self.install(root)
        unity.write_config(root, self.cfg)
        self.assertNotEqual(cp.read_bytes(), original)
        self.assertEqual(unity.remove(root, self.logs.append), 'removed')
        self.assertEqual(cp.read_bytes(), original)
        self.assertTrue(unity.xunity_present(root))

    def test_new_config_removed_without_deleting_existing_loader(self):
        root = self.game()
        self.install(root)
        self.assertTrue(Path(unity.config_path(root)).is_file())
        self.assertEqual(unity.remove(root, self.logs.append), 'removed')
        self.assertFalse(Path(unity.config_path(root)).exists())
        self.assertTrue(unity.bepinex_present(root))
        self.assertFalse(unity.xunity_present(root))

    def test_restore_write_failure_preserves_backup_for_retry(self):
        root = self.game()
        original = b'[Service]\nEndpoint=GoogleTranslate\n'
        cp = self.set_config(root, original)
        self.install(root)
        with patch.object(unity, '_copy_atomic', side_effect=PermissionError('locked config')):
            self.assertEqual(unity.remove(root, self.logs.append), 'partial')
        man = core.read_json(core._data_path(root, 'unity_manifest'))
        self.assertEqual(base64.b64decode(man['config_before']), original)
        self.assertEqual(unity.remove(root, self.logs.append), 'removed')
        self.assertEqual(cp.read_bytes(), original)

    def test_failed_ini_update_keeps_backup_and_original(self):
        root = self.game()
        original = b'[Service]\nEndpoint=GoogleTranslate\n'
        cp = self.set_config(root, original)
        with patch.object(unity, '_write_ini', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.install(root)
        self.assertEqual(cp.read_bytes(), original)
        self.assertEqual(base64.b64decode(core.read_json(core._data_path(root, 'unity_manifest'))['config_before']), original)

    def test_partial_dll_not_published_or_detected_and_retry_succeeds(self):
        root = self.game()
        def fail(src, dst):
            dst.write(b'partial')
            raise OSError('disk full')
        with patch.object(unity.shutil, 'copyfileobj', side_effect=fail):
            with self.assertRaises(OSError):
                self.install(root)
        self.assertFalse(unity.xunity_present(root))
        self.assertEqual(list(Path(root).rglob('.cn-install-*')), [])
        self.assertEqual(list(Path(root).rglob('XUnity.AutoTranslator.Plugin.Core.dll')), [])
        self.install(root)
        self.assertTrue(unity.xunity_present(root))
        self.assertEqual(core.read_json(core._data_path(root, 'unity_manifest'))['pending_packages'], [])

    def test_retry_completes_dependency_after_core_dlls_were_written(self):
        root = self.game()
        copyfileobj = unity.shutil.copyfileobj
        calls = []
        def fail_fourth(src, dst):
            calls.append(1)
            if len(calls) == 4:
                raise OSError('dependency failed')
            copyfileobj(src, dst)
        with patch.object(unity.shutil, 'copyfileobj', side_effect=fail_fourth):
            with self.assertRaises(OSError):
                self.install(root)
        self.assertFalse(unity.xunity_present(root))
        self.assertIn('xunity', core.read_json(core._data_path(root, 'unity_manifest'))['pending_packages'])
        self.install(root)
        self.assertTrue((Path(root) / 'BepInEx/plugins/XUnity.AutoTranslator/ExIni.dll').is_file())
        self.assertEqual(core.read_json(core._data_path(root, 'unity_manifest'))['pending_packages'], [])

    def test_il2cpp_entrypoint_detected_separately_from_mono(self):
        root = self.game()
        (Path(root) / 'GameAssembly.dll').write_bytes(b'fixture')
        unity._extract_new(self.plugin_zip(), root, [])
        self.assertFalse(unity.xunity_present(root))
        plugin = Path(root) / 'BepInEx/plugins/XUnity.AutoTranslator'
        (plugin / 'XUnity.AutoTranslator.Plugin.BepInEx-IL2CPP.dll').write_bytes(b'IL2CPP entrypoint')
        self.assertTrue(unity.xunity_present(root))

    def test_archive_does_not_overwrite_user_files_or_escape_game(self):
        root = self.game()
        existing = Path(root) / 'existing.txt'
        existing.write_bytes(b'user data')
        zip_path = self.root / 'unsafe.zip'
        with zipfile.ZipFile(zip_path, 'w') as z:
            z.writestr('existing.txt', b'overwrite')
            z.writestr('../outside.txt', b'escape')
        created = []
        unity._extract_new(str(zip_path), root, created)
        self.assertEqual(existing.read_bytes(), b'user data')
        self.assertFalse((self.root / 'outside.txt').exists())
        self.assertEqual(created, [])


class CacheTests(IsolatedTest):
    def fake_api(self, cfg, *args, **kwargs):
        return '学习' if cfg['unity_from_lang'] == 'ja' else '勉强'

    def test_languages_have_independent_persisted_cache(self):
        svc = unity.TranslateService(self.cfg, self.logs.append)
        with patch.object(core, 'call_with_retry', side_effect=self.fake_api) as api:
            self.assertEqual(svc.translate('勉強', 'ja'), '学习')
            self.assertEqual(svc.translate('勉強', 'zh-TW'), '勉强')
            self.assertEqual(svc.translate('勉強', 'ja'), '学习')
            self.assertEqual(api.call_count, 2)
        self.assertTrue(svc.save())
        restored = unity.TranslateService(self.cfg, self.logs.append)
        with patch.object(core, 'call_with_retry', side_effect=AssertionError('cache miss')):
            self.assertEqual(restored.translate('勉強', 'ja'), '学习')
            self.assertEqual(restored.translate('勉強', 'zh-TW'), '勉强')

    def test_http_request_language_overrides_ui_language(self):
        svc = unity.TranslateService(self.cfg, self.logs.append)
        with patch.object(unity, 'PORT', 0):
            self.assertTrue(svc.start())
        self.addCleanup(svc.shutdown)
        url = 'http://127.0.0.1:%d/translate' % svc.httpd.server_address[1]
        with patch.object(core, 'call_with_retry', side_effect=self.fake_api):
            for lang, expected in (('ja', '学习'), ('zh-TW', '勉强')):
                q = unity.urllib.parse.urlencode({'from': lang, 'to': 'zh-CN', 'text': '勉強'})
                with urllib.request.urlopen(url + '?' + q, timeout=5) as response:
                    self.assertEqual(response.read().decode('utf-8'), expected)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(url + '?to=fr&text=Hello', timeout=5)

    def test_duplicate_requests_share_one_api_call(self):
        svc = unity.TranslateService(self.cfg, self.logs.append)
        entered, release = threading.Event(), threading.Event()
        def api(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return '学习'
        results = []
        with patch.object(core, 'call_with_retry', side_effect=api) as mocked:
            first = threading.Thread(target=lambda: results.append(svc.translate('勉強', 'ja')))
            second = threading.Thread(target=lambda: results.append(svc.translate('勉強', 'ja')))
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            release.set()
            first.join(5)
            second.join(5)
            self.assertEqual(results, ['学习', '学习'])
            self.assertEqual(mocked.call_count, 1)
        self.assertEqual(svc.busy(), 0)

    def test_legacy_cache_is_preserved_and_not_assigned_a_guessed_language(self):
        old = self.data / 'unity_cache.zh.json'
        raw = json.dumps({'勉強': '旧译文'}, ensure_ascii=False).encode('utf-8')
        old.write_bytes(raw)
        svc = unity.TranslateService(self.cfg, self.logs.append)
        with patch.object(core, 'call_with_retry', side_effect=self.fake_api):
            self.assertEqual(svc.translate('勉強', 'ja'), '学习')
        svc.save()
        self.assertEqual(old.read_bytes(), raw)

    def test_delete_and_model_counts_use_game_language(self):
        root = self.game()
        self.set_config(root, b'[General]\nFromLanguage=ja\n')
        svc = unity.TranslateService(self.cfg, self.logs.append)
        with patch.object(core, 'call_with_retry', side_effect=self.fake_api):
            svc.translate('勉強', 'ja')
            svc.translate('勉強', 'zh-TW')
        svc.save()
        game_cache = Path(unity.translation_file(root))
        game_cache.parent.mkdir(parents=True)
        game_cache.write_text('勉強=学习\n', encoding='utf-8')
        self.assertEqual(unity.model_counts(root)[self.cfg['model']], 1)
        self.assertEqual(svc.delete_translations(root, self.cfg['model']), 1)
        self.assertNotIn(unity.cache_key('勉強', 'ja'), svc.cache)
        self.assertEqual(svc.cache[unity.cache_key('勉強', 'zh-TW')], '勉强')
        self.assertEqual(game_cache.read_text(encoding='utf-8'), '')


class RenpyTests(IsolatedTest):
    def extract(self, lines, src_lang='auto', extractor=core.extract_texts):
        Say = core._fake_class('renpy.ast', 'Say')
        nodes = []
        for line in lines:
            node = Say()
            node.what, node.who = line, None
            nodes.append(node)
        with patch.object(core, 'script_sources', return_value=[('test.rpyc', lambda: b'')]), \
                patch.object(core, 'rpyc_statements', return_value=nodes):
            return extractor(str(self.root), src_lang=src_lang)

    def test_korean_and_japanese_kanji_are_extracted(self):
        lines = ['안녕하세요', '開始', 'こんにちは', 'Hello!']
        self.assertEqual(self.extract(lines)['texts'], lines)
        self.assertEqual(self.extract(['開始'], 'ja')['texts'], ['開始'])

    def test_chinese_is_not_automatically_misclassified_as_japanese(self):
        self.assertEqual(self.extract(['开始', '你好'])['texts'], [])
        self.assertEqual(self.extract(['開始'], 'zh-TW')['texts'], ['開始'])

    def test_extraction_cache_changes_with_language_setting(self):
        with patch.object(core, 'sources_signature', return_value='fixture'), \
                patch.object(core, 'extract_texts', side_effect=lambda r, log=None, src_lang='auto': self.extract(['開始'], src_lang)) as extract:
            self.assertEqual(core.get_texts(str(self.root)), [])
            self.assertEqual(core.get_texts(str(self.root), src_lang='ja'), ['開始'])
            self.assertEqual(core.get_texts(str(self.root), src_lang='ja'), ['開始'])
            self.assertEqual(extract.call_count, 2)

    def test_language_instructions_override_keep_chinese_rule(self):
        self.cfg['renpy_from_lang'] = 'ja'
        self.assertIn('纯汉字的日文也必须翻成简体中文', core.system_prompt(self.cfg))
        self.cfg['renpy_from_lang'] = 'zh-TW'
        self.assertIn('必须转成简体中文', core.system_prompt(self.cfg))

    def test_selected_game_root_and_child_are_visible(self):
        module = ast.parse(Path(cn_gui.__file__).read_text(encoding='utf-8'))
        gui = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'run_gui')
        funcs = [n for n in gui.body if isinstance(n, ast.FunctionDef) and n.name in ('_key', '_under')]
        ns = {'os': os}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), 'gui-filter', 'exec'), ns)
        root = str(self.root / 'Novel')
        (Path(root) / 'game').mkdir(parents=True)
        (Path(root) / 'renpy').mkdir()
        self.assertEqual([g for g in core.find_games(root) if ns['_under'](g, root)], [root])
        self.assertTrue(ns['_under'](os.path.join(root, 'child'), root))
        self.assertFalse(ns['_under'](root + '-sibling', root))


class FontTests(IsolatedTest):
    def test_existing_tmp_fallback_is_preserved_without_bundle(self):
        root = self.game()
        self.set_config(root, b'[Behaviour]\nFallbackFontTextMeshPro=Fonts/ExistingChinese\n')
        unity.write_config(root, self.cfg)
        self.assertIn('FallbackFontTextMeshPro=Fonts/ExistingChinese', Path(unity.config_path(root)).read_text())

    def test_bundle_installed_cancelled_and_removed_with_config_restored(self):
        root = self.game()
        original = b'[Behaviour]\nFallbackFontTextMeshPro=Fonts/ExistingChinese\n'
        cp = self.set_config(root, original)
        bundle = self.root / 'chinese.bundle'
        bundle.write_bytes(b'UnityFS\x00font fixture')
        self.cfg['unity_tmp_font'] = str(bundle)
        unity.write_config(root, self.cfg)
        man = core.read_json(core._data_path(root, 'unity_manifest'))
        self.assertEqual(len(man['created']), 1)
        rel = man['created'][0]
        self.assertEqual((Path(root) / rel).read_bytes(), bundle.read_bytes())
        self.assertIn('FallbackFontTextMeshPro=' + rel, cp.read_text())
        unity.write_config(root, self.cfg)
        self.assertEqual(len(core.read_json(core._data_path(root, 'unity_manifest'))['created']), 1)
        self.cfg.pop('unity_tmp_font')
        unity.write_config(root, self.cfg)
        self.assertIn('FallbackFontTextMeshPro=Fonts/ExistingChinese', cp.read_text())
        self.assertEqual(unity.remove(root, self.logs.append), 'removed')
        self.assertFalse((Path(root) / rel).exists())
        self.assertEqual(cp.read_bytes(), original)

    def test_ttf_rejected_without_modifying_game_config(self):
        root = self.game()
        original = b'[Behaviour]\nFallbackFontTextMeshPro=valid-resource\n'
        cp = self.set_config(root, original)
        ttf = self.root / 'font.ttf'
        ttf.write_bytes(b'\x00\x01\x00\x00not an asset bundle')
        self.cfg['unity_tmp_font'] = str(ttf)
        with self.assertRaises(RuntimeError):
            unity.write_config(root, self.cfg)
        self.assertEqual(cp.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()

import os
import unittest
from unittest.mock import MagicMock, patch

from dbt.adapters.postgres import Plugin as PostgresPlugin
from dbt.adapters.factory import reset_adapters
import dbt.clients.system
import dbt.compilation
import dbt.exceptions
import dbt.flags
import dbt.parser
import dbt.config
import dbt.utils
import dbt.parser.manifest
from dbt.contracts.files import SourceFile, FileHash, FilePath
from dbt.contracts.graph.manifest import Manifest, MacroManifest, ManifestStateCheck
from dbt.parser.base import BaseParser
from dbt.graph import NodeSelector, parse_difference

try:
    from queue import Empty
except ImportError:
    from Queue import Empty

from dbt.logger import GLOBAL_LOGGER as logger # noqa

from .utils import config_from_parts_or_dicts, generate_name_macros, inject_plugin


class GraphTest(unittest.TestCase):

    def tearDown(self):
        self.write_gpickle_patcher.stop()
        self.load_projects_patcher.stop()
        self.file_system_patcher.stop()
        self.get_adapter_patcher.stop()
        self.get_adapter_patcher_parser.stop()
        self.mock_filesystem_constructor.stop()
        self.mock_hook_constructor.stop()
        self.load_state_check.stop()
        self.load_source_file_patcher.stop()
        reset_adapters()

    def setUp(self):
        # create various attributes
        dbt.flags.STRICT_MODE = True
        self.graph_result = None
        self.profile = {
            'outputs': {
                'test': {
                    'type': 'postgres',
                    'threads': 4,
                    'host': 'thishostshouldnotexist',
                    'port': 5432,
                    'user': 'root',
                    'pass': 'password',
                    'dbname': 'dbt',
                    'schema': 'dbt_test'
                }
            },
            'target': 'test'
        }
        self.macro_manifest = MacroManifest(
            {n.unique_id: n for n in generate_name_macros('test_models_compile')}, {})
        self.mock_models = []  # used by filesystem_searcher

        # Create gpickle patcher
        self.write_gpickle_patcher = patch('networkx.write_gpickle')
        def mock_write_gpickle(graph, outfile):
            self.graph_result = graph
        self.mock_write_gpickle = self.write_gpickle_patcher.start()
        self.mock_write_gpickle.side_effect = mock_write_gpickle

        # Create file system patcher and filesystem searcher
        self.file_system_patcher = patch.object(
            dbt.parser.search.FilesystemSearcher, '__new__'
        )
        self.mock_filesystem_constructor = self.file_system_patcher.start()
        def filesystem_iter(iter_self):
            if 'sql' not in iter_self.extension:
                return []
            if 'models' not in iter_self.relative_dirs:
                return []
            return [model.path for model in self.mock_models]
        def create_filesystem_searcher(cls, project, relative_dirs, extension):
            result = MagicMock(project=project, relative_dirs=relative_dirs, extension=extension)
            result.__iter__.side_effect = lambda: iter(filesystem_iter(result))
            return result
        self.mock_filesystem_constructor.side_effect = create_filesystem_searcher

        # Create HookParser patcher
        self.hook_patcher = patch.object(
            dbt.parser.hooks.HookParser, '__new__'
        )
        def create_hook_patcher(cls, project, manifest, root_project):
            result = MagicMock(project=project, manifest=manifest, root_project=root_project)
            result.__iter__.side_effect = lambda: iter([])
            return result
        self.mock_hook_constructor = self.hook_patcher.start()
        self.mock_hook_constructor.side_effect = create_hook_patcher

        # Create get_adapter patcher
        self.get_adapter_patcher = patch('dbt.context.providers.get_adapter')
        self.factory = self.get_adapter_patcher.start()
        # Also patch the base class
        self.get_adapter_patcher_parser = patch('dbt.parser.base.get_adapter')
        self.factory_cmn = self.get_adapter_patcher_parser.start()
        inject_plugin(PostgresPlugin)

        # Create load_projects patcher
        self.load_projects_patcher = patch('dbt.parser.manifest._load_projects')
        self.mock_load_projects = self.load_projects_patcher.start()
        def _load_projects(config, paths):
            yield config.project_name, config
        self.mock_load_projects.side_effect = _load_projects

        # Create the Manifest.state_check patcher
        @patch('dbt.parser.manifest.ManifestLoader.build_manifest_state_check')
        def _mock_state_check(self):
            config = self.root_project
            all_projects = self.all_projects
            return ManifestStateCheck(
                vars_hash=FileHash.from_contents('vars'),
                project_hashes={name: FileHash.from_contents(name) for name in all_projects},
                profile_hash=FileHash.from_contents('profile'),
            )
        self.load_state_check = patch('dbt.parser.manifest.ManifestLoader.build_manifest_state_check')
        self.mock_state_check = self.load_state_check.start()
        self.mock_state_check.side_effect = _mock_state_check

        # Create the source file patcher
        self.load_source_file_patcher = patch.object(BaseParser, 'load_file')
        self.mock_source_file = self.load_source_file_patcher.start()
        self.mock_source_file.side_effect = lambda path: [n for n in self.mock_models if n.path == path][0]

    def get_config(self, extra_cfg=None):
        if extra_cfg is None:
            extra_cfg = {}

        cfg = {
            'name': 'test_models_compile',
            'version': '0.1',
            'profile': 'test',
            'project-root': os.path.abspath('.'),
            'config-version': 2,
        }
        cfg.update(extra_cfg)

        return config_from_parts_or_dicts(project=cfg, profile=self.profile)

    def get_compiler(self, project):
        return dbt.compilation.Compiler(project)

    def use_models(self, models):
        for k, v in models.items():
            path = FilePath(
                searched_path='models',
                project_root=os.path.normcase(os.getcwd()),
                relative_path='{}.sql'.format(k),
            )
            source_file = SourceFile(path=path, checksum=FileHash.empty())
            source_file.contents = v
            self.mock_models.append(source_file)

    def load_manifest(self, config):
        loader = dbt.parser.manifest.ManifestLoader(config, {config.project_name: config})
        loader.manifest.macros = self.macro_manifest.macros
        loader.load()
        loader.update_manifest()
        return loader.manifest

    def test__single_model(self):
        self.use_models({
            'model_one': 'select * from events',
        })

        config = self.get_config()
        manifest = self.load_manifest(config)

        compiler = self.get_compiler(config)
        linker = compiler.compile(manifest)

        self.assertEqual(
            list(linker.nodes()),
            ['model.test_models_compile.model_one'])

        self.assertEqual(
            list(linker.edges()),
            [])

    def test__two_models_simple_ref(self):
        self.use_models({
            'model_one': 'select * from events',
            'model_two': "select * from {{ref('model_one')}}",
        })

        config = self.get_config()
        manifest = self.load_manifest(config)
        compiler = self.get_compiler(config)
        linker = compiler.compile(manifest)

        self.assertCountEqual(
            linker.nodes(),
            [
                'model.test_models_compile.model_one',
                'model.test_models_compile.model_two',
            ]
        )

        self.assertCountEqual(
            linker.edges(),
            [('model.test_models_compile.model_one', 'model.test_models_compile.model_two',)]
        )

    def test__model_materializations(self):
        self.use_models({
            'model_one': 'select * from events',
            'model_two': "select * from {{ref('model_one')}}",
            'model_three': "select * from events",
            'model_four': "select * from events",
        })

        cfg = {
            "models": {
                "materialized": "table",
                "test_models_compile": {
                    "model_one": {"materialized": "table"},
                    "model_two": {"materialized": "view"},
                    "model_three": {"materialized": "ephemeral"}
                }
            }
        }

        config = self.get_config(cfg)
        manifest = self.load_manifest(config)
        compiler = self.get_compiler(config)
        linker = compiler.compile(manifest)

        expected_materialization = {
            "model_one": "table",
            "model_two": "view",
            "model_three": "ephemeral",
            "model_four": "table"
        }

        for model, expected in expected_materialization.items():
            key = 'model.test_models_compile.{}'.format(model)
            actual = manifest.nodes[key].config.materialized
            self.assertEqual(actual, expected)

    def test__model_incremental(self):
        self.use_models({
            'model_one': 'select * from events'
        })

        cfg = {
            "models": {
                "test_models_compile": {
                    "model_one": {
                        "materialized": "incremental",
                        "unique_key": "id"
                    },
                }
            }
        }

        config = self.get_config(cfg)
        manifest = self.load_manifest(config)
        compiler = self.get_compiler(config)
        linker = compiler.compile(manifest)

        node = 'model.test_models_compile.model_one'

        self.assertEqual(list(linker.nodes()), [node])
        self.assertEqual(list(linker.edges()), [])

        self.assertEqual(manifest.nodes[node].config.materialized, 'incremental')

    def test__dependency_list(self):
        self.use_models({
            'model_1': 'select * from events',
            'model_2': 'select * from {{ ref("model_1") }}',
            'model_3': '''
                select * from {{ ref("model_1") }}
                union all
                select * from {{ ref("model_2") }}
            ''',
            'model_4': 'select * from {{ ref("model_3") }}'
        })

        config = self.get_config()
        manifest = self.load_manifest(config)
        compiler = self.get_compiler(config)
        graph = compiler.compile(manifest)

        models = ('model_1', 'model_2', 'model_3', 'model_4')
        model_ids = ['model.test_models_compile.{}'.format(m) for m in models]

        manifest = MagicMock(nodes={
            n: MagicMock(
                unique_id=n,
                name=n.split('.')[-1],
                package_name='test_models_compile',
                fqn=['test_models_compile', n],
                empty=False,
                config=MagicMock(enabled=True),
            )
            for n in model_ids
        })
        manifest.expect.side_effect = lambda n: MagicMock(unique_id=n)
        selector = NodeSelector(graph, manifest)
        queue = selector.get_graph_queue(parse_difference(None, None))

        for model_id in model_ids:
            self.assertFalse(queue.empty())
            got = queue.get(block=False)
            self.assertEqual(got.unique_id, model_id)
            with self.assertRaises(Empty):
                queue.get(block=False)
            queue.mark_done(got.unique_id)
        self.assertTrue(queue.empty())

    def test__partial_parse(self):
        config = self.get_config()

        manifest = self.load_manifest(config)

        # we need a loader to compare the two manifests
        loader = dbt.parser.manifest.ManifestLoader(config, {config.project_name: config})
        loader.manifest.macros = self.macro_manifest.macros
        loader.load()
        loader.update_manifest()

        self.assertTrue(loader.matching_parse_results(manifest))
        manifest.metadata.dbt_version = '0.0.1a1'
        self.assertFalse(loader.matching_parse_results(manifest))
        manifest.metadata.dbt_version = '99999.99.99'
        self.assertFalse(loader.matching_parse_results(manifest))

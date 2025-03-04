from dataclasses import dataclass
from dataclasses import field
import os
import pickle
from typing import (
    Dict, Optional, Mapping, Callable, Any, List, Type, Union
)
import time

import dbt.exceptions
import dbt.tracking
import dbt.flags as flags

from dbt.adapters.factory import (
    get_adapter,
    get_relation_class_by_name,
)
from dbt.helper_types import PathSet
from dbt.logger import GLOBAL_LOGGER as logger, DbtProcessState
from dbt.node_types import NodeType
from dbt.clients.jinja import get_rendered
from dbt.clients.system import make_directory
from dbt.config import Project, RuntimeConfig
from dbt.context.docs import generate_runtime_docs
from dbt.contracts.files import FilePath, FileHash
from dbt.contracts.graph.compiled import ManifestNode
from dbt.contracts.graph.manifest import (
    Manifest, Disabled, MacroManifest, ManifestStateCheck
)
from dbt.contracts.graph.parsed import (
    ParsedSourceDefinition, ParsedNode, ParsedMacro, ColumnInfo, ParsedExposure
)
from dbt.contracts.util import Writable
from dbt.exceptions import (
    ref_target_not_found,
    get_target_not_found_or_disabled_msg,
    source_target_not_found,
    get_source_not_found_or_disabled_msg,
    warn_or_error,
)
from dbt.parser.base import BaseParser, Parser
from dbt.parser.analysis import AnalysisParser
from dbt.parser.data_test import DataTestParser
from dbt.parser.docs import DocumentationParser
from dbt.parser.hooks import HookParser
from dbt.parser.macros import MacroParser
from dbt.parser.models import ModelParser
from dbt.parser.schemas import SchemaParser
from dbt.parser.search import FileBlock
from dbt.parser.seeds import SeedParser
from dbt.parser.snapshots import SnapshotParser
from dbt.parser.sources import patch_sources
from dbt.ui import warning_tag
from dbt.version import __version__

from dbt.dataclass_schema import dbtClassMixin

PARTIAL_PARSE_FILE_NAME = 'partial_parse.pickle'
PARSING_STATE = DbtProcessState('parsing')
DEFAULT_PARTIAL_PARSE = False


@dataclass
class ParserInfo(dbtClassMixin):
    parser: str
    elapsed: float
    path_count: int = 0


@dataclass
class ProjectLoaderInfo(dbtClassMixin):
    project_name: str
    elapsed: float
    parsers: List[ParserInfo]
    path_count: int = 0


@dataclass
class ManifestLoaderInfo(dbtClassMixin, Writable):
    path_count: int = 0
    is_partial_parse_enabled: Optional[bool] = None
    parse_project_elapsed: Optional[float] = None
    patch_sources_elapsed: Optional[float] = None
    process_manifest_elapsed: Optional[float] = None
    load_all_elapsed: Optional[float] = None
    projects: List[ProjectLoaderInfo] = field(default_factory=list)


_parser_types: List[Type[Parser]] = [
    ModelParser,
    SnapshotParser,
    AnalysisParser,
    DataTestParser,
    HookParser,
    SeedParser,
    DocumentationParser,
    SchemaParser,
]


class ManifestLoader:
    def __init__(
        self,
        root_project: RuntimeConfig,
        all_projects: Mapping[str, Project],
        macro_hook: Optional[Callable[[Manifest], Any]] = None,
    ) -> None:
        self.root_project: RuntimeConfig = root_project
        self.all_projects: Mapping[str, Project] = all_projects
        self.manifest: Manifest = Manifest({}, {}, {}, {}, {}, {}, [], {})
        self.manifest.metadata = root_project.get_metadata()
        self.macro_hook: Callable[[Manifest], Any]
        if macro_hook is None:
            self.macro_hook = lambda m: None
        else:
            self.macro_hook = macro_hook

        self._loaded_file_cache: Dict[str, FileBlock] = {}
        self._perf_info = ManifestLoaderInfo(
            is_partial_parse_enabled=self._partial_parse_enabled()
        )
        # Creating state_check must go before read_saved_manifest
        self.manifest.state_check = self.build_manifest_state_check()
        self.old_manifest: Optional[Manifest] = self.read_saved_manifest()

    def track_project_load(self):
        invocation_id = dbt.tracking.active_user.invocation_id
        dbt.tracking.track_project_load({
            "invocation_id": invocation_id,
            "project_id": self.root_project.hashed_name(),
            "path_count": self._perf_info.path_count,
            "parse_project_elapsed": self._perf_info.parse_project_elapsed,
            "patch_sources_elapsed": self._perf_info.patch_sources_elapsed,
            "process_manifest_elapsed": (
                self._perf_info.process_manifest_elapsed
            ),
            "load_all_elapsed": self._perf_info.load_all_elapsed,
            "is_partial_parse_enabled": (
                self._perf_info.is_partial_parse_enabled
            ),
        })

    # This is where we use the partial-parse state from the
    # pickle file (if it exists)
    def parse_with_cache(
        self,
        path: FilePath,
        parser: BaseParser,
    ) -> None:
        block = self._get_file(path, parser)
        # _get_cached actually copies the nodes, etc, that were
        # generated from the file to the results, in 'sanitized_update'
        if not self._get_cached(block, parser):
            parser.parse_file(block)

    # check if we have a stored parse file, then check if
    # file checksums are the same or not and either return
    # the old ... stuff or return false (not cached)
    def _get_cached(
        self,
        block: FileBlock,
        parser: BaseParser,
    ) -> bool:
        # TODO: handle multiple parsers w/ same files, by
        # tracking parser type vs node type? Or tracking actual
        # parser type during parsing?
        if self.old_manifest is None:
            return False
        # The 'has_file' method is where we check to see if
        # the checksum of the old file is the same as the new
        # file. If the checksum is different, 'has_file' returns
        # false. If it's the same, the file and the things that
        # were generated from it are used.
        if self.old_manifest.has_file(block.file):
            return self.manifest.sanitized_update(
                block.file, self.old_manifest, parser.resource_type
            )
        return False

    def _get_file(self, path: FilePath, parser: BaseParser) -> FileBlock:
        if path.search_key in self._loaded_file_cache:
            block = self._loaded_file_cache[path.search_key]
        else:
            block = FileBlock(file=parser.load_file(path))
            self._loaded_file_cache[path.search_key] = block
        return block

    def parse_project(
        self,
        project: Project,
    ) -> None:
        parsers: List[Parser] = []
        for cls in _parser_types:
            parser = cls(project, self.manifest, self.root_project)
            parsers.append(parser)

        # per-project cache.
        self._loaded_file_cache.clear()

        project_parser_info: List[ParserInfo] = []
        start_timer = time.perf_counter()
        total_path_count = 0
        for parser in parsers:
            parser_path_count = 0
            parser_start_timer = time.perf_counter()
            for path in parser.search():
                self.parse_with_cache(path, parser)
                parser_path_count = parser_path_count + 1

            if parser_path_count > 0:
                project_parser_info.append(ParserInfo(
                    parser=parser.resource_type,
                    path_count=parser_path_count,
                    elapsed=time.perf_counter() - parser_start_timer
                ))
                total_path_count = total_path_count + parser_path_count

        elapsed = time.perf_counter() - start_timer
        project_info = ProjectLoaderInfo(
            project_name=project.project_name,
            path_count=total_path_count,
            elapsed=elapsed,
            parsers=project_parser_info
        )
        self._perf_info.projects.append(project_info)
        self._perf_info.path_count = (
            self._perf_info.path_count + total_path_count
        )

    # This is where the main action happens
    def load(self):
        start_timer = time.perf_counter()

        if self.old_manifest is not None:
            logger.debug('Got an acceptable cached parse result')

        for project in self.all_projects.values():
            # parse a single project
            self.parse_project(project)

        self._perf_info.parse_project_elapsed = (
            time.perf_counter() - start_timer
        )

    def write_manifest_for_partial_parse(self):
        path = os.path.join(self.root_project.target_path,
                            PARTIAL_PARSE_FILE_NAME)
        make_directory(self.root_project.target_path)
        with open(path, 'wb') as fp:
            pickle.dump(self.manifest, fp)

    def matching_parse_results(self, manifest: Manifest) -> bool:
        """Compare the global hashes of the read-in parse results' values to
        the known ones, and return if it is ok to re-use the results.
        """
        try:
            if manifest.metadata.dbt_version != __version__:
                logger.debug(
                    'dbt version mismatch: {} != {}, cache invalidated'
                    .format(manifest.metadata.dbt_version, __version__)
                )
                return False
        except AttributeError as exc:
            logger.debug(f"malformed result file, cache invalidated: {exc}")
            return False

        valid = True

        if not self.manifest.state_check or not manifest.state_check:
            return False

        if self.manifest.state_check.vars_hash != manifest.state_check.vars_hash:
            logger.debug('vars hash mismatch, cache invalidated')
            valid = False
        if self.manifest.state_check.profile_hash != manifest.state_check.profile_hash:
            logger.debug('profile hash mismatch, cache invalidated')
            valid = False

        missing_keys = {
            k for k in self.manifest.state_check.project_hashes
            if k not in manifest.state_check.project_hashes
        }
        if missing_keys:
            logger.debug(
                'project hash mismatch: values missing, cache invalidated: {}'
                .format(missing_keys)
            )
            valid = False

        for key, new_value in self.manifest.state_check.project_hashes.items():
            if key in manifest.state_check.project_hashes:
                old_value = manifest.state_check.project_hashes[key]
                if new_value != old_value:
                    logger.debug(
                        'For key {}, hash mismatch ({} -> {}), cache '
                        'invalidated'
                        .format(key, old_value, new_value)
                    )
                    valid = False
        return valid

    def _partial_parse_enabled(self):
        # if the CLI is set, follow that
        if flags.PARTIAL_PARSE is not None:
            return flags.PARTIAL_PARSE
        # if the config is set, follow that
        elif self.root_project.config.partial_parse is not None:
            return self.root_project.config.partial_parse
        else:
            return DEFAULT_PARTIAL_PARSE

    def read_saved_manifest(self) -> Optional[Manifest]:
        if not self._partial_parse_enabled():
            logger.debug('Partial parsing not enabled')
            return None
        path = os.path.join(self.root_project.target_path,
                            PARTIAL_PARSE_FILE_NAME)

        if os.path.exists(path):
            try:
                with open(path, 'rb') as fp:
                    manifest: Manifest = pickle.load(fp)
                # keep this check inside the try/except in case something about
                # the file has changed in weird ways, perhaps due to being a
                # different version of dbt
                if self.matching_parse_results(manifest):
                    return manifest
            except Exception as exc:
                logger.debug(
                    'Failed to load parsed file from disk at {}: {}'
                    .format(path, exc),
                    exc_info=True
                )
        return None

    # This find the sources, refs, and docs and resolves them
    # for nodes and exposures
    def process_manifest(self):
        project_name = self.root_project.project_name
        process_sources(self.manifest, project_name)
        process_refs(self.manifest, project_name)
        process_docs(self.manifest, self.root_project)

    def update_manifest(self) -> Manifest:
        start_patch = time.perf_counter()
        # patch_sources converts the UnparsedSourceDefinitions in the
        # Manifest.sources to ParsedSourceDefinition via 'patch_source'
        # in SourcePatcher
        sources = patch_sources(self.root_project, self.manifest)
        self.manifest.sources = sources
        # ParseResults had a 'disabled' attribute which was a dictionary
        # which is now named '_disabled'. This used to copy from
        # ParseResults to the Manifest. Can this be normalized so
        # there's only one disabled?
        disabled = []
        for value in self.manifest._disabled.values():
            disabled.extend(value)
        self.manifest.disabled = disabled
        self._perf_info.patch_sources_elapsed = (
            time.perf_counter() - start_patch
        )

        self.manifest.selectors = self.root_project.manifest_selectors

        # do the node and macro patches
        self.manifest.patch_nodes()
        self.manifest.patch_macros()

        # process_manifest updates the refs, sources, and docs
        start_process = time.perf_counter()
        self.process_manifest()

        self._perf_info.process_manifest_elapsed = (
            time.perf_counter() - start_process
        )

        return self.manifest

    # TODO: this should be calculated per-file based on the vars() calls made in
    # parsing, so changing one var doesn't invalidate everything. also there should
    # be something like that for env_var - currently changing env_vars in way that
    # impact graph selection or configs will result in weird test failures.
    # finally, we should hash the actual profile used, not just root project +
    # profiles.yml + relevant args. While sufficient, it is definitely overkill.
    def build_manifest_state_check(self):
        config = self.root_project
        all_projects = self.all_projects
        # if any of these change, we need to reject the parser
        vars_hash = FileHash.from_contents(
            '\x00'.join([
                getattr(config.args, 'vars', '{}') or '{}',
                getattr(config.args, 'profile', '') or '',
                getattr(config.args, 'target', '') or '',
                __version__
            ])
        )

        profile_path = os.path.join(config.args.profiles_dir, 'profiles.yml')
        with open(profile_path) as fp:
            profile_hash = FileHash.from_contents(fp.read())

        project_hashes = {}
        for name, project in all_projects.items():
            path = os.path.join(project.project_root, 'dbt_project.yml')
            with open(path) as fp:
                project_hashes[name] = FileHash.from_contents(fp.read())

        state_check = ManifestStateCheck(
            vars_hash=vars_hash,
            profile_hash=profile_hash,
            project_hashes=project_hashes,
        )
        return state_check

    # Get "full" manifest means a manifest with the macros already loaded
    # We sometimes build a manifest without that, so it can't be done in
    # an init routine. We call the adapter for this because apparently
    # it stores a lightweight manifest with only macros and we want to
    # reuse that if it exists.
    @classmethod
    def get_full_manifest(
        cls,
        config: RuntimeConfig,
        *,
        reset: bool = False,
    ) -> Manifest:

        adapter = get_adapter(config)  # type: ignore
        # reset is set in a TaskManager load_manifest call
        if reset:
            config.clear_dependencies()
            adapter.clear_macro_manifest()
        # load_macro_manifest here calls load_macros in ManifestLoader
        # The MacroManifest is like the Manifest but only contains
        # macros and files.
        macro_hook = adapter.connections.set_query_header

        with PARSING_STATE:
            start_load_all = time.perf_counter()

            projects = config.load_dependencies()
            loader = ManifestLoader(config, projects, macro_hook)
            loader.load_macros_from_adapter(adapter)
            loader.load()
            loader.write_manifest_for_partial_parse()
            manifest = loader.update_manifest()
            _check_manifest(manifest, config)
            manifest.build_flat_graph()

            loader._perf_info.load_all_elapsed = (
                time.perf_counter() - start_load_all
            )

            loader.track_project_load()

        return manifest

    # The point of this is to store the lightweight
    # MacroManifest in the adapter, with the same files
    # and macros in the Manifest
    def load_macros_from_adapter(self, adapter):
        if adapter._macro_manifest_lazy is None:
            macro_manifest = self.create_macro_manifest()
            adapter._macro_manifest_lazy = macro_manifest
            # macro_hook = adapter MacroQueryStringSetter callable
            self.macro_hook(macro_manifest)
            # the files and macros are already in self.manifest
        else:
            macro_manifest = adapter._macro_manifest_lazy
            self.manifest.files = macro_manifest.files
            self.manifest.macros = macro_manifest.macros
            # macro_hook = adapter MacroQueryStringSetter callable
            self.macro_hook(macro_manifest)

    # This creates a MacroManifest with 'files' and 'macros' to
    # save in the adapter
    def create_macro_manifest(self):
        for project in self.all_projects.values():
            # what is the manifest passed in actually used for?
            parser = MacroParser(project, self.manifest)
            for path in parser.search():
                self.parse_with_cache(path, parser)
        macro_manifest = MacroManifest(self.manifest.macros, self.manifest.files)
        return macro_manifest

    # This is called by the adapter code only, to create the
    # MacroManifest that's stored in the adapter.
    # 'get_full_manifest' uses a persistent ManifestLoader while this
    # creates a temporary ManifestLoader and throws it away.
    # Not sure when this would actually get used. The
    # ManifestLoader uses 'load_macros_from_adapter' above.
    @classmethod
    def load_macros(
        cls,
        root_config: RuntimeConfig,
        macro_hook: Callable[[Manifest], Any],
    ) -> Manifest:
        with PARSING_STATE:
            projects = root_config.load_dependencies()
            # This creates a loader object, including result,
            # and then throws it away, returning only the
            # manifest
            loader = cls(root_config, projects, macro_hook)
            macro_manifest = loader.create_macro_manifest()

        return macro_manifest


def invalid_ref_fail_unless_test(node, target_model_name,
                                 target_model_package, disabled):

    if node.resource_type == NodeType.Test:
        msg = get_target_not_found_or_disabled_msg(
            node, target_model_name, target_model_package, disabled
        )
        if disabled:
            logger.debug(warning_tag(msg))
        else:
            warn_or_error(
                msg,
                log_fmt=warning_tag('{}')
            )
    else:
        ref_target_not_found(
            node,
            target_model_name,
            target_model_package,
            disabled=disabled,
        )


def invalid_source_fail_unless_test(
    node, target_name, target_table_name, disabled
):
    if node.resource_type == NodeType.Test:
        msg = get_source_not_found_or_disabled_msg(
            node, target_name, target_table_name, disabled
        )
        if disabled:
            logger.debug(warning_tag(msg))
        else:
            warn_or_error(
                msg,
                log_fmt=warning_tag('{}')
            )
    else:
        source_target_not_found(
            node,
            target_name,
            target_table_name,
            disabled=disabled
        )


def _check_resource_uniqueness(
    manifest: Manifest,
    config: RuntimeConfig,
) -> None:
    names_resources: Dict[str, ManifestNode] = {}
    alias_resources: Dict[str, ManifestNode] = {}

    for resource, node in manifest.nodes.items():
        if node.resource_type not in NodeType.refable():
            continue
        # appease mypy - sources aren't refable!
        assert not isinstance(node, ParsedSourceDefinition)

        name = node.name
        # the full node name is really defined by the adapter's relation
        relation_cls = get_relation_class_by_name(config.credentials.type)
        relation = relation_cls.create_from(config=config, node=node)
        full_node_name = str(relation)

        existing_node = names_resources.get(name)
        if existing_node is not None:
            dbt.exceptions.raise_duplicate_resource_name(
                existing_node, node
            )

        existing_alias = alias_resources.get(full_node_name)
        if existing_alias is not None:
            dbt.exceptions.raise_ambiguous_alias(
                existing_alias, node, full_node_name
            )

        names_resources[name] = node
        alias_resources[full_node_name] = node


def _warn_for_unused_resource_config_paths(
    manifest: Manifest, config: RuntimeConfig
) -> None:
    resource_fqns: Mapping[str, PathSet] = manifest.get_resource_fqns()
    disabled_fqns: PathSet = frozenset(tuple(n.fqn) for n in manifest.disabled)
    config.warn_for_unused_resource_config_paths(resource_fqns, disabled_fqns)


def _check_manifest(manifest: Manifest, config: RuntimeConfig) -> None:
    _check_resource_uniqueness(manifest, config)
    _warn_for_unused_resource_config_paths(manifest, config)


# This is just used in test cases
def _load_projects(config, paths):
    for path in paths:
        try:
            project = config.new_project(path)
        except dbt.exceptions.DbtProjectError as e:
            raise dbt.exceptions.DbtProjectError(
                'Failed to read package at {}: {}'
                .format(path, e)
            )
        else:
            yield project.project_name, project


def _get_node_column(node, column_name):
    """Given a ParsedNode, add some fields that might be missing. Return a
    reference to the dict that refers to the given column, creating it if
    it doesn't yet exist.
    """
    if column_name in node.columns:
        column = node.columns[column_name]
    else:
        node.columns[column_name] = ColumnInfo(name=column_name)
        node.columns[column_name] = column

    return column


DocsContextCallback = Callable[
    [Union[ParsedNode, ParsedSourceDefinition]],
    Dict[str, Any]
]


# node and column descriptions
def _process_docs_for_node(
    context: Dict[str, Any],
    node: ManifestNode,
):
    node.description = get_rendered(node.description, context)
    for column_name, column in node.columns.items():
        column.description = get_rendered(column.description, context)


# source and table descriptions, column descriptions
def _process_docs_for_source(
    context: Dict[str, Any],
    source: ParsedSourceDefinition,
):
    table_description = source.description
    source_description = source.source_description
    table_description = get_rendered(table_description, context)
    source_description = get_rendered(source_description, context)
    source.description = table_description
    source.source_description = source_description

    for column in source.columns.values():
        column_desc = column.description
        column_desc = get_rendered(column_desc, context)
        column.description = column_desc


# macro argument descriptions
def _process_docs_for_macro(
    context: Dict[str, Any], macro: ParsedMacro
) -> None:
    macro.description = get_rendered(macro.description, context)
    for arg in macro.arguments:
        arg.description = get_rendered(arg.description, context)


# exposure descriptions
def _process_docs_for_exposure(
    context: Dict[str, Any], exposure: ParsedExposure
) -> None:
    exposure.description = get_rendered(exposure.description, context)


# nodes: node and column descriptions
# sources: source and table descriptions, column descriptions
# macros: macro argument descriptions
# exposures: exposure descriptions
def process_docs(manifest: Manifest, config: RuntimeConfig):
    for node in manifest.nodes.values():
        ctx = generate_runtime_docs(
            config,
            node,
            manifest,
            config.project_name,
        )
        _process_docs_for_node(ctx, node)
    for source in manifest.sources.values():
        ctx = generate_runtime_docs(
            config,
            source,
            manifest,
            config.project_name,
        )
        _process_docs_for_source(ctx, source)
    for macro in manifest.macros.values():
        ctx = generate_runtime_docs(
            config,
            macro,
            manifest,
            config.project_name,
        )
        _process_docs_for_macro(ctx, macro)
    for exposure in manifest.exposures.values():
        ctx = generate_runtime_docs(
            config,
            exposure,
            manifest,
            config.project_name,
        )
        _process_docs_for_exposure(ctx, exposure)


def _process_refs_for_exposure(
    manifest: Manifest, current_project: str, exposure: ParsedExposure
):
    """Given a manifest and a exposure in that manifest, process its refs"""
    for ref in exposure.refs:
        target_model: Optional[Union[Disabled, ManifestNode]] = None
        target_model_name: str
        target_model_package: Optional[str] = None

        if len(ref) == 1:
            target_model_name = ref[0]
        elif len(ref) == 2:
            target_model_package, target_model_name = ref
        else:
            raise dbt.exceptions.InternalException(
                f'Refs should always be 1 or 2 arguments - got {len(ref)}'
            )

        target_model = manifest.resolve_ref(
            target_model_name,
            target_model_package,
            current_project,
            exposure.package_name,
        )

        if target_model is None or isinstance(target_model, Disabled):
            # This may raise. Even if it doesn't, we don't want to add
            # this exposure to the graph b/c there is no destination exposure
            invalid_ref_fail_unless_test(
                exposure, target_model_name, target_model_package,
                disabled=(isinstance(target_model, Disabled))
            )

            continue

        target_model_id = target_model.unique_id

        exposure.depends_on.nodes.append(target_model_id)
        manifest.update_exposure(exposure)


def _process_refs_for_node(
    manifest: Manifest, current_project: str, node: ManifestNode
):
    """Given a manifest and a node in that manifest, process its refs"""
    for ref in node.refs:
        target_model: Optional[Union[Disabled, ManifestNode]] = None
        target_model_name: str
        target_model_package: Optional[str] = None

        if len(ref) == 1:
            target_model_name = ref[0]
        elif len(ref) == 2:
            target_model_package, target_model_name = ref
        else:
            raise dbt.exceptions.InternalException(
                f'Refs should always be 1 or 2 arguments - got {len(ref)}'
            )

        target_model = manifest.resolve_ref(
            target_model_name,
            target_model_package,
            current_project,
            node.package_name,
        )

        if target_model is None or isinstance(target_model, Disabled):
            # This may raise. Even if it doesn't, we don't want to add
            # this node to the graph b/c there is no destination node
            node.config.enabled = False
            invalid_ref_fail_unless_test(
                node, target_model_name, target_model_package,
                disabled=(isinstance(target_model, Disabled))
            )

            continue

        target_model_id = target_model.unique_id

        node.depends_on.nodes.append(target_model_id)
        # TODO: I think this is extraneous, node should already be the same
        # as manifest.nodes[node.unique_id] (we're mutating node here, not
        # making a new one)
        # Q: could we stop doing this?
        manifest.update_node(node)


# Takes references in 'refs' array of nodes and exposures, finds the target
# node, and updates 'depends_on.nodes' with the unique id
def process_refs(manifest: Manifest, current_project: str):
    for node in manifest.nodes.values():
        _process_refs_for_node(manifest, current_project, node)
    for exposure in manifest.exposures.values():
        _process_refs_for_exposure(manifest, current_project, exposure)
    return manifest


def _process_sources_for_exposure(
    manifest: Manifest, current_project: str, exposure: ParsedExposure
):
    target_source: Optional[Union[Disabled, ParsedSourceDefinition]] = None
    for source_name, table_name in exposure.sources:
        target_source = manifest.resolve_source(
            source_name,
            table_name,
            current_project,
            exposure.package_name,
        )
        if target_source is None or isinstance(target_source, Disabled):
            invalid_source_fail_unless_test(
                exposure,
                source_name,
                table_name,
                disabled=(isinstance(target_source, Disabled))
            )
            continue
        target_source_id = target_source.unique_id
        exposure.depends_on.nodes.append(target_source_id)
        manifest.update_exposure(exposure)


def _process_sources_for_node(
    manifest: Manifest, current_project: str, node: ManifestNode
):
    target_source: Optional[Union[Disabled, ParsedSourceDefinition]] = None
    for source_name, table_name in node.sources:
        target_source = manifest.resolve_source(
            source_name,
            table_name,
            current_project,
            node.package_name,
        )

        if target_source is None or isinstance(target_source, Disabled):
            # this folows the same pattern as refs
            node.config.enabled = False
            invalid_source_fail_unless_test(
                node,
                source_name,
                table_name,
                disabled=(isinstance(target_source, Disabled))
            )
            continue
        target_source_id = target_source.unique_id
        node.depends_on.nodes.append(target_source_id)
        manifest.update_node(node)


# Loops through all nodes and exposures, for each element in
# 'sources' array finds the source node and updates the
# 'depends_on.nodes' array with the unique id
def process_sources(manifest: Manifest, current_project: str):
    for node in manifest.nodes.values():
        if node.resource_type == NodeType.Source:
            continue
        assert not isinstance(node, ParsedSourceDefinition)
        _process_sources_for_node(manifest, current_project, node)
    for exposure in manifest.exposures.values():
        _process_sources_for_exposure(manifest, current_project, exposure)
    return manifest


# This is called in task.rpc.sql_commands when a "dynamic" node is
# created in the manifest, in 'add_refs'
def process_macro(
    config: RuntimeConfig, manifest: Manifest, macro: ParsedMacro
) -> None:
    ctx = generate_runtime_docs(
        config,
        macro,
        manifest,
        config.project_name,
    )
    _process_docs_for_macro(ctx, macro)


# This is called in task.rpc.sql_commands when a "dynamic" node is
# created in the manifest, in 'add_refs'
def process_node(
    config: RuntimeConfig, manifest: Manifest, node: ManifestNode
):

    _process_sources_for_node(
        manifest, config.project_name, node
    )
    _process_refs_for_node(manifest, config.project_name, node)
    ctx = generate_runtime_docs(config, node, manifest, config.project_name)
    _process_docs_for_node(ctx, node)

"""
VB.NET Language Server using Roslyn Language Server build that includes the Visual Basic
language services (the stock NuGet packages of Microsoft.CodeAnalysis.LanguageServer ship the
C# services only).
"""
# SPDX-License-Identifier: MIT

import logging
import os
import platform
import shutil
import tempfile
import threading
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from overrides import override

from serena.util.dotnet import DotNETUtil
from solidlsp.dependency_provider import DownloadedDependency, DownloadedDependencyHashDatabase
from solidlsp.ls import (
    LanguageServerDependencyProvider,
    LSPFileBuffer,
    RawDocumentSymbol,
    SolidLanguageServer,
)
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_types import Hover
from solidlsp.ls_utils import FileUtils, PathUtils
from solidlsp.lsp_protocol_handler.lsp_types import InitializeResult
from solidlsp.settings import SolidLSPSettings

from .common import RuntimeDependency, RuntimeDependencyCollection

log = logging.getLogger(__name__)

VBNET_LANGUAGE_SERVER_RELEASE_VERSION = "v1.0.0"
"""Tag of the `LaunchCG/roslyn-vbnet-languageserver` release providing the pre-built binaries."""

REQUIRED_DOTNET_VERSION = "10.0"
"""
Minimum .NET runtime required by `VBNET_LANGUAGE_SERVER_RELEASE_VERSION`.

This must track the `runtimeOptions.tfm` of the release's
`Microsoft.CodeAnalysis.LanguageServer.runtimeconfig.json`. The `v1.0.0` binaries target
`net10.0`; the later `v1.1.0` release was rebuilt against `net8.0`, so moving the pin back up
requires moving this down as well.
"""

VBNET_LANGUAGE_SERVER_BASE_URL = (
    f"https://github.com/LaunchCG/roslyn-vbnet-languageserver/releases/download/{VBNET_LANGUAGE_SERVER_RELEASE_VERSION}"
)

GITHUB_RELEASE_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")

LANGUAGE_SERVER_DLL_NAME = "Microsoft.CodeAnalysis.LanguageServer.dll"

PAYLOAD_DIR_NAME = "LanguageServer"
"""Name of the top-level directory the release archives wrap the server payload in."""

VISUAL_BASIC_ASSEMBLIES = (
    # A Roslyn Language Server build without the Visual Basic workspace services starts and
    # negotiates capabilities successfully but then silently reports nothing, so the presence
    # of these assemblies is asserted after installation rather than trusting the download.
    "Microsoft.CodeAnalysis.VisualBasic.dll",
    "Microsoft.CodeAnalysis.VisualBasic.Features.dll",
    "Microsoft.CodeAnalysis.VisualBasic.Workspaces.dll",
)

C_SHARP_ASSEMBLIES = (
    # The same build also ships the C# language services, which is why this server is
    # registered for `.cs` as well (see LanguageServerId.get_source_fn_matcher).
    "Microsoft.CodeAnalysis.CSharp.dll",
    "Microsoft.CodeAnalysis.CSharp.Features.dll",
    "Microsoft.CodeAnalysis.CSharp.Workspaces.dll",
)

_LANGUAGE_ID = "vbnet"

_ARCHIVE_TYPE_BY_PLATFORM: dict[str, FileUtils.ArchiveType] = {
    "win-x64": "zip",
    "win-arm64": "zip",
    "osx-x64": "gztar",
    "osx-arm64": "gztar",
    "linux-x64": "gztar",
    "linux-arm64": "gztar",
}

_ARCHIVE_SUFFIX_BY_PLATFORM: dict[str, str] = {
    platform_id: "zip" if archive_type == "zip" else "tar.gz" for platform_id, archive_type in _ARCHIVE_TYPE_BY_PLATFORM.items()
}
"""File name suffix of the release asset, which differs from the archive type the extraction requires."""

_PLATFORM_LABELS = {
    "win-x64": "Windows (x64)",
    "win-arm64": "Windows (ARM64)",
    "osx-x64": "macOS (x64)",
    "osx-arm64": "macOS (ARM64)",
    "linux-x64": "Linux (x64)",
    "linux-arm64": "Linux (ARM64)",
}

_RUNTIME_DEPENDENCIES = [
    RuntimeDependency(
        id="VBNetLanguageServer",
        description=f"Roslyn Language Server with VB.NET support for {_PLATFORM_LABELS[platform_id]}",
        url=f"{VBNET_LANGUAGE_SERVER_BASE_URL}/roslyn-vbnet-{platform_id}.{_ARCHIVE_SUFFIX_BY_PLATFORM[platform_id]}",
        platform_id=platform_id,
        archive_type=_ARCHIVE_TYPE_BY_PLATFORM[platform_id],
        binary_name=LANGUAGE_SERVER_DLL_NAME,
        package_name=f"roslyn-vbnet-{platform_id}",
        package_version=VBNET_LANGUAGE_SERVER_RELEASE_VERSION,
        extract_path="",
    )
    for platform_id in ("win-x64", "win-arm64", "osx-x64", "osx-arm64", "linux-x64", "linux-arm64")
]

SUPPORTED_SOLUTION_SUFFIXES = (".sln", ".slnx")
SUPPORTED_PROJECT_SUFFIXES = (".vbproj", ".fsproj", ".csproj")


def breadth_first_file_scan(root_dir: str) -> Iterable[str]:
    """
    Perform a breadth-first scan of files in the given directory.
    Yields file paths in breadth-first order.
    """
    queue = [root_dir]
    while queue:
        current_dir = queue.pop(0)
        try:
            for item in os.listdir(current_dir):
                if item.startswith("."):
                    continue
                item_path = os.path.join(current_dir, item)
                if os.path.isdir(item_path):
                    queue.append(item_path)
                elif os.path.isfile(item_path):
                    yield item_path
        except (PermissionError, OSError):
            # Skip directories we can't access
            pass


def find_solution_or_project_file(root_dir: str) -> str | None:
    """
    Find the first .sln or .slnx file in breadth-first order.
    If no solution file is found, look for the first .NET project file.
    """
    sln_file = None
    project_file = None

    for filename in breadth_first_file_scan(root_dir):
        if filename.endswith(SUPPORTED_SOLUTION_SUFFIXES) and sln_file is None:
            sln_file = filename
        elif filename.endswith(SUPPORTED_PROJECT_SUFFIXES) and project_file is None:
            project_file = filename

        # If we found a solution file, return it immediately
        if sln_file:
            return sln_file

    # If no solution file was found, return the first project file
    return project_file


class VBNetLanguageServer(SolidLanguageServer):
    """
    Provides VB.NET specific instantiation of the LanguageServer class using
    `Microsoft.CodeAnalysis.LanguageServer`, a Roslyn-based language server build that includes
    the Visual Basic language services.

    The build also ships the C# language services, so this server serves `.cs` files in
    addition to `.vb` files. A project configured with ``language_servers: [vbnet]`` therefore
    needs no separate ``csharp`` server, even for solutions mixing C# and VB.NET projects.

    You can pass a list of runtime dependency overrides in
    ``ls_specific_settings["vbnet"]["runtime_dependencies"]``. This is a list of dicts, each
    containing at least the "id" key, and optionally "platform_id" to uniquely identify the
    dependency to override.

    The standard ``ls_path``, ``ls_base_cmd``, ``ls_args`` and ``ls_extra_args`` settings are
    honoured as well; ``ls_path`` is the most useful one, as it allows pointing Serena at a
    pre-existing Roslyn VB.NET build instead of downloading one.

    Note: the .NET runtime is required and is installed automatically via Microsoft's official
    install scripts. If you have a custom .NET installation, ensure 'dotnet' is available in
    PATH.
    """

    @classmethod
    def supports_implementation_request(cls) -> bool:
        return True

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """
        Creates a VBNetLanguageServer instance. This class is not meant to be instantiated directly.
        Use LanguageServer.create() instead.
        """
        super().__init__(config, repository_root_path, None, _LANGUAGE_ID, solidlsp_settings)
        # Cache for original Roslyn symbol names with type annotations.
        # Key: (relative_file_path, line, character) -> Value: original name
        self._original_symbol_names: dict[tuple[str, int, int], str] = {}
        # Set once Roslyn confirms it finished (re)loading the solution/projects; reused by
        # notify_files_created to wait out a reload triggered after startup.
        self._project_reload_complete = threading.Event()

    def _create_dependency_provider(self) -> LanguageServerDependencyProvider:
        return self.DependencyProvider(self._custom_settings, self._ls_resources_dir, self._solidlsp_settings, self.repository_root_path)

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in ["bin", "obj", "packages", ".vs"]

    @override
    def request_hover(self, relative_file_path: str, line: int, column: int, file_buffer: LSPFileBuffer | None = None) -> Hover | None:
        """
        Override to inject original Roslyn symbol names (with type annotations) into hover responses.

        When hovering over a symbol whose name was normalized, we prepend the original
        full name (e.g. 'Add(Integer, Integer) As Integer') to the hover content.
        """
        hover = super().request_hover(relative_file_path, line, column, file_buffer=file_buffer)

        if hover is None:
            return None

        original_name = self._original_symbol_names.get((relative_file_path, line, column))

        if original_name and "contents" in hover:
            contents = hover["contents"]
            if isinstance(contents, dict) and "value" in contents:
                prefix = f"**{original_name}**\n\n---\n\n"
                contents["value"] = prefix + contents["value"]

        return hover

    def _document_symbols_cache_fingerprint(self) -> Hashable | None:
        normalize_symbol_name_version = 2
        return normalize_symbol_name_version

    def _normalize_symbol_name(self, symbol: RawDocumentSymbol, relative_file_path: str) -> str:
        # Roslyn returns symbol names with type annotations, in the language's own signature
        # syntax: VB.NET uses "As" where C# uses ":" (e.g. "Name As String" for a property,
        # "Add(Integer, Integer) As Integer" for a method). The name is normalized to its base
        # form so that find_symbol/referencing lookups match what the user typed, while the
        # original annotated name is cached and surfaced via hover.
        original_name = symbol["name"]

        normalized_name, type_info = self._extract_base_name_and_type(original_name)

        if original_name != normalized_name:
            sel_range = symbol.get("selectionRange")
            if sel_range:
                start = sel_range.get("start")
                if start and "line" in start and "character" in start:
                    self._original_symbol_names[(relative_file_path, start["line"], start["character"])] = original_name

            # Populate 'detail' field with type/signature information (for UnifiedSymbolInformation)
            if type_info and "detail" not in symbol:
                symbol["detail"] = type_info  # type: ignore

        return normalized_name

    @staticmethod
    def _extract_base_name_and_type(roslyn_name: str) -> tuple[str, str]:
        """
        Extract base name and type/signature information from Roslyn symbol names.

        Examples:
            "Name As String" -> ("Name", "As String")
            "Add(Integer, Integer) As Integer" -> ("Add", "(Integer, Integer) As Integer")
            "ToString()" -> ("ToString", "()")
            "SimpleMethod" -> ("SimpleMethod", "")
            "Position As (X As Integer, Y As String)" -> ("Position", "As (X As Integer, Y As String)")

        Returns:
            Tuple of (base_name, type_info)

        """
        # VB.NET type annotation separator. The '(' guard must look only at the name segment
        # before the first " As ", not the whole string, since a tuple type
        # ("(X As Integer, Y As String)") legitimately contains parentheses.
        for separator in (" As ", " : "):
            if separator in roslyn_name:
                base_name, type_part = roslyn_name.split(separator, 1)
                if "(" not in base_name:
                    return base_name.strip(), f"{separator.strip()} {type_part.strip()}"

        # Method pattern: "MethodName(params) As ReturnType"
        if "(" in roslyn_name:
            paren_idx = roslyn_name.index("(")
            base_name = roslyn_name[:paren_idx].strip()
            signature = roslyn_name[paren_idx:].strip()
            return base_name, signature

        # No type annotation
        return roslyn_name, ""

    class DependencyProvider(LanguageServerDependencyProvider):
        def __init__(
            self,
            custom_settings: SolidLSPSettings.CustomLSSettings,
            ls_resources_dir: str,
            solidlsp_settings: SolidLSPSettings,
            repository_root_path: str,
        ):
            super().__init__(custom_settings, ls_resources_dir)
            self._solidlsp_settings = solidlsp_settings
            self._repository_root_path = repository_root_path
            self._dotnet_path, self._language_server_path = self._ensure_server_installed()

        @classmethod
        def update_dep_hashes(cls) -> None:
            """
            Records the SHA256 hashes of the pinned language server releases in the central hash
            database which backs :class:`DownloadedDependency`.
            """
            with DownloadedDependencyHashDatabase.get_instance().update_context() as database:
                for dependency in _RUNTIME_DEPENDENCIES:
                    database.update(cls._create_downloaded_dependency(dependency))

        @staticmethod
        def _create_downloaded_dependency(dependency: RuntimeDependency) -> DownloadedDependency:
            """
            :param dependency: the runtime dependency to be downloaded
            :return: the hash-verified download handle for the given dependency
            """
            if dependency.url is None:
                raise SolidLSPException(f"No URL specified for package {dependency.package_name} version {dependency.package_version}")
            return DownloadedDependency(
                dependency.url,
                archive_type=dependency.archive_type or "zip",
                allowed_hosts=dependency.allowed_hosts or GITHUB_RELEASE_HOSTS,
            )

        def create_launch_command(self) -> list[str]:
            solution_or_project = find_solution_or_project_file(self._repository_root_path)

            log_dir = Path(self._ls_resources_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            cmd = [self._dotnet_path, self._language_server_path, "--logLevel=Information", f"--extensionLogDirectory={log_dir}", "--stdio"]

            # The language server will discover the solution/project from the workspace root
            if solution_or_project:
                log.info(f"Found solution/project file: {solution_or_project}")
            else:
                log.warning(
                    "No %s or %s file found, language server will attempt auto-discovery",
                    "/".join(SUPPORTED_SOLUTION_SUFFIXES),
                    "/".join(SUPPORTED_PROJECT_SUFFIXES),
                )

            log.debug(f"Language server command: {' '.join(cmd)}")

            return cmd

        def _ensure_server_installed(self) -> tuple[str, str]:
            """
            Ensure the .NET runtime and Microsoft.CodeAnalysis.LanguageServer are available.
            :return: a tuple of (dotnet_path, language_server_dll_path)
            """
            dotnet_path = self._ensure_dotnet_runtime()
            language_server_path = self._resolve_language_server_path()

            return dotnet_path, language_server_path

        def _ensure_dotnet_runtime(self) -> str:
            """Ensure the .NET runtime is available and return the dotnet executable path."""
            return DotNETUtil(REQUIRED_DOTNET_VERSION, allow_higher_version=True).get_dotnet_path_or_raise()

        def _resolve_language_server_path(self) -> str:
            """
            Resolve the path of the Roslyn Language Server DLL, honouring an explicit
            ``ls_path`` setting and downloading the pre-built VB.NET-capable Roslyn otherwise.
            """
            explicit_path = self._custom_settings.get("ls_path")
            if explicit_path is not None:
                dll_path = Path(explicit_path)
                if not dll_path.exists():
                    raise SolidLSPException(f"The configured 'ls_path' does not exist: {dll_path}")
                self._assert_language_capabilities(dll_path.parent, explicit=True)
                log.info(f"Using Roslyn VB.NET Language Server from 'ls_path': {dll_path}")
                return str(dll_path)

            runtime_dependency_overrides = cast(list[dict[str, Any]], self._custom_settings.get("runtime_dependencies", []))
            release_version = self._custom_settings.get("vbnet_language_server_version", VBNET_LANGUAGE_SERVER_RELEASE_VERSION)

            runtime_dependencies = RuntimeDependencyCollection(
                _runtime_dependencies_for_version(str(release_version)),
                overrides=runtime_dependency_overrides,
            )

            language_server_dependency = runtime_dependencies.get_single_dep_for_current_platform("VBNetLanguageServer")
            return self._ensure_language_server(language_server_dependency)

        def _ensure_language_server(self, language_server_dependency: RuntimeDependency) -> str:
            package_name = language_server_dependency.package_name
            package_version = language_server_dependency.package_version
            binary_name = language_server_dependency.binary_name
            if package_name is None or package_version is None or binary_name is None:
                raise SolidLSPException(f"Incomplete runtime dependency definition: {language_server_dependency}")

            server_dir = Path(self._ls_resources_dir) / f"{package_name}.{package_version}"
            server_dll = server_dir / binary_name

            if server_dll.exists():
                log.info(f"Using cached Roslyn VB.NET Language Server from {server_dll}")
                self._assert_language_capabilities(server_dir)
                return str(server_dll)

            # A partially extracted payload from an interrupted install would be picked up by the
            # check above and then fail with a confusing "missing assemblies" error, so remove it.
            if server_dir.exists():
                log.info(f"Found an incomplete Roslyn VB.NET Language Server installation at {server_dir}; removing it")
                shutil.rmtree(server_dir, ignore_errors=True)

            log.info(f"Downloading {package_name} version {package_version}...")
            archive_path = self._download_archive(language_server_dependency, server_dir)

            log.info(f"Extracting Roslyn VB.NET Language Server to {server_dir}")
            self._extract_language_server(language_server_dependency, archive_path, server_dir)
            self._remove_download_dir(archive_path)

            if not server_dll.exists():
                raise SolidLSPException(f"Roslyn VB.NET Language Server DLL not found after extraction: {server_dll}")

            if platform.system().lower() != "windows":
                server_dll.chmod(0o755)

            self._assert_language_capabilities(server_dir)
            log.info(f"Successfully installed Roslyn VB.NET Language Server to {server_dll}")
            return str(server_dll)

        def _download_archive(self, dependency: RuntimeDependency, server_dir: Path) -> Path:
            downloaded_dependency = self._create_downloaded_dependency(dependency)
            url = downloaded_dependency.get_url()

            # The extracted archive is ~500 files; extracting it directly into the (deep) cache
            # path can exceed the Windows MAX_PATH limit, so download and extract into a
            # short-lived temporary directory and copy the payload into the cache afterwards.
            download_dir = Path(tempfile.mkdtemp(prefix="serena-vbnet-ls-"))
            archive_path = download_dir / "roslyn-vbnet"
            try:
                log.debug(f"Downloading Roslyn VB.NET Language Server from: {url}")
                downloaded_dependency.download_to(archive_path)
            except Exception as e:
                raise SolidLSPException(f"Failed to download the Roslyn VB.NET Language Server from {url}: {e}") from e

            log.debug(f"Downloaded to {archive_path} (server dir: {server_dir})")
            return archive_path

        @staticmethod
        def _remove_download_dir(archive_path: Path) -> None:
            """
            Remove the temporary download/extraction directory.

            The payload has already been copied into the cache directory, so a failure here
            (e.g. a file still held open on Windows) must not fail the installation.
            """
            shutil.rmtree(archive_path.parent, ignore_errors=True)

        @staticmethod
        def _extract_language_server(dependency: RuntimeDependency, archive_dir: Path, server_dir: Path) -> None:
            """
            Copy the language server payload out of the extracted archive into the cache directory.

            The release archives wrap the payload in a top-level ``LanguageServer/`` directory
            alongside a ``VERSION.txt`` marker, so the payload has to be hoisted out of that
            wrapper; otherwise ``binary_name`` would not be found directly in ``server_dir``.
            """
            if not archive_dir.exists():
                raise SolidLSPException(f"Extracted archive not found: {archive_dir}")

            source_dir = archive_dir
            wrapper_dir = archive_dir / PAYLOAD_DIR_NAME
            if wrapper_dir.is_dir() and (wrapper_dir / LANGUAGE_SERVER_DLL_NAME).exists():
                source_dir = wrapper_dir

            server_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_dir, server_dir, dirs_exist_ok=True)

        @staticmethod
        def _assert_language_capabilities(server_dir: Path, explicit: bool = False) -> None:
            """
            Assert that the language server directory actually contains the language services for
            the languages this server is registered for.

            A stock Microsoft.CodeAnalysis.LanguageServer build negotiates capabilities and starts
            up successfully while lacking the Visual Basic workspace services, in which case
            every symbol request comes back empty rather than failing. Fail loudly instead.

            :param server_dir: the directory containing the language server DLL
            :param explicit: whether the directory came from an explicit ``ls_path`` setting
            """
            missing = [assembly for assembly in VISUAL_BASIC_ASSEMBLIES + C_SHARP_ASSEMBLIES if not (server_dir / assembly).exists()]
            if not missing:
                return

            source = f"The 'ls_path' configured for the vbnet language server ({server_dir})" if explicit else f"{server_dir}"
            raise SolidLSPException(
                f"{source} does not contain the assemblies required to serve VB.NET/C#: {', '.join(missing)}. "
                "Microsoft's stock Roslyn Language Server packages do not include the Visual Basic workspace "
                "services; a VB.NET-capable build (see the vbnet_language_server docstring) is required."
            )

    def _create_base_initialize_params(self) -> dict:
        """
        Returns the initialize params for the Microsoft.CodeAnalysis.LanguageServer.
        """
        return {
            "capabilities": {
                "window": {
                    "workDoneProgress": True,
                    "showMessage": {"messageActionItem": {"additionalPropertiesSupport": True}},
                    "showDocument": {"support": True},
                },
                "workspace": {
                    "applyEdit": True,
                    "workspaceEdit": {"documentChanges": True},
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "didChangeWatchedFiles": {"dynamicRegistration": True},
                    "symbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "executeCommand": {"dynamicRegistration": True},
                    "configuration": True,
                    "workspaceFolders": True,
                    "workDoneProgress": True,
                },
                "textDocument": {
                    "synchronization": {"dynamicRegistration": True, "willSave": True, "willSaveWaitUntil": True, "didSave": True},
                    "hover": {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]},
                    "signatureHelp": {
                        "dynamicRegistration": True,
                        "signatureInformation": {
                            "documentationFormat": ["markdown", "plaintext"],
                            "parameterInformation": {"labelOffsetSupport": True},
                        },
                    },
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                },
            },
        }

    def _start_server(self) -> None:
        indexing_complete = threading.Event()

        def do_nothing(params: dict) -> None:
            return

        def window_log_message(msg: dict) -> None:
            """Log messages from the language server."""
            message_text = msg.get("message", "")
            level = msg.get("type", 4)  # Default to Log level

            # Map LSP message types to Python logging levels
            level_map = {1: logging.ERROR, 2: logging.WARNING, 3: logging.INFO, 4: logging.DEBUG}  # Error  # Warning  # Info  # Log

            log.log(level_map.get(level, logging.DEBUG), f"LSP: {message_text}")

            # workspace/projectInitializationComplete (below) fires too early on a project
            # *re*-open to signal reload completion; this log line is the reliable one for
            # notify_files_created's wait.
            if "Completed (re)load of all projects" in message_text:
                self._project_reload_complete.set()

        def handle_progress(params: dict) -> None:
            """Handle progress notifications from the language server."""
            token = params.get("token", "")
            value = params.get("value", {})

            log.debug(f"Progress notification received: {params}")

            kind = value.get("kind")

            if kind == "begin":
                title = value.get("title", "Operation in progress")
                message = value.get("message", "")
                percentage = value.get("percentage")

                if percentage is not None:
                    log.debug(f"Progress [{token}]: {title} - {message} ({percentage}%)")
                else:
                    log.info(f"Progress [{token}]: {title} - {message}")

            elif kind == "report":
                message = value.get("message", "")
                percentage = value.get("percentage")

                if percentage is not None:
                    log.info(f"Progress [{token}]: {message} ({percentage}%)")
                elif message:
                    log.info(f"Progress [{token}]: {message}")

            elif kind == "end":
                message = value.get("message", "Operation completed")
                log.info(f"Progress [{token}]: {message}")

        def handle_workspace_configuration(params: dict) -> list:
            """Handle workspace/configuration requests from the server."""
            items = params.get("items", [])
            result: list[Any] = []

            for item in items:
                section = item.get("section", "")

                # Provide default values based on the configuration section
                if section.startswith(("dotnet", "vb", "visualbasic")):
                    if "enable" in section or "show" in section or "suppress" in section or "navigate" in section:
                        # Boolean settings
                        result.append(False)
                    elif "scope" in section:
                        # Scope settings - use appropriate enum values
                        result.append("openFiles")
                    elif section == "dotnet_member_insertion_location":
                        # ImplementTypeInsertionBehavior enum
                        result.append("with_other_members_of_the_same_kind")
                    elif section == "dotnet_property_generation_behavior":
                        # ImplementTypePropertyGenerationBehavior enum
                        result.append("prefer_throwing_properties")
                    elif "location" in section or "behavior" in section:
                        # Other enum settings - return null to avoid parsing errors
                        result.append(None)
                    else:
                        # Default for other dotnet/vb settings
                        result.append(None)
                elif section == "tab_width" or section == "indent_size":
                    result.append(4)
                elif section == "insert_final_newline":
                    result.append(True)
                else:
                    # Unknown configuration - return null
                    result.append(None)

            return result

        def handle_work_done_progress_create(params: dict) -> None:
            """Handle work done progress create requests."""
            return

        def handle_register_capability(params: dict) -> None:
            """Handle client/registerCapability requests."""
            return

        def handle_project_needs_restore(params: dict) -> None:
            return

        def handle_workspace_indexing_complete(params: dict) -> None:
            indexing_complete.set()

        # Set up notification handlers
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", handle_progress)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)
        self.server.on_notification("workspace/projectInitializationComplete", handle_workspace_indexing_complete)
        self.server.on_request("workspace/configuration", handle_workspace_configuration)
        self.server.on_request("window/workDoneProgress/create", handle_work_done_progress_create)
        self.server.on_request("client/registerCapability", handle_register_capability)
        self.server.on_request("workspace/_roslyn_projectNeedsRestore", handle_project_needs_restore)

        log.info("Starting Microsoft.CodeAnalysis.LanguageServer process")

        try:
            self.server.start()
        except Exception as e:
            log.info(f"Failed to start language server process: {e}", logging.ERROR)
            raise SolidLSPException(f"Failed to start VB.NET language server: {e}")

        # Send initialization
        initialize_params = self._create_initialize_params()

        log.info("Sending initialize request to language server")
        try:
            init_response = self.server.send.initialize(initialize_params)
            log.info(f"Received initialize response: {init_response}")
        except Exception as e:
            raise SolidLSPException(f"Failed to initialize VB.NET language server for {self.repository_root_path}: {e}") from e

        # Apply diagnostic capabilities
        self._force_pull_diagnostics(init_response)

        # Verify required capabilities
        capabilities = init_response.get("capabilities", {})
        required_capabilities = [
            "textDocumentSync",
            "definitionProvider",
            "referencesProvider",
            "documentSymbolProvider",
        ]
        missing = [cap for cap in required_capabilities if cap not in capabilities]
        if missing:
            raise RuntimeError(
                f"Language server is missing required capabilities: {', '.join(missing)}. "
                "Initialization failed. Please ensure a VB.NET-capable version of Microsoft.CodeAnalysis.LanguageServer is installed and the .NET runtime is working."
            )

        # Complete initialization
        self.server.notify.initialized({})

        # Open solution and project files
        self._open_solution_and_projects()

        log.info(
            "Microsoft.CodeAnalysis.LanguageServer initialized and ready\n"
            "Waiting for language server to index project files...\n"
            "This may take a while for large projects"
        )

        if indexing_complete.wait(30):  # Wait up to 30 seconds for indexing
            log.info("Indexing complete")
        else:
            log.warning("Timeout waiting for indexing to complete, proceeding anyway")

    def _force_pull_diagnostics(self, init_response: dict | InitializeResult) -> None:
        """
        Apply the diagnostic capabilities hack.
        Forces the server to support pull diagnostics.
        """
        capabilities = init_response.get("capabilities", {})
        diagnostic_provider: Any = capabilities.get("diagnosticProvider", {})

        if isinstance(diagnostic_provider, dict):
            diagnostic_provider.update(
                {
                    "interFileDependencies": True,
                    "workDoneProgress": True,
                    "workspaceDiagnostics": True,
                }
            )
            log.debug("Applied diagnostic capabilities hack for better VB.NET diagnostics")

    def _open_solution_and_projects(self) -> None:
        """
        Open solution and project files using notifications.
        """
        # Find solution file (.sln or .slnx)
        solution_file = None
        for filename in breadth_first_file_scan(self.repository_root_path):
            if filename.endswith(SUPPORTED_SOLUTION_SUFFIXES):
                solution_file = filename
                break

        if solution_file:
            solution_uri = PathUtils.path_to_uri(solution_file)
            self.server.notify.send_notification("solution/open", {"solution": solution_uri})
            log.debug(f"Opened solution file: {solution_file}")

        # Find and open project files, skipping any that the project's ignore settings exclude.
        # Vendored, third-party and sample trees routinely contain project files that the language
        # server cannot restore or build. Each one costs a project load on every server start, and
        # the resulting restore failures bury the diagnostics of the projects the user cares about.
        project_files = []
        skipped = 0
        for filename in breadth_first_file_scan(self.repository_root_path):
            if not filename.endswith(SUPPORTED_PROJECT_SUFFIXES):
                continue
            relative_path = os.path.relpath(filename, self.repository_root_path)
            # ignore_unsupported_files=False, because a project file is not itself a VB.NET
            # source file and would otherwise be excluded on file type rather than by the
            # ignore patterns.
            if self.is_ignored_path(relative_path, ignore_unsupported_files=False):
                skipped += 1
                continue
            project_files.append(filename)
        if skipped:
            log.debug(f"Skipped {skipped} project file(s) matched by the project's ignore settings")

        if project_files:
            project_uris = [PathUtils.path_to_uri(project_file) for project_file in project_files]
            self.server.notify.send_notification("project/open", {"projects": project_uris})
            log.debug(f"Opened project files: {project_files}")

    @override
    def _get_wait_time_for_cross_file_referencing(self) -> float:
        return 2

    @override
    def notify_files_created(self, relative_file_paths: Sequence[str]) -> None:
        """
        Roslyn's project system only learns which files belong to a project from the
        `solution/open`/`project/open` notifications sent once at startup (see
        `_open_solution_and_projects`); a plain `didChangeWatchedFiles`/open-close cycle
        does not make it re-evaluate the project, so a file created after startup is
        analyzed as a standalone Miscellaneous Files document instead of being folded into
        the already-loaded compilation. Resend the same notifications and wait for the
        server to confirm the reload before the file is opened.
        """
        self._project_reload_complete.clear()
        self._open_solution_and_projects()
        if not self._project_reload_complete.wait(30):
            log.warning("Timeout waiting for project reload after new file(s) were created: %s", list(relative_file_paths))


def _runtime_dependencies_for_version(version: str) -> list[RuntimeDependency]:
    """Return the Roslyn VB.NET LS runtime dependencies for the configured release version."""
    result: list[RuntimeDependency] = []
    for dependency in _RUNTIME_DEPENDENCIES:
        if dependency.url is None:
            raise SolidLSPException(f"No URL specified for package {dependency.package_name} version {dependency.package_version}")
        result.append(replace(dependency, url=dependency.url.replace(VBNET_LANGUAGE_SERVER_RELEASE_VERSION, version)))
    return result

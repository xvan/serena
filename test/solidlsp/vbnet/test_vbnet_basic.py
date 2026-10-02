import os
import re
import tempfile
from pathlib import Path

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.language_servers.vbnet_language_server import (
    VBNetLanguageServer,
    breadth_first_file_scan,
    find_solution_or_project_file,
)
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_utils import SymbolUtils

CALCULATOR_FILE = "Calculator.vb"
PERSON_FILE = os.path.join("Models", "Person.vb")
PROGRAM_FILE = os.path.join("ConsoleApp", "Program.vb")
CSHARP_FILE = os.path.join("CoreLib", "Formatter.cs")
CONSUMER_FILE = os.path.join("Consumer", "Program.cs")


def _document_symbols(language_server: SolidLanguageServer, file_path: str) -> list[dict]:
    """Returns the flat list of document symbols reported for the given file."""
    symbols = language_server.request_document_symbols(file_path).get_all_symbols_and_roots()
    if symbols and isinstance(symbols[0], list):
        symbols = symbols[0]
    return list(symbols)


def _find_symbol(symbols: list[dict], name: str) -> dict:
    """Returns the symbol with the given normalized base name."""
    for symbol in symbols:
        if symbol.get("name") == name:
            return symbol
    raise AssertionError(f"Could not find symbol '{name}'; got {[s.get('name') for s in symbols]}")


@pytest.mark.vbnet
class TestVBNetLanguageServer:
    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_find_symbol(self, language_server: SolidLanguageServer) -> None:
        """Test finding symbols of both VB.NET projects in the full symbol tree."""
        symbols = language_server.request_full_symbol_tree()
        for name in ("Calculator", "Add", "Subtract", "Person", "IsAdult", "IGreeter", "ConsoleGreeter", "FormatGreeting"):
            assert SymbolUtils.symbol_tree_contains_name(symbols, name), f"{name} not found in symbol tree"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_get_document_symbols(self, language_server: SolidLanguageServer) -> None:
        """Test getting document symbols from a VB.NET file."""
        symbols = _document_symbols(language_server, CALCULATOR_FILE)
        names = [symbol.get("name") for symbol in symbols]

        # Symbol names are normalized to their base form, without the VB type annotations
        # that Roslyn reports (e.g. "Add" rather than "Add(a As Integer, b As Integer) As Integer").
        assert "Calculator" in names
        for method in ("Add", "Subtract", "Multiply", "Divide"):
            assert method in names, f"{method} not found in {names}"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_document_symbols_in_namespaced_file(self, language_server: SolidLanguageServer) -> None:
        """Test getting symbols from a file declaring a namespace."""
        names = [symbol.get("name") for symbol in _document_symbols(language_server, PERSON_FILE)]

        assert "Person" in names
        for member in ("Name", "Age", "Email", "IsAdult", "CalculateYearsUntilRetirement", "ToString"):
            assert member in names, f"{member} not found in {names}"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_find_referencing_symbols_across_files(self, language_server: SolidLanguageServer) -> None:
        """Test finding references to a VB.NET method from another file."""
        subtract = _find_symbol(_document_symbols(language_server, CALCULATOR_FILE), "Subtract")
        selection_start = subtract["selectionRange"]["start"]
        line, character = selection_start["line"], selection_start["character"] + 1

        references = language_server.request_references(CALCULATOR_FILE, line, character)
        reference_files = [str(reference.get("relativePath", "")).replace(os.sep, "/") for reference in references]

        assert any(PERSON_FILE.replace(os.sep, "/") in path for path in reference_files), (
            f"Should find a reference in {PERSON_FILE} where Calculator.Subtract is called; got {reference_files}"
        )

        # The first call may trigger initialization and change the state of the language server,
        # so a repeated call must yield the same result.
        assert language_server.request_references(CALCULATOR_FILE, line, character) == references

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_definition_within_project(self, language_server: SolidLanguageServer) -> None:
        """Test that a call from one VB.NET file resolves to the declaring file."""
        person_source = _read_source(PERSON_FILE)
        line, character = _position_of(person_source, "Subtract")
        definitions = language_server.request_definition(PERSON_FILE, line, character)

        target_paths = [str(definition.get("relativePath", "")).replace(os.sep, "/") for definition in definitions or []]
        assert any(path == CALCULATOR_FILE for path in target_paths), f"Expected definition in {CALCULATOR_FILE}; got {target_paths}"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_csharp_file_is_served(self, language_server: SolidLanguageServer) -> None:
        """Test that the VB.NET language server also serves the C# files of a mixed-language solution."""
        names = [symbol.get("name") for symbol in _document_symbols(language_server, CSHARP_FILE)]

        assert "Formatter" in names, f"Formatter not found in {names}"
        assert "Currency" in names, f"Currency not found in {names}"
        assert "Label" in names, f"Label not found in {names}"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_definition_across_projects_and_languages(self, language_server: SolidLanguageServer) -> None:
        """Test that a call from a VB.NET project into a referenced C# project resolves to the C# source."""
        program_source = _read_source(PROGRAM_FILE)
        line, character = _position_of(program_source, "Currency")
        definitions = language_server.request_definition(PROGRAM_FILE, line, character)

        target_paths = [str(definition.get("relativePath", "")).replace(os.sep, "/") for definition in definitions or []]
        expected = CSHARP_FILE.replace(os.sep, "/")
        assert any(path == expected for path in target_paths), f"Expected definition in {expected}; got {target_paths}"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_references_across_projects_and_languages(self, language_server: SolidLanguageServer) -> None:
        """Test that the references to a C# method are found in the referencing VB.NET project."""
        source = _read_source(CSHARP_FILE)
        line, character = _position_of(source, "Currency")
        references = language_server.request_references(CSHARP_FILE, line, character)

        reference_files = [str(reference.get("relativePath", "")).replace(os.sep, "/") for reference in references]
        expected = PROGRAM_FILE.replace(os.sep, "/")
        assert any(path == expected for path in reference_files), (
            f"Should find a reference in {expected} where Formatter.Currency is called; got {reference_files}"
        )

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_definition_from_csharp_into_vbnet(self, language_server: SolidLanguageServer) -> None:
        """Test that a C# project resolves a member of a referenced VB.NET project."""
        source = _read_source(CONSUMER_FILE)
        line, character = _position_of(source, "IsAdult")
        definitions = language_server.request_definition(CONSUMER_FILE, line, character)

        target_paths = [str(definition.get("relativePath", "")).replace(os.sep, "/") for definition in definitions or []]
        expected = PERSON_FILE.replace(os.sep, "/")
        assert any(path == expected for path in target_paths), (
            f"Expected definition in {expected} where Person.IsAdult is declared; got {target_paths}"
        )

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_references_from_csharp_into_vbnet(self, language_server: SolidLanguageServer) -> None:
        """Test that references made from C# to a VB.NET member are found."""
        source = _read_source(PERSON_FILE)
        line, character = _position_of(source, "IsAdult")
        references = language_server.request_references(PERSON_FILE, line, character)

        reference_files = [str(reference.get("relativePath", "")).replace(os.sep, "/") for reference in references]
        expected = CONSUMER_FILE.replace(os.sep, "/")
        assert any(path == expected for path in reference_files), (
            f"Should find a reference in {expected} where Person.IsAdult is called from C#; got {reference_files}"
        )

    def test_vb_project_does_not_redeclare_root_namespace(self) -> None:
        """Test that the VB.NET project does not re-prefix the declared namespaces.

        A VB.NET SDK-style project defaults ``RootNamespace`` to the project name and
        prepends it to every explicit ``Namespace X ... End Namespace`` block, which
        silently compiles these types into ``TestProject.TestProject.*`` instead of the
        declared ``TestProject.*``. Nothing in a name-only symbol lookup can see that,
        so the project file itself is asserted on. The cross-language tests fail as a
        consequence if this regresses.
        """
        from test.conftest import get_repo_path

        project_file = Path(str(get_repo_path(LanguageServerId.VBNET))) / "TestProject.vbproj"
        declared = re.findall(r"<RootNamespace>(.*?)</RootNamespace>", project_file.read_text(encoding="utf-8"))

        assert declared == [""], (
            f"RootNamespace must stay empty so the explicit Namespace blocks in the sources are used as written; got {declared}"
        )

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_hover(self, language_server: SolidLanguageServer) -> None:
        """Test that hover information is available for a VB.NET symbol."""
        language_server.open_file(CALCULATOR_FILE)
        source = _read_source(CALCULATOR_FILE)
        line, character = _position_of(source, "Subtract")

        hover = language_server.request_hover(CALCULATOR_FILE, line, character)

        assert hover is not None, "Hover should return information for a VB.NET method"
        assert isinstance(hover, dict), "Hover should be a dict"
        assert "contents" in hover, "Hover should have contents"

        contents = hover["contents"]
        assert isinstance(contents, dict), "Hover contents should be a dict"
        hover_text = contents["value"]

        # The hover reports the signature in VB.NET syntax, using "As" for type annotations.
        assert "Subtract" in hover_text, f"Hover should include the method name, got: {hover_text}"
        assert "As Integer" in hover_text, f"Hover should include the return type, got: {hover_text}"

    @pytest.mark.parametrize("language_server", [LanguageServerId.VBNET], indirect=True)
    def test_default_ignored_directories(self, language_server: SolidLanguageServer) -> None:
        """Test that build output and IDE directories are pruned."""
        for dirname in ("bin", "obj", ".vs", "packages"):
            assert language_server.is_ignored_dirname(dirname), f"{dirname} should be pruned"

    @pytest.mark.parametrize(
        ("roslyn_name", "expected"),
        [
            ("Name As String", ("Name", "As String")),
            ("Add(Integer, Integer) As Integer", ("Add", "(Integer, Integer) As Integer")),
            ("ToString()", ("ToString", "()")),
            ("SimpleMethod", ("SimpleMethod", "")),
            ("Position As (X As Integer, Y As String)", ("Position", "As (X As Integer, Y As String)")),
        ],
    )
    def test_extract_base_name_and_type(self, roslyn_name: str, expected: tuple[str, str]) -> None:
        """Test the normalization of Roslyn's VB.NET symbol names."""
        assert VBNetLanguageServer._extract_base_name_and_type(roslyn_name) == expected


@pytest.mark.vbnet
class TestVBNetSolutionProjectOpening:
    """Test solution and project file discovery."""

    def test_breadth_first_file_scan(self) -> None:
        """Test that breadth_first_file_scan finds files in breadth-first order."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            (temp_path / "file1.txt").touch()
            (temp_path / "subdir1").mkdir()
            (temp_path / "subdir1" / "file2.txt").touch()
            (temp_path / "subdir2").mkdir()
            (temp_path / "subdir2" / "file3.txt").touch()

            filenames = [os.path.basename(file) for file in breadth_first_file_scan(str(temp_path))]

            assert len(filenames) == 3
            assert filenames[0] == "file1.txt"

    def test_find_solution_or_project_file_prefers_solution(self) -> None:
        """Test that a .sln is preferred over a project file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            solution_file = temp_path / "MySolution.sln"
            solution_file.touch()
            (temp_path / "MyProject.vbproj").touch()

            assert find_solution_or_project_file(str(temp_path)) == str(solution_file)

    def test_find_solution_or_project_file_falls_back_to_vbproj(self) -> None:
        """Test that a .vbproj is found when no solution file is present."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            project_file = temp_path / "MyProject.vbproj"
            project_file.touch()

            assert find_solution_or_project_file(str(temp_path)) == str(project_file)

    def test_find_solution_or_project_file_returns_none(self) -> None:
        """Test that no solution or project file is reported for an unrelated directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "readme.txt").touch()

            assert find_solution_or_project_file(str(temp_path)) is None


def _read_source(file_path: str) -> list[str]:
    """Returns the lines of a file of the test repository."""
    from test.conftest import get_repo_path

    with open(os.path.join(str(get_repo_path(LanguageServerId.VBNET)), file_path), encoding="utf-8") as file:
        return file.read().splitlines()


def _position_of(source_lines: list[str], identifier: str) -> tuple[int, int]:
    """
    Returns the zero-based (line, column) of the first occurrence of the given identifier,
    with the column pointing just inside the identifier.
    """
    for line_number, line in enumerate(source_lines):
        column = line.find(identifier)
        if column >= 0:
            return line_number, column + 1
    raise AssertionError(f"Identifier '{identifier}' not found in source")
